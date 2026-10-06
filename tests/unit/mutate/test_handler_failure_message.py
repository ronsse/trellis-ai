"""What a caller reads when a handler fails.

A FAILED result's ``message`` reaches every caller: the REST 400 body, the
batch response, the MCP result JSON and the CLI's JSON and text output. A
``TrellisError`` carries text Trellis wrote, and the typed catch keeps it
(``Execution failed: <text>``). Any other exception reaching the untyped
catch is named by its type alone (``Execution failed: IntegrityError``),
because its text can be a driver's, which can carry query text and values.
The full text still reaches the operator log and the ``MUTATION_REJECTED``
audit payload.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, NoReturn

import pytest

from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
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
