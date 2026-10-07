"""The handler-panic catch covers psycopg's and the Bolt driver's own base
errors, which the Postgres and Bolt graph stores raise unmapped, without
``trellis.mutate.executor`` importing either optional driver.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests.integration._live_server import repo_src_pythonpath
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from collections.abc import Iterator

_COMMAND_ID = "cmd-synthetic-optdeps-1"


class _Raises:
    """A handler that raises the exception it was built with."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def handle(self, command: Command) -> NoReturn:
        raise self._exc


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def test_importing_the_executor_imports_neither_driver() -> None:
    # A fresh interpreter, because in-process sys.modules holds whatever
    # earlier tests imported.
    script = (
        "import sys\n"
        "import trellis.mutate.executor\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules if m.split('.')[0] in {'psycopg', 'neo4j'}\n"
        ")\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter + inline script, no shell
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": repo_src_pythonpath()},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("module", "class_name"),
    [
        ("psycopg", "Error"),
        ("neo4j.exceptions", "DriverError"),
        ("neo4j.exceptions", "Neo4jError"),
    ],
)
def test_driver_base_error_is_a_handled_panic(
    event_log: SQLiteEventLog, module: str, class_name: str
) -> None:
    # The base class itself, so a catch narrowed to a subclass fails here.
    exc = getattr(pytest.importorskip(module), class_name)("synthetic-driver-text")
    executor = MutationExecutor(
        event_log=event_log, handlers={Operation.ENTITY_CREATE: _Raises(exc)}
    )
    command = Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "synthetic"},
        command_id=_COMMAND_ID,
        requested_by="test:synthetic",
    )

    result = executor.execute(command)

    assert result.status == CommandStatus.FAILED
    assert result.message == f"Execution failed: {class_name}"
    events = event_log.get_events(event_type=EventType.MUTATION_REJECTED, limit=50)
    failed = [e.payload for e in events if e.payload.get("status") == "failed"]
    assert [p["command_id"] for p in failed] == [_COMMAND_ID]
