"""MCP document doors and a ``trace-summary:`` id (#1054).

``save_memory(doc_id=...)`` and ``execute_mutation("evidence.ingest", ...)``
end in ``EvidenceIngestHandler``, which refuses a caller-chosen id under the
trace embed worker's prefix. ``save_knowledge`` takes no document id: it mints
the evidence document's id itself, so a caller cannot name one through it, and
an ``evidence_ref`` only points at a document that already exists.

Defective subject: drop the prefix check in ``_evidence_document_id`` and the
refusals below store the row.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.hashing import content_hash
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)
save_knowledge = unwrap_tool(server_mod.save_knowledge)
save_memory = unwrap_tool(server_mod.save_memory)

RESERVED_ID = "trace-summary:trace-0001"
CONTROL_IDS = ["trace-summary", "Trace-Summary:x", "my-trace-summary:x"]
WORKER_ACTOR = "worker:embed-traces"


def _nothing_stored(registry: StoreRegistry, doc_id: str) -> bool:
    return (
        registry.knowledge.document_store.get(doc_id) is None
        and registry.knowledge.vector_store.get(doc_id) is None
    )


def _evidence_args(doc_id: str) -> dict:
    return {"evidence": {"doc_id": doc_id, "content": "a decoy under a summary id"}}


def test_save_memory_refuses_a_summary_id(temp_registry: StoreRegistry) -> None:
    with pytest.raises(McpError) as excinfo:
        save_memory("a decoy under a summary id", doc_id=RESERVED_ID)

    assert "trace embed worker" in excinfo.value.error.message
    assert _nothing_stored(temp_registry, RESERVED_ID)


@pytest.mark.parametrize("doc_id", CONTROL_IDS)
def test_save_memory_writes_an_id_that_only_resembles_the_prefix(
    temp_registry: StoreRegistry, doc_id: str
) -> None:
    save_memory(f"a note filed under {doc_id}", doc_id=doc_id)

    assert temp_registry.knowledge.document_store.get(doc_id) is not None


def test_save_knowledge_mints_its_own_evidence_id(
    temp_registry: StoreRegistry,
) -> None:
    """A caller-chosen entity name does not become a document id."""
    reply = save_knowledge(RESERVED_ID, content="a decoy under a summary id")

    store = temp_registry.knowledge.document_store
    row = store.get_by_hash(content_hash("a decoy under a summary id"))
    assert row is not None
    assert not row["doc_id"].startswith("trace-summary:")
    assert f"Evidence document: {row['doc_id']}" in reply
    assert _nothing_stored(temp_registry, RESERVED_ID)


def test_save_knowledge_cannot_point_at_a_summary_id_that_does_not_exist(
    temp_registry: StoreRegistry,
) -> None:
    with pytest.raises(McpError):
        save_knowledge("X", evidence_ref=RESERVED_ID)

    assert _nothing_stored(temp_registry, RESERVED_ID)


def test_execute_mutation_refuses_a_summary_id(temp_registry: StoreRegistry) -> None:
    payload = json.loads(
        execute_mutation(operation="evidence.ingest", args=_evidence_args(RESERVED_ID))
    )

    assert payload["status"] in ("rejected", "failed")
    assert "trace embed worker" in payload["message"]
    assert _nothing_stored(temp_registry, RESERVED_ID)


def test_execute_mutation_with_a_forged_worker_actor_is_still_refused(
    temp_registry: StoreRegistry,
) -> None:
    payload = json.loads(
        execute_mutation(
            operation="evidence.ingest",
            args=_evidence_args(RESERVED_ID),
            actor=WORKER_ACTOR,
        )
    )

    assert payload["status"] in ("rejected", "failed")
    assert _nothing_stored(temp_registry, RESERVED_ID)


@pytest.mark.parametrize("doc_id", CONTROL_IDS)
def test_execute_mutation_writes_an_id_that_only_resembles_the_prefix(
    temp_registry: StoreRegistry, doc_id: str
) -> None:
    payload = json.loads(
        execute_mutation(operation="evidence.ingest", args=_evidence_args(doc_id))
    )

    assert payload["status"] == "success"
    assert temp_registry.knowledge.document_store.get(doc_id) is not None
