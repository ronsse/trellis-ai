"""``trellis worker embed-traces`` — the front door.

Covers what a scheduled invocation depends on: the JSON contract, and the exit
code. A pass that left traces unreachable must not exit zero; a green systemd
unit over a silent gap is the shape of failure this worker exists to remove.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from trellis_cli.worker import worker_app
from trellis_workers.trace_embed import trace_summary_doc_id

from .conftest import seed_traces

runner = CliRunner()


def _run_json(*args: str) -> dict:
    result = runner.invoke(worker_app, ["embed-traces", "--format", "json", *args])
    return json.loads(result.output.strip().splitlines()[-1]), result


class TestEmbedTracesCLI:
    def test_command_is_registered(self) -> None:
        assert "embed-traces" in [c.name for c in worker_app.registered_commands]

    def test_embeds_and_reports(self, registry, tmp_path) -> None:
        traces = seed_traces(registry, 3)
        payload, result = _run_json("--watermark", str(tmp_path / "wm.json"))
        assert result.exit_code == 0, result.output
        assert payload["status"] == "ok"
        assert payload["embedded"] == 3
        assert payload["scanned"] == 3
        assert all(
            registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
            for t in traces
        )

    def test_rerun_is_a_no_op(self, registry, tmp_path) -> None:
        seed_traces(registry, 3)
        wm = str(tmp_path / "wm.json")
        _run_json("--watermark", wm)
        payload, result = _run_json("--watermark", wm)
        assert result.exit_code == 0
        assert payload["embedded"] == 0
        assert payload["scanned"] == 0

    def test_dry_run_writes_nothing(self, registry, tmp_path, recorder) -> None:
        seed_traces(registry, 2)
        payload, result = _run_json(
            "--dry-run", "--watermark", str(tmp_path / "w.json")
        )
        assert result.exit_code == 0
        assert payload["dry_run"] is True
        assert payload["embedded"] == 2
        assert recorder.texts == []

    def test_a_partial_pass_exits_non_zero(self, registry, tmp_path, recorder) -> None:
        seed_traces(registry, 4)
        recorder.fail_after = 1
        payload, result = _run_json("--watermark", str(tmp_path / "wm.json"))
        assert result.exit_code == 1, result.output
        assert payload["status"] == "partial"
        assert payload["failed"] == 3
        assert len(payload["failures"]) == 3

    def test_missing_embedder_exits_non_zero(
        self, registry, tmp_path, monkeypatch
    ) -> None:
        from trellis_cli.stores import _reset_registry

        monkeypatch.delenv("TRELLIS_EMBEDDING_FN", raising=False)
        _reset_registry()
        result = runner.invoke(
            worker_app,
            ["embed-traces", "--format", "json", "--watermark", str(tmp_path / "w")],
        )
        assert result.exit_code == 1
        assert json.loads(result.output.strip().splitlines()[-1])["status"] == "error"

    def test_text_output_names_the_cursor_move(self, registry, tmp_path) -> None:
        seed_traces(registry, 2)
        result = runner.invoke(
            worker_app, ["embed-traces", "--watermark", str(tmp_path / "wm.json")]
        )
        assert result.exit_code == 0, result.output
        assert "worker embed-traces" in result.output
        assert "cursor:" in result.output
