"""MCP ``execute_mutation`` cannot forge a precedent's ``source_excerpt_status``.

The tool has no caller-settable ``Command.metadata``, so the reserved
``learning_promotion`` mark the promotion's own command carries cannot come
through it; a ``metadata`` or mark smuggled inside ``args`` is only another
argument and exempts nothing. Also covers the ``if_absent`` flag as an agent
sends it: a string ``"false"`` is not ``True`` and replaces as any create does.

Defective subject: drop the guard in ``EntityCreateHandler`` and the first test
stores the stamp; read ``if_absent`` by truthiness and the last test leaves the
node alone.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.precedent_document import (
    PROMOTION_COMMAND_MARK,
    SOURCE_EXCERPT_STATUS_KEY,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)

CODE = "precedent_excerpt_status_via_entity_create"


def _create_args(**extra: Any) -> dict[str, Any]:
    return {
        "entity_type": "precedent",
        "entity_id": "n1",
        "name": "Pooling",
        "properties": {SOURCE_EXCERPT_STATUS_KEY: "source_labelled"},
        **extra,
    }


class TestTheStampIsRefused:
    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"metadata": {PROMOTION_COMMAND_MARK: True}},
            {PROMOTION_COMMAND_MARK: True},
        ],
        ids=["plain", "metadata-in-args", "mark-in-args"],
    )
    def test_a_caller_set_status_on_a_precedent_is_rejected_and_audited(
        self, temp_registry: StoreRegistry, extra: dict[str, Any]
    ) -> None:
        payload = json.loads(
            execute_mutation(operation="entity.create", args=_create_args(**extra))
        )

        assert payload["status"] == "rejected", payload
        rejected = temp_registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
        assert [e.payload["reason"] for e in rejected] == [CODE]
        assert temp_registry.knowledge.graph_store.get_node("n1") is None

    def test_an_existing_precedent_keeps_its_stamp(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.graph_store.upsert_node(
            node_id="n1",
            node_type="precedent",
            properties={
                "name": "Pooling",
                SOURCE_EXCERPT_STATUS_KEY: "source_included",
            },
        )

        payload = json.loads(
            execute_mutation(operation="entity.create", args=_create_args())
        )

        assert payload["status"] == "rejected", payload
        node = temp_registry.knowledge.graph_store.get_node("n1")
        assert node is not None
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"


class TestIfAbsentAsAnAgentSendsIt:
    def test_the_string_false_replaces_like_any_create(
        self, temp_registry: StoreRegistry
    ) -> None:
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node(
            node_id="n1", node_type="concept", properties={"name": "Old", "k": 1}
        )

        payload = json.loads(
            execute_mutation(
                operation="entity.create",
                args={
                    "entity_type": "concept",
                    "entity_id": "n1",
                    "name": "New",
                    "if_absent": "false",
                },
            )
        )

        assert payload["status"] == "success", payload
        assert "left alone" not in payload["message"]
        node = graph.get_node("n1")
        assert node is not None
        assert node["properties"] == {"name": "New"}

    def test_the_string_false_creates_an_absent_node(
        self, temp_registry: StoreRegistry
    ) -> None:
        payload = json.loads(
            execute_mutation(
                operation="entity.create",
                args={
                    "entity_type": "concept",
                    "entity_id": "n2",
                    "name": "Fresh",
                    "if_absent": "false",
                },
            )
        )

        assert payload["status"] == "success", payload
        assert temp_registry.knowledge.graph_store.get_node("n2") is not None

    def test_a_real_true_leaves_an_existing_node_alone(
        self, temp_registry: StoreRegistry
    ) -> None:
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node(node_id="n1", node_type="concept", properties={"name": "Old"})

        payload = json.loads(
            execute_mutation(
                operation="entity.create",
                args={
                    "entity_type": "concept",
                    "entity_id": "n1",
                    "name": "New",
                    "if_absent": True,
                },
            )
        )

        assert payload["status"] == "success", payload
        assert "left alone" in payload["message"]
        node = graph.get_node("n1")
        assert node is not None
        assert node["properties"] == {"name": "Old"}
