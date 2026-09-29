"""A refused or failed curate write exits non-zero, in both output formats.

``curate prune``, ``restore`` and ``redact`` already exit ``2``
(``EXIT_VALIDATION``) on a REJECTED command and ``5`` (``EXIT_STORE``) on a
FAILED one. ``entity``, ``promote``, ``label`` and ``feedback`` exited ``0``
on both. ``entity`` also printed "Entity created: None" and emitted
``"status": "ok"``, so the Neo4j guides' smoke check,
``trellis curate entity concept smoke-check``, passed on a write that never
happened.

A DUPLICATE is not a failure. It still exits ``0``, as it does in prune,
restore and redact.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.mutate.commands import Command, CommandResult, CommandStatus
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis_cli import curate as curate_cli
from trellis_cli.main import app

runner = CliRunner()

# A closing tag with no opener. Printed without ``escape``, Rich raises
# ``MarkupError`` and the command dies with exit 1 instead of reporting.
MESSAGE = "boom [/x]"

_ENTITY = ["curate", "entity", "concept", "n"]
_LABEL = ["curate", "label", "e1", "x"]
_PROMOTE = ["curate", "promote", "t1", "--title", "T", "--description", "D"]
_FEEDBACK = ["curate", "feedback", "t1", "0.9"]


@pytest.fixture
def stores_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temp data dir and hand back its stores dir."""
    data_dir = tmp_path / "data"
    stores = data_dir / "stores"
    stores.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    return stores


def _deny_all(stores: Path) -> None:
    """Refuse every write at Stage 2, through the real executor."""
    policy = Policy(
        policy_id="pol-deny",
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=[PolicyRule(operation="*", condition="not permitted", action="deny")],
        enforcement=Enforcement.ENFORCE,
    )
    (stores / POLICY_FILENAME).write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}), encoding="utf-8"
    )


def _fake(monkeypatch: pytest.MonkeyPatch, status: CommandStatus) -> None:
    """Make every command come back with *status*.

    A real FAILED exists: a neo4j graph with a blank ``TRELLIS_NEO4J_URI``
    fails ``entity`` and ``label`` writes. These tests stay on the default
    stores, so the executor is replaced. It echoes the submitted operation
    back, as the real one does.
    """
    executor = MagicMock(spec=MutationExecutor)

    def _execute(cmd: Command) -> CommandResult:
        return CommandResult(
            command_id="cmd-x",
            status=status,
            operation=cmd.operation,
            message=MESSAGE,
        )

    executor.execute.side_effect = _execute
    monkeypatch.setattr(curate_cli, "build_curate_executor", lambda _reg: executor)


def _json(args: list[str]) -> tuple[int, dict]:
    result = runner.invoke(app, [*args, "--format", "json"])
    return result.exit_code, json.loads(result.stdout.strip())


class TestEntity:
    def test_rejected_json(self, stores_dir: Path) -> None:
        _deny_all(stores_dir)
        code, data = _json(_ENTITY)
        assert code == 2, data
        assert data["status"] == "rejected"
        assert data["command_id"]
        assert "node_id" not in data, "a refused write has no node to name"

    def test_rejected_text(self, stores_dir: Path) -> None:
        _deny_all(stores_dir)
        result = runner.invoke(app, _ENTITY)
        assert result.exit_code == 2, result.output
        assert "Entity created" not in plain(result.output)

    def test_failed_json(
        self, stores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake(monkeypatch, CommandStatus.FAILED)
        code, data = _json(_ENTITY)
        assert code == 5, data
        assert data == {
            "status": "failed",
            "command_id": "cmd-x",
            "message": MESSAGE,
            "warnings": [],
        }

    def test_failed_text_prints_the_message_verbatim(
        self, stores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake(monkeypatch, CommandStatus.FAILED)
        result = runner.invoke(app, _ENTITY)
        assert result.exit_code == 5, result.output
        output = plain(result.output)
        assert MESSAGE in output
        assert "Entity created" not in output


class TestExecuteCommand:
    """``promote``, ``label`` and ``feedback`` share ``_execute_command``."""

    @pytest.mark.parametrize(
        "args", [_LABEL, _PROMOTE, _FEEDBACK], ids=["label", "promote", "feedback"]
    )
    def test_rejected_json(self, stores_dir: Path, args: list[str]) -> None:
        _deny_all(stores_dir)
        code, data = _json(args)
        assert code == 2, data
        assert data["status"] == "rejected"

    @pytest.mark.parametrize(
        "args", [_LABEL, _PROMOTE, _FEEDBACK], ids=["label", "promote", "feedback"]
    )
    def test_rejected_text(self, stores_dir: Path, args: list[str]) -> None:
        _deny_all(stores_dir)
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        assert "Command rejected" in plain(result.output)

    def test_failed_json(
        self, stores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake(monkeypatch, CommandStatus.FAILED)
        code, data = _json(_LABEL)
        assert code == 5, data
        assert data["status"] == "failed"

    def test_failed_text(
        self, stores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake(monkeypatch, CommandStatus.FAILED)
        result = runner.invoke(app, _LABEL)
        assert result.exit_code == 5, result.output
        assert "Command failed" in plain(result.output)

    def test_duplicate_exits_zero(
        self, stores_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake(monkeypatch, CommandStatus.DUPLICATE)
        code, data = _json(_LABEL)
        # The status proves the fake served: a real executor would answer a
        # label on a missing node with SUCCESS, which also exits 0.
        assert data["status"] == "duplicate"
        assert code == 0, data
