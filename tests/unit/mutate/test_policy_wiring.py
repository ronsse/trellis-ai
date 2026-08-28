"""Tests for Stage 2 wiring — policy source, and the transparency guarantee.

The single most important property here is
:class:`TestDefaultPostureIsTransparent`: wiring a gate must not change
behaviour for any deployment that has not declared a policy. Those tests
are written to fail loudly if the empty gate ever stops being a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trellis.errors import ConfigError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.mutate.policy_source import (
    POLICY_FILENAME,
    build_policy_gate,
    load_policies,
    resolve_policy_path,
)
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType


class _RecordingEventLog:
    """Minimal EventLog capturing emitted events for comparison."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(
        self,
        event_type: EventType,
        actor: str,
        *,
        entity_id: str | None = None,
        entity_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "payload": dict(payload or {}),
            }
        )

    def has_idempotency_key(self, key: str) -> bool:
        return False


class _EchoHandler:
    """Handler that succeeds and reports a stable id."""

    def handle(self, command: Command) -> tuple[str | None, str]:
        return "created-1", "ok"


def _cmd(
    op: Operation = Operation.ENTITY_CREATE,
    *,
    target_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Command:
    return Command(
        operation=op,
        args={"entity_type": "service", "name": "auth"},
        target_type=target_type,
        metadata=metadata or {},
    )


def _policy(
    *,
    level: str = "global",
    value: str | None = None,
    rules: list[PolicyRule] | None = None,
    enforcement: Enforcement = Enforcement.ENFORCE,
) -> Policy:
    return Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level=level, value=value),  # type: ignore[arg-type]
        rules=rules or [],
        enforcement=enforcement,
    )


def _write_policy_file(stores_dir: Path, policies: list[Policy]) -> Path:
    stores_dir.mkdir(parents=True, exist_ok=True)
    path = stores_dir / POLICY_FILENAME
    path.write_text(
        json.dumps({"policies": [p.model_dump(mode="json") for p in policies]}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------------


class TestDefaultPostureIsTransparent:
    """An empty gate must behave exactly like no gate at all.

    If any of these fail, wiring the gate has changed production
    behaviour for deployments that never asked for governance — the one
    outcome this change must not have.
    """

    @pytest.mark.parametrize(
        ("operation", "args"),
        [
            (Operation.ENTITY_CREATE, {"entity_type": "service", "name": "auth"}),
            (Operation.TRACE_INGEST, {"trace": {}}),
            (
                Operation.PRECEDENT_PROMOTE,
                {"title": "t", "description": "d"},
            ),
            (
                Operation.EVIDENCE_ATTACH,
                {
                    "evidence_id": "e1",
                    "target_id": "n1",
                    "target_type": "entity",
                },
            ),
        ],
    )
    def test_result_identical_with_and_without_gate(
        self, operation: Operation, args: dict[str, Any]
    ) -> None:
        """Same status, message, warnings — gate or no gate.

        Args are deliberately *valid* so the command survives Stage 1 and
        actually traverses Stage 2. An invalid command short-circuits at
        validation and would pass this test without ever reaching the gate,
        which is exactly the kind of test that looks like coverage and is not.
        """
        command = Command(operation=operation, args=args)

        ungated = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={operation: _EchoHandler()},
        )
        gated = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={operation: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[]),
        )

        a = ungated.execute(command)
        b = gated.execute(command)

        # Reaching SUCCESS proves Stage 2 was traversed, not skipped.
        assert a.status == b.status == CommandStatus.SUCCESS
        assert a.message == b.message
        assert a.warnings == b.warnings == []
        assert a.created_id == b.created_id

    def test_emitted_events_identical_with_and_without_gate(self) -> None:
        """The audit payload must not gain a key just because a gate exists.

        ``policy_warnings`` is added only when a policy actually fired,
        so a policy-free deployment's events stay byte-identical to the
        pre-wiring world.
        """
        ungated_log = _RecordingEventLog()
        gated_log = _RecordingEventLog()

        ungated = MutationExecutor(
            event_log=ungated_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
        )
        gated = MutationExecutor(
            event_log=gated_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[]),
        )

        command = _cmd()
        ungated.execute(command)
        gated.execute(command)

        assert len(ungated_log.events) == len(gated_log.events) == 1
        # command_id is shared (same Command object), so payloads must match
        # exactly — including the absence of ``policy_warnings``.
        assert ungated_log.events[0] == gated_log.events[0]
        assert "policy_warnings" not in gated_log.events[0]["payload"]

    def test_rejection_still_happens_at_stage_1_with_empty_gate(self) -> None:
        """An empty gate must not mask validation failures."""
        gated = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={},
            policy_gate=DefaultPolicyGate(policies=[]),
        )
        # entity.create requires args the registry validates.
        result = gated.execute(Command(operation=Operation.ENTITY_CREATE, args={}))
        assert result.status == CommandStatus.FAILED

    def test_build_curate_executor_wires_a_gate(self, tmp_path: Path) -> None:
        """The wiring itself: the factory must attach a gate, not None."""
        from trellis.stores.registry import StoreRegistry

        registry = StoreRegistry(
            config={"event_log": {"backend": "null"}},
            stores_dir=tmp_path / "stores",
        )
        executor = build_curate_executor(registry)
        assert executor._policy_gate is not None
        assert isinstance(executor._policy_gate, DefaultPolicyGate)

    def test_build_curate_executor_gate_is_empty_by_default(
        self, tmp_path: Path
    ) -> None:
        """No policy file means no policies. Trellis ships none."""
        from trellis.stores.registry import StoreRegistry

        registry = StoreRegistry(
            config={"event_log": {"backend": "null"}},
            stores_dir=tmp_path / "stores",
        )
        gate = build_curate_executor(registry)._policy_gate
        assert isinstance(gate, DefaultPolicyGate)
        assert gate._policies == []
        # And it allows.
        allowed, message, warnings = gate.check(_cmd())
        assert (allowed, message, warnings) == (True, "", [])


# ---------------------------------------------------------------------------
# Policy source: resolution + loading
# ---------------------------------------------------------------------------


class TestResolvePolicyPath:
    def test_none_stores_dir_returns_none(self) -> None:
        assert resolve_policy_path(None) is None

    def test_canonical_path_under_stores_dir(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        assert resolve_policy_path(stores_dir) == stores_dir / POLICY_FILENAME

    def test_legacy_path_is_honoured_when_canonical_absent(
        self, tmp_path: Path
    ) -> None:
        """A file written by the pre-unification CLI must not be ignored."""
        data_dir = tmp_path
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)
        legacy = data_dir / POLICY_FILENAME
        legacy.write_text('{"policies": []}', encoding="utf-8")

        assert resolve_policy_path(stores_dir) == legacy

    def test_canonical_wins_when_both_exist(self, tmp_path: Path) -> None:
        data_dir = tmp_path
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)
        (data_dir / POLICY_FILENAME).write_text('{"policies": []}', encoding="utf-8")
        canonical = stores_dir / POLICY_FILENAME
        canonical.write_text('{"policies": []}', encoding="utf-8")

        assert resolve_policy_path(stores_dir) == canonical


class TestLoadPolicies:
    def test_no_file_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert load_policies(tmp_path / "stores") == []

    def test_none_stores_dir_is_empty(self) -> None:
        assert load_policies(None) == []

    def test_round_trips_a_written_policy(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        _write_policy_file(stores_dir, [policy])

        loaded = load_policies(stores_dir)
        assert len(loaded) == 1
        assert loaded[0].policy_id == policy.policy_id
        assert loaded[0].rules[0].action == "deny"

    def test_round_trips_through_the_crud_store(self, tmp_path: Path) -> None:
        """What ``trellis policy add`` writes is what the gate loads.

        This is the property the shared resolver exists to guarantee —
        it would have failed before unification, because the CLI wrote
        one path and enforcement would have read another.
        """
        from trellis.stores.policy_store import PolicyStore

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])

        store = PolicyStore(resolve_policy_path(stores_dir))
        store.add(policy)

        loaded = load_policies(stores_dir)
        assert [p.policy_id for p in loaded] == [policy.policy_id]

    def test_malformed_json_raises_rather_than_degrading(
        self, tmp_path: Path
    ) -> None:
        """Fail closed: a corrupt policy file must not silently mean 'allow all'."""
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text("{not json", encoding="utf-8")

        with pytest.raises(ConfigError) as exc_info:
            load_policies(stores_dir)
        assert POLICY_FILENAME in str(exc_info.value.setting)

    def test_wrong_top_level_shape_raises(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text('["a", "b"]', encoding="utf-8")

        with pytest.raises(ConfigError):
            load_policies(stores_dir)

    def test_invalid_policy_entry_raises_and_names_the_index(
        self, tmp_path: Path
    ) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        good = _policy(rules=[PolicyRule(operation="*", action="deny")])
        (stores_dir / POLICY_FILENAME).write_text(
            json.dumps(
                {
                    "policies": [
                        good.model_dump(mode="json"),
                        {"policy_type": "not_a_real_type"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ConfigError, match="index 1"):
            load_policies(stores_dir)

    def test_empty_policies_list_is_not_an_error(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text('{"policies": []}', encoding="utf-8")
        assert load_policies(stores_dir) == []


class TestBuildPolicyGate:
    def test_gate_carries_declared_policies(self, tmp_path: Path) -> None:
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        _write_policy_file(stores_dir, [policy])

        registry = StoreRegistry(config={}, stores_dir=stores_dir)
        gate = build_policy_gate(registry)

        allowed, message, _ = gate.check(_cmd())
        assert allowed is False
        assert "Denied by policy" in message

    def test_gate_is_empty_without_a_registry_stores_dir(self) -> None:
        from trellis.stores.registry import StoreRegistry

        gate = build_policy_gate(StoreRegistry(config={}))
        assert gate.check(_cmd()) == (True, "", [])


# ---------------------------------------------------------------------------
# End-to-end: a denied command is REJECTED *and* observable
# ---------------------------------------------------------------------------


class TestDeniedCommandIsLoud:
    def test_denied_command_rejects_and_emits_mutation_rejected(self) -> None:
        event_log = _RecordingEventLog()
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.REJECTED
        assert "Denied by policy" in result.message

        assert len(event_log.events) == 1
        event = event_log.events[0]
        assert event["event_type"] == EventType.MUTATION_REJECTED
        assert event["payload"]["reason"] == "policy_violation"
        assert event["actor"] == "mutation_executor"

    def test_denied_command_does_not_reach_the_handler(self) -> None:
        """Stage 2 must block before Stage 4 — no partial write."""

        class _ExplodingHandler:
            def handle(self, command: Command) -> tuple[str | None, str]:
                msg = "handler must not be reached"
                raise AssertionError(msg)

        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        executor = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={Operation.ENTITY_CREATE: _ExplodingHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        assert executor.execute(_cmd()).status == CommandStatus.REJECTED

    def test_rejection_reason_matches_the_capture_health_shape(self) -> None:
        """``MUTATION_REJECTED`` is what ``analyze health`` counts.

        The capture-health banner aggregates executor ``MUTATION_REJECTED``
        events; a policy rejection must land in that same channel rather
        than a private one, or a policy that blocks every write would look
        like silence instead of an outage.
        """
        event_log = _RecordingEventLog()
        policy = _policy(rules=[PolicyRule(operation="*", action="deny")])
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        executor.execute(_cmd())

        payload = event_log.events[0]["payload"]
        assert payload["status"] == CommandStatus.REJECTED
        assert payload["reason"] == "policy_violation"
        assert "operation" in payload
        assert "requested_by" in payload


# ---------------------------------------------------------------------------
# Warn enforcement is observable (it was not, before)
# ---------------------------------------------------------------------------


class TestWarningsReachTheCaller:
    def test_warn_enforcement_surfaces_on_a_successful_result(self) -> None:
        """Regression: warnings used to be computed and then dropped."""
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.WARN,
        )
        executor = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert len(result.warnings) == 1
        assert policy.policy_id in result.warnings[0]

    def test_warn_enforcement_is_recorded_on_the_audit_event(self) -> None:
        event_log = _RecordingEventLog()
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.WARN,
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        executor.execute(_cmd())

        payload = event_log.events[0]["payload"]
        assert payload["status"] == CommandStatus.SUCCESS
        assert len(payload["policy_warnings"]) == 1

    def test_audit_only_stays_silent_to_the_caller(self) -> None:
        event_log = _RecordingEventLog()
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.AUDIT_ONLY,
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert result.warnings == []
        assert "policy_warnings" not in event_log.events[0]["payload"]
