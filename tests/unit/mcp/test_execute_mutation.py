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
from trellis.stores.settings_store import SettingsStore, resolve_settings_path

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


class TestPrecedentDemoteIsMcpReachable:
    """``precedent.demote`` needs no MCP-specific wiring: ``_resolve_operation``
    and ``execute_mutation`` are generic over every ``Operation`` enum
    member, so registering the handler in ``create_curate_handlers`` is
    sufficient. These tests pin that it actually is reachable end to end,
    not merely registered.
    """

    def test_demotes_a_precedent_node(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None, node_type="precedent", properties={"name": "Pooling pattern"}
        )

        raw = execute_mutation(
            operation="precedent.demote",
            args={"target_id": node_id, "reason": "superseded by a broader pattern"},
        )
        payload = json.loads(raw)

        assert payload["status"] == "success"
        assert payload["operation"] == "precedent.demote"
        assert payload["created_id"] == node_id
        node = graph.get_node(node_id)
        assert node["properties"]["lifecycle"]["state"] == "demoted"

    def test_refuses_a_non_precedent_node(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None, node_type="owner", properties={"name": "Nate"}
        )

        raw = execute_mutation(
            operation="precedent.demote",
            args={"target_id": node_id, "reason": "r"},
        )
        payload = json.loads(raw)

        assert payload["status"] == "rejected"

    def test_refuses_a_missing_target(self, temp_registry: StoreRegistry) -> None:
        raw = execute_mutation(
            operation="precedent.demote",
            args={"target_id": "ghost", "reason": "r"},
        )
        payload = json.loads(raw)

        assert payload["status"] == "rejected"
        assert "Node not found: ghost" in payload["message"]

    def test_mutation_executed_event_carries_the_target_as_entity_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The literal bug this guards against: an MCP ``execute_mutation``
        call used to reach the executor with ``Command.target_id`` unset,
        so the audited ``MUTATION_EXECUTED`` event's ``entity_id`` came
        back ``None`` -- an audit trail that cannot say which precedent a
        demotion touched.

        ``_resolve_execute_mutation_target`` fixes this by deriving
        ``target_id`` from ``args["target_id"]`` before the command is
        built. Defective subject: reverting that derivation (passing
        ``target_id=None`` through to the ``Command``) makes this
        assertion fail with ``entity_id is None``, the exact symptom
        reported.
        """
        graph = temp_registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None, node_type="precedent", properties={"name": "Pooling pattern"}
        )

        raw = execute_mutation(
            operation="precedent.demote",
            args={"target_id": node_id, "reason": "superseded"},
        )
        payload = json.loads(raw)
        assert payload["status"] == "success"

        events = [
            e
            for e in temp_registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED
            )
            if e.payload.get("operation") == "precedent.demote"
        ]
        assert len(events) == 1
        assert events[0].entity_id == node_id


class TestPrecedentReinstateIsMcpReachable:
    """``precedent.reinstate`` needs no MCP-specific wiring either -- same
    genericity as :class:`TestPrecedentDemoteIsMcpReachable` above, which
    these tests mirror.
    """

    def _demoted_precedent(self, temp_registry: StoreRegistry) -> str:
        graph = temp_registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None, node_type="precedent", properties={"name": "Pooling pattern"}
        )
        raw = execute_mutation(
            operation="precedent.demote",
            args={"target_id": node_id, "reason": "superseded"},
        )
        assert json.loads(raw)["status"] == "success"
        return node_id

    def test_reinstates_a_demoted_precedent(self, temp_registry: StoreRegistry) -> None:
        node_id = self._demoted_precedent(temp_registry)

        raw = execute_mutation(
            operation="precedent.reinstate",
            args={"target_id": node_id, "reason": "demotion was a mistake"},
        )
        payload = json.loads(raw)

        assert payload["status"] == "success"
        assert payload["operation"] == "precedent.reinstate"
        assert payload["created_id"] == node_id
        graph = temp_registry.knowledge.graph_store
        node = graph.get_node(node_id)
        assert node["properties"]["lifecycle"]["state"] == "current"

    def test_refuses_a_non_demoted_target(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None, node_type="precedent", properties={"name": "Pooling pattern"}
        )

        raw = execute_mutation(
            operation="precedent.reinstate",
            args={"target_id": node_id, "reason": "r"},
        )
        payload = json.loads(raw)

        assert payload["status"] == "rejected"
        assert "not 'demoted'" in payload["message"]

    def test_refuses_the_automatic_requester(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Human-only, end to end through MCP: the automatic demotion
        policy's own identity may not reinstate anything it -- or anyone
        else -- demoted."""
        node_id = self._demoted_precedent(temp_registry)

        raw = execute_mutation(
            operation="precedent.reinstate",
            args={"target_id": node_id, "reason": "r"},
            actor="auto:learning_candidate_demote",
        )
        payload = json.loads(raw)

        assert payload["status"] == "rejected"
        assert "human-only" in payload["message"]
        graph = temp_registry.knowledge.graph_store
        node = graph.get_node(node_id)
        assert node["properties"]["lifecycle"]["state"] == "demoted"

    def test_refuses_a_missing_target(self, temp_registry: StoreRegistry) -> None:
        raw = execute_mutation(
            operation="precedent.reinstate",
            args={"target_id": "ghost", "reason": "r"},
        )
        payload = json.loads(raw)

        assert payload["status"] == "rejected"
        assert "Node not found: ghost" in payload["message"]

    def test_mutation_executed_event_carries_the_target_as_entity_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Same audit-trail guarantee as demote's own version of this
        test, for the reversal verb."""
        node_id = self._demoted_precedent(temp_registry)

        raw = execute_mutation(
            operation="precedent.reinstate",
            args={"target_id": node_id, "reason": "demotion was a mistake"},
        )
        payload = json.loads(raw)
        assert payload["status"] == "success"

        events = [
            e
            for e in temp_registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED
            )
            if e.payload.get("operation") == "precedent.reinstate"
        ]
        assert len(events) == 1
        assert events[0].entity_id == node_id


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
        ``ConfigError``, whose text names the file and the fix."""
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


class TestScheduleAdminOperationsAreNotMcpReachable:
    """``schedule.update``/``job.run_request`` resolve as real operations

    (``_resolve_operation`` finds them in the ``Operation`` enum) but have
    no handler in ``build_curate_executor`` — only
    ``trellis.mutate.build_schedule_executor`` wires them, and only the
    admin REST route and ``trellis admin schedule`` build an executor from
    that function. A mutate-scoped MCP credential must not be able to
    edit a job's schedule or request a run, so this is the refusal, not
    an oversight (see ``trellis.mutate.schedule_handlers``'s module
    docstring).
    """

    @pytest.mark.parametrize("operation", ["schedule.update", "job.run_request"])
    def test_no_handler_registered_is_a_plain_failed_result(
        self, temp_registry: StoreRegistry, operation: str
    ) -> None:
        payload = json.loads(execute_mutation(operation=operation, args={}))

        assert payload["status"] == "failed"
        assert "no handler registered" in payload["message"].lower()
        assert operation in payload["message"].lower()
        # Not an McpError: a structured outcome, exactly like any other
        # unhandled operation reaching this executor.

    @pytest.mark.parametrize("operation", ["schedule.update", "job.run_request"])
    def test_screaming_snake_form_is_refused_identically(
        self, temp_registry: StoreRegistry, operation: str
    ) -> None:
        payload = json.loads(
            execute_mutation(operation=operation.upper().replace(".", "_"), args={})
        )

        assert payload["status"] == "failed"
        assert "no handler registered" in payload["message"].lower()


# ---------------------------------------------------------------------------
# settings.update is unreachable here
# ---------------------------------------------------------------------------


class TestSettingsUpdateIsUnreachable:
    """``execute_mutation`` builds :func:`build_curate_executor`
    (see the import above and the module docstring's "same five-stage
    pipeline" line), which no longer registers
    ``Operation.SETTINGS_UPDATE`` at all. Only the admin-scoped REST route
    and ``trellis admin settings`` build the narrow
    :func:`~trellis.mutate.build_settings_executor` that does -- this MCP
    tool, being a generic surface, must not have a settings write path.

    ``status`` stays ``"failed"`` -- nothing was evaluated and no
    ``MUTATION_REJECTED`` event exists to name a reason, so relabelling
    it ``"rejected"`` would claim an audit trail that was not written
    (:func:`~trellis.mutate.redirect_unreachable_settings_update`'s own
    docstring). Only the message changes, to name where the write *is*
    reachable instead of a bare "no handler registered".
    """

    def test_settings_update_is_refused_as_no_handler_registered(
        self, temp_registry: StoreRegistry
    ) -> None:
        raw = execute_mutation(
            operation="settings.update",
            args={"name": "pack_holdout_rate", "value": 0.9},
        )
        payload = json.loads(raw)

        assert payload["status"] == "failed"
        assert "No handler registered" in payload["message"]
        assert "PUT" in payload["message"]
        assert "/api/v1/settings/{name}" in payload["message"]
        assert "trellis admin settings set" in payload["message"]

        assert temp_registry.stores_dir is not None
        path = resolve_settings_path(temp_registry.stores_dir)
        if path is not None and path.exists():
            assert SettingsStore(path).get("pack_holdout_rate") is None
