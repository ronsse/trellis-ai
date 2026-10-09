"""A REJECTED ``CommandResult`` names why, as its ``MUTATION_REJECTED`` event does.

The CLI picks an exit code from the ``CommandResult`` alone: ``3`` for a
policy refusal and ``2`` for any other (``docs/design/adr-cli-exit-codes.md``
§3). ``MutationExecutor`` refuses at five sites, and each copies the
``reason`` its audit event carries into ``metadata["rejection_reason"]``:

* unattended-writer roster -- ``immutable_core``
* operation registry, a missing required arg -- ``validate``
* policy gate, deny and require_approval -- ``policy_violation``
* handler raises ``ValidationError`` -- its ``code``, or ``handler_validate``
  when it set none
* handler raises ``PolicyViolationError`` -- ``policy_violation``

Each test submits two commands, with different reasons where the site can
produce them, and compares each result with the event for the same command.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from trellis.errors import PolicyViolationError, ValidationError
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import CommandHandler, MutationExecutor
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

# Two operations the gate blocks, by different actions.
_DENIED_OP = Operation.LINK_REMOVE
_APPROVAL_OP = Operation.LABEL_REMOVE


class _Succeeds:
    def handle(self, command: Command) -> tuple[str | None, str]:
        return f"created-for-{command.command_id}", "ok"


class _RaisesByName:
    """Raises the exception registered under the command's ``name`` arg."""

    def __init__(self, raises: dict[str, BaseException]) -> None:
        self._raises = raises

    def handle(self, command: Command) -> tuple[str | None, str]:
        raise self._raises[command.args["name"]]


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _gate() -> DefaultPolicyGate:
    rules = [
        PolicyRule(operation=str(_DENIED_OP), condition="frozen", action="deny"),
        PolicyRule(
            operation=str(_APPROVAL_OP),
            condition="reviewed",
            action="require_approval",
        ),
    ]
    policy = Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=rules,
        enforcement=Enforcement.ENFORCE,
    )
    return DefaultPolicyGate([policy])


def _run(
    event_log: SQLiteEventLog, handler: CommandHandler, commands: list[Command]
) -> list[CommandResult]:
    executor = MutationExecutor(
        policy_gate=_gate(),
        event_log=event_log,
        handlers={Operation.ENTITY_CREATE: handler},
    )
    return executor.execute_batch(
        CommandBatch(commands=commands, strategy=BatchStrategy.SEQUENTIAL)
    )


def _create(command_id: str, name: str, **kwargs: object) -> Command:
    return Command(
        command_id=command_id,
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": name},
        **kwargs,  # type: ignore[arg-type]
    )


def _assert_reasons(
    event_log: SQLiteEventLog,
    commands: list[Command],
    results: list[CommandResult],
    expected: list[str],
) -> None:
    events = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
    audited = {e.payload["command_id"]: e.payload["reason"] for e in events}
    assert len(results) == len(commands) == len(expected)
    for command, result, reason in zip(commands, results, expected, strict=True):
        assert result.status == CommandStatus.REJECTED, result.message
        assert result.metadata.get("rejection_reason") == reason, result.metadata
        assert audited[command.command_id] == reason, audited


def test_immutable_core_refusal(event_log: SQLiteEventLog) -> None:
    commands = [
        _create("cid-roster-a", "a", requested_by="worker:embed-traces"),
        Command(
            command_id="cid-roster-b",
            operation=_DENIED_OP,
            args={"edge_id": "e-1"},
            requested_by="worker:session-capture",
        ),
    ]
    results = _run(event_log, _Succeeds(), commands)
    _assert_reasons(event_log, commands, results, ["immutable_core"] * 2)


def test_registry_validation_refusal(event_log: SQLiteEventLog) -> None:
    """A missing required arg is the caller's error: refused, not failed."""
    commands = [
        Command(
            command_id="cid-validate-a",
            operation=Operation.LINK_CREATE,
            args={"source_id": "syn-node-a"},
        ),
        Command(
            command_id="cid-validate-b",
            operation=Operation.ENTITY_CREATE,
            args={"name": "b"},
        ),
    ]
    results = _run(event_log, _Succeeds(), commands)
    _assert_reasons(event_log, commands, results, ["validate"] * 2)
    assert [r.message for r in results] == [
        "Validation failed: Missing required args: edge_kind, target_id",
        "Validation failed: Missing required args: entity_type",
    ]
    rejected = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
    assert sorted(e.payload["command_id"] for e in rejected) == [
        "cid-validate-a",
        "cid-validate-b",
    ]
    assert event_log.get_events(event_type=EventType.MUTATION_EXECUTED) == []


def test_policy_gate_refusal(event_log: SQLiteEventLog) -> None:
    commands = [
        Command(command_id="cid-deny", operation=_DENIED_OP, args={"edge_id": "e-1"}),
        Command(
            command_id="cid-approval",
            operation=_APPROVAL_OP,
            args={"target_id": "t-1", "label": "x"},
        ),
    ]
    results = _run(event_log, _Succeeds(), commands)
    _assert_reasons(event_log, commands, results, ["policy_violation"] * 2)


def test_handler_validation_error(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": ValidationError("orphan edge", code="orphan_edge"),
            "b": ValidationError("bad shape"),
        }
    )
    commands = [_create("cid-hvalid-a", "a"), _create("cid-hvalid-b", "b")]
    results = _run(event_log, handler, commands)
    _assert_reasons(event_log, commands, results, ["orphan_edge", "handler_validate"])


def test_handler_policy_violation(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": PolicyViolationError("row-level deny a", policy_id="pol-a"),
            "b": PolicyViolationError("row-level deny b", policy_id="pol-b"),
        }
    )
    commands = [_create("cid-hpolicy-a", "a"), _create("cid-hpolicy-b", "b")]
    results = _run(event_log, handler, commands)
    _assert_reasons(event_log, commands, results, ["policy_violation"] * 2)
