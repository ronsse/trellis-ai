"""Tests for quickstart command and Claude integration utilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.claude_integration import (
    get_claude_settings_path,
    merge_mcp_server,
    read_claude_settings,
    write_claude_settings,
)
from trellis_cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Unit tests for claude_integration.py
# ---------------------------------------------------------------------------


class TestGetClaudeSettingsPath:
    def test_root_scope(self):
        path = get_claude_settings_path("root")
        assert path == Path.home() / ".claude" / "settings.json"

    def test_project_scope(self, tmp_path):
        path = get_claude_settings_path("project", project_dir=tmp_path)
        assert path == tmp_path / ".claude" / "settings.local.json"

    def test_project_scope_requires_dir(self):
        with pytest.raises(ValueError, match="project_dir is required"):
            get_claude_settings_path("project")


class TestReadClaudeSettings:
    def test_missing_file(self, tmp_path):
        assert read_claude_settings(tmp_path / "nope.json") == {}

    def test_empty_file(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text("")
        assert read_claude_settings(f) == {}

    def test_valid_json(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text('{"mcpServers": {"foo": {}}}')
        assert read_claude_settings(f) == {"mcpServers": {"foo": {}}}


class TestMergeMcpServer:
    def test_adds_to_empty(self):
        settings, changed = merge_mcp_server({}, "trellis", {"command": "trellis-mcp"})
        assert changed is True
        assert settings["mcpServers"]["trellis"] == {"command": "trellis-mcp"}

    def test_adds_alongside_existing(self):
        settings = {"mcpServers": {"other": {"command": "other-cmd"}}}
        settings, changed = merge_mcp_server(
            settings, "trellis", {"command": "trellis-mcp"}
        )
        assert changed is True
        assert "other" in settings["mcpServers"]
        assert "trellis" in settings["mcpServers"]

    def test_skips_if_present(self):
        settings = {"mcpServers": {"trellis": {"command": "old"}}}
        settings, changed = merge_mcp_server(
            settings, "trellis", {"command": "trellis-mcp"}
        )
        assert changed is False
        assert settings["mcpServers"]["trellis"]["command"] == "old"

    def test_force_overwrites(self):
        settings = {"mcpServers": {"trellis": {"command": "old"}}}
        settings, changed = merge_mcp_server(
            settings, "trellis", {"command": "trellis-mcp"}, force=True
        )
        assert changed is True
        assert settings["mcpServers"]["trellis"]["command"] == "trellis-mcp"

    def test_preserves_non_mcp_keys(self):
        settings = {"apiKey": "secret", "mcpServers": {}}
        settings, _ = merge_mcp_server(settings, "trellis", {"command": "trellis-mcp"})
        assert settings["apiKey"] == "secret"


class TestWriteClaudeSettings:
    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "settings.json"
        write_claude_settings(path, {"mcpServers": {}})
        assert path.exists()
        assert json.loads(path.read_text()) == {"mcpServers": {}}

    def test_file_ends_with_newline(self, tmp_path):
        path = tmp_path / "settings.json"
        write_claude_settings(path, {})
        assert path.read_text().endswith("\n")


# ---------------------------------------------------------------------------
# Integration tests for `trellis admin quickstart`
# ---------------------------------------------------------------------------


class TestQuickstart:
    @pytest.fixture(autouse=True)
    def _setup_env(self, tmp_path, monkeypatch):
        """Redirect all paths to tmp_path so we never touch real config."""
        self.tmp = tmp_path
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "trellis-config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "trellis-data"))
        # Redirect HOME so Claude settings go to tmp_path
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def test_fresh_quickstart(self):
        result = runner.invoke(app, ["admin", "quickstart"])
        assert result.exit_code == 0
        assert "Quickstart complete" in result.stdout

        # Stores initialized
        assert (self.tmp / "trellis-config" / "config.yaml").exists()
        assert (self.tmp / "trellis-data" / "stores").exists()

        # Claude settings written
        settings_path = self.tmp / "home" / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "trellis" in settings["mcpServers"]
        assert settings["mcpServers"]["trellis"]["command"] == "trellis-mcp"

    def test_idempotent_run(self):
        runner.invoke(app, ["admin", "quickstart"])
        result = runner.invoke(app, ["admin", "quickstart"])
        assert result.exit_code == 0
        assert "already" in result.stdout.lower()

    def test_force_overwrites_mcp(self):
        # First run
        runner.invoke(app, ["admin", "quickstart"])

        # Modify the entry
        settings_path = self.tmp / "home" / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["mcpServers"]["trellis"]["command"] = "old-cmd"
        settings_path.write_text(json.dumps(settings))

        # Force run
        result = runner.invoke(app, ["admin", "quickstart", "--force"])
        assert result.exit_code == 0

        settings = json.loads(settings_path.read_text())
        assert settings["mcpServers"]["trellis"]["command"] == "trellis-mcp"

    def test_json_output(self):
        result = runner.invoke(app, ["admin", "quickstart", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["scope"] == "root"
        assert "stores_initialized" in data["steps"]
        assert "mcp_registered" in data["steps"]

    def test_project_scope(self, monkeypatch):
        project_dir = self.tmp / "myproject"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        result = runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        assert result.exit_code == 0

        # Settings written to project-local path
        local_settings = project_dir / ".claude" / "settings.local.json"
        assert local_settings.exists()
        settings = json.loads(local_settings.read_text())
        entry = settings["mcpServers"]["trellis"]
        assert "env" in entry
        assert "TRELLIS_CONFIG_DIR" in entry["env"]

    def test_project_scope_creates_gitignore(self, monkeypatch):
        project_dir = self.tmp / "myproject"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        gitignore = project_dir / ".gitignore"
        assert gitignore.exists()
        assert ".trellis/" in gitignore.read_text()

    def test_project_scope_appends_to_gitignore(self, monkeypatch):
        project_dir = self.tmp / "myproject"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        gitignore = project_dir / ".gitignore"
        gitignore.write_text("node_modules/\n")

        runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        lines = gitignore.read_text().splitlines()
        assert "node_modules/" in lines
        assert ".trellis/" in lines

    def test_project_scope_no_duplicate_gitignore(self, monkeypatch):
        project_dir = self.tmp / "myproject"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        gitignore = project_dir / ".gitignore"
        gitignore.write_text(".trellis/\n")

        runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        assert gitignore.read_text().count(".trellis/") == 1

    def test_preserves_existing_mcp_servers(self):
        settings_path = self.tmp / "home" / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps({"mcpServers": {"other-server": {"command": "other"}}})
        )

        result = runner.invoke(app, ["admin", "quickstart"])
        assert result.exit_code == 0

        settings = json.loads(settings_path.read_text())
        assert "other-server" in settings["mcpServers"]
        assert "trellis" in settings["mcpServers"]

    def test_with_vectors_missing_lancedb(self, monkeypatch):
        # Make lancedb unimportable
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "lancedb":
                msg = "no lancedb"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        result = runner.invoke(app, ["admin", "quickstart", "--with-vectors"])
        assert result.exit_code == 1
        assert "lancedb" in result.stdout.lower()
