"""``Enforcement.WARN`` is visible on the CLI, in both output formats.

The CLI is the surface with the least fallback. ``trellis_cli.main``
pins ``TRELLIS_LOG_LEVEL`` to ``WARNING`` absent ``-v``, and the gate logs
``policy_warning`` at ``info`` — so on this surface, and only on this
surface, the structlog record is not a second channel. If the command's
own output does not say it, nothing does.

The JSON key is unconditional for the same reason the wire DTO's is (see
``tests/unit/api/test_command_warnings.py``): a machine consumer cannot
tell an absent key from a build that predates the field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis_cli.main import app

runner = CliRunner()

WARNING_TEXT = "Policy warning (pol-warn): unusual write"


@pytest.fixture
def stores_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temp data dir and hand back its stores dir.

    ``resolve_policy_path`` canonicalises on ``<stores_dir>/policies.json``,
    which is what ``build_curate_executor`` reads per call — so a policy
    written here is picked up without restarting anything.
    """
    data_dir = tmp_path / "data"
    stores = data_dir / "stores"
    stores.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    return stores


def _declare(
    stores: Path,
    *,
    warn: bool = True,
    deny: bool = False,
    condition: str = "unusual write",
) -> None:
    rules = []
    if warn:
        rules.append(PolicyRule(operation="*", condition=condition, action="warn"))
    if deny:
        rules.append(
            PolicyRule(operation="*", condition="not permitted", action="deny")
        )
    policy = Policy(
        policy_id="pol-warn",
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=rules,
        enforcement=Enforcement.ENFORCE,
    )
    (stores / POLICY_FILENAME).write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}), encoding="utf-8"
    )


def _label(*extra: str, exit_code: int = 0) -> str:
    result = runner.invoke(app, ["curate", "label", "ent_1", "important", *extra])
    assert result.exit_code == exit_code, result.output
    return result.output


class TestJsonFormat:
    def test_warning_present_when_a_policy_fires(self, stores_dir: Path) -> None:
        _declare(stores_dir)
        data = json.loads(_label("--format", "json").strip())
        assert data["warnings"] == [WARNING_TEXT]

    def test_warning_survives_a_rejection(self, stores_dir: Path) -> None:
        _declare(stores_dir, deny=True)
        data = json.loads(_label("--format", "json", exit_code=2).strip())
        assert data["status"] == "rejected"
        assert data["warnings"] == [WARNING_TEXT]

    def test_key_is_present_and_empty_with_no_policies(self, stores_dir: Path) -> None:
        data = json.loads(_label("--format", "json").strip())
        assert "warnings" in data, "absent key is indistinguishable from an old build"
        assert data["warnings"] == []


class TestTextFormat:
    def test_warning_is_printed(self, stores_dir: Path) -> None:
        _declare(stores_dir)
        assert WARNING_TEXT in plain(_label())

    def test_nothing_printed_when_no_policy_fires(self, stores_dir: Path) -> None:
        """No policies must leave the rendering exactly as it was.

        The transparency property the JSON arm gives up on purpose: a human
        surface has no parser to mislead, so an empty ``Warning:`` line would
        be noise rather than evidence.
        """
        assert "Warning:" not in plain(_label())

    def test_warning_survives_a_rejection(self, stores_dir: Path) -> None:
        """A refusal exits 2 only after the text arm has printed its warnings."""
        _declare(stores_dir, deny=True)
        output = plain(_label(exit_code=2))
        assert "Command rejected" in output
        assert WARNING_TEXT in output


# The curate writes that build their own result, instead of going through
# ``_execute_command`` as ``label``, ``promote`` and ``feedback`` do.
_SITES = ["entity", "prune", "restore", "redact", "link"]

# ``link`` refuses with exit 1 and status "error", not 2 and "rejected".
# That is a separate defect; these tests pin today's code.
_REFUSAL_EXIT = {"entity": 2, "prune": 2, "restore": 2, "redact": 2, "link": 1}


def _seed(name: str) -> str:
    """Create an entity before any policy exists, and return its id."""
    result = runner.invoke(
        app, ["curate", "entity", "concept", name, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output.strip())["node_id"]


def _argv(command: str) -> list[str]:
    """Build *command*'s argv, seeding the entities it needs."""
    if command == "entity":
        return ["curate", "entity", "concept", "n"]
    if command == "prune":
        # ``--apply`` skips the dry-run notice, so a warning line nested
        # under that notice's ``if`` would go missing.
        return ["curate", "prune", "--reason", "r", "--noise-documents", "--apply"]
    if command == "restore":
        return ["curate", "restore", "--item-id", "x1", "--reason", "r"]
    if command == "redact":
        return ["curate", "redact", _seed("a"), "--reason", "r", "--yes"]
    return ["curate", "link", _seed("a"), _seed("b")]


def _run(argv: list[str], *extra: str, exit_code: int = 0) -> str:
    result = runner.invoke(app, [*argv, *extra])
    assert result.exit_code == exit_code, result.output
    return result.output


@pytest.mark.parametrize("command", _SITES)
class TestEveryCurateWrite:
    """The other curate writes show a warning the way ``label`` does."""

    def test_json_carries_the_warning(self, stores_dir: Path, command: str) -> None:
        argv = _argv(command)
        _declare(stores_dir)
        data = json.loads(_run(argv, "--format", "json").strip())
        assert data["warnings"] == [WARNING_TEXT]

    def test_json_refusal_carries_the_warning(
        self, stores_dir: Path, command: str
    ) -> None:
        argv = _argv(command)
        _declare(stores_dir, deny=True)
        output = _run(argv, "--format", "json", exit_code=_REFUSAL_EXIT[command])
        assert json.loads(output.strip())["warnings"] == [WARNING_TEXT]

    def test_json_key_is_present_and_empty_with_no_policies(
        self, stores_dir: Path, command: str
    ) -> None:
        data = json.loads(_run(_argv(command), "--format", "json").strip())
        assert data["warnings"] == []

    def test_json_refusal_with_no_warning_keeps_the_empty_key(
        self, stores_dir: Path, command: str
    ) -> None:
        argv = _argv(command)
        _declare(stores_dir, warn=False, deny=True)
        output = _run(argv, "--format", "json", exit_code=_REFUSAL_EXIT[command])
        assert json.loads(output.strip())["warnings"] == []

    def test_text_prints_the_warning(self, stores_dir: Path, command: str) -> None:
        argv = _argv(command)
        _declare(stores_dir)
        assert WARNING_TEXT in plain(_run(argv))

    def test_text_refusal_prints_the_warning(
        self, stores_dir: Path, command: str
    ) -> None:
        argv = _argv(command)
        _declare(stores_dir, deny=True)
        assert WARNING_TEXT in plain(_run(argv, exit_code=_REFUSAL_EXIT[command]))


def test_markup_in_a_policy_condition_prints_verbatim(stores_dir: Path) -> None:
    """A policy's condition is free text, so the warning line escapes it.

    Unescaped, ``[/x]`` raises ``MarkupError`` after the write has committed,
    and the command exits 1. The id-markup rule cannot catch that, because
    ``warning`` is not an id-shaped name.
    """
    _declare(stores_dir, condition="unusual [/x] write")
    output = plain(_run(_argv("prune")))
    assert "Policy warning (pol-warn): unusual [/x] write" in output
