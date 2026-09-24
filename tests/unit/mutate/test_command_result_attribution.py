"""Every ``CommandResult`` names the ``Command`` it answers.

Under batch execution (``SEQUENTIAL`` / ``CONTINUE_ON_ERROR``)
``CommandResult.command_id`` is the only key attributing a result to the
command that was submitted, and every surface hands it back to its caller.
``MutationExecutor`` builds a ``CommandResult`` by hand at twelve sites --
one per pipeline outcome -- each copying ``command_id=command.command_id``,
and until this module no assertion anywhere in ``tests/`` compared a
result's id to its command's. The existing checks were truthiness
(``assert data["command_id"]``, satisfied by any non-empty string) or
compared two event payloads emitted from one command, so folding all twelve
copies to one constant survived the full default selection
(``docs/agent-guide/testing.md`` § *Fields copied everywhere and asserted
nowhere*).

The twelve sites, read top to bottom through ``execute`` and
``_check_idempotency``, and the test that pins each:

=====================================  ==========  ======================================
outcome                                status      test
=====================================  ==========  ======================================
unattended-writer roster refusal       REJECTED    ``test_immutable_core_refusal``
registry arg validation                FAILED      ``test_arg_validation_failure``
policy gate: deny / require_approval   REJECTED    ``test_policy_gate_rejection``
Stage 3 replay, in-memory key cache    DUPLICATE   ``test_in_memory_replay``
Stage 3 replay, persisted event log    DUPLICATE   ``test_persisted_replay``
no handler registered                  FAILED      ``test_no_handler``
handler raises ``ValidationError``     REJECTED    ``test_handler_validation_error``
handler raises ``PolicyViolationError``  REJECTED  ``test_handler_policy_violation``
handler raises ``IdempotencyError``    DUPLICATE   ``test_handler_idempotency_error``
handler raises ``StoreError`` /        FAILED      ``test_handler_typed_error``
``TrellisError``
handler raises an untyped panic        FAILED      ``test_handler_panic``
handler returns                        SUCCESS     ``test_success``
=====================================  ==========  ======================================

Status alone does not identify a site (four are REJECTED, four FAILED),
so every test also asserts a fragment of the message only that site
writes. That is what makes the table above a per-site map rather than a
per-status one: a test cannot pass by reaching a neighbouring branch.

Three properties every test holds, and why:

* **Population > 1 with distinct ids.** Each batch carries at least two
  commands with explicit, distinct ``command_id``\\ s, and the helper
  refuses a batch whose ids collide. With one command -- or two sharing an
  id -- ``result.command_id == command.command_id`` is satisfiable by a
  constant that happens to equal the fixture's value (the #447 shape).
* **Results are compared positionally and strictly.** ``zip(strict=True)``
  plus a length check, so a result list that is short, long or reordered
  fails rather than being silently truncated.
* **Real execution, no mocked seam.** A real ``MutationExecutor``, a real
  ``SQLiteEventLog`` and a real ``DefaultPolicyGate``; handlers are the only
  test doubles, and they are the injection point the executor declares.

The replay tests give the replaying command a *different* ``command_id``
from the one that recorded the key. A duplicate is the one outcome where a
second, earlier command is semantically in reach, so an implementation that
answered with the original command's id is the plausible wrong-object copy;
asserting the replay's own id, and that it differs from the original, is
what rules it out.

Not a site, stated so it is not mistaken for a gap: an audit-emit failure
appends a warning to whichever result the branch builds and constructs no
``CommandResult`` of its own; and ``execute_batch`` under ``STOP_ON_ERROR``
synthesises no result for the commands it does not run -- it returns the
prefix it executed, each built by one of the twelve sites above.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from trellis.errors import (
    IdempotencyError,
    PolicyViolationError,
    StoreError,
    TrellisError,
    ValidationError,
)
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
from trellis.stores.sqlite.event_log import SQLiteEventLog

# Two operations the gate blocks, by different actions, so the policy test
# reaches its site through both blocking verdicts.
_DENIED_OP = Operation.LINK_REMOVE
_APPROVAL_OP = Operation.LABEL_REMOVE


class _Succeeds:
    """Returns a created id derived from the command, so no two collide."""

    def handle(self, command: Command) -> tuple[str | None, str]:
        return f"created-for-{command.args['name']}", "ok"


class _RaisesByName:
    """Raises the exception registered under the command's ``name`` arg.

    Keyed per command so one batch can drive two *different* exceptions
    through the same catch clause -- a non-uniform population for the site
    under test.
    """

    def __init__(self, raises: dict[str, BaseException]) -> None:
        self._raises = raises

    def handle(self, command: Command) -> tuple[str | None, str]:
        raise self._raises[command.args["name"]]


def _gate() -> DefaultPolicyGate:
    return DefaultPolicyGate(
        [
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="global"),
                rules=[
                    PolicyRule(
                        operation=str(_DENIED_OP), condition="frozen", action="deny"
                    ),
                    PolicyRule(
                        operation=str(_APPROVAL_OP),
                        condition="reviewed",
                        action="require_approval",
                    ),
                ],
                enforcement=Enforcement.ENFORCE,
            )
        ]
    )


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _executor(
    event_log: SQLiteEventLog,
    handler: CommandHandler | None = None,
) -> MutationExecutor:
    handlers: dict[str, CommandHandler] = {}
    if handler is not None:
        handlers[Operation.ENTITY_CREATE] = handler
    return MutationExecutor(
        policy_gate=_gate(),
        event_log=event_log,
        handlers=handlers,
    )


def _create(command_id: str, name: str, **kwargs: object) -> Command:
    return Command(
        command_id=command_id,
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": name},
        **kwargs,  # type: ignore[arg-type]
    )


def _run(executor: MutationExecutor, commands: list[Command]) -> list[CommandResult]:
    return executor.execute_batch(
        CommandBatch(commands=commands, strategy=BatchStrategy.SEQUENTIAL)
    )


def _assert_attributed(
    commands: list[Command],
    results: list[CommandResult],
    expected: list[tuple[CommandStatus, str]],
) -> None:
    """Each result carries its own command's id, and the ids are distinct.

    ``expected`` pins, per command, the status and a message fragment only
    the targeted site writes -- the per-site half of the map in the module
    docstring.
    """
    ids = [c.command_id for c in commands]
    assert len(ids) >= 2, "a population of one cannot tell a copy from a constant"
    assert len(set(ids)) == len(ids), f"fixture ids collide: {ids}"
    assert len(results) == len(commands) == len(expected)

    for command, result, (status, fragment) in zip(
        commands, results, expected, strict=True
    ):
        assert result.status == status, (result.status, result.message)
        assert fragment in result.message, result.message
        assert result.command_id == command.command_id, (
            f"result for {command.command_id!r} is attributed to "
            f"{result.command_id!r}"
        )

    returned = [r.command_id for r in results]
    assert len(set(returned)) == len(returned), returned


def test_immutable_core_refusal(event_log: SQLiteEventLog) -> None:
    commands = [
        _create("cid-roster-a", "a", requested_by="worker:embed-traces"),
        Command(
            command_id="cid-roster-b",
            operation=Operation.LINK_REMOVE,
            args={"edge_id": "e-1"},
            requested_by="worker:session-capture",
        ),
    ]
    results = _run(_executor(event_log, _Succeeds()), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.REJECTED, "unattended_writer_operation_not_allowed"),
            (CommandStatus.REJECTED, "unattended_writer_operation_not_allowed"),
        ],
    )


def test_arg_validation_failure(event_log: SQLiteEventLog) -> None:
    commands = [
        Command(
            command_id="cid-validate-a",
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service"},
        ),
        Command(
            command_id="cid-validate-b",
            operation=Operation.LINK_CREATE,
            args={"source_id": "s", "target_id": "t"},
        ),
    ]
    results = _run(_executor(event_log, _Succeeds()), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.FAILED, "Validation failed: Missing required args: name"),
            (
                CommandStatus.FAILED,
                "Validation failed: Missing required args: edge_kind",
            ),
        ],
    )


def test_policy_gate_rejection(event_log: SQLiteEventLog) -> None:
    commands = [
        Command(
            command_id="cid-policy-deny",
            operation=_DENIED_OP,
            args={"edge_id": "e-1"},
        ),
        Command(
            command_id="cid-policy-approval",
            operation=_APPROVAL_OP,
            args={"target_id": "t-1", "label": "x"},
        ),
    ]
    results = _run(_executor(event_log, _Succeeds()), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.REJECTED, "Denied by policy: frozen"),
            (CommandStatus.REJECTED, "Approval required: reviewed"),
        ],
    )


def test_in_memory_replay(event_log: SQLiteEventLog) -> None:
    """A replay answers with the replaying command's id, not the recorder's."""
    commands = [
        _create("cid-first-a", "a", idempotency_key="key-a"),
        _create("cid-first-b", "b", idempotency_key="key-b"),
        _create("cid-replay-a", "a", idempotency_key="key-a"),
        _create("cid-replay-b", "b", idempotency_key="key-b"),
    ]
    results = _run(_executor(event_log, _Succeeds()), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.SUCCESS, "ok"),
            (CommandStatus.SUCCESS, "ok"),
            (CommandStatus.DUPLICATE, "Duplicate command: key-a"),
            (CommandStatus.DUPLICATE, "Duplicate command: key-b"),
        ],
    )


def test_persisted_replay(event_log: SQLiteEventLog) -> None:
    """The cross-restart branch: a fresh executor, the same event log."""
    recorders = [
        _create("cid-recorded-a", "a", idempotency_key="key-a"),
        _create("cid-recorded-b", "b", idempotency_key="key-b"),
    ]
    first = _run(_executor(event_log, _Succeeds()), recorders)
    assert [r.status for r in first] == [CommandStatus.SUCCESS] * 2

    replays = [
        _create("cid-restarted-a", "a", idempotency_key="key-a"),
        _create("cid-restarted-b", "b", idempotency_key="key-b"),
    ]
    results = _run(_executor(event_log, _Succeeds()), replays)
    _assert_attributed(
        replays,
        results,
        [
            (CommandStatus.DUPLICATE, "Duplicate command (persisted): key-a"),
            (CommandStatus.DUPLICATE, "Duplicate command (persisted): key-b"),
        ],
    )
    recorded = {c.command_id for c in recorders}
    assert recorded.isdisjoint(r.command_id for r in results)


def test_no_handler(event_log: SQLiteEventLog) -> None:
    commands = [
        Command(
            command_id="cid-unhandled-a",
            operation=Operation.ENTITY_UPDATE,
            args={"entity_id": "n-1"},
        ),
        Command(
            command_id="cid-unhandled-b",
            operation=Operation.LABEL_ADD,
            args={"target_id": "t-1", "label": "x"},
        ),
    ]
    results = _run(_executor(event_log, _Succeeds()), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.FAILED, "No handler registered for: entity.update"),
            (CommandStatus.FAILED, "No handler registered for: label.add"),
        ],
    )


def test_handler_validation_error(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": ValidationError("orphan edge", code="orphan_edge"),
            "b": ValidationError("bad shape"),
        }
    )
    commands = [_create("cid-hvalid-a", "a"), _create("cid-hvalid-b", "b")]
    results = _run(_executor(event_log, handler), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.REJECTED, "orphan edge"),
            (CommandStatus.REJECTED, "bad shape"),
        ],
    )


def test_handler_policy_violation(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": PolicyViolationError("row-level deny a", policy_id="pol-a"),
            "b": PolicyViolationError("row-level deny b", policy_id="pol-b"),
        }
    )
    commands = [_create("cid-hpolicy-a", "a"), _create("cid-hpolicy-b", "b")]
    results = _run(_executor(event_log, handler), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.REJECTED, "row-level deny a"),
            (CommandStatus.REJECTED, "row-level deny b"),
        ],
    )


def test_handler_idempotency_error(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": IdempotencyError(idempotency_key="store-key-a"),
            "b": IdempotencyError(idempotency_key="store-key-b"),
        }
    )
    commands = [_create("cid-hidem-a", "a"), _create("cid-hidem-b", "b")]
    results = _run(_executor(event_log, handler), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.DUPLICATE, "Duplicate command: store-key-a"),
            (CommandStatus.DUPLICATE, "Duplicate command: store-key-b"),
        ],
    )


def test_handler_typed_error(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": StoreError("graph write lost", store="graph"),
            "b": TrellisError("generic trellis failure"),
        }
    )
    commands = [_create("cid-htyped-a", "a"), _create("cid-htyped-b", "b")]
    results = _run(_executor(event_log, handler), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.FAILED, "Execution failed: graph write lost"),
            (CommandStatus.FAILED, "Execution failed: generic trellis failure"),
        ],
    )


def test_handler_panic(event_log: SQLiteEventLog) -> None:
    handler = _RaisesByName(
        {
            "a": RuntimeError("backend panic"),
            "b": KeyError("missing-key"),
        }
    )
    commands = [_create("cid-hpanic-a", "a"), _create("cid-hpanic-b", "b")]
    results = _run(_executor(event_log, handler), commands)
    _assert_attributed(
        commands,
        results,
        [
            (CommandStatus.FAILED, "Execution failed: backend panic"),
            (CommandStatus.FAILED, "Execution failed: 'missing-key'"),
        ],
    )


def test_success(event_log: SQLiteEventLog) -> None:
    commands = [_create("cid-ok-a", "a"), _create("cid-ok-b", "b")]
    results = _run(_executor(event_log, _Succeeds()), commands)
    _assert_attributed(
        commands,
        results,
        [(CommandStatus.SUCCESS, "ok"), (CommandStatus.SUCCESS, "ok")],
    )
    assert [r.created_id for r in results] == ["created-for-a", "created-for-b"]
