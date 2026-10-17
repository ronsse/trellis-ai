"""``requested_by_claim`` on MUTATION_EXECUTED / MUTATION_REJECTED events.

``trellis_api.routes._principal.resolve_requested_by`` derives the
audited ``requested_by`` from the authenticated principal and stamps the
request body's own (overridden) claim onto
``command.metadata["requested_by_claim"]`` -- see
``tests/unit/api/test_requested_by_principal_rule.py`` and
``test_mutations_authz_followups.py``. This file proves the executor
surfaces that claim on the audit event, right beside ``requested_by``,
and that a command with no claim produces a payload byte-identical to
one from before this field existed -- the same transparency contract
``test_policy_wiring.py::TestDefaultPostureIsTransparent`` holds for
policy wiring.
"""

from __future__ import annotations

from typing import Any

from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_gate import DefaultPolicyGate
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
    def handle(self, command: Command) -> tuple[str | None, str]:
        return "created-1", "ok"


def _cmd(
    *,
    requested_by: str = "api:abc123",
    metadata: dict[str, Any] | None = None,
) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "auth"},
        requested_by=requested_by,
        metadata=metadata or {},
    )


class TestRequestedByClaimOnExecutedEvent:
    def test_a_claim_is_recorded_beside_requested_by(self) -> None:
        event_log = _RecordingEventLog()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
        )

        executor.execute(
            _cmd(
                requested_by="api:abc123",
                metadata={"requested_by_claim": "attacker-chosen-label"},
            )
        )

        (event,) = event_log.events
        assert event["event_type"] == EventType.MUTATION_EXECUTED
        assert event["payload"]["requested_by"] == "api:abc123"
        assert event["payload"]["requested_by_claim"] == "attacker-chosen-label"

    def test_no_claim_leaves_the_payload_byte_identical(self) -> None:
        """The key the brief requires absent, is absent.

        Same command shape minus the claim, compared key-for-key against
        a payload that never knew this field existed.
        """
        event_log = _RecordingEventLog()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
        )

        executor.execute(_cmd(requested_by="cli:link"))

        (event,) = event_log.events
        assert "requested_by_claim" not in event["payload"]
        assert event["payload"] == {
            "command_id": event["payload"]["command_id"],
            "operation": Operation.ENTITY_CREATE,
            "status": CommandStatus.SUCCESS,
            "message": "ok",
            "requested_by": "cli:link",
            "idempotency_key": None,
        }


def _deny_entity_create_policy() -> Policy:
    return Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global", value=None),  # type: ignore[arg-type]
        rules=[PolicyRule(operation="entity.create", action="deny")],
        enforcement=Enforcement.ENFORCE,
    )


class TestRequestedByClaimOnRejectedEvent:
    def test_a_claim_is_recorded_on_a_policy_rejection(self) -> None:
        event_log = _RecordingEventLog()
        policy = _deny_entity_create_policy()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(
            _cmd(
                requested_by="api:abc123",
                metadata={"requested_by_claim": "auto:learning_candidate"},
            )
        )

        assert result.status == CommandStatus.REJECTED
        (event,) = event_log.events
        assert event["event_type"] == EventType.MUTATION_REJECTED
        assert event["payload"]["requested_by"] == "api:abc123"
        assert event["payload"]["requested_by_claim"] == "auto:learning_candidate"

    def test_no_claim_leaves_a_rejected_payload_byte_identical(self) -> None:
        event_log = _RecordingEventLog()
        policy = _deny_entity_create_policy()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        executor.execute(_cmd(requested_by="cli:link"))

        (event,) = event_log.events
        assert "requested_by_claim" not in event["payload"]
