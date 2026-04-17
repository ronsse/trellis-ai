"""Tests for admin CLI commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from trellis_cli.admin import admin_app
from trellis_cli.main import app

runner = CliRunner()


class TestAdminInit:
    def test_init_creates_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "init"])
        assert result.exit_code == 0
        assert (tmp_path / "config" / "config.yaml").exists()
        assert (tmp_path / "data" / "stores").exists()

    def test_init_custom_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        custom = str(tmp_path / "custom")
        result = runner.invoke(app, ["admin", "init", "--data-dir", custom])
        assert result.exit_code == 0
        assert (tmp_path / "custom" / "stores").exists()

    def test_init_no_overwrite_without_force(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "init"])
        assert result.exit_code == 0
        assert "already exists" in result.stdout or "exists" in result.stdout

    def test_init_force_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "init", "--force"])
        assert result.exit_code == 0

    def test_init_json_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "init", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "initialized"


class TestAdminHealth:
    def test_health_uninitialized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "health"])
        assert result.exit_code == 0

    def test_health_after_init(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "health"])
        assert result.exit_code == 0

    def test_health_json_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "health", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["config"] is True
        assert data["data_dir"] is True


class TestAppStructure:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Trellis" in result.stdout

    def test_admin_help(self):
        result = runner.invoke(app, ["admin", "--help"])
        assert result.exit_code == 0
        assert "init" in result.stdout
        assert "health" in result.stdout

    def test_command_groups_exist(self):
        result = runner.invoke(app, ["--help"])
        for group in ["admin", "ingest", "curate", "retrieve", "analyze", "worker"]:
            assert group in result.stdout


_SENTINEL_LLM_CLIENT = object()


def _make_registry(
    *,
    llm_client=_SENTINEL_LLM_CLIENT,
    provider: str | None = "openai",
    model: str | None = "gpt-4o-mini",
):
    """Construct a mock StoreRegistry for check-extractors tests.

    ``llm_client=None`` simulates a non-configurable LLM. Anything
    non-``None`` (the default sentinel) simulates a buildable client.
    """
    reg = MagicMock()
    reg.build_llm_client.return_value = llm_client
    reg._llm_config = {"provider": provider, "model": model}
    reg.graph_store = MagicMock()
    return reg


class TestCheckExtractorsReady:
    @patch("trellis_cli.admin._get_registry")
    def test_ready_exit_zero(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors"])
        assert result.exit_code == 0
        assert "READY" in result.stdout

    @patch("trellis_cli.admin._get_registry")
    def test_ready_json(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ready"
        assert data["exit_code"] == 0
        assert data["llm_client"]["config_buildable"] is True
        assert data["llm_client"]["provider"] == "openai"
        assert data["llm_client"]["model"] == "gpt-4o-mini"
        assert data["llm_client"]["env_fallback_available"] is True
        assert data["feature_flag"]["name"] == "TRELLIS_ENABLE_MEMORY_EXTRACTION"
        assert data["feature_flag"]["set"] is True
        assert data["dependencies"]["alias_resolver"] is True
        assert data["dependencies"]["llm_client"] is True
        assert data["dependencies"]["memory_prompt"] is True
        assert data["warnings"] == []


class TestCheckExtractorsBlocked:
    @patch("trellis_cli.admin._get_registry")
    def test_flag_set_no_llm_anywhere_exits_two(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry(
            llm_client=None, provider=None, model=None
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors"])
        assert result.exit_code == 2
        assert "BLOCKED" in result.stdout

    @patch("trellis_cli.admin._get_registry")
    def test_blocked_json(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry(
            llm_client=None, provider=None, model=None
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "blocked"
        assert data["exit_code"] == 2
        assert data["llm_client"]["config_buildable"] is False
        assert data["llm_client"]["env_fallback_available"] is False
        assert any(w["signal"] == "no_llm_client" for w in data["warnings"])


class TestCheckExtractorsWarn:
    @patch("trellis_cli.admin._get_registry")
    def test_flag_unset_but_llm_buildable(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)
        result = runner.invoke(admin_app, ["check-extractors"])
        assert result.exit_code == 1
        assert "WARN" in result.stdout

    @patch("trellis_cli.admin._get_registry")
    def test_flag_unset_json(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout.strip())
        assert data["status"] == "warn"
        assert data["exit_code"] == 1
        assert any(w["signal"] == "flag_unset" for w in data["warnings"])

    @patch("trellis_cli.admin._get_registry")
    def test_env_fallback_only(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry(
            llm_client=None, provider=None, model=None
        )
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout.strip())
        assert data["status"] == "warn"
        assert any(w["signal"] == "env_fallback_only" for w in data["warnings"])
