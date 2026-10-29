"""A caller may not name a document in the minted chunk shape (#1008).

Only corpus ingestion writes ``<parent>#chunk-<digits>`` rows, and every reader
takes that shape for a chunk, so a caller-named ``notes#chunk-5`` is a document
the system cannot tell from a fragment of ``notes``. The evidence funnel
(``save_memory``, ``POST /documents``, ``POST /ingest/evidence``, the generic
mutation surface) refuses it with the reserved-id code; ``trellis ingest
evidence`` writes directly and refuses it the same way
(``tests/unit/cli/test_ingest.py``).

Defective subject: drop the ``is_chunk_doc_id`` check in
``_evidence_document_id`` and every refusal below stores the row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.errors import ValidationError
from trellis.ingest_corpus.models import chunk_doc_id, reserved_chunk_doc_id_message
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandResult, CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.mutate.handlers import _evidence_document_id
from trellis.stores.registry import StoreRegistry

REFUSED = [
    "notes#chunk-5",
    "notes#chunk-0",
    "a#chunk-intro#chunk-12",
    "x#chunk-5#chunk-0",
]
# Shapes ``is_chunk_doc_id`` does not read as a chunk, so they stay documents.
ACCEPTED = ["notes#chunk-intro", "notes#chunk-5x", "notes#chunk-", "notes#chunk"]


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _ingest(registry: StoreRegistry, doc_id: str) -> CommandResult:
    command = build_evidence_ingest_command(
        doc_id=doc_id,
        content="a memory a caller filed under its own id",
        requested_by="test",
        embed_mode="none",
    )
    return build_curate_executor(
        registry, evidence_embed="none", clearance="restricted"
    ).execute(command)


@pytest.mark.parametrize("doc_id", REFUSED)
def test_the_funnel_refuses_a_caller_named_chunk_id(
    registry: StoreRegistry, doc_id: str
) -> None:
    result = _ingest(registry, doc_id)

    assert result.status in (CommandStatus.FAILED, CommandStatus.REJECTED)
    assert "shape of a chunk id" in (result.message or "")
    assert registry.knowledge.document_store.get(doc_id) is None


@pytest.mark.parametrize("doc_id", ACCEPTED)
def test_an_id_that_is_not_the_minted_shape_is_still_a_document(
    registry: StoreRegistry, doc_id: str
) -> None:
    result = _ingest(registry, doc_id)

    assert result.status is CommandStatus.SUCCESS
    assert registry.knowledge.document_store.get(doc_id) is not None


def test_the_refusal_carries_the_reserved_id_code() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _evidence_document_id(chunk_doc_id("notes", 5))

    assert excinfo.value.code == "reserved_document_id"
    assert str(excinfo.value) == reserved_chunk_doc_id_message(
        "evidence.doc_id", "notes#chunk-5"
    )
