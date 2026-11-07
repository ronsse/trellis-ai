"""A held trace goes on a bounded retry list; it no longer starves the cursor.

Before the list, a trace that could not be embedded yet (a cited document no
store could answer for, a failed write) held the cursor, and every pass took the
oldest ``max_scan`` traces after it: the same slice, led by the same held trace.
Newer traces were never reached, the backlog outgrew ``max_scan *
SCAN_CEILING_FACTOR`` and every pass then refused.

Each test stages a held trace with a poison set that rejects chosen trace ids at
the validation stage (a plain ``failed``), and lifts it by emptying the set. The
assertions are made against the vector store and the watermark file, not only
against the report.
"""

from __future__ import annotations

import json

import pytest

from tests.cli_output import plain
from tests.redaction_tombstone import make_tombstone
from trellis.core.vector_metadata import resolve_vector_store
from trellis.mutate.commands import OperationRegistry
from trellis.schemas.trace import TraceStep
from trellis_cli.worker import worker_app
from trellis_workers.trace_embed import (
    TraceCursor,
    TraceEmbedWatermark,
    run_trace_embed_pass,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.worker import RETRY_LIST_LIMIT

from .conftest import seed_traces


@pytest.fixture
def poison(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Trace ids the validation stage refuses; empty the set to lift them."""
    refused: set[str] = set()
    original = OperationRegistry.validate

    def validate(self, command):
        evidence = command.args.get("evidence") or {}
        trace_id = evidence.get("trace_id") if isinstance(evidence, dict) else None
        if trace_id in refused:
            return False, ["staged rejection for this test"]
        return original(self, command)

    monkeypatch.setattr(OperationRegistry, "validate", validate)
    return refused


def _entry(trace) -> TraceCursor:
    return TraceCursor(created_at=trace.created_at, trace_id=trace.trace_id)


def _embedded(registry, traces) -> set[str]:
    return {
        t.trace_id
        for t in traces
        if registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
        is not None
    }


class TestAHeldTraceDoesNotStarveTheCursor:
    def test_successive_passes_reach_every_newer_trace(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        """``max_scan=2``, one held trace at the front, five newer traces."""
        traces = seed_traces(registry, 6)
        held, newer = traces[0], traces[1:]
        poison.add(held.trace_id)

        reports = [
            run_trace_embed_pass(
                registry, watermark_path=watermark_path, max_scan=2, page_size=2
            )
            for _ in range(3)
        ]

        assert _embedded(registry, traces) == {t.trace_id for t in newer}
        # Each pass does at most ``max_scan`` new traces, plus the listed one.
        assert [r.scanned for r in reports] == [2, 3, 3]
        assert [r.embedded for r in reports] == [1, 2, 2]
        assert [r.retry_pending for r in reports] == [1, 1, 1]
        assert all(r.to_dict()["status"] == "partial" for r in reports)
        assert TraceEmbedWatermark(watermark_path).retry == (_entry(held),)
        assert reports[-1].watermark_after == newer[-1].created_at.isoformat()
        assert reports[-1].more_remaining is False

        # The store recovers: the next pass embeds the listed trace and the
        # list empties, and the report stops being partial.
        poison.clear()
        recovered = run_trace_embed_pass(
            registry, watermark_path=watermark_path, max_scan=2, page_size=2
        )
        assert recovered.embedded == 1
        assert recovered.retry_pending == 0
        assert recovered.to_dict()["status"] == "ok"
        assert _embedded(registry, traces) == {t.trace_id for t in traces}
        assert TraceEmbedWatermark(watermark_path).retry == ()
        for trace in traces:
            assert recorder.calls_containing(trace.intent) == 1

    def test_the_old_starvation_does_not_end_in_a_scan_refusal(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        """The failure the list exists for, driven to its end: a held trace at
        the front and a backlog past ``max_scan * SCAN_CEILING_FACTOR`` would
        refuse every pass. With the list, the backlog is worked off instead."""
        traces = seed_traces(registry, 12)
        poison.add(traces[0].trace_id)

        for _ in range(12):
            run_trace_embed_pass(
                registry, watermark_path=watermark_path, max_scan=1, page_size=3
            )

        assert _embedded(registry, traces) == {t.trace_id for t in traces[1:]}

    def test_a_held_trace_that_is_still_unreadable_stays_listed(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        """No attempt budget: it is retried on every pass, however many."""
        traces = seed_traces(registry, 2)
        poison.add(traces[0].trace_id)

        reports = [
            run_trace_embed_pass(registry, watermark_path=watermark_path)
            for _ in range(4)
        ]

        assert [r.failed for r in reports] == [1, 1, 1, 1]
        assert [r.retry_pending for r in reports] == [1, 1, 1, 1]
        assert TraceEmbedWatermark(watermark_path).retry == (_entry(traces[0]),)


class TestAFullListHoldsTheCursor:
    def test_the_cursor_holds_as_it_did_before_the_list(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 4)
        poison.update({traces[0].trace_id, traces[1].trace_id})

        first = run_trace_embed_pass(
            registry, watermark_path=watermark_path, retry_limit=1
        )

        saved = TraceEmbedWatermark(watermark_path)
        assert first.failed == 2
        assert first.embedded == 2, "later traces are still attempted"
        assert saved.retry == (_entry(traces[0]),)
        # Pinned behind the second held trace, which the list had no room for.
        assert saved.cursor == _entry(traces[0])
        assert first.retry_pending == 1

        poison.clear()
        second = run_trace_embed_pass(
            registry, watermark_path=watermark_path, retry_limit=1
        )

        assert second.embedded == 2, "the listed trace and the one behind the cursor"
        assert second.skipped_existing == 2
        assert second.retry_pending == 0
        assert _embedded(registry, traces) == {t.trace_id for t in traces}
        assert TraceEmbedWatermark(watermark_path).retry == ()
        for trace in traces:
            assert recorder.calls_containing(trace.intent) == 1

    def test_no_trace_is_dropped_to_make_room(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 6)
        poison.update(t.trace_id for t in traces[:5])

        for _ in range(3):
            run_trace_embed_pass(registry, watermark_path=watermark_path, retry_limit=2)
        listed = TraceEmbedWatermark(watermark_path).retry
        assert listed == (_entry(traces[0]), _entry(traces[1]))

        poison.clear()
        for _ in range(3):
            run_trace_embed_pass(registry, watermark_path=watermark_path, retry_limit=2)
        assert _embedded(registry, traces) == {t.trace_id for t in traces}

    def test_a_limit_of_zero_is_the_behaviour_before_the_list(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 3)
        poison.add(traces[0].trace_id)

        report = run_trace_embed_pass(
            registry, watermark_path=watermark_path, retry_limit=0
        )

        assert report.embedded == 2
        assert report.retry_pending == 0
        assert report.watermark_after is None
        saved = TraceEmbedWatermark(watermark_path)
        assert saved.cursor is None
        assert saved.retry == ()

    def test_the_default_bound_is_a_small_whole_number(self) -> None:
        assert 0 < RETRY_LIST_LIMIT <= 1000


class TestAPermanentOutcomeLeavesTheList:
    def test_a_redaction_found_on_retry_removes_the_trace(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 2)
        poison.add(traces[0].trace_id)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert TraceEmbedWatermark(watermark_path).retry == (_entry(traces[0]),)

        # The held summary is redacted meanwhile: the write seam now refuses it
        # on every attempt, so the retry is settled, not pending.
        poison.clear()
        make_tombstone(
            registry.knowledge.document_store,
            resolve_vector_store(registry),
            trace_summary_doc_id(traces[0].trace_id),
        )
        report = run_trace_embed_pass(registry, watermark_path=watermark_path)

        assert report.skipped_redacted == 1
        assert report.retry_pending == 0
        assert report.to_dict()["status"] == "ok"
        assert TraceEmbedWatermark(watermark_path).retry == ()
        assert _embedded(registry, traces) == {traces[1].trace_id}

    def test_a_trace_the_store_no_longer_has_leaves_the_list(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 2)
        poison.add(traces[0].trace_id)
        run_trace_embed_pass(registry, watermark_path=watermark_path)

        store = registry.operational.trace_store
        store._conn.execute(
            "DELETE FROM traces WHERE trace_id = ?", (traces[0].trace_id,)
        )
        store._conn.commit()
        report = run_trace_embed_pass(registry, watermark_path=watermark_path)

        assert report.retry_pending == 0
        assert report.scanned == 0, "nothing to attempt, nothing to count"
        assert TraceEmbedWatermark(watermark_path).retry == ()

    def test_a_trace_the_store_no_longer_has_is_counted_once(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        """The warning that used to be the only record is invisible on the
        default CLI, so the loss is a report counter, and the pass is partial
        for the one pass that finds it."""
        traces = seed_traces(registry, 2)
        poison.add(traces[0].trace_id)
        run_trace_embed_pass(registry, watermark_path=watermark_path)

        store = registry.operational.trace_store
        store._conn.execute(
            "DELETE FROM traces WHERE trace_id = ?", (traces[0].trace_id,)
        )
        store._conn.commit()
        stored = watermark_path.read_bytes()

        dry = run_trace_embed_pass(
            registry, watermark_path=watermark_path, dry_run=True
        )
        assert dry.retry_missing == 1
        assert dry.to_dict()["status"] == "partial"
        assert watermark_path.read_bytes() == stored, "a dry run lists nothing"

        found = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert found.retry_missing == 1
        assert found.retry_pending == 0
        assert found.to_dict()["retry_missing"] == 1
        assert found.to_dict()["status"] == "partial"

        settled = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert settled.retry_missing == 0
        assert settled.to_dict()["status"] == "ok"


class TestAnEmptyRenderIsHeldNotRefused:
    """``skipped_empty`` is not a permanent outcome, so it stays on the list.

    A trace is immutable, but what it renders to is not a function of the trace
    alone: ``include_step_errors`` is a per-pass setting. A trace whose only
    prose is a step error renders to nothing under ``--no-step-errors`` and to a
    summary otherwise, so a later pass can embed what an earlier one could not.
    Advancing past it as a permanent refusal would skip that trace for good.
    """

    def test_a_trace_empty_only_without_step_errors_is_listed_then_embedded(
        self, registry, watermark_path, recorder
    ) -> None:
        traces = seed_traces(registry, 3)
        only_an_error = traces[1].model_copy(
            update={
                "intent": "",
                "outcome": None,
                "steps": [
                    TraceStep(step_type="tool_call", name="build", error="disk full")
                ],
            }
        )
        store = registry.operational.trace_store
        store._conn.execute(
            "UPDATE traces SET trace_json = ? WHERE trace_id = ?",
            (only_an_error.model_dump_json(), only_an_error.trace_id),
        )
        store._conn.commit()

        narrow = run_trace_embed_pass(
            registry, watermark_path=watermark_path, include_step_errors=False
        )
        assert narrow.skipped_empty == 1
        assert narrow.retry_pending == 1
        assert narrow.to_dict()["status"] == "partial"
        saved = TraceEmbedWatermark(watermark_path)
        assert saved.cursor == _entry(traces[2]), "the cursor moved past it"
        assert saved.retry == (_entry(only_an_error),), "and it is listed, not dropped"
        assert _embedded(registry, traces) == {traces[0].trace_id, traces[2].trace_id}

        wide = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert wide.skipped_empty == 0
        assert wide.embedded == 1
        assert wide.retry_pending == 0
        assert TraceEmbedWatermark(watermark_path).retry == ()
        assert _embedded(registry, traces) == {t.trace_id for t in traces}


class TestDryRunAndReset:
    def test_a_dry_run_writes_no_list_and_leaves_the_stored_one(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 3)
        poison.add(traces[0].trace_id)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        before = watermark_path.read_bytes()

        poison.clear()
        dry = run_trace_embed_pass(
            registry, watermark_path=watermark_path, dry_run=True
        )

        assert watermark_path.read_bytes() == before
        assert dry.retry_pending == 1, "a dry run reports the list as stored"
        assert _embedded(registry, traces) == {traces[1].trace_id, traces[2].trace_id}

    def test_a_dry_run_over_a_fresh_store_creates_no_file(
        self, registry, watermark_path, recorder
    ) -> None:
        seed_traces(registry, 3)
        run_trace_embed_pass(registry, watermark_path=watermark_path, dry_run=True)
        assert not watermark_path.exists()

    def test_reset_forgets_the_list_with_the_cursor(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 3)
        poison.add(traces[0].trace_id)
        run_trace_embed_pass(registry, watermark_path=watermark_path)

        reset = run_trace_embed_pass(
            registry, watermark_path=watermark_path, reset_watermark=True
        )

        # The re-scan reaches all three itself; had the list survived, the held
        # trace would have been attempted twice (three scanned plus one).
        assert reset.scanned == 3
        assert reset.failed == 1
        assert TraceEmbedWatermark(watermark_path).retry == (_entry(traces[0]),)

    def test_a_cooperative_stop_keeps_the_list(
        self, registry, watermark_path, recorder, poison
    ) -> None:
        traces = seed_traces(registry, 3)
        poison.add(traces[0].trace_id)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        before = watermark_path.read_bytes()

        stopped = run_trace_embed_pass(
            registry, watermark_path=watermark_path, should_continue=lambda: False
        )

        assert stopped.stopped_early is True
        assert stopped.scanned == 0
        assert stopped.retry_pending == 1
        assert watermark_path.read_bytes() == before


class TestTheCLI:
    def _run(self, runner, watermark_path, *args: str):
        return runner.invoke(
            worker_app,
            ["embed-traces", "--watermark", str(watermark_path), *args],
        )

    def test_a_listed_trace_makes_the_pass_partial_and_exit_non_zero(
        self, registry, watermark_path, recorder, poison, cli_runner
    ) -> None:
        traces = seed_traces(registry, 2)
        poison.add(traces[0].trace_id)

        result = self._run(cli_runner, watermark_path, "--format", "json")
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert result.exit_code == 1, result.output
        assert payload["status"] == "partial"
        assert payload["retry_pending"] == 1

        text = self._run(cli_runner, watermark_path)
        assert text.exit_code == 1, text.output
        assert "retry list: 1 trace(s)" in plain(text.output)

    def test_a_clean_pass_carries_a_zero_count_and_no_line(
        self, registry, watermark_path, recorder, cli_runner
    ) -> None:
        seed_traces(registry, 2)

        result = self._run(cli_runner, watermark_path, "--format", "json")
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert result.exit_code == 0, result.output
        assert payload["status"] == "ok"
        assert payload["retry_pending"] == 0

        text = self._run(cli_runner, watermark_path)
        assert "retry list" not in plain(text.output)
        assert payload["retry_missing"] == 0

    def test_a_listed_trace_that_vanished_exits_non_zero_in_both_formats(
        self, registry, tmp_path, recorder, poison, cli_runner
    ) -> None:
        """The exit is the same whichever ``--format`` asked, and it is the one
        pass that finds the loss: the next one is clean."""
        traces = seed_traces(registry, 2)
        poison.add(traces[0].trace_id)
        for name in ("json.wm", "text.wm"):
            self._run(cli_runner, tmp_path / name)
        store = registry.operational.trace_store
        store._conn.execute(
            "DELETE FROM traces WHERE trace_id = ?", (traces[0].trace_id,)
        )
        store._conn.commit()

        as_json = self._run(cli_runner, tmp_path / "json.wm", "--format", "json")
        payload = json.loads(as_json.output.strip().splitlines()[-1])
        assert as_json.exit_code == 1, as_json.output
        assert payload["status"] == "partial"
        assert payload["retry_missing"] == 1

        as_text = self._run(cli_runner, tmp_path / "text.wm")
        assert as_text.exit_code == 1, as_text.output
        assert "no longer in the trace store" in plain(as_text.output)

        again = self._run(cli_runner, tmp_path / "json.wm", "--format", "json")
        assert again.exit_code == 0, again.output
        assert json.loads(again.output.strip().splitlines()[-1])["retry_missing"] == 0


def test_a_pass_resumes_from_a_watermark_written_before_the_list(
    registry, watermark_path, recorder
) -> None:
    """An older file has no ``retry`` key: it loads as an empty list."""
    traces = seed_traces(registry, 4)
    watermark_path.write_text(
        json.dumps({"version": 1, "cursor": _entry(traces[1]).to_json()}),
        encoding="utf-8",
    )

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.scanned == 2
    assert report.embedded == 2
    assert report.retry_pending == 0
    assert _embedded(registry, traces) == {traces[2].trace_id, traces[3].trace_id}
