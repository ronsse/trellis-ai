"""A caller cannot make a trace look embedded by filing its summary id (#1054).

``trace_is_embedded`` asks the vector store whether a row exists at
``trace-summary:<trace_id>``. With embed-on-ingest on, a document written at
that id through the evidence funnel gets a vector row there, and the worker
would then skip the real trace for good. The funnel refuses the prefix; the
worker's own handler is the one writer exempt from that refusal.

Defective subject: drop the prefix check in ``_evidence_document_id`` and
``test_filing_a_summary_id_does_not_hide_the_trace`` counts the trace as
``skipped_existing`` with its real summary never written.
"""

from __future__ import annotations

import pytest

from trellis.core.reserved_doc_ids import TRACE_SUMMARY_DOC_ID_PREFIX
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis_workers.trace_embed import (
    DOC_ID_PREFIX,
    run_trace_embed_pass,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.handler import (
    TraceSummaryIngestHandler,
    build_trace_summary_command,
)
from trellis_workers.trace_embed.render import DOC_ID_PREFIX as RENDER_DOC_ID_PREFIX

from .conftest import seed_traces


@pytest.fixture(autouse=True)
def _embed_on_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_ENABLE_EMBED_ON_INGEST", "1")


def test_filing_a_summary_id_does_not_hide_the_trace(
    registry, watermark_path, recorder
) -> None:
    [trace] = seed_traces(registry, 1)
    doc_id = trace_summary_doc_id(trace.trace_id)

    command = build_evidence_ingest_command(
        doc_id=doc_id,
        content="a decoy filed under the summary id",
        requested_by="test",
    )
    result = build_curate_executor(registry, clearance="restricted").execute(command)

    assert result.status in (CommandStatus.FAILED, CommandStatus.REJECTED)
    assert registry.knowledge.document_store.get(doc_id) is None
    assert registry.knowledge.vector_store.get(doc_id) is None

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.embedded == 1
    assert report.skipped_existing == 0
    assert registry.knowledge.vector_store.get(doc_id) is not None
    stored = registry.knowledge.document_store.get(doc_id)
    assert stored is not None
    assert trace.intent in stored["content"]


def test_the_worker_handler_is_exempt_and_writes_its_own_id(registry, recorder) -> None:
    """The one writer the refusal does not cover: the worker's own handler."""
    doc_id = trace_summary_doc_id("trace-0001")
    command = build_trace_summary_command(
        doc_id=doc_id,
        trace_id="trace-0001",
        content="rendered summary of a trace",
        metadata={},
    )

    handler = TraceSummaryIngestHandler(registry, recorder)
    created_id, _message = handler.handle(command)

    assert TraceSummaryIngestHandler.reserved_doc_id_prefixes == ()
    assert created_id == doc_id
    assert registry.knowledge.document_store.get(doc_id) is not None
    assert registry.knowledge.vector_store.get(doc_id) is not None


def test_a_pass_still_embeds_a_trace_end_to_end(
    registry, watermark_path, recorder
) -> None:
    traces = seed_traces(registry, 2)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.embedded == 2
    assert report.failed == 0
    for trace in traces:
        doc_id = trace_summary_doc_id(trace.trace_id)
        assert registry.knowledge.document_store.get(doc_id) is not None
        assert registry.knowledge.vector_store.get(doc_id) is not None


def test_the_workers_prefix_is_the_cores() -> None:
    assert RENDER_DOC_ID_PREFIX == TRACE_SUMMARY_DOC_ID_PREFIX
    assert DOC_ID_PREFIX == TRACE_SUMMARY_DOC_ID_PREFIX
    assert TRACE_SUMMARY_DOC_ID_PREFIX == "trace-summary:", (
        "operations.md and the refusal text name this literal"
    )
