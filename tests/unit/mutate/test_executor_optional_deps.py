"""The handler-panic catch covers psycopg's and the Bolt driver's own base
errors, which the Postgres and Bolt graph stores raise unmapped, without
``trellis.mutate.executor`` importing either optional driver.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests.integration._live_server import repo_src_pythonpath
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


def test_broken_driver_import_does_not_replace_handler_panic() -> None:
    """A driver whose import machinery raises something other than
    ``ImportError`` (a broken install, a native-library load failure) must
    not replace a handler's own ``RuntimeError`` and FAILED result with an
    escaping exception. A fresh interpreter, because this installs a
    meta-path finder that would otherwise affect other tests' imports.
    """
    script = (
        "import sys\n"
        "import importlib.abc\n"
        "import tempfile\n"
        "from pathlib import Path\n"
        "\n"
        "class _BoomFinder(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'psycopg':\n"
        "            raise RuntimeError('synthetic broken install')\n"
        "        return None\n"
        "\n"
        "sys.meta_path.insert(0, _BoomFinder())\n"
        "\n"
        "from trellis.mutate.commands import Command, Operation\n"
        "from trellis.mutate.executor import MutationExecutor\n"
        "from trellis.stores.sqlite.event_log import SQLiteEventLog\n"
        "\n"
        "class _Raises:\n"
        "    def handle(self, command):\n"
        "        raise RuntimeError('synthetic handler panic')\n"
        "\n"
        "with tempfile.TemporaryDirectory() as tmp:\n"
        "    log = SQLiteEventLog(Path(tmp) / 'events.db')\n"
        "    executor = MutationExecutor(\n"
        "        event_log=log, handlers={Operation.ENTITY_CREATE: _Raises()}\n"
        "    )\n"
        "    command = Command(\n"
        "        operation=Operation.ENTITY_CREATE,\n"
        "        args={'entity_type': 'service', 'name': 'synthetic'},\n"
        "        command_id='cmd-broken-driver-1',\n"
        "        requested_by='test:synthetic',\n"
        "    )\n"
        "    result = executor.execute(command)\n"
        "    assert result.status.value == 'failed', result\n"
        "    expected = 'Execution failed: RuntimeError'\n"
        "    assert result.message == expected, result.message\n"
        "    log.close()\n"
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


def test_driver_imported_after_first_catch_is_still_recognised(
    event_log: SQLiteEventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver still being imported when a catch runs is skipped rather
    than raising from the lookup, and is recognised on the next catch once
    its import completes: a lookup cached at the first catch would stay
    stale for the rest of the process.
    """

    class _SyntheticDriverError(Exception):
        """Stands in for ``psycopg.Error``."""

    # Mid-import (in another thread, say), the module is already in
    # sys.modules but its classes are not bound yet.
    fake_psycopg = types.ModuleType("psycopg")
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    executor = MutationExecutor(
        event_log=event_log,
        handlers={
            Operation.ENTITY_CREATE: _Raises(
                _SyntheticDriverError("not yet importable")
            )
        },
    )
    first_command = Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "synthetic-1"},
        command_id="cmd-stale-cache-1",
        requested_by="test:synthetic",
    )
    # Not a recognised class yet, so it propagates like any unmapped
    # exception, not replaced by an AttributeError from the lookup.
    with pytest.raises(_SyntheticDriverError):
        executor.execute(first_command)

    # The import completes.
    fake_psycopg.Error = _SyntheticDriverError  # type: ignore[attr-defined]

    second_command = Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "synthetic-2"},
        command_id="cmd-stale-cache-2",
        requested_by="test:synthetic",
    )
    result = executor.execute(second_command)

    assert result.status == CommandStatus.FAILED
    assert result.message == f"Execution failed: {_SyntheticDriverError.__name__}"


@pytest.mark.parametrize(
    "load_class",
    [
        pytest.param(lambda: pytest.importorskip("psycopg").Error, id="Error"),
        pytest.param(
            lambda: pytest.importorskip("neo4j").exceptions.DriverError,
            id="DriverError",
        ),
        pytest.param(
            lambda: pytest.importorskip("neo4j").exceptions.Neo4jError,
            id="Neo4jError",
        ),
    ],
)
def test_driver_base_error_is_a_handled_panic(
    event_log: SQLiteEventLog, load_class: Callable[[], type[Exception]]
) -> None:
    # The base class itself, so a catch narrowed to a subclass fails here.
    exc_class = load_class()
    exc = exc_class("synthetic-driver-text")
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
    assert result.message == f"Execution failed: {exc_class.__name__}"
    events = event_log.get_events(event_type=EventType.MUTATION_REJECTED, limit=50)
    failed = [e.payload for e in events if e.payload.get("status") == "failed"]
    assert [p["command_id"] for p in failed] == [_COMMAND_ID]
