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


def _declare(stores: Path, *, deny: bool = False) -> None:
    rules = [PolicyRule(operation="*", condition="unusual write", action="warn")]
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


def _label(*extra: str) -> str:
    result = runner.invoke(app, ["curate", "label", "ent_1", "important", *extra])
    assert result.exit_code == 0, result.output
    return result.output


class TestJsonFormat:
    def test_warning_present_when_a_policy_fires(self, stores_dir: Path) -> None:
        _declare(stores_dir)
        data = json.loads(_label("--format", "json").strip())
        assert data["warnings"] == [WARNING_TEXT]

    def test_warning_survives_a_rejection(self, stores_dir: Path) -> None:
        _declare(stores_dir, deny=True)
        data = json.loads(_label("--format", "json").strip())
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
