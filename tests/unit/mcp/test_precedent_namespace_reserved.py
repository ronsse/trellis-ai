"""MCP ``execute_mutation`` cannot create under ``precedent://learning/`` (#1080).

The tool has no caller-settable ``Command.metadata``, so the reserved
``learning_promotion`` mark cannot come through it; a mark or ``metadata``
smuggled inside ``args`` is only another argument. MCP has no vector-write
tool, and an ``evidence.ingest`` command (which embeds its document) is refused
at a namespace document id before anything is written.

Defective subject: drop the new refusal in ``EntityCreateHandler`` and the
create tests store the node.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.precedent_document import (
    PRECEDENT_ENTITY_ID_PREFIX,
    PROMOTION_COMMAND_MARK,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)

CODE = "precedent_namespace_via_entity_create"
NODE_ID = f"{PRECEDENT_ENTITY_ID_PREFIX}p1"


def _args(**extra: Any) -> dict[str, Any]:
    return {
        "entity_type": "precedent",
        "entity_id": NODE_ID,
        "name": "Pooling",
        **extra,
    }


class TestCreate:
    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"metadata": {PROMOTION_COMMAND_MARK: True}},
            {PROMOTION_COMMAND_MARK: True},
            {"if_absent": True},
        ],
        ids=["plain", "metadata-in-args", "mark-in-args", "if-absent"],
    )
    def test_a_precedent_at_a_namespace_id_is_rejected_and_audited(
        self, temp_registry: StoreRegistry, extra: dict[str, Any]
    ) -> None:
        payload = json.loads(
            execute_mutation(operation="entity.create", args=_args(**extra))
        )

        assert payload["status"] == "rejected", payload
        rejected = temp_registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
        assert [e.payload["reason"] for e in rejected] == [CODE]
        assert temp_registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_an_ordinary_id_is_unaffected(self, temp_registry: StoreRegistry) -> None:
        payload = json.loads(
            execute_mutation(operation="entity.create", args=_args(entity_id="n1"))
        )

        assert payload["status"] == "success", payload
        assert temp_registry.knowledge.graph_store.get_node("n1") is not None

    def test_an_existing_precedent_is_edited_through_entity_update(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.graph_store.upsert_node(
            node_id=NODE_ID, node_type="precedent", properties={"name": "Pooling"}
        )

        payload = json.loads(
            execute_mutation(
                operation="entity.update",
                args={"entity_id": NODE_ID, "properties": {"note": "edited"}},
            )
        )

        assert payload["status"] == "success", payload


class TestNoVectorDoor:
    def test_no_mcp_tool_writes_a_vector_row_by_id(self) -> None:
        names = [n for n in dir(server_mod) if "vector" in n.lower()]
        assert not [n for n in names if "upsert" in n.lower() or "write" in n.lower()]

    def test_evidence_ingest_at_a_namespace_document_id_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        payload = json.loads(
            execute_mutation(
                operation="evidence.ingest",
                args={
                    "evidence": {
                        "doc_id": f"{PRECEDENT_ENTITY_ID_PREFIX}mine",
                        "content": "a lesson of my own",
                    }
                },
            )
        )

        assert payload["status"] == "rejected", payload
        assert (
            temp_registry.knowledge.document_store.get(
                f"{PRECEDENT_ENTITY_ID_PREFIX}mine"
            )
            is None
        )
        assert (
            temp_registry.knowledge.vector_store.get(
                f"{PRECEDENT_ENTITY_ID_PREFIX}mine"
            )
            is None
        )
