"""What a caller reads when a handler fails.

A FAILED result's ``message`` reaches every caller: the REST 400 body, the
batch response, the MCP result JSON and the CLI's JSON and text output. A
``TrellisError`` carries text Trellis wrote, and the typed catch keeps it
(``Execution failed: <text>``). Any other exception reaching the untyped
catch is named by its type alone (``Execution failed: IntegrityError``),
because its text can be a driver's, which can carry query text and values.
The full text still reaches the operator log and the ``MUTATION_REJECTED``
audit payload; this module pins the payload unchanged.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, NoReturn

import pytest

from trellis.errors import ConfigError, StoreError, TrellisError
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from trellis.mutate.commands import CommandResult

#: Stands in for a server's error text. Nothing else in this module contains
#: it, so finding it in a result means the exception's text was copied out.
_MARKER = "synthetic-secret-c55h"
_DRIVER_TEXT = f"server says: {_MARKER}"
_COMMAND_ID = "cmd-synthetic-hfm-1"

#: Exceptions outside the Trellis hierarchy, each with the type name the
#: caller should read instead of its text.
_UNTYPED = [
    pytest.param(sqlite3.IntegrityError(_DRIVER_TEXT), "IntegrityError", id="sqlite3"),
    pytest.param(
        ConnectionResetError(_DRIVER_TEXT), "ConnectionResetError", id="oserror"
    ),
    pytest.param(ValueError(_DRIVER_TEXT), "ValueError", id="valueerror"),
]
_UNTYPED_EXCEPTIONS = [pytest.param(case.values[0], id=case.id) for case in _UNTYPED]


class _Raises:
    """A handler that raises the exception it was built with."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def handle(self, command: Command) -> NoReturn:
        raise self._exc


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _fail(event_log: SQLiteEventLog, exc: Exception) -> CommandResult:
    return MutationExecutor(
        event_log=event_log, handlers={Operation.ENTITY_CREATE: _Raises(exc)}
    ).execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "synthetic"},
            command_id=_COMMAND_ID,
            requested_by="test:synthetic",
        )
    )


@pytest.mark.parametrize(("exc", "type_name"), _UNTYPED)
def test_an_untyped_failure_is_named_by_its_type_alone(
    event_log: SQLiteEventLog, exc: Exception, type_name: str
) -> None:
    result = _fail(event_log, exc)

    assert result.status == CommandStatus.FAILED
    assert result.message == f"Execution failed: {type_name}"
    assert _MARKER not in result.model_dump_json()


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        pytest.param(
            TrellisError("synthetic trellis failure"),
            "Execution failed: synthetic trellis failure",
            id="trelliserror",
        ),
        pytest.param(
            StoreError("synthetic store failure", store="graph"),
            "Execution failed: synthetic store failure",
            id="storeerror",
        ),
        pytest.param(
            ConfigError("synthetic config failure", setting="synthetic"),
            "Execution failed: synthetic config failure",
            id="configerror",
        ),
    ],
)
def test_a_trellis_failure_keeps_its_text(
    event_log: SQLiteEventLog, exc: TrellisError, message: str
) -> None:
    """Byte for byte what the typed catch wrote before untyped text was dropped."""
    result = _fail(event_log, exc)

    assert result.status == CommandStatus.FAILED
    assert result.message == message


@pytest.mark.parametrize("exc", _UNTYPED_EXCEPTIONS)
def test_the_audit_payload_keeps_the_full_text(
    event_log: SQLiteEventLog, exc: Exception
) -> None:
    """The FAILED event is the operator's record; its payload is unchanged."""
    _fail(event_log, exc)

    (event,) = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
    assert event.payload == {
        "command_id": _COMMAND_ID,
        "operation": "entity.create",
        "status": "failed",
        "message": _DRIVER_TEXT,
        "requested_by": "test:synthetic",
        "idempotency_key": None,
    }
