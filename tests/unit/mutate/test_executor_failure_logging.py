"""What a failed handler writes to the operator log.

A typed handler failure (a ``StoreError``, or another ``TrellisError`` that
fails the command) is logged as its type, its message and the command it
belongs to, without the chained traceback. The chained cause is where a
backend's own text lives: the Postgres and Bolt purges (#702, #713) raise
``StoreError(<type-only message>) from <driver error>`` because a server's
message can carry query text and values, and a rendered traceback prints that
cause. An unexpected exception (a ``RuntimeError``, say) is logged with its
full traceback, which is how the bug behind it gets found.

The log is read as a deployment renders it, through the real processor chains:
``configure_stderr_logging`` (CLI and MCP server) and the API's
``configure_logging`` (JSON by default). ``structlog.testing.capture_logs`` on
its own cannot see a leaked cause: it records ``exc_info=True``, and the
traceback text only appears when a chain's ``format_exc_info`` renders it.
"""

from __future__ import annotations

import logging
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
