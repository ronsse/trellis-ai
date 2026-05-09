"""Tests for the ``execute_mutation`` MCP tool.

Lives in its own module so it stays file-disjoint from the broader
``test_server.py`` test surface that other swarm units may be expanding
in parallel.
"""

from __future__ import annotations

import json

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import execute_mutation as _execute_mutation
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(_execute_mutation)


# ``_suppress_structlog`` and ``temp_registry`` come from conftest.py.


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestExecuteMutationHappyPath:
    def test_link_create_round_trip(self, temp_registry: StoreRegistry) -> None:
        """LINK_CREATE with valid args creates an edge and returns success."""
        graph = temp_registry.knowledge.graph_store
        source_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "src"}
        )
        target_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "dst"}
        )

        # Accept the wire form value ("link.create").
        raw = execute_mutation(
            operation="link.create",
            args={
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": "entity_related_to",
            },
        )
        payload = json.loads(raw)

        assert payload["status"] == "success"
        assert payload["operation"] == "link.create"
        assert "created_id" in payload
        assert payload["created_id"]
        assert payload["command_id"]

        # Edge actually exists in the graph store.
        edges = graph.get_edges(source_id, direction="outgoing")
        assert any(e.get("target_id") == target_id for e in edges)

    def test_screaming_snake_operation_alias(
        self, temp_registry: StoreRegistry
    ) -> None:
        """``LINK_CREATE`` (enum-name form) resolves to ``link.create``."""
        graph = temp_registry.knowledge.graph_store
        source_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "src2"}
        )
        target_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "dst2"}
        )

        raw = execute_mutation(
            operation="LINK_CREATE",
            args={
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": "entity_related_to",
            },
        )
        payload = json.loads(raw)
        assert payload["status"] == "success"
        assert payload["operation"] == "link.create"

    def test_actor_is_recorded_on_event(self, temp_registry: StoreRegistry) -> None:
        """The ``actor`` argument flows into the audit event payload."""
        graph = temp_registry.knowledge.graph_store
        source_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "a"}
        )
        target_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "b"}
        )

        raw = execute_mutation(
            operation="link.create",
            args={
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": "entity_related_to",
            },
            actor="cli:operator-script",
        )
        payload = json.loads(raw)
        assert payload["status"] == "success"

        events = temp_registry.operational.event_log.get_events(limit=50)
        mutation_events = [
            ev for ev in events if ev.payload and "requested_by" in ev.payload
        ]
        assert any(
            ev.payload.get("requested_by") == "cli:operator-script"
            for ev in mutation_events
        )

    def test_idempotency_key_dedups_repeat_submission(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Same idempotency_key on a second call returns ``duplicate``."""
        graph = temp_registry.knowledge.graph_store
        source_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "x"}
        )
        target_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "y"}
        )
        key = "idem-test-1"

        first = json.loads(
            execute_mutation(
                operation="link.create",
                args={
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_kind": "entity_related_to",
                },
                idempotency_key=key,
            )
        )
        second = json.loads(
            execute_mutation(
                operation="link.create",
                args={
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_kind": "entity_related_to",
                },
                idempotency_key=key,
            )
        )
        assert first["status"] == "success"
        assert second["status"] == "duplicate"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestExecuteMutationErrors:
    def test_unknown_operation_returns_error(
        self, temp_registry: StoreRegistry
    ) -> None:
        """An operation string that matches no enum member is rejected."""
        raw = execute_mutation(
            operation="link.zorblax",
            args={"source_id": "a", "target_id": "b", "edge_kind": "k"},
        )
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "unknown operation" in payload["message"].lower()

    def test_empty_operation_returns_error(
        self, temp_registry: StoreRegistry
    ) -> None:
        raw = execute_mutation(operation="   ", args={})
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "operation must not be empty" in payload["message"].lower()

    def test_missing_required_arg_returns_validation_error(
        self, temp_registry: StoreRegistry
    ) -> None:
        """LINK_CREATE without ``edge_kind`` fails validation in the executor."""
        raw = execute_mutation(
            operation="link.create",
            args={"source_id": "a", "target_id": "b"},  # missing edge_kind
        )
        payload = json.loads(raw)
        # The executor surfaces this as a FAILED CommandResult, which the
        # tool relays verbatim — status is the executor's "failed", not
        # the tool's pre-flight "error".
        assert payload["status"] == "failed"
        assert "validation failed" in payload["message"].lower()
        assert "edge_kind" in payload["message"]
        assert payload["operation"] == "link.create"

    def test_handler_failure_surfaces_failed_status(
        self, temp_registry: StoreRegistry
    ) -> None:
        """A handler that raises (e.g. unknown source node) yields ``failed``."""
        raw = execute_mutation(
            operation="link.create",
            args={
                "source_id": "does-not-exist-source",
                "target_id": "does-not-exist-target",
                "edge_kind": "entity_related_to",
            },
        )
        payload = json.loads(raw)
        assert payload["status"] == "failed"
        assert "does not reference an existing entity" in payload["message"].lower()
