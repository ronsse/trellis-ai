"""A caller may not name a document under ``trace-summary:`` (#1054).

The trace embed worker decides a trace is already embedded by asking the vector
store for a row at ``trace-summary:<trace_id>``. A caller who could file a
document there, with embed-on-ingest on, would hide the real trace from the
worker for good. Every document door ends in ``EvidenceIngestHandler``, so the
refusal lives there; the worker's own handler clears it with a class attribute,
which a caller cannot reach the way it can write ``Command.requested_by``.

Defective subject: drop the prefix check in ``_evidence_document_id`` and every
refusal below stores the row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.core.reserved_doc_ids import (
    RESERVED_TRACE_SUMMARY_ID_CODE,
    TRACE_SUMMARY_DOC_ID_PREFIX,
    is_trace_summary_doc_id,
    reserved_trace_summary_id_message,
)
from trellis.errors import ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandResult, CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.mutate.handlers import EvidenceIngestHandler, _evidence_document_id
from trellis.stores.registry import StoreRegistry
from trellis_workers.trace_embed.handler import (
    REQUESTED_BY,
    build_trace_summary_command,
)

REFUSED = [
    "trace-summary:abc",
    "trace-summary:",
    "trace-summary:trace:1",
    "trace-summary:a#chunk-x",
]
# Near misses: the prefix is exact (colon included) and case-sensitive, and it
# must start the id.
ACCEPTED = [
    "trace-summary",
    "Trace-Summary:x",
    "my-trace-summary:x",
    "trace-summary-x",
    " trace-summary:x",
]


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _ingest(
    registry: StoreRegistry, doc_id: str, requested_by: str = "test"
) -> CommandResult:
    command = build_evidence_ingest_command(
        doc_id=doc_id,
        content="a memory a caller filed under its own id",
        requested_by=requested_by,
        embed_mode="none",
    )
    return build_curate_executor(
        registry, evidence_embed="none", clearance="restricted"
    ).execute(command)


@pytest.mark.parametrize("doc_id", REFUSED)
def test_the_funnel_refuses_a_caller_named_summary_id(
    registry: StoreRegistry, doc_id: str
) -> None:
    result = _ingest(registry, doc_id)

    assert result.status is CommandStatus.REJECTED
    assert result.metadata.get("rejection_reason") == RESERVED_TRACE_SUMMARY_ID_CODE
    assert "trace embed worker" in result.message
    assert registry.knowledge.document_store.get(doc_id) is None
    assert registry.knowledge.vector_store.get(doc_id) is None


@pytest.mark.parametrize("doc_id", ACCEPTED)
def test_an_id_that_only_resembles_the_prefix_is_still_a_document(
    registry: StoreRegistry, doc_id: str
) -> None:
    result = _ingest(registry, doc_id)

    assert result.status is CommandStatus.SUCCESS
    assert registry.knowledge.document_store.get(doc_id) is not None


def test_a_forged_worker_requested_by_does_not_clear_the_refusal(
    registry: StoreRegistry,
) -> None:
    """``requested_by`` is a string the caller writes; it must not be the key."""
    result = _ingest(registry, "trace-summary:abc", requested_by=REQUESTED_BY)

    assert result.status is CommandStatus.REJECTED
    assert registry.knowledge.document_store.get("trace-summary:abc") is None


def test_the_core_handler_refuses_the_workers_own_command(
    registry: StoreRegistry,
) -> None:
    """The exemption is the subclass, not anything the command says."""
    command = build_trace_summary_command(
        doc_id="trace-summary:t-1",
        trace_id="t-1",
        content="rendered summary",
        metadata={},
    )
    assert command.requested_by == REQUESTED_BY
    handler = EvidenceIngestHandler(
        registry, embed="strict", embedding_fn=lambda _text: [1.0, 0.0]
    )

    with pytest.raises(ValidationError) as excinfo:
        handler.handle(command)

    assert excinfo.value.code == RESERVED_TRACE_SUMMARY_ID_CODE
    assert registry.knowledge.document_store.get("trace-summary:t-1") is None


def test_the_refusal_carries_its_own_code_and_message() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _evidence_document_id("trace-summary:abc")

    assert excinfo.value.code == "reserved_trace_summary_id"
    assert excinfo.value.code != "reserved_document_id"
    assert str(excinfo.value) == reserved_trace_summary_id_message(
        "evidence.doc_id", "trace-summary:abc"
    )


def test_the_prefixes_are_a_parameter_with_the_workers_as_default() -> None:
    assert EvidenceIngestHandler.reserved_doc_id_prefixes == (
        TRACE_SUMMARY_DOC_ID_PREFIX,
    )
    with pytest.raises(ValidationError):
        _evidence_document_id("trace-summary:abc")
    assert _evidence_document_id("trace-summary:abc", ()) == "trace-summary:abc"
    with pytest.raises(ValidationError):
        _evidence_document_id("other:abc", ("other:",))


@pytest.mark.parametrize(
    ("doc_id", "expected"),
    [
        ("trace-summary:x", True),
        ("trace-summary:", True),
        ("trace-summary", False),
        ("Trace-Summary:x", False),
        ("my-trace-summary:x", False),
        (None, False),
        (5, False),
        (b"trace-summary:x", False),
    ],
)
def test_is_trace_summary_doc_id(doc_id: object, expected: bool) -> None:
    assert is_trace_summary_doc_id(doc_id) is expected
