"""Every write goes through the governed pipeline, and refuses to look green.

Three things are asserted here that no summary count can show:

* the write is audited — a ``MUTATION_EXECUTED`` event under
  ``evidence.ingest``, attributed to ``worker:embed-traces``;
* the embed is **not** fail-soft, unlike the ingest-time hook it shares a core
  with, because this worker's success contract *is* the vector row;
* a pass with no embedder or no vector store raises instead of reporting a
  clean run over zero traces.
"""

from __future__ import annotations

import pytest

from trellis.mutate.commands import CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventType
from trellis_workers.trace_embed import (
    TraceEmbedUnavailableError,
    TraceSummaryIngestHandler,
    build_trace_summary_command,
    run_trace_embed_pass,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.handler import REQUESTED_BY

from .conftest import seed_traces


def _executed_events(registry):
    return [
        e
        for e in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED, limit=500
        )
        if e.payload.get("operation") == Operation.EVIDENCE_INGEST
    ]


class TestGovernedWrite:
    def test_each_embed_emits_one_audited_mutation(
        self, registry, watermark_path
    ) -> None:
        traces = seed_traces(registry, 3)
        run_trace_embed_pass(registry, watermark_path=watermark_path)

        events = _executed_events(registry)
        assert len(events) == 3
        assert {e.entity_id for e in events} == {
            trace_summary_doc_id(t.trace_id) for t in traces
        }
        assert {e.payload["requested_by"] for e in events} == {REQUESTED_BY}
        assert {e.payload["status"] for e in events} == {CommandStatus.SUCCESS}
        assert {e.entity_type for e in events} == {"document"}

    def test_a_second_pass_emits_nothing(self, registry, watermark_path) -> None:
        """Idempotency is state-based: the already-embedded traces are skipped
        before a command is built, so the audit log does not accumulate a
        replay event per trace per pass."""
        seed_traces(registry, 3)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        run_trace_embed_pass(
            registry, watermark_path=watermark_path, reset_watermark=True
        )
        assert len(_executed_events(registry)) == 3

    def test_the_command_carries_no_idempotency_key(self) -> None:
        """Load-bearing, and the reason is in ``handler.py``: keying on the
        trace would make every later attempt a permanent ``DUPLICATE``, so a
        vector row that went missing after a successful write could never be
        repaired — and the pass would report it deduped rather than absent."""
        command = build_trace_summary_command(
            doc_id="trace-summary:x",
            trace_id="x",
            content="body",
            metadata={},
        )
        assert command.idempotency_key is None
        assert command.operation is Operation.EVIDENCE_INGEST

    def test_resubmitting_the_same_command_writes_again(
        self, registry, recorder
    ) -> None:
        """The behaviour that key would have blocked, exercised directly."""
        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers={
                "evidence.ingest": TraceSummaryIngestHandler(registry, recorder),
            },
        )
        command = build_trace_summary_command(
            doc_id="trace-summary:x",
            trace_id="x",
            content="a body with words in it",
            metadata={},
        )
        assert executor.execute(command).status is CommandStatus.SUCCESS
        registry.knowledge.vector_store.delete("trace-summary:x")
        assert executor.execute(command).status is CommandStatus.SUCCESS
        assert registry.knowledge.vector_store.get("trace-summary:x") is not None


class TestFailuresAreLoud:
    def test_an_embed_failure_fails_the_command(self, registry, recorder) -> None:
        recorder.fail_after = 0
        recorder.texts = ["already used"]
        recorder.fail_after = 1
        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers={
                "evidence.ingest": TraceSummaryIngestHandler(registry, recorder),
            },
        )
        result = executor.execute(
            build_trace_summary_command(
                doc_id="trace-summary:x",
                trace_id="x",
                content="a body with words in it",
                metadata={},
            )
        )
        assert result.status is CommandStatus.FAILED
        # Doc-first: the document row is written before the embed, so it
        # survives. That is the acceptable half of a partial failure — the
        # next pass overwrites it, and no vector row points at nothing.
        assert registry.knowledge.document_store.get("trace-summary:x") is not None
        assert registry.knowledge.vector_store.get("trace-summary:x") is None

    def test_an_empty_body_is_a_structured_rejection(self, registry, recorder) -> None:
        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers={
                "evidence.ingest": TraceSummaryIngestHandler(registry, recorder),
            },
        )
        result = executor.execute(
            build_trace_summary_command(
                doc_id="trace-summary:x", trace_id="x", content="   ", metadata={}
            )
        )
        assert result.status is CommandStatus.REJECTED
        rejected = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED, limit=50
        )
        assert any(e.payload.get("reason") == "trace_summary_empty" for e in rejected)

    def test_no_embedder_refuses_to_run(
        self, registry, watermark_path, monkeypatch
    ) -> None:
        """The failure mode this whole item is about: a pass that reports a
        clean run having made nothing retrievable."""
        monkeypatch.setattr(type(registry), "embedding_fn", property(lambda self: None))
        with pytest.raises(TraceEmbedUnavailableError, match="embeddings config"):
            run_trace_embed_pass(registry, watermark_path=watermark_path)

    def test_no_vector_store_refuses_to_run(
        self, registry, watermark_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            type(registry.knowledge), "vector_store", property(lambda self: None)
        )
        with pytest.raises(TraceEmbedUnavailableError, match="vector store"):
            run_trace_embed_pass(registry, watermark_path=watermark_path)


class TestDryRun:
    def test_writes_nothing_and_leaves_the_cursor(
        self, registry, watermark_path, recorder
    ) -> None:
        traces = seed_traces(registry, 4)
        report = run_trace_embed_pass(
            registry, watermark_path=watermark_path, dry_run=True
        )
        assert report.embedded == 4
        assert report.dry_run is True
        assert recorder.texts == []
        assert not watermark_path.exists()
        assert all(
            registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
            is None
            for t in traces
        )

    def test_a_dry_run_after_real_work_counts_only_the_remainder(
        self, registry, watermark_path
    ) -> None:
        seed_traces(registry, 4)
        run_trace_embed_pass(registry, watermark_path=watermark_path, limit=2)
        report = run_trace_embed_pass(
            registry, watermark_path=watermark_path, dry_run=True
        )
        assert report.embedded == 2


class TestLimit:
    def test_limit_bounds_the_pass_and_the_cursor_follows(
        self, registry, watermark_path
    ) -> None:
        traces = seed_traces(registry, 6)
        first = run_trace_embed_pass(registry, watermark_path=watermark_path, limit=2)
        assert first.embedded == 2
        assert first.watermark_after == traces[1].created_at.isoformat()
        second = run_trace_embed_pass(registry, watermark_path=watermark_path)
        assert second.embedded == 4
        assert second.scanned == 4


class TestDocumentRowStamps:
    def test_a_repair_does_not_re_rank_the_document(
        self, registry, watermark_path, recorder
    ) -> None:
        """``updated_at`` drives ``KeywordSearch``'s recency decay, and the
        body is derived from an immutable trace — so a re-put is a repair,
        never an edit, and must not bump the stamp."""
        traces = seed_traces(registry, 1)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        doc_id = trace_summary_doc_id(traces[0].trace_id)
        first = registry.knowledge.document_store.get(doc_id)["updated_at"]

        registry.knowledge.vector_store.delete(doc_id)
        run_trace_embed_pass(
            registry, watermark_path=watermark_path, reset_watermark=True
        )
        assert registry.knowledge.document_store.get(doc_id)["updated_at"] == first

    def test_the_vector_row_carries_the_trace_timestamp(
        self, registry, watermark_path, recorder
    ) -> None:
        """The semantic axis gets the real stamp even though the document row
        cannot be backdated — that asymmetry is documented in the handler."""
        traces = seed_traces(registry, 1)
        run_trace_embed_pass(registry, watermark_path=watermark_path)
        row = registry.knowledge.vector_store.get(
            trace_summary_doc_id(traces[0].trace_id)
        )
        assert row["metadata"]["created_at"] == traces[0].created_at.isoformat()
