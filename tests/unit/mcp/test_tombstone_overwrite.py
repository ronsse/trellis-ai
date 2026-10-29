"""MCP surfaces and a redaction tombstone (#1034).

``save_memory`` carries no re-ingest parameter: an agent's save is not the
owner's say-so, so it is refused and the message names the two places that can
say so. ``execute_mutation`` carries the flag in ``evidence``.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.redaction_tombstone import make_tombstone
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.document_write import REINGEST_STAMP_KEY, is_redaction_tombstone
from trellis.core.vector_metadata import resolve_vector_store
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
execute_mutation = unwrap_tool(server_mod.execute_mutation)

DOC_ID = "memory-gone"
NEW_TEXT = "Quartz harbor rewritten text"


@pytest.fixture
def tombstone(temp_registry: StoreRegistry) -> StoreRegistry:
    make_tombstone(
        temp_registry.knowledge.document_store,
        resolve_vector_store(temp_registry),
        DOC_ID,
    )
    return temp_registry


def test_save_memory_over_a_tombstone_is_refused_and_names_the_ways_out(
    tombstone: StoreRegistry,
) -> None:
    """Defective subject: drop the tombstone check in ``put_document`` and the
    save returns normally with the redacted id written back."""
    with pytest.raises(McpError) as excinfo:
        save_memory(content=NEW_TEXT, doc_id=DOC_ID)

    message = excinfo.value.error.message
    assert "trellis ingest evidence --reingest-redacted" in message
    assert "execute_mutation" in message
    assert is_redaction_tombstone(tombstone.knowledge.document_store.get(DOC_ID))


def test_execute_mutation_without_the_flag_is_rejected(
    tombstone: StoreRegistry,
) -> None:
    payload = json.loads(
        execute_mutation(
            operation="evidence.ingest",
            args={"evidence": {"doc_id": DOC_ID, "content": NEW_TEXT}},
        )
    )

    assert payload["status"] == "rejected"
    assert "reingest-redacted" in payload["message"]
    assert is_redaction_tombstone(tombstone.knowledge.document_store.get(DOC_ID))


def test_execute_mutation_with_the_flag_writes_and_stamps(
    tombstone: StoreRegistry,
) -> None:
    payload = json.loads(
        execute_mutation(
            operation="evidence.ingest",
            args={
                "evidence": {
                    "doc_id": DOC_ID,
                    "content": NEW_TEXT,
                    "reingest_redacted": True,
                }
            },
        )
    )

    assert payload["status"] == "success", payload
    row = tombstone.knowledge.document_store.get(DOC_ID)
    assert row["content"] == NEW_TEXT
    assert REINGEST_STAMP_KEY in row["metadata"]
