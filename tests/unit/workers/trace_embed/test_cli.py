"""``trellis worker embed-traces`` — the front door.

Covers what a scheduled invocation depends on: the JSON contract, and the exit
code. A pass that left traces unreachable must not exit zero; a green systemd
unit over a silent gap is the shape of failure this worker exists to remove.
"""

from __future__ import annotations

import json

from tests.cli_output import plain
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
        payload, result = _run_json(
            cli_runner, "--watermark", str(tmp_path / "wm.json")
        )
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
        payload, result = _run_json(
            cli_runner, "--watermark", str(tmp_path / "wm.json")
        )
        assert result.exit_code == 1, result.output
        assert payload["status"] == "partial"
        assert payload["failed"] == 3
        assert len(payload["failures"]) == 3

    def test_a_permanently_invalid_row_exits_non_zero(
        self, registry, tmp_path, cli_runner
    ) -> None:
        """A hazardous trace id advances the cursor, but the pass that moved
        past it must still exit non-zero — a scheduler that only watches the
        exit code must see this, not just the JSON payload."""
        from .conftest import make_trace

        good = make_trace(0)
        bad = make_trace(1).model_copy(update={"trace_id": "bad\x00trace"})
        for trace in (good, bad):
            registry.operational.trace_store.append(trace)

        payload, result = _run_json(
            cli_runner, "--watermark", str(tmp_path / "wm.json")
        )
        assert result.exit_code == 1, result.output
        assert payload["status"] == "partial"
        assert payload["skipped_invalid"] == 1
        assert payload["failed"] == 0

    def test_only_skipped_empty_exits_non_zero_and_reports_partial(
        self, registry, tmp_path, cli_runner
    ) -> None:
        """``skipped_empty`` alone must flip ``status`` to ``partial`` in
        both formats, matching the exit code the CLI already returns for
        it (src/trellis_cli/worker.py): a green ``status: ok`` next to a
        non-zero exit would be a payload that disagrees with the process
        result a scheduler actually observes."""
        from .conftest import make_trace

        good = make_trace(0)
        empty = make_trace(1)
        for trace in (good, empty):
            registry.operational.trace_store.append(trace)
        # Nothing to render: no intent, no outcome, no step errors. Written
        # as a direct store update (like TestPermanentlyInvalidRowDoesNotPin
        # TheWatermark in test_resume.py) because Trace itself requires a
        # non-empty intent.
        blanked = empty.model_copy(update={"intent": "", "outcome": None, "steps": []})
        registry.operational.trace_store._conn.execute(
            "UPDATE traces SET trace_json = ? WHERE trace_id = ?",
            (blanked.model_dump_json(), blanked.trace_id),
        )
        registry.operational.trace_store._conn.commit()

        payload, result = _run_json(
            cli_runner, "--watermark", str(tmp_path / "wm.json")
        )
        assert result.exit_code == 1, result.output
        assert payload["status"] == "partial"
        assert payload["skipped_empty"] == 1
        assert payload["failed"] == 0
        assert payload["skipped_invalid"] == 0

        text_result = cli_runner.invoke(
            worker_app,
            ["embed-traces", "--watermark", str(tmp_path / "wm-text.json")],
        )
        assert text_result.exit_code == 1, text_result.output

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

    def test_text_output_shows_redacted_summaries_only_when_there_are_some(
        self, registry, tmp_path, cli_runner
    ) -> None:
        from tests.redaction_tombstone import make_tombstone
        from trellis.core.vector_metadata import resolve_vector_store

        traces = seed_traces(registry, 3)
        make_tombstone(
            registry.knowledge.document_store,
            resolve_vector_store(registry),
            trace_summary_doc_id(traces[1].trace_id),
        )

        redacted = cli_runner.invoke(
            worker_app,
            ["embed-traces", "--dry-run", "--watermark", str(tmp_path / "wm.json")],
        )
        assert redacted.exit_code == 0, redacted.output
        assert "1 trace(s) skipped because their summary was redacted" in plain(
            redacted.stdout
        )

    def test_text_output_omits_the_redacted_line_when_none_were(
        self, registry, tmp_path, cli_runner
    ) -> None:
        seed_traces(registry, 2)
        result = cli_runner.invoke(
            worker_app,
            ["embed-traces", "--dry-run", "--watermark", str(tmp_path / "wm.json")],
        )
        assert result.exit_code == 0, result.output
        # Not a bare "redacted": the structured log line carries
        # ``skipped_redacted=0`` as a key, whatever the renderer prints.
        assert "because their summary was redacted" not in plain(result.stdout)

    def test_json_always_carries_skipped_redacted(
        self, registry, tmp_path, cli_runner
    ) -> None:
        seed_traces(registry, 1)
        payload, result = _run_json(
            cli_runner, "--dry-run", "--watermark", str(tmp_path / "wm.json")
        )
        assert result.exit_code == 0, result.output
        assert payload["skipped_redacted"] == 0
