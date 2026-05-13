"""Tests for the ``execute_mutation`` MCP tool.

Lives in its own module so it stays file-disjoint from the broader
``test_server.py`` test surface that other swarm units may be expanding
in parallel.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

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
    def test_unknown_operation_raises_invalid_params(
        self, temp_registry: StoreRegistry
    ) -> None:
        """An operation string that matches no enum member raises INVALID_PARAMS."""
        with pytest.raises(McpError) as excinfo:
            execute_mutation(
                operation="link.zorblax",
                args={"source_id": "a", "target_id": "b", "edge_kind": "k"},
            )
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "unknown operation" in excinfo.value.error.message.lower()
        assert excinfo.value.error.data is not None
        assert excinfo.value.error.data["value"] == "link.zorblax"

    def test_empty_operation_raises_invalid_params(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(McpError) as excinfo:
            execute_mutation(operation="   ", args={})
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "operation must not be empty" in excinfo.value.error.message.lower()
        assert excinfo.value.error.data == {"field": "operation"}

    def test_non_dict_args_raises_invalid_params(
        self, temp_registry: StoreRegistry
    ) -> None:
        """``args`` must be a dict; bytes / list / scalar all rejected pre-flight."""
        with pytest.raises(McpError) as excinfo:
            execute_mutation(operation="link.create", args="not-a-dict")  # type: ignore[arg-type]
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "args must be a dict" in excinfo.value.error.message.lower()

    def test_executor_crash_raises_internal_error_with_chain(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unexpected executor exceptions surface as INTERNAL_ERROR with
        the original cause chained via ``from`` and the command_id in
        ``data`` for correlation."""
        import trellis.mcp.server as server_mod

        class _ExplodingExecutor:
            def execute(self, _command: object) -> None:
                msg = "fake executor outage"
                raise RuntimeError(msg)

        monkeypatch.setattr(
            server_mod, "build_curate_executor", lambda _r: _ExplodingExecutor()
        )

        with pytest.raises(McpError) as excinfo:
            execute_mutation(
                operation="link.create",
                args={"source_id": "a", "target_id": "b", "edge_kind": "k"},
            )
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "execution failed" in err.error.message.lower()
        assert "fake executor outage" in err.error.message
        # ``from exc`` preserves the original cause.
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == "fake executor outage"
        assert err.error.data is not None
        assert err.error.data["operation"] == "link.create"
        assert "command_id" in err.error.data
        assert err.error.data["error_class"] == "RuntimeError"

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

    def test_handler_failure_surfaces_rejected_status(
        self, temp_registry: StoreRegistry
    ) -> None:
        """A handler-raised ``ValidationError`` (e.g. orphan-edge FK miss)
        now surfaces as ``rejected`` rather than ``failed``: per Variant A'
        in adr-extraction-validation.md §5.5, ``LinkCreateHandler`` raises
        ``ValidationError(code="orphan_edge")`` and the executor routes that
        through ``_emit_rejection`` so the audit event carries a structured
        ``reason`` field — distinct from unexpected handler exceptions
        which still surface as ``failed``."""
        raw = execute_mutation(
            operation="link.create",
            args={
                "source_id": "does-not-exist-source",
                "target_id": "does-not-exist-target",
                "edge_kind": "entity_related_to",
            },
        )
        payload = json.loads(raw)
        assert payload["status"] == "rejected"
        assert "does not reference an existing entity" in payload["message"].lower()
