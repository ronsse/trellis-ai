"""A refused or failed curate write exits non-zero, in both output formats.

A write that a policy refuses exits ``3`` (``EXIT_POLICY``), one refused for
any other reason ``2`` (``EXIT_VALIDATION``), and a FAILED one ``5``
(``EXIT_STORE``), whichever curate command submitted it. ``entity``,
``promote``, ``label`` and ``feedback`` once exited ``0`` on a REJECTED or
FAILED command. ``entity`` also printed "Entity created: None" and emitted
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
from trellis_cli.stores import _get_registry

runner = CliRunner()

# A closing tag with no opener. Printed without ``escape``, Rich raises
# ``MarkupError`` and the command dies with exit 1 instead of reporting.
MESSAGE = "boom [/x]"

_ENTITY = ["curate", "entity", "concept", "n"]
_LABEL = ["curate", "label", "e1", "x"]
_PROMOTE = ["curate", "promote", "t1", "--title", "T", "--description", "D"]
_FEEDBACK = ["curate", "feedback", "t1", "0.9"]
# A worker id the unattended-writer roster refuses ``precedent.promote`` to.
_ROSTER = [*_PROMOTE, "--by", "worker:embed-traces"]

# One argv per place curate.py picks an exit code. ``promote`` and
# ``feedback`` reach the same one as ``label``, through ``_execute_command``.
_SITES = {
    "entity": _ENTITY,
    "label": _LABEL,
    "link": ["curate", "link", "a", "b"],
    "prune": ["curate", "prune", "--reason", "r", "--noise-documents"],
    "restore": ["curate", "restore", "--item-id", "x1", "--reason", "r"],
    "redact": ["curate", "redact", "t", "--reason", "r", "--yes"],
}

# Only a policy refusal exits 3. The roster row is the refusal most easily
# mistaken for a policy one, and the row with no reason is what any REJECTED
# built outside the executor would look like.
_OUTCOMES = [
    pytest.param(
        CommandStatus.REJECTED, {"rejection_reason": "policy_violation"}, 3, id="policy"
    ),
    pytest.param(
        CommandStatus.REJECTED, {"rejection_reason": "immutable_core"}, 2, id="roster"
    ),
    pytest.param(CommandStatus.REJECTED, {}, 2, id="no-reason"),
    pytest.param(CommandStatus.FAILED, {}, 5, id="failed"),
]

_NO_CRITERIA = (
    "No criteria selected — a prune must say what it is pruning. "
    "Pass at least one of --noise-documents / --unconfirmed-mints / "
    "--lifecycle-state."
)
_NO_IDS = "No ids supplied — pass --item-id (repeatable) or --from-file."
# The OS error names the path. Printed without ``escape``, its ``[/x]`` raises
# ``MarkupError`` and restore exits 1.
_MISSING = "ids[/x].txt"


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


def _fake(
    monkeypatch: pytest.MonkeyPatch,
    status: CommandStatus,
    metadata: dict[str, str] | None = None,
) -> None:
    """Make every command come back with *status* and *metadata*.

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
            metadata=dict(metadata or {}),
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
        assert code == 3, data
        assert data["status"] == "rejected"
        assert data["command_id"]
        assert "node_id" not in data, "a refused write has no node to name"

    def test_rejected_text(self, stores_dir: Path) -> None:
        _deny_all(stores_dir)
        result = runner.invoke(app, _ENTITY)
        assert result.exit_code == 3, result.output
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
        assert code == 3, data
        assert data["status"] == "rejected"

    @pytest.mark.parametrize(
        "args", [_LABEL, _PROMOTE, _FEEDBACK], ids=["label", "promote", "feedback"]
    )
    def test_rejected_text(self, stores_dir: Path, args: list[str]) -> None:
        _deny_all(stores_dir)
        result = runner.invoke(app, args)
        assert result.exit_code == 3, result.output
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
        # The status proves the fake served: a real executor refuses a label
        # on a missing node, which exits 2.
        assert data["status"] == "duplicate"
        assert code == 0, data


@pytest.mark.parametrize("site", sorted(_SITES))
@pytest.mark.parametrize(("status", "metadata", "expected"), _OUTCOMES)
class TestEveryExitSite:
    """Every site exits by the refusal's reason, and ``link`` reports like the rest."""

    def test_json(
        self,
        stores_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        site: str,
        status: CommandStatus,
        metadata: dict[str, str],
        expected: int,
    ) -> None:
        _fake(monkeypatch, status, metadata)
        code, data = _json(_SITES[site])
        assert code == expected, data
        assert data["status"] == status.value
        assert data["command_id"] == "cmd-x"

    def test_text(
        self,
        stores_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        site: str,
        status: CommandStatus,
        metadata: dict[str, str],
        expected: int,
    ) -> None:
        _fake(monkeypatch, status, metadata)
        result = runner.invoke(app, _SITES[site])
        assert result.exit_code == expected, result.output
        assert MESSAGE in plain(result.output)


class TestRosterRefusal:
    """The unattended-writer roster is not a ``PolicyGate``, so it exits 2."""

    def test_json(self, stores_dir: Path) -> None:
        code, data = _json(_ROSTER)
        assert code == 2, data
        assert data["status"] == "rejected"

    def test_text(self, stores_dir: Path) -> None:
        result = runner.invoke(app, _ROSTER)
        assert result.exit_code == 2, result.output
        assert "Command rejected" in plain(result.output)


class TestBeforeACommandIsBuilt:
    """An exit taken before any command exists answers JSON, and exits 2."""

    def test_prune_without_criteria_json(self, stores_dir: Path) -> None:
        code, data = _json(["curate", "prune", "--reason", "r"])
        assert code == 2, data
        assert data == {"status": "error", "message": _NO_CRITERIA}

    def test_prune_without_criteria_text(self, stores_dir: Path) -> None:
        result = runner.invoke(app, ["curate", "prune", "--reason", "r"])
        assert result.exit_code == 2, result.output
        assert "No criteria selected" in plain(result.output)

    def test_restore_without_ids_json(self, stores_dir: Path) -> None:
        code, data = _json(["curate", "restore", "--reason", "r"])
        assert code == 2, data
        assert data == {"status": "error", "message": _NO_IDS}

    def test_restore_without_ids_text(self, stores_dir: Path) -> None:
        result = runner.invoke(app, ["curate", "restore", "--reason", "r"])
        assert result.exit_code == 2, result.output
        assert "No ids supplied" in plain(result.output)

    @pytest.mark.parametrize("unreadable", ["missing", "directory"])
    def test_unreadable_from_file_json(
        self, stores_dir: Path, tmp_path: Path, unreadable: str
    ) -> None:
        path = tmp_path / _MISSING if unreadable == "missing" else tmp_path
        with pytest.raises(OSError) as raised:
            path.read_text()
        code, data = _json(
            ["curate", "restore", "--reason", "r", "--from-file", str(path)]
        )
        assert code == 2, data
        assert data == {
            "status": "error",
            "message": f"Cannot read --from-file: {raised.value}",
        }

    @pytest.mark.parametrize("unreadable", ["missing", "directory"])
    def test_unreadable_from_file_text(
        self, stores_dir: Path, tmp_path: Path, unreadable: str
    ) -> None:
        path = tmp_path / _MISSING if unreadable == "missing" else tmp_path
        result = runner.invoke(
            app, ["curate", "restore", "--reason", "r", "--from-file", str(path)]
        )
        assert result.exit_code == 2, result.output
        assert "Cannot read --from-file" in plain(result.output)

    def test_invalid_properties_json(self, stores_dir: Path) -> None:
        code, data = _json([*_ENTITY, "--properties", "{bad"])
        assert code == 2, data
        assert data["status"] == "error"
        assert data["message"].startswith("Invalid JSON for --properties")

    def test_invalid_properties_text(self, stores_dir: Path) -> None:
        result = runner.invoke(app, [*_ENTITY, "--properties", "{bad"])
        assert result.exit_code == 2, result.output
        assert "Invalid JSON for --properties" in plain(result.output)


def _feedback(rating: str, *, json_format: bool) -> list[str]:
    """``curate feedback`` argv; ``--`` lets a negative rating stay positional."""
    fmt = ["--format", "json"] if json_format else []
    return ["curate", "feedback", *fmt, "--", "t1", rating]


def _refusal(rating: str) -> str:
    return f"rating must be between 0.0 and 1.0, got {float(rating)}"


class TestFeedbackRating:
    """``feedback`` refuses a rating outside [0.0, 1.0] before writing anything.

    The range is the MCP ``record_feedback`` tool's, inclusive at both ends.
    ``nan`` compares false to both bounds, so it is refused with the range
    rather than slipping past a pair of ``<`` / ``>`` tests.
    """

    @pytest.mark.parametrize("rating", ["nan", "inf", "-inf", "-0.1", "1.1"])
    def test_refused_json(self, stores_dir: Path, rating: str) -> None:
        result = runner.invoke(app, _feedback(rating, json_format=True))
        assert result.exit_code == 2, result.output
        assert json.loads(result.stdout.strip()) == {
            "status": "error",
            "message": _refusal(rating),
        }
        assert _get_registry().operational.event_log.count() == 0

    def test_refused_text(self, stores_dir: Path) -> None:
        result = runner.invoke(app, _feedback("nan", json_format=False))
        assert result.exit_code == 2, result.output
        assert _refusal("nan") in plain(result.output)
        assert _get_registry().operational.event_log.count() == 0

    @pytest.mark.parametrize("rating", ["0.0", "1.0"])
    def test_in_range_json(self, stores_dir: Path, rating: str) -> None:
        result = runner.invoke(app, _feedback(rating, json_format=True))
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["status"] == "success"
        assert _get_registry().operational.event_log.count() > 0
