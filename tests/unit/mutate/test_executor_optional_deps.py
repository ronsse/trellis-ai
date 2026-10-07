"""The handler-panic tuple covers Postgres's and the Bolt driver's own
unmapped base errors, without ``trellis.mutate.executor`` ever importing
either driver package.

``psycopg`` and ``neo4j`` are optional extras (``pyproject.toml``'s
``postgres``/``neo4j``/``arcadedb`` groups). A Postgres graph store's writes
that raise their own unmapped ``psycopg.Error``, and a Bolt-backed store's
(Neo4j or ArcadeDB, which share the same driver) ``DriverError``/
``Neo4jError``, must still produce a FAILED ``CommandResult`` and a
``MUTATION_REJECTED`` audit event — exactly like the built-in exceptions and
``sqlite3.Error`` already covered by ``_UNEXPECTED_HANDLER_FAILURE`` — rather
than escaping ``execute()``/``execute_batch()`` raw.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests.integration._live_server import repo_src_pythonpath
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.mutate.commands import CommandResult

#: Stands in for a driver's own error text. The FAILED message must not
#: carry it (that is #748's fix, already covered by
#: test_handler_failure_message.py); this module only checks that the
#: event lands at all.
_DRIVER_TEXT = "synthetic-driver-panic-x91k"
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


def _failed_events(log: SQLiteEventLog) -> list[dict[str, object]]:
    events = log.get_events(event_type=EventType.MUTATION_REJECTED, limit=50)
    return [e.payload for e in events if e.payload.get("status") == "failed"]


def _create_cmd() -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "synthetic"},
        command_id=_COMMAND_ID,
        requested_by="test:synthetic",
    )


class TestDoesNotImportOptionalDrivers:
    """``import trellis.mutate.executor`` must not import psycopg or neo4j.

    A module-level guarded import (``try: import psycopg ... except
    ImportError: ...``) would import whichever of the two happens to be
    installed in the interpreter running the import — which rules it out,
    because the standard test venv has ``neo4j`` installed. Only a
    subprocess, started fresh, can observe this; checking ``sys.modules``
    in-process would just see whatever earlier tests in the same run
    already imported.
    """

    def test_import_alone_does_not_pull_in_either_driver(self) -> None:
        env = {**os.environ, "PYTHONPATH": repo_src_pythonpath()}
        script = (
            "import sys\n"
            "import trellis.mutate.executor\n"
            "leaked = sorted(\n"
            "    m for m in sys.modules if m == 'psycopg' or m == 'neo4j'\n"
            "    or m.startswith('psycopg.') or m.startswith('neo4j.')\n"
            ")\n"
            "assert not leaked, leaked\n"
            "print('OK')\n"
        )
        result = subprocess.run(  # noqa: S603 — fixed interpreter + inline script, no shell
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout


class TestPsycopgErrorIsCaught:
    """An unmapped ``psycopg.Error`` from a handler is a handled panic."""

    def test_single_execute_fails_and_is_audited(
        self, event_log: SQLiteEventLog
    ) -> None:
        psycopg = pytest.importorskip("psycopg")
        exc = psycopg.OperationalError(_DRIVER_TEXT)
        executor = MutationExecutor(
            event_log=event_log, handlers={Operation.ENTITY_CREATE: _Raises(exc)}
        )

        result: CommandResult = executor.execute(_create_cmd())

        assert result.status == CommandStatus.FAILED
        assert result.message == "Execution failed: OperationalError"
        failed = _failed_events(event_log)
        assert len(failed) == 1
        assert failed[0]["command_id"] == _COMMAND_ID

    def test_continue_on_error_batch_runs_the_later_command(
        self, event_log: SQLiteEventLog
    ) -> None:
        psycopg = pytest.importorskip("psycopg")
        exc = psycopg.OperationalError(_DRIVER_TEXT)
        good_handler = _GoodHandler()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _FirstRaisesThenDelegates(exc, good_handler)
            },
        )
        batch = CommandBatch(
            commands=[_create_cmd(), _create_cmd()],
            strategy=BatchStrategy.CONTINUE_ON_ERROR,
        )

        results = executor.execute_batch(batch)

        assert len(results) == 2
        assert results[0].status == CommandStatus.FAILED
        assert results[1].status == CommandStatus.SUCCESS
        assert len(_failed_events(event_log)) == 1


class TestBoltDriverErrorIsCaught:
    """An unmapped Bolt driver error (Neo4j and ArcadeDB share the driver)
    from a handler is a handled panic, for each of the driver's two
    sibling base classes.
    """

    @pytest.mark.parametrize("class_name", ["DriverError", "Neo4jError"])
    def test_single_execute_fails_and_is_audited(
        self, event_log: SQLiteEventLog, class_name: str
    ) -> None:
        pytest.importorskip("neo4j")
        from neo4j import exceptions

        exc_cls = getattr(exceptions, class_name)
        exc = exc_cls(_DRIVER_TEXT)
        executor = MutationExecutor(
            event_log=event_log, handlers={Operation.ENTITY_CREATE: _Raises(exc)}
        )

        result: CommandResult = executor.execute(_create_cmd())

        assert result.status == CommandStatus.FAILED
        assert result.message == f"Execution failed: {class_name}"
        failed = _failed_events(event_log)
        assert len(failed) == 1
        assert failed[0]["command_id"] == _COMMAND_ID

    def test_continue_on_error_batch_runs_the_later_command(
        self, event_log: SQLiteEventLog
    ) -> None:
        pytest.importorskip("neo4j")
        from neo4j import exceptions

        exc = exceptions.DriverError(_DRIVER_TEXT)
        good_handler = _GoodHandler()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _FirstRaisesThenDelegates(exc, good_handler)
            },
        )
        batch = CommandBatch(
            commands=[_create_cmd(), _create_cmd()],
            strategy=BatchStrategy.CONTINUE_ON_ERROR,
        )

        results = executor.execute_batch(batch)

        assert len(results) == 2
        assert results[0].status == CommandStatus.FAILED
        assert results[1].status == CommandStatus.SUCCESS
        assert len(_failed_events(event_log)) == 1


class _GoodHandler:
    """A handler that succeeds, returning a fresh created_id each call."""

    def __init__(self) -> None:
        self._calls = 0

    def handle(self, command: Command) -> tuple[str | None, str]:
        self._calls += 1
        return (f"synthetic-{self._calls}", "ok")


class _FirstRaisesThenDelegates:
    """Raises once, then delegates every later call to a good handler.

    Models a batch where the *first* command hits an unmapped driver panic
    (e.g. a lost connection) and a *later*, independent command on the same
    handler succeeds — the scenario CONTINUE_ON_ERROR exists for.
    """

    def __init__(self, exc: BaseException, good: _GoodHandler) -> None:
        self._exc = exc
        self._good = good
        self._raised = False

    def handle(self, command: Command) -> tuple[str | None, str]:
        if not self._raised:
            self._raised = True
            raise self._exc
        return self._good.handle(command)
