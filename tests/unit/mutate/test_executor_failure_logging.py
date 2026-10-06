"""What a failed handler writes to the operator log.

A typed handler failure (a ``StoreError``, or another ``TrellisError`` that
fails the command) is logged as its type, its message and the command it
belongs to, without the chained traceback. The chained cause is where a
backend's own text lives: the Postgres and Bolt purges (#702, #713) raise
``StoreError(<type-only message>) from <driver error>`` because a server's
message can carry query text and values, and a rendered traceback prints that
cause. An unexpected exception (a ``RuntimeError``, say) is logged with its
full traceback, which is how the bug behind it gets found.

An audit event that cannot be written (``audit_emit_failed``) is logged as
its type, plus its message when it is a ``TrellisError``, and never with a
traceback. An emit from inside an ``except`` block (a failed handler, a failed
idempotency read) has that failure as its context, so a rendered traceback
would print it, driver text and all, even when the emit's own error is clean.

The log is read as a deployment renders it, through the real processor chains:
``configure_stderr_logging`` (CLI and MCP server) and the API's
``configure_logging`` (JSON by default). ``structlog.testing.capture_logs`` on
its own cannot see a leaked cause: it records ``exc_info=True``, and the
traceback text only appears when a chain's ``format_exc_info`` renders it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import MagicMock, call

import pytest

from tests.structlog_isolation import reset_structlog_global_state
from trellis.errors import StoreError
from trellis.logging import configure_stderr_logging
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventType
from trellis_api.logging import _UVICORN_LOGGERS, configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from trellis.mutate.commands import CommandResult
    from trellis.mutate.executor import CommandHandler

#: Stands in for a server's error text (query fragments, values, host:port).
#: Nothing else in this module contains it, so finding it in the log means the
#: chained cause was rendered.
_SERVER_TEXT = "SYNTHETIC-SERVER-TEXT-9c41"
_COMMAND_ID = "cmd-synthetic-0001"
_TYPED_MESSAGE = "Purge of node node-synthetic-1 failed: _DriverError"
_PANIC_MESSAGE = "synthetic backend panic"

_CHAINS = [
    pytest.param(configure_stderr_logging, id="cli-mcp"),
    pytest.param(configure_logging, id="api-json"),
]


class _DriverError(Exception):
    """A backend driver's own exception, outside the Trellis hierarchy."""


def _driver_call() -> NoReturn:
    msg = f"server says: {_SERVER_TEXT}"
    raise _DriverError(msg)


class _MappedDriverFailure:
    """Raises a type-only StoreError from a driver error, as the purges do."""

    def handle(self, command: Command) -> tuple[str | None, str]:
        try:
            _driver_call()
        except _DriverError as exc:
            raise StoreError(_TYPED_MESSAGE, store="graph") from exc


class _Panic:
    """Raises an exception that no typed catch expects."""

    def handle(self, command: Command) -> tuple[str | None, str]:
        raise RuntimeError(_PANIC_MESSAGE)


@pytest.fixture(autouse=True)
def _isolate_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test a freshly configured chain, and leave none behind.

    Both configure functions write process-global state: structlog's config and
    the binds cached on every lazy proxy (the executor's module logger among
    them), and, for the API, the stdlib root and uvicorn loggers.
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "INFO")
    monkeypatch.delenv("TRELLIS_LOG_FORMAT", raising=False)
    root = logging.getLogger()
    root_state = (list(root.handlers), root.level)
    uvicorn = {name: logging.getLogger(name) for name in _UVICORN_LOGGERS}
    uvicorn_state = {
        name: (list(lg.handlers), lg.propagate, lg.level)
        for name, lg in uvicorn.items()
    }
    reset_structlog_global_state()
    try:
        yield
    finally:
        reset_structlog_global_state()
        root.handlers, level = root_state
        root.setLevel(level)
        for name, (handlers, propagate, lg_level) in uvicorn_state.items():
            uvicorn[name].handlers = handlers
            uvicorn[name].propagate = propagate
            uvicorn[name].setLevel(lg_level)


def _rendered_log(
    capsys: pytest.CaptureFixture[str],
    configure: Callable[[], None],
    handler: CommandHandler,
) -> str:
    """Everything *configure*'s chain writes to stderr while *handler* fails."""
    configure()
    MutationExecutor(
        event_log=MagicMock(), handlers={Operation.ENTITY_CREATE: handler}
    ).execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "synthetic"},
            command_id=_COMMAND_ID,
        )
    )
    return capsys.readouterr().err


@pytest.mark.parametrize("configure", _CHAINS)
def test_a_typed_failure_logs_its_message_and_not_its_cause(
    capsys: pytest.CaptureFixture[str], configure: Callable[[], None]
) -> None:
    out = _rendered_log(capsys, configure, _MappedDriverFailure())

    assert "handler_typed_error" in out
    assert "StoreError" in out
    assert _TYPED_MESSAGE in out
    assert _COMMAND_ID in out
    assert _SERVER_TEXT not in out


@pytest.mark.parametrize("configure", _CHAINS)
def test_an_unexpected_failure_keeps_its_traceback(
    capsys: pytest.CaptureFixture[str], configure: Callable[[], None]
) -> None:
    out = _rendered_log(capsys, configure, _Panic())

    assert "handler_failed_unexpected" in out
    assert "Traceback (most recent call last)" in out
    assert _PANIC_MESSAGE in out
    assert _COMMAND_ID in out


def _refuse_the_read(key: str) -> NoReturn:
    """Fail an idempotency read as an event log that maps driver errors does."""
    try:
        _driver_call()
    except _DriverError as exc:
        msg = "Event log has_idempotency_key failed: _DriverError"
        raise StoreError(msg, store="event_log") from exc


@pytest.mark.parametrize("configure", _CHAINS)
def test_a_failed_idempotency_read_logs_its_type_and_not_its_cause(
    capsys: pytest.CaptureFixture[str], configure: Callable[[], None]
) -> None:
    """Stage 3's ``idempotency_check_failed`` line carries no traceback.

    The read's error is chained to the driver's, so a rendered traceback
    would print the server's text.
    """
    configure()
    event_log = MagicMock()
    event_log.has_idempotency_key.side_effect = _refuse_the_read
    handler = MagicMock()

    result = MutationExecutor(
        event_log=event_log, handlers={Operation.ENTITY_CREATE: handler}
    ).execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "synthetic"},
            command_id=_COMMAND_ID,
            idempotency_key="idem-synthetic-2",
        )
    )
    out = capsys.readouterr().err

    assert result.status == CommandStatus.FAILED
    handler.handle.assert_not_called()
    [line] = [ln for ln in out.splitlines() if "idempotency_check_failed" in ln]
    assert "StoreError" in line
    assert "idem-synthetic-2" in line
    assert _SERVER_TEXT not in out


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        pytest.param(_MappedDriverFailure(), _TYPED_MESSAGE, id="typed"),
        pytest.param(_Panic(), _PANIC_MESSAGE, id="unexpected"),
    ],
)
def test_a_handler_failure_returns_failed_and_audits_one_rejection(
    handler: CommandHandler, message: str
) -> None:
    """The FAILED result and the MUTATION_REJECTED event a failure produces.

    Every field is pinned, and the command's optional fields are set to distinct
    values, so a change to how the failure is logged cannot move what the caller
    and the audit see. ``executed_at`` is a clock and ``schema_version`` the
    model's own.
    """
    event_log = MagicMock()
    event_log.has_idempotency_key.return_value = False
    result = MutationExecutor(
        event_log=event_log, handlers={Operation.ENTITY_CREATE: handler}
    ).execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "synthetic"},
            command_id=_COMMAND_ID,
            target_id="node-synthetic-1",
            target_type="entity",
            requested_by="test:synthetic",
            idempotency_key="idem-synthetic-1",
        )
    )

    dumped = result.model_dump(mode="json", exclude={"executed_at", "schema_version"})
    assert dumped == {
        "command_id": _COMMAND_ID,
        "status": "failed",
        "operation": "entity.create",
        "target_id": None,
        "created_id": None,
        "message": f"Execution failed: {message}",
        "warnings": [],
        "metadata": {},
    }
    assert event_log.emit.call_args_list == [
        call(
            EventType.MUTATION_REJECTED,
            "mutation_executor",
            entity_id="node-synthetic-1",
            entity_type="entity",
            payload={
                "command_id": _COMMAND_ID,
                "operation": Operation.ENTITY_CREATE,
                "status": CommandStatus.FAILED,
                "message": message,
                "requested_by": "test:synthetic",
                "idempotency_key": "idem-synthetic-1",
            },
        )
    ]


def _mapped(operation: str) -> Callable[..., NoReturn]:
    """An event-log call failing as a backend that maps its driver's errors does.

    A type-only ``StoreError`` with the driver's error on ``__cause__``.
    """

    def fail(*args: object, **kwargs: object) -> NoReturn:
        try:
            _driver_call()
        except _DriverError as exc:
            msg = f"Event log {operation} failed: _DriverError"
            raise StoreError(msg, store="event_log") from exc

    return fail


def _raw(*args: object, **kwargs: object) -> NoReturn:
    """An event-log call failing as the SQLite backend does, with its own error."""
    msg = f"server says: {_SERVER_TEXT}"
    raise sqlite3.OperationalError(msg)


def _run_keyed_command(
    read: Callable[..., NoReturn], emit: Callable[..., NoReturn]
) -> tuple[CommandResult, MagicMock, MagicMock]:
    """Execute a keyed command whose idempotency read and audit emit both fail.

    Returns the result, the event log and the handler, which must not run.
    """
    event_log = MagicMock()
    event_log.has_idempotency_key.side_effect = read
    event_log.emit.side_effect = emit
    handler = MagicMock()
    result = MutationExecutor(
        event_log=event_log, handlers={Operation.ENTITY_CREATE: handler}
    ).execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "synthetic"},
            command_id=_COMMAND_ID,
            target_id="node-synthetic-3",
            target_type="entity",
            requested_by="test:synthetic",
            idempotency_key="idem-synthetic-3",
        )
    )
    return result, event_log, handler


@pytest.mark.parametrize(
    ("read", "emit", "error_type", "error"),
    [
        pytest.param(
            _mapped("has_idempotency_key"),
            _mapped("append"),
            "StoreError",
            "Event log append failed: _DriverError",
            id="mapped",
        ),
        pytest.param(_raw, _raw, "OperationalError", None, id="raw"),
    ],
)
def test_a_failed_audit_emit_logs_its_type_and_not_the_failure_before_it(
    capsys: pytest.CaptureFixture[str],
    read: Callable[..., NoReturn],
    emit: Callable[..., NoReturn],
    error_type: str,
    error: str | None,
) -> None:
    """``audit_emit_failed`` names the emit's error and renders no chain.

    The rejection event for a failed idempotency read is emitted inside the
    read's ``except`` block, so the read's error, and the driver text it
    carries, is the emit error's context. A ``TrellisError``'s own message
    is logged (``mapped``); a raw driver error's text is not (``raw``). The
    line is read through the API's JSON chain, one record, and pinned whole.
    """
    configure_logging()
    _run_keyed_command(read, emit)
    out = capsys.readouterr().err

    [line] = [ln for ln in out.splitlines() if "audit_emit_failed" in ln]
    record = json.loads(line)
    record.pop("timestamp")  # a clock
    # No ``exception`` key: that is the field a rendered traceback fills.
    assert record == {
        "event": "audit_emit_failed",
        "level": "error",
        "command_id": _COMMAND_ID,
        "operation": "entity.create",
        "event_type": "mutation.rejected",
        "error_type": error_type,
        "error": error,
    }
    assert _SERVER_TEXT not in out


def test_a_failed_read_and_emit_return_failed_with_the_audit_warning() -> None:
    """The FAILED result and the one emit attempted, pinned whole.

    How the emit failure is logged cannot move what the caller sees. The
    warning carries the emit error's own text, even a raw driver error's:
    the result is not the log, and this pin keeps it as it is.
    """
    result, event_log, handler = _run_keyed_command(_raw, _raw)

    handler.handle.assert_not_called()
    dumped = result.model_dump(mode="json", exclude={"executed_at", "schema_version"})
    assert dumped == {
        "command_id": _COMMAND_ID,
        "status": "failed",
        "operation": "entity.create",
        "target_id": None,
        "created_id": None,
        "message": "Idempotency check failed: OperationalError",
        "warnings": [
            (
                "audit_event_not_recorded: the mutation.rejected event for this "
                "command could not be written to the event log "
                f"(OperationalError: server says: {_SERVER_TEXT}). The outcome "
                "this result reports stands; only its audit record is missing."
            )
        ],
        "metadata": {},
    }
    assert event_log.emit.call_args_list == [
        call(
            EventType.MUTATION_REJECTED,
            "mutation_executor",
            entity_id="node-synthetic-3",
            entity_type="entity",
            payload={
                "command_id": _COMMAND_ID,
                "operation": Operation.ENTITY_CREATE,
                "status": CommandStatus.REJECTED,
                "message": "Idempotency check failed: OperationalError",
                "requested_by": "test:synthetic",
                "idempotency_key": "idem-synthetic-3",
                "reason": "idempotency_check_failed",
            },
        )
    ]
