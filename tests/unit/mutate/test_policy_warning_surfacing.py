"""Every outcome after Stage 2 carries the gate's warnings.

``Enforcement.WARN`` — and an ``action="warn"`` rule under any
enforcement — means "allow, but say so". The saying-so had exactly one
channel, ``CommandResult.warnings``, which 34 of the 35
``executor.execute`` call sites in ``src/`` discard; and the gate
accumulates warnings *before* it reaches the rule that blocks, so a
``warn`` firing ahead of a ``deny`` was lost on the one path where the
write did not happen.

The invariant pinned here is deliberately uniform: once Stage 2 has run,
**no** outcome drops the warnings — not a rejection, not a duplicate, not
a handler failure. Forwarding them only on the paths that seemed to
matter is how this was lost the first time, so the tests enumerate every
reachable outcome rather than the interesting ones. Stage 1 is the single
exception, because it runs before the gate.

Latent on a shipped deployment, like #461: the gate is the only producer
of a warning and Trellis ships zero policies, so none of this is
reachable until an operator declares one. ``TestTransparencyIsPreserved``
is what keeps that true — an empty gate must still emit a byte-identical
payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.errors import (
    IdempotencyError,
    PolicyViolationError,
    StoreError,
    ValidationError,
)
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType

#: The exact string ``DefaultPolicyGate`` builds for the policy below. Pinned
#: verbatim so a test cannot pass against a constant or a truncated message.
WARNING_TEXT = "Policy warning (pol-warn): unusual write"


class _RecordingEventLog:
    """EventLog capturing emitted events, with a settable idempotency answer."""

    def __init__(self, *, known_key: str | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._known_key = known_key

    def emit(
        self,
        event_type: EventType,
        actor: str,
        *,
        entity_id: str | None = None,
        entity_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append({"event_type": event_type, "payload": dict(payload or {})})

    def has_idempotency_key(self, key: str) -> bool:
        return self._known_key is not None and key == self._known_key


class _Handler:
    """Handler that succeeds, or raises whatever it was given."""

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises

    def handle(self, command: Command) -> tuple[str | None, str]:
        if self._raises is not None:
            raise self._raises
        return "created-1", "ok"


def _policy(*, deny: bool) -> Policy:
    """A warn rule, optionally followed by a deny rule in the same policy.

    Order matters: the gate returns on the first blocking rule, so the warn
    has to precede the deny for the accumulated-then-blocked case to arise
    at all.
    """
    rules = [PolicyRule(operation="*", condition="unusual write", action="warn")]
    if deny:
        rules.append(
            PolicyRule(operation="*", condition="not permitted", action="deny")
        )
    return Policy(
        policy_id="pol-warn",
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=rules,
        enforcement=Enforcement.ENFORCE,
    )


def _cmd(**kwargs: Any) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "auth"},
        **kwargs,
    )


#: ``(outcome id, expected status, emits an event)``. One row per branch of
#: :meth:`MutationExecutor.execute` reachable *after* the policy gate has run.
OUTCOMES: list[tuple[str, CommandStatus, bool]] = [
    ("success", CommandStatus.SUCCESS, True),
    ("policy_deny", CommandStatus.REJECTED, True),
    ("duplicate_in_memory", CommandStatus.DUPLICATE, True),
    ("duplicate_persisted", CommandStatus.DUPLICATE, True),
    # The only post-gate branch that emits nothing at all -- a pre-existing
    # gap in the audit trail, out of scope here. The result still carries the
    # warnings, which is the half this module is about.
    ("no_handler", CommandStatus.FAILED, False),
    ("handler_validation_error", CommandStatus.REJECTED, True),
    ("handler_policy_violation", CommandStatus.REJECTED, True),
    ("handler_idempotency_error", CommandStatus.DUPLICATE, True),
    ("handler_store_error", CommandStatus.FAILED, True),
]


def _run(outcome: str, *, gate: DefaultPolicyGate | None) -> tuple[Any, list[dict]]:
    """Drive ``execute`` to one named outcome; return its result and events."""
    log = _RecordingEventLog(
        known_key="seen-elsewhere" if outcome == "duplicate_persisted" else None
    )
    raises: Exception | None = {
        "handler_validation_error": ValidationError("bad", code="orphan_edge"),
        "handler_policy_violation": PolicyViolationError("nope", policy_id="p9"),
        "handler_idempotency_error": IdempotencyError(idempotency_key="k9"),
        "handler_store_error": StoreError("backend down", store="graph"),
    }.get(outcome)
    handlers = (
        {} if outcome == "no_handler" else {Operation.ENTITY_CREATE: _Handler(raises)}
    )
    executor = MutationExecutor(
        policy_gate=gate,
        event_log=log,
        handlers=handlers,  # type: ignore[arg-type]
    )

    if outcome == "duplicate_in_memory":
        executor.execute(_cmd(idempotency_key="k1"))
        log.events.clear()
        result = executor.execute(_cmd(idempotency_key="k1"))
    elif outcome == "duplicate_persisted":
        result = executor.execute(_cmd(idempotency_key="seen-elsewhere"))
    else:
        result = executor.execute(_cmd())
    return result, log.events


class TestEveryPostGateOutcomeCarriesWarnings:
    """The uniform rule, enumerated rather than sampled."""

    @pytest.mark.parametrize(
        ("outcome", "status", "emits"), OUTCOMES, ids=[o[0] for o in OUTCOMES]
    )
    def test_result_and_event_carry_the_warning(
        self, outcome: str, status: CommandStatus, emits: bool
    ) -> None:
        gate = DefaultPolicyGate([_policy(deny=outcome == "policy_deny")])
        result, events = _run(outcome, gate=gate)

        assert result.status == status
        assert result.warnings == [WARNING_TEXT]

        if not emits:
            assert events == []
            return
        assert events, f"{outcome} emitted no event"
        assert events[-1]["payload"]["policy_warnings"] == [WARNING_TEXT]

    def test_warning_survives_the_rule_that_blocks(self) -> None:
        """The case the one-line forward was for: warn, then deny.

        The gate returns ``(False, message, warnings)`` — the warnings
        accumulated before the blocking rule ride the rejection, and are the
        only record that a second policy also had something to say about a
        write that never happened.
        """
        gate = DefaultPolicyGate([_policy(deny=True)])
        result, events = _run("policy_deny", gate=gate)

        assert result.status == CommandStatus.REJECTED
        assert "not permitted" in result.message
        assert result.warnings == [WARNING_TEXT]
        payload = events[-1]["payload"]
        assert payload["reason"] == "policy_violation"
        assert payload["policy_warnings"] == [WARNING_TEXT]


class TestStage1IsTheOneException:
    def test_validation_failure_carries_nothing(self) -> None:
        """Stage 1 runs before the gate, so there is nothing to forward.

        Asserting the *absence* of the key matters as much as the presence
        elsewhere: it is what makes ``policy_warnings`` mean "the gate saw
        this command", rather than "someone remembered to pass a kwarg".
        """
        log = _RecordingEventLog()
        executor = MutationExecutor(
            policy_gate=DefaultPolicyGate([_policy(deny=False)]),
            event_log=log,
            handlers={Operation.ENTITY_CREATE: _Handler()},  # type: ignore[arg-type]
        )
        result = executor.execute(Command(operation=Operation.ENTITY_CREATE, args={}))

        assert result.status == CommandStatus.FAILED
        assert result.warnings == []
        assert "policy_warnings" not in log.events[-1]["payload"]
        assert log.events[-1]["payload"]["reason"] == "validate"


class TestTransparencyIsPreserved:
    """A deployment with no policies must emit the pre-gate payload, exactly.

    The same property ``test_policy_wiring.TestDefaultPostureIsTransparent``
    pins for the allow path, extended to every outcome the forwarding now
    touches — because a kwarg threaded through nine call sites is nine
    chances to make the key unconditional.
    """

    @pytest.mark.parametrize(
        ("outcome", "status", "emits"), OUTCOMES, ids=[o[0] for o in OUTCOMES]
    )
    @pytest.mark.parametrize("gate", [None, "empty"], ids=["no_gate", "empty_gate"])
    def test_no_policy_warnings_key(
        self, outcome: str, status: CommandStatus, emits: bool, gate: str | None
    ) -> None:
        if outcome == "policy_deny":
            pytest.skip("unreachable without a policy")
        result, events = _run(
            outcome, gate=DefaultPolicyGate([]) if gate == "empty" else None
        )

        assert result.status == status
        assert result.warnings == []
        for event in events:
            assert "policy_warnings" not in event["payload"]
