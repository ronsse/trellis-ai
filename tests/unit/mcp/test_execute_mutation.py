"""Tests for the ``execute_mutation`` MCP tool.

Lives in its own module so it stays file-disjoint from the broader
``test_server.py`` test surface that other swarm units may be expanding
in parallel.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS
from pydantic import ValidationError

from tests.unit.mcp.conftest import unwrap_tool
from trellis.errors import ConfigError
from trellis.mcp.server import execute_mutation as _execute_mutation
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.trace import Trace
from trellis.stores.base.event_log import Event, EventType
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

    def test_evidence_ingest_allocates_id_without_clearing_capture_banner(
        self, temp_registry: StoreRegistry
    ) -> None:
        payload = json.loads(
            execute_mutation(
                operation="evidence.ingest",
                args={"evidence": {"content": "operator supplied evidence"}},
            )
        )

        assert payload["status"] == "success"
        assert temp_registry.knowledge.document_store.get(payload["created_id"])
        assert not temp_registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED,
            limit=50,
        )

    def test_evidence_ingest_accepts_uri_without_content(
        self, temp_registry: StoreRegistry
    ) -> None:
        payload = json.loads(
            execute_mutation(
                operation="evidence.ingest",
                args={"evidence": {"uri": "s3://bucket/object.json"}},
            )
        )

        assert payload["status"] == "success"
        stored = temp_registry.knowledge.document_store.get(payload["created_id"])
        assert stored["content"] == ""
        assert stored["metadata"]["uri"] == "s3://bucket/object.json"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestExecuteMutationErrors:
    @pytest.mark.parametrize(
        "evidence",
        [
            {"content": "body", "metadata": ["not", "a", "mapping"]},
            {"content": "body", "embed_mode": "none"},
        ],
    )
    def test_evidence_ingest_rejects_unsupported_shape(
        self,
        temp_registry: StoreRegistry,
        evidence: dict,
    ) -> None:
        payload = json.loads(
            execute_mutation(
                operation="evidence.ingest",
                args={"evidence": evidence},
            )
        )

        assert payload["status"] == "rejected"
        assert temp_registry.knowledge.document_store.count() == 0

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
        ``data`` for correlation. The message names the exception by its
        type alone, because its text can be a driver's."""
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
        assert err.error.message == "execution failed: RuntimeError"
        assert "fake executor outage" not in str(err.error.data)
        # ``from exc`` preserves the original cause.
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == "fake executor outage"
        assert err.error.data is not None
        assert err.error.data["operation"] == "link.create"
        assert "command_id" in err.error.data
        assert err.error.data["error_class"] == "RuntimeError"

    def test_a_trellis_error_keeps_its_text(self, temp_registry: StoreRegistry) -> None:
        """A damaged policy file fails ``build_curate_executor`` with a
        ``ConfigError``, whose text names the file and the fix. Its message
        is what it was before untyped text was dropped."""
        (temp_registry.stores_dir / "policies.json").write_text(
            '{"polices": []}', encoding="utf-8"
        )

        with pytest.raises(McpError) as excinfo:
            execute_mutation(
                operation="link.create",
                args={"source_id": "a", "target_id": "b", "edge_kind": "k"},
            )
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert isinstance(err.__cause__, ConfigError)
        assert err.error.message == f"execution failed: {err.__cause__}"
        assert "policies.json" in err.error.message

    def test_missing_required_arg_returns_validation_error(
        self, temp_registry: StoreRegistry
    ) -> None:
        """LINK_CREATE without ``edge_kind`` fails validation in the executor."""
        raw = execute_mutation(
            operation="link.create",
            args={"source_id": "a", "target_id": "b"},  # missing edge_kind
        )
        payload = json.loads(raw)
        # The executor refuses this at Stage 1 with a REJECTED CommandResult,
        # which the tool relays verbatim — status is the executor's
        # "rejected", not the tool's pre-flight "error".
        assert payload["status"] == "rejected"
        assert "validation failed" in payload["message"].lower()
        assert "edge_kind" in payload["message"]
        assert payload["operation"] == "link.create"

    def test_a_missing_arg_is_reported_as_the_roster_refusal_is(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Both Stage 1 refusals answer ``rejected`` with the same fields."""
        missing_arg = json.loads(
            execute_mutation(operation="link.create", args={"source_id": "syn-node-a"})
        )
        roster = json.loads(
            execute_mutation(
                operation="entity.create",
                args={"entity_type": "service", "name": "syn-svc"},
                actor="worker:embed-traces",
            )
        )
        assert missing_arg["status"] == roster["status"] == "rejected"
        assert sorted(missing_arg) == sorted(roster)
        assert missing_arg["message"] == (
            "Validation failed: Missing required args: edge_kind, target_id"
        )

    def test_feedback_record_refuses_an_out_of_range_rating(
        self, temp_registry: StoreRegistry
    ) -> None:
        """A ``feedback.record`` rating of 5.0 is rejected and nothing is recorded.

        ``execute_mutation`` passes caller ``args`` straight into the ``Command``.
        """
        raw = execute_mutation(
            operation="feedback.record",
            args={"target_id": "t1", "rating": 5.0},
        )
        payload = json.loads(raw)
        assert payload["status"] == "rejected"
        assert "rating" in payload["message"].lower()
        assert not temp_registry.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=5
        )

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


# ---------------------------------------------------------------------------
# trace.ingest targets its trace
# ---------------------------------------------------------------------------

_TRACE: dict[str, Any] = {
    "source": "agent",
    "intent": "exercise the trace target",
    "context": {"agent_id": "test-agent"},
}


def _ingest(trace: Any) -> dict[str, Any]:
    return json.loads(execute_mutation(operation="trace.ingest", args={"trace": trace}))


def _audit_event(registry: StoreRegistry, command_id: str) -> Event:
    """Return the executor's one audit event for *command_id*."""
    (event,) = [
        event
        for event_type in (EventType.MUTATION_EXECUTED, EventType.MUTATION_REJECTED)
        for event in registry.operational.event_log.get_events(
            event_type=event_type, limit=100
        )
        if event.payload.get("command_id") == command_id
    ]
    return event


def _deny_traces(registry: StoreRegistry, operation: str) -> None:
    """Declare one enforced ``deny`` rule for *operation*, scoped to traces."""
    policy = Policy(
        policy_type="mutation",
        scope=PolicyScope(level="entity_type", value="trace"),
        rules=[
            PolicyRule(operation=operation, condition="synthetic-freeze", action="deny")
        ],
        enforcement="enforce",
    )
    (registry.stores_dir / "policies.json").write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}),
        encoding="utf-8",
    )


class TestTraceIngestTarget:
    """``trace.ingest`` targets its trace, as ``save_experience`` does.

    ``trellis ingest trace`` and ``POST /api/v1/traces`` do the same. The
    policy gate matches an ``entity_type`` scope on ``target_type``, and the
    executor's audit event records ``target_type`` and ``target_id``.
    """

    def test_the_audit_event_names_each_stored_trace(
        self, temp_registry: StoreRegistry
    ) -> None:
        traces = [
            _TRACE,  # the id is minted when the server validates the trace
            {**_TRACE, "trace_id": "synthetic-trace-1"},
            Trace.model_validate(_TRACE),
        ]
        for trace in traces:
            payload = _ingest(trace)
            assert payload["status"] == "success", payload
            stored = temp_registry.operational.trace_store.get(payload["created_id"])
            assert stored is not None

            event = _audit_event(temp_registry, payload["command_id"])

            assert (event.event_type, event.entity_type, event.entity_id) == (
                EventType.MUTATION_EXECUTED,
                "trace",
                stored.trace_id,
            )

    def test_a_policy_scoped_to_traces_refuses_the_ingest(
        self, temp_registry: StoreRegistry
    ) -> None:
        _deny_traces(temp_registry, "trace.ingest")

        payload = _ingest(_TRACE)

        assert (payload["status"], payload["message"]) == (
            "rejected",
            "Denied by policy: synthetic-freeze",
        )
        assert temp_registry.operational.trace_store.count() == 0

    def test_a_policy_scoped_to_traces_leaves_other_operations_alone(
        self, temp_registry: StoreRegistry
    ) -> None:
        _deny_traces(temp_registry, "*")
        graph = temp_registry.knowledge.graph_store
        source_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "src"}
        )
        target_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "dst"}
        )

        link = json.loads(
            execute_mutation(
                operation="link.create",
                args={
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_kind": "entity_related_to",
                    # A trace among another operation's args does not make
                    # that operation a trace write.
                    "trace": _TRACE,
                },
            )
        )

        assert link["status"] == "success", link
        # The same policy is live: it refuses a trace.
        assert _ingest(_TRACE)["status"] == "rejected"

    @pytest.mark.parametrize(
        "trace",
        [
            {"source": "agent", "intent": "exercise the trace target"},
            json.dumps(_TRACE),
            None,
        ],
        ids=["missing-field", "json-string", "null"],
    )
    def test_an_invalid_trace_is_still_refused_by_the_handler(
        self, temp_registry: StoreRegistry, trace: Any
    ) -> None:
        """A trace that does not validate goes on untargeted.

        A policy scoped to traces does not match it, and the handler refuses
        it, naming the trace's validation error by its type.
        """
        _deny_traces(temp_registry, "trace.ingest")
        with pytest.raises(ValidationError):
            Trace.model_validate(trace)

        payload = _ingest(trace)

        assert (payload["status"], payload["message"]) == (
            "failed",
            "Execution failed: ValidationError",
        )
        assert temp_registry.operational.trace_store.count() == 0
        event = _audit_event(temp_registry, payload["command_id"])
        assert (event.entity_type, event.entity_id) == (None, None)
