"""``trellis worker embed-traces`` — the front door.

Covers what a scheduled invocation depends on: the JSON contract, and the exit
code. A pass that left traces unreachable must not exit zero; a green systemd
unit over a silent gap is the shape of failure this worker exists to remove.
"""

from __future__ import annotations

import json

from trellis_cli.worker import worker_app
from trellis_workers.trace_embed import trace_summary_doc_id

from .conftest import seed_traces


def _run_json(runner, *args: str) -> tuple[dict, object]:
    """Invoke the worker via the caller's runner and parse the last stdout line.

    The runner is a parameter rather than a module-level singleton because a
    bare ``CliRunner`` poisons structlog process-wide (#377); callers pass the
    root ``cli_runner`` fixture.
    """
    result = runner.invoke(worker_app, ["embed-traces", "--format", "json", *args])
    return json.loads(result.output.strip().splitlines()[-1]), result


class TestEmbedTracesCLI:
    def test_command_is_registered(self) -> None:
        assert "embed-traces" in [c.name for c in worker_app.registered_commands]

    def test_embeds_and_reports(self, registry, tmp_path, cli_runner) -> None:
        traces = seed_traces(registry, 3)
        payload, result = _run_json(cli_runner, "--watermark", str(tmp_path / "wm.json"))
        assert result.exit_code == 0, result.output
        assert payload["status"] == "ok"
        assert payload["embedded"] == 3
        assert payload["scanned"] == 3
        assert all(
            registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
            for t in traces
        )

    def test_rerun_is_a_no_op(self, registry, tmp_path, cli_runner) -> None:
        seed_traces(registry, 3)
        wm = str(tmp_path / "wm.json")
        _run_json(cli_runner, "--watermark", wm)
        payload, result = _run_json(cli_runner, "--watermark", wm)
        assert result.exit_code == 0
        assert payload["embedded"] == 0
        assert payload["scanned"] == 0

    def test_dry_run_writes_nothing(
        self, registry, tmp_path, recorder, cli_runner
    ) -> None:
        seed_traces(registry, 2)
        payload, result = _run_json(
            cli_runner, "--dry-run", "--watermark", str(tmp_path / "w.json")
        )
        assert result.exit_code == 0
        assert payload["dry_run"] is True
        assert payload["embedded"] == 2
        assert recorder.texts == []

    def test_a_partial_pass_exits_non_zero(
        self, registry, tmp_path, recorder, cli_runner
    ) -> None:
        seed_traces(registry, 4)
        recorder.fail_after = 1
        payload, result = _run_json(cli_runner, "--watermark", str(tmp_path / "wm.json"))
        assert result.exit_code == 1, result.output
        assert payload["status"] == "partial"
        assert payload["failed"] == 3
        assert len(payload["failures"]) == 3

    def test_missing_embedder_exits_non_zero(
        self, registry, tmp_path, monkeypatch, cli_runner
    ) -> None:
        from trellis_cli.stores import _reset_registry

        monkeypatch.delenv("TRELLIS_EMBEDDING_FN", raising=False)
        _reset_registry()
        result = cli_runner.invoke(
            worker_app,
            ["embed-traces", "--format", "json", "--watermark", str(tmp_path / "w")],
        )
        assert result.exit_code == 1
        assert json.loads(result.output.strip().splitlines()[-1])["status"] == "error"

    def test_text_output_names_the_cursor_move(
        self, registry, tmp_path, cli_runner
    ) -> None:
        seed_traces(registry, 2)
        result = cli_runner.invoke(
            worker_app, ["embed-traces", "--watermark", str(tmp_path / "wm.json")]
        )
        assert result.exit_code == 0, result.output
        assert "worker embed-traces" in result.output
        assert "cursor:" in result.output
