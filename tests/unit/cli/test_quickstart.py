"""Tests for ``trellis admin quickstart``."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis_cli import admin
from trellis_cli.main import app
from trellis_cli.output import build_console

runner = CliRunner()

# Hand-written rather than imported from the command: these are what Claude
# Code's ``claude mcp add`` accepts, verified against a scratch HOME. ``-e``
# is variadic, so the order is name, ``-e KEY=val``, ``--``, command.
ROOT_REGISTER = [
    "claude",
    "mcp",
    "add",
    "--scope",
    "user",
    "trellis",
    "--",
    "trellis-mcp",
]


def _project_register(project_dir: Path) -> list[str]:
    return [
        "claude",
        "mcp",
        "add",
        "--scope",
        "local",
        "trellis",
        "-e",
        f"TRELLIS_CONFIG_DIR={project_dir / '.trellis'}",
        "--",
        "trellis-mcp",
    ]


class TestQuickstart:
    @pytest.fixture(autouse=True)
    def _setup_env(self, tmp_path, monkeypatch):
        """Redirect all paths to tmp_path so we never touch real config."""
        self.tmp = tmp_path
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "trellis-config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "trellis-data"))
        # Redirect HOME so nothing can land in the real ~/.claude
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def _enter_project(self, monkeypatch) -> Path:
        """Chdir into a project whose path holds a space and a ``[word]``.

        Those are the two ways a printed path stops pasting: a space needs
        shell quoting, and Rich reads ``[draft]`` as a style tag and deletes
        it. With a plain name the tests cannot tell quoted output from
        unquoted, or markup on from off.
        """
        project_dir = self.tmp / "my project [draft]"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        return project_dir

    def test_fresh_quickstart(self):
        result = runner.invoke(app, ["admin", "quickstart"])
        assert result.exit_code == 0
        assert "Quickstart complete" in result.stdout

        # Stores initialized
        assert (self.tmp / "trellis-config" / "config.yaml").exists()
        assert (self.tmp / "trellis-data" / "stores").exists()

    def test_idempotent_run(self):
        runner.invoke(app, ["admin", "quickstart"])
        result = runner.invoke(app, ["admin", "quickstart"])
        assert result.exit_code == 0
        assert "already" in result.stdout.lower()

    @pytest.mark.parametrize("colour", [False, True], ids=["plain", "colour"])
    def test_missing_mcp_hint_keeps_its_extra(self, monkeypatch, colour):
        """Rich read ``[dev]`` as a markup tag, so the hint said ``-e "."``."""
        monkeypatch.setattr(admin.shutil, "which", lambda *_args, **_kwargs: None)
        if colour:
            force_colour(monkeypatch, admin)

        result = runner.invoke(app, ["admin", "quickstart"])

        assert result.exit_code == 0, result.output
        text = assert_coloured(result.stdout) if colour else plain(result.stdout)
        assert 'Run: uv pip install -e ".[dev]"' in " ".join(text.split())

    @pytest.mark.parametrize("scope_args", [[], ["--scope", "project"]])
    def test_rerun_leaves_config_yaml_byte_identical(self, monkeypatch, scope_args):
        """A re-run never rewrites an existing ``config.yaml``.

        The store registry keeps its plane blocks in the same file, and
        ``TrellisConfig.save`` rewrites the whole file with only the CLI's
        keys, so a rewrite would drop a configured backend and the registry
        would come up on its local defaults without an error.
        """
        project_dir = self._enter_project(monkeypatch)
        args = ["admin", "quickstart", *scope_args, "--format", "json"]
        assert runner.invoke(app, args).exit_code == 0
        if scope_args:
            config_path = project_dir / ".trellis" / "config.yaml"
        else:
            config_path = self.tmp / "trellis-config" / "config.yaml"
        plane_block = b"knowledge:\n  graph:\n    backend: neo4j\n"
        edited = config_path.read_bytes() + plane_block
        config_path.write_bytes(edited)

        result = runner.invoke(app, args)
        assert result.exit_code == 0
        steps = json.loads(result.stdout.strip())["steps"]
        assert steps[0] == "stores_already_initialized"
        assert config_path.read_bytes() == edited

    @pytest.mark.parametrize("scope_args", [[], ["--scope", "project"]])
    def test_writes_no_claude_settings(self, monkeypatch, scope_args):
        """Claude Code reads no ``mcpServers`` from either settings file.

        Both scopes used to merge an entry into one of them and report
        ``MCP server registered`` — an entry nothing loads. Now neither
        file is written, and the summary asks the user to register rather
        than claiming it did.
        """
        project_dir = self._enter_project(monkeypatch)

        result = runner.invoke(app, ["admin", "quickstart", *scope_args])
        assert result.exit_code == 0

        assert not (self.tmp / "home" / ".claude").exists()
        assert not (project_dir / ".claude").exists()
        output = plain(result.output)
        assert "Register the MCP server with Claude Code (run once):" in output
        assert "registered" not in output.lower()

    @pytest.mark.parametrize("earlier_entry", [False, True])
    @pytest.mark.parametrize("force_args", [[], ["--force"]])
    @pytest.mark.parametrize("scope_args", [[], ["--scope", "project"]])
    def test_leaves_existing_settings_byte_identical(
        self, monkeypatch, scope_args, force_args, earlier_entry
    ):
        """Each scope leaves the settings file it used to write untouched.

        With no ``trellis`` entry the old code merged one in; with the entry
        an earlier version wrote, it skipped unless ``--force``. Both
        fixtures are needed to catch both, and the earlier entry stays: it
        is inert, and the user's to delete.
        """
        if scope_args:
            claude_dir = self._enter_project(monkeypatch) / ".claude"
            settings_path = claude_dir / "settings.local.json"
        else:
            settings_path = self.tmp / "home" / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        servers = {"other-server": {"command": "other", "args": ["-v"]}}
        if earlier_entry:
            servers["trellis"] = {"command": "trellis-mcp", "args": []}
        # Compact, several unrelated keys, no trailing newline: any
        # re-serialisation changes the bytes, not only an added entry.
        original = json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
                "theme": "dark",
                "mcpServers": servers,
            },
            separators=(",", ":"),
        ).encode()
        settings_path.write_bytes(original)

        result = runner.invoke(app, ["admin", "quickstart", *scope_args, *force_args])
        assert result.exit_code == 0

        assert settings_path.read_bytes() == original

    def test_json_output(self):
        result = runner.invoke(app, ["admin", "quickstart", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["scope"] == "root"
        # ``steps`` records actions taken; quickstart registers nothing.
        assert data["steps"] == ["stores_initialized"]
        assert data["mcp_register_command"] == ROOT_REGISTER
        assert "settings_path" not in data

        rerun = runner.invoke(app, ["admin", "quickstart", "--format", "json"])
        assert rerun.exit_code == 0
        assert json.loads(rerun.stdout.strip())["steps"] == [
            "stores_already_initialized"
        ]

    def test_json_output_project_scope(self, monkeypatch):
        project_dir = self._enter_project(monkeypatch)

        result = runner.invoke(
            app, ["admin", "quickstart", "--scope", "project", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["steps"] == ["stores_initialized", "gitignore_created"]
        assert data["mcp_register_command"] == _project_register(project_dir)
        assert "settings_path" not in data

    def test_project_scope(self, monkeypatch):
        """Project stores land in ``./.trellis/`` and nowhere else."""
        project_dir = self._enter_project(monkeypatch)
        # Wide enough that the summary's path line is not folded.
        monkeypatch.setattr(admin, "console", build_console(width=500, height=25))

        result = runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        assert result.exit_code == 0

        config_path = project_dir / ".trellis" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        # Absolute and pinned: config.yaml's data_dir overrides a global
        # TRELLIS_DATA_DIR (set by this fixture) in StoreRegistry.
        assert config["data_dir"] == str(project_dir / ".trellis" / "data")
        assert (project_dir / ".trellis" / "data" / "stores").is_dir()
        assert not (self.tmp / "trellis-config").exists()
        assert not (self.tmp / "trellis-data").exists()
        assert f"Stores initialized: {config_path}" in plain(result.output)

    def test_project_scope_after_root(self, monkeypatch):
        """A global install does not make a project look initialized."""
        assert runner.invoke(app, ["admin", "quickstart"]).exit_code == 0
        project_dir = self._enter_project(monkeypatch)

        result = runner.invoke(
            app, ["admin", "quickstart", "--scope", "project", "--format", "json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip())["steps"][0] == "stores_initialized"
        assert (project_dir / ".trellis" / "config.yaml").is_file()

    @pytest.mark.parametrize("scope", ["root", "project"])
    def test_register_command_is_pasteable_when_narrow(self, monkeypatch, scope):
        """The printed command is one unbroken line at any terminal width.

        Rich folds at the console width, and a command split across lines
        does not paste. The project path holds a space and a ``[word]``, so
        this also pins the shell quoting and the markup-off print.
        ``height`` is passed only so Rich honours ``width`` on a forced dumb
        terminal, where it otherwise reports 80 columns.
        """
        project_dir = self._enter_project(monkeypatch)
        monkeypatch.setattr(admin, "console", build_console(width=40, height=25))
        expected = ROOT_REGISTER if scope == "root" else _project_register(project_dir)

        result = runner.invoke(app, ["admin", "quickstart", "--scope", scope])
        assert result.exit_code == 0

        assert shlex.join(expected) in plain(result.output)

    def test_project_scope_creates_gitignore(self, monkeypatch):
        project_dir = self._enter_project(monkeypatch)

        runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        gitignore = project_dir / ".gitignore"
        assert gitignore.exists()
        assert ".trellis/" in gitignore.read_text()

    def test_project_scope_appends_to_gitignore(self, monkeypatch):
        project_dir = self._enter_project(monkeypatch)
        gitignore = project_dir / ".gitignore"
        gitignore.write_text("node_modules/\n")

        runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        lines = gitignore.read_text().splitlines()
        assert "node_modules/" in lines
        assert ".trellis/" in lines

    def test_project_scope_no_duplicate_gitignore(self, monkeypatch):
        project_dir = self._enter_project(monkeypatch)
        gitignore = project_dir / ".gitignore"
        gitignore.write_text(".trellis/\n")

        runner.invoke(app, ["admin", "quickstart", "--scope", "project"])
        assert gitignore.read_text().count(".trellis/") == 1

    def test_no_skills_by_default(self):
        """Skills are not installed unless --with-skills is passed."""
        result = runner.invoke(app, ["admin", "quickstart", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert "skills" not in data
        assert "skills_installed" not in data["steps"]
        assert not (self.tmp / "home" / ".claude" / "skills").exists()

    def test_with_skills_user(self):
        result = runner.invoke(
            app,
            ["admin", "quickstart", "--with-skills", "user", "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert "skills_installed" in data["steps"]
        statuses = {s["name"]: s["status"] for s in data["skills"]}
        assert statuses == {
            "retrieve-before-task": "installed",
            "record-after-task": "installed",
            "link-evidence": "installed",
        }
        skills_dir = self.tmp / "home" / ".claude" / "skills"
        for name in statuses:
            assert (skills_dir / name / "SKILL.md").exists()

    def test_with_skills_project(self, monkeypatch):
        project_dir = self._enter_project(monkeypatch)

        result = runner.invoke(
            app,
            [
                "admin",
                "quickstart",
                "--scope",
                "project",
                "--with-skills",
                "project",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["skills_dir"] == str(project_dir / ".claude" / "skills")
        assert (
            project_dir / ".claude" / "skills" / "retrieve-before-task" / "SKILL.md"
        ).exists()

    def test_with_skills_text_output(self):
        result = runner.invoke(app, ["admin", "quickstart", "--with-skills", "user"])
        assert result.exit_code == 0
        assert "Skills installed to:" in result.stdout
        assert "retrieve-before-task" in result.stdout

    def test_with_skills_invalid_scope(self):
        result = runner.invoke(
            app,
            ["admin", "quickstart", "--with-skills", "bogus", "--format", "json"],
        )
        assert result.exit_code == 2
        assert json.loads(result.stdout) == {
            "status": "error",
            "error": "--with-skills must be 'user' or 'project', got 'bogus'",
        }

    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_unknown_scope_exits_before_writing(self, monkeypatch, output_format):
        """A ``--scope`` other than ``root`` or ``project`` writes nothing.

        Only ``project`` was special-cased, so a typo ran the root setup:
        it initialized the global config and data dirs and exited 0. The
        value holds a ``[word]``, which Rich reads as a style tag and
        deletes unless the text arm escapes it.
        """
        project_dir = self._enter_project(monkeypatch)
        result = runner.invoke(
            app,
            ["admin", "quickstart", "--scope", "[project]", "--format", output_format],
        )
        msg = "--scope must be 'root' or 'project', got '[project]'"
        assert result.exit_code == 2
        if output_format == "json":
            assert json.loads(result.stdout) == {"status": "error", "error": msg}
        else:
            assert plain(result.stdout).strip() == f"Error: {msg}"
        assert not (self.tmp / "trellis-config").exists()
        assert not (self.tmp / "trellis-data").exists()
        assert not (project_dir / ".trellis").exists()
