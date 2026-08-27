"""The acceptance test: an interrupted pass resumes without skipping rows.

This file is deliberately first. Everything else in this worker is ordinary —
render some markdown, upsert a vector — and the one thing that can fail
*silently* is the bookkeeping that decides which traces a pass looks at. A
tracking gap does not raise, does not fail a summary, and does not show up in
any count: the trace is simply never embedded and every report says ok.

So each test here ends with the same two assertions, made against the **vector
store**, not against the report the code under test produced:

* every seeded trace has a vector row (nothing skipped), and
* the embedder saw each trace's text exactly once (nothing embedded twice).

Three interruption shapes are covered, because they leave different wreckage:

1. **Cooperative stop** (SIGINT with a handler) — the pass saves the cursor it
   actually reached.
2. **Hard kill** (SIGKILL, power loss) — the cursor is never written at all.
   The pass that follows re-scans from zero and must recognise the finished
   work from the store rather than re-embedding it.
3. **Mid-pass backend failure** — the embedder dies partway. The cursor must
   pin behind the first failure even though later traces were still attempted.
"""

from __future__ import annotations

import pytest

from trellis_workers.trace_embed import (
    TraceEmbedWatermark,
    run_trace_embed_pass,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.watermark import TraceCursor

from .conftest import seed_traces


def _embedded_trace_ids(registry, traces) -> set[str]:
    """Trace ids the *vector store* can actually serve, read back directly."""
    return {
        t.trace_id
        for t in traces
        if registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
        is not None
    }


def _assert_complete_and_deduped(registry, traces, recorder) -> None:
    assert _embedded_trace_ids(registry, traces) == {t.trace_id for t in traces}
    for trace in traces:
        assert recorder.calls_containing(trace.intent) == 1, (
            f"{trace.trace_id} was embedded "
            f"{recorder.calls_containing(trace.intent)} times"
        )


class TestInterruptedPassResumes:
    def test_cooperative_stop_then_resume(
        self, registry, watermark_path, recorder
    ) -> None:
        """SIGINT-shaped stop: cursor saved at the contiguous prefix."""
        traces = seed_traces(registry, 10)

        # Stop after the pass has considered 4 traces — the shape a signal
        # handler produces by flipping a flag the loop polls.
        budget = {"left": 4}

        def should_continue() -> bool:
            if budget["left"] <= 0:
                return False
            budget["left"] -= 1
            return True

        first = run_trace_embed_pass(
            registry,
            watermark_path=watermark_path,
            should_continue=should_continue,
        )
        assert first.stopped_early is True
        assert first.embedded == 4
        assert len(_embedded_trace_ids(registry, traces)) == 4

        # The cursor stopped exactly where the work did — not at the newest
        # trace it had collected.
        saved = TraceEmbedWatermark(watermark_path).cursor
        assert saved == TraceCursor(
            created_at=traces[3].created_at, trace_id=traces[3].trace_id
        )

        second = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert second.embedded == 6
        assert second.skipped_existing == 0, (
            "resuming from a saved cursor should not re-read finished traces"
        )
        _assert_complete_and_deduped(registry, traces, recorder)

    def test_hard_kill_loses_the_cursor_entirely(
        self, registry, watermark_path, recorder, monkeypatch
    ) -> None:
        """SIGKILL: the cursor never reaches disk, so pass 2 starts from zero.

        This is the case a watermark-only design gets wrong in the *other*
        direction — it re-embeds everything. The store-state check is what
        makes the re-scan free.
        """
        traces = seed_traces(registry, 10)

        budget = {"left": 4}

        def should_continue() -> bool:
            if budget["left"] <= 0:
                return False
            budget["left"] -= 1
            return True

        # The process dies before the cursor is flushed.
        monkeypatch.setattr(TraceEmbedWatermark, "save", lambda self: None)
        run_trace_embed_pass(
            registry,
            watermark_path=watermark_path,
            should_continue=should_continue,
        )
        monkeypatch.undo()

        assert not watermark_path.exists(), "staging error: cursor should be lost"
        assert len(_embedded_trace_ids(registry, traces)) == 4

        second = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert second.scanned == 10, "a lost cursor must re-scan the whole range"
        assert second.skipped_existing == 4, (
            "the four already-embedded traces must be recognised from the "
            "vector store, not re-embedded"
        )
        assert second.embedded == 6
        _assert_complete_and_deduped(registry, traces, recorder)

    def test_backend_failure_pins_the_cursor_behind_the_gap(
        self, registry, watermark_path, recorder
    ) -> None:
        """The embedder dies mid-pass; later traces are still attempted."""
        traces = seed_traces(registry, 8)
        recorder.fail_after = 3

        first = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert first.embedded == 3
        assert first.failed == 5, "every trace after the gap is still attempted"
        assert first.to_dict()["status"] == "partial"

        # Pinned behind the first failure — not at the newest success, and not
        # at the end of the batch.
        saved = TraceEmbedWatermark(watermark_path).cursor
        assert saved == TraceCursor(
            created_at=traces[2].created_at, trace_id=traces[2].trace_id
        )

        recorder.fail_after = 0
        second = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert second.embedded == 5
        _assert_complete_and_deduped(registry, traces, recorder)

    def test_failure_in_the_middle_does_not_strand_later_traces(
        self, registry, watermark_path, recorder
    ) -> None:
        """One unrenderable trace must not stall every newer one forever.

        The cursor stays behind it (so it is retried), but the traces after it
        are embedded on the *same* pass — a stop-the-world design would leave
        the newest work unreachable until someone fixed the bad row.
        """
        traces = seed_traces(registry, 6)
        # A trace with nothing to render: no intent, no outcome summary, no
        # step errors. The renderer returns "" and the driver refuses to
        # advance past it.
        broken = traces[2]
        raw = broken.model_copy(update={"intent": "", "outcome": None, "steps": []})
        registry.operational.trace_store._conn.execute(
            "UPDATE traces SET trace_json = ? WHERE trace_id = ?",
            (raw.model_dump_json(), broken.trace_id),
        )
        registry.operational.trace_store._conn.commit()

        report = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert report.skipped_empty == 1
        assert report.embedded == 5, "later traces are embedded despite the gap"

        saved = TraceEmbedWatermark(watermark_path).cursor
        assert saved == TraceCursor(
            created_at=traces[1].created_at, trace_id=traces[1].trace_id
        )

        # A re-run re-reaches the broken trace (and everything after it, all of
        # which the store-state check reports as already done).
        again = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert again.scanned == 4
        assert again.skipped_empty == 1
        assert again.skipped_existing == 3
        assert again.embedded == 0


class TestStateIsTheAuthority:
    def test_a_deleted_vector_row_is_repaired_not_reported_done(
        self, registry, watermark_path, recorder
    ) -> None:
        """The case an idempotency key would have made permanent.

        Keying the governed command on the trace id would make every later
        attempt a ``DUPLICATE``, so a trace whose vector row went missing
        after a successful write could never be repaired — and the summary
        would call it deduped rather than missing. Asking the vector store
        instead cannot say "done" about a row that is not there.
        """
        traces = seed_traces(registry, 3)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert len(_embedded_trace_ids(registry, traces)) == 3

        victim = traces[1]
        registry.knowledge.vector_store.delete(trace_summary_doc_id(victim.trace_id))
        assert (
            registry.knowledge.vector_store.get(trace_summary_doc_id(victim.trace_id))
            is None
        )

        # The cursor is past it, so the repair needs a reset — which is exactly
        # what the cursor's docstring promises costs time and not rows.
        repaired = run_trace_embed_pass(
            registry, watermark_path=watermark_path, reset_watermark=True
        )
        assert repaired.embedded == 1
        assert repaired.skipped_existing == 2
        assert _embedded_trace_ids(registry, traces) == {t.trace_id for t in traces}

    def test_a_document_without_a_vector_is_not_counted_as_done(
        self, registry, watermark_path, recorder
    ) -> None:
        """Doc-first ordering means the document can exist while the vector
        does not. The done-check must read the vector, or that trace is
        skipped forever while looking finished."""
        traces = seed_traces(registry, 2)
        doc_id = trace_summary_doc_id(traces[0].trace_id)
        registry.knowledge.document_store.put(doc_id, "a half-written row", metadata={})

        report = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert report.embedded == 2, "the orphaned document must not mask the gap"
        assert _embedded_trace_ids(registry, traces) == {t.trace_id for t in traces}


class TestCollectionHasNoGaps:
    @pytest.mark.parametrize("page_size", [1, 2, 3, 7, 200])
    def test_paging_reaches_every_trace(
        self, registry, watermark_path, recorder, page_size
    ) -> None:
        """``TraceStore.query`` is newest-first with no offset, so the walk
        pages *backwards*. Any off-by-one there drops traces silently."""
        traces = seed_traces(registry, 12)
        report = run_trace_embed_pass(
            registry, watermark_path=watermark_path, page_size=page_size
        )
        assert report.scanned == 12
        assert report.embedded == 12
        _assert_complete_and_deduped(registry, traces, recorder)

    def test_traces_sharing_a_timestamp_are_all_collected(
        self, registry, watermark_path, recorder
    ) -> None:
        """Same-instant siblings are the tie-break case the cursor carries a
        ``trace_id`` for; an inclusive ``since`` would otherwise step over
        one of them."""
        from .conftest import make_trace

        base = make_trace(0).created_at
        siblings = []
        for idx in range(4):
            trace = make_trace(idx).model_copy(
                update={"trace_id": f"tie-{idx}", "created_at": base}
            )
            registry.operational.trace_store.append(trace)
            siblings.append(trace)

        run_trace_embed_pass(
            registry, watermark_path=watermark_path, page_size=2, limit=2
        )
        run_trace_embed_pass(registry, watermark_path=watermark_path, page_size=2)
        _assert_complete_and_deduped(registry, siblings, recorder)
