"""Tests for policy CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _add_policy() -> str:
    """Helper: add a policy and return its ID."""
    result = runner.invoke(
        app,
        [
            "policy",
            "add",
            "--type",
            "mutation",
            "--scope",
            "global",
            "--operation",
            "entity.create",
            "--action",
            "deny",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout.strip())
    return data["policy_id"]


class TestPolicyList:
    def test_list_empty(self) -> None:
        result = runner.invoke(app, ["policy", "list"])
        assert result.exit_code == 0
        assert "No policies" in result.stdout

    def test_list_empty_json(self) -> None:
        result = runner.invoke(app, ["policy", "list", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["count"] == 0
        assert data["policies"] == []

    def test_list_after_add(self) -> None:
        _add_policy()
        result = runner.invoke(app, ["policy", "list"])
        assert result.exit_code == 0
        assert "mutation" in result.stdout

    def test_list_after_add_json(self) -> None:
        _add_policy()
        result = runner.invoke(app, ["policy", "list", "--format", "json"])
        data = json.loads(result.stdout.strip())
        assert data["count"] == 1
        assert data["policies"][0]["policy_type"] == "mutation"


class TestPolicyAdd:
    def test_add_text(self) -> None:
        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--type",
                "mutation",
                "--scope",
                "global",
                "--operation",
                "entity.delete",
                "--action",
                "warn",
            ],
        )
        assert result.exit_code == 0
        assert "Policy added" in result.stdout

    def test_add_json(self) -> None:
        policy_id = _add_policy()
        assert len(policy_id) > 0

    def test_add_with_scope_value(self) -> None:
        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--type",
                "access",
                "--scope",
                "domain",
                "--scope-value",
                "platform",
                "--operation",
                "trace.read",
                "--action",
                "allow",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"

    def test_add_with_custom_enforcement(self) -> None:
        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--type",
                "mutation",
                "--scope",
                "global",
                "--operation",
                "*",
                "--action",
                "deny",
                "--enforcement",
                "audit_only",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"


class TestPolicyShow:
    def test_show_by_id(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "show", policy_id])
        assert result.exit_code == 0
        assert "mutation" in result.stdout

    def test_show_by_prefix(self) -> None:
        policy_id = _add_policy()
        prefix = policy_id[:8]
        result = runner.invoke(app, ["policy", "show", prefix])
        assert result.exit_code == 0
        assert policy_id in result.stdout

    def test_show_json(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "show", policy_id, "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["policy_id"] == policy_id

    def test_show_not_found(self) -> None:
        result = runner.invoke(app, ["policy", "show", "nonexistent"])
        assert result.exit_code == 1


class TestPolicyRemove:
    def test_remove_by_id(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "remove", policy_id])
        assert result.exit_code == 0
        assert "removed" in result.stdout.lower()

    def test_remove_json(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "remove", policy_id, "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"

    def test_remove_not_found(self) -> None:
        result = runner.invoke(app, ["policy", "remove", "nonexistent"])
        assert result.exit_code == 1

    def test_remove_not_found_json(self) -> None:
        result = runner.invoke(
            app, ["policy", "remove", "nonexistent", "--format", "json"]
        )
        assert result.exit_code == 1

    def test_remove_then_list_empty(self) -> None:
        policy_id = _add_policy()
        runner.invoke(app, ["policy", "remove", policy_id])
        result = runner.invoke(app, ["policy", "list", "--format", "json"])
        data = json.loads(result.stdout.strip())
        assert data["count"] == 0


class TestPolicyHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["policy", "--help"])
        assert result.exit_code == 0
        for cmd in ["list", "show", "add", "remove"]:
            assert cmd in result.stdout
