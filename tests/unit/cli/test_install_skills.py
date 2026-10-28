"""Tests for skill installation: helpers, package data, and the CLI command."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import force_colour, plain
from trellis.schemas.trace import Trace
from trellis_cli import admin
from trellis_cli.claude_integration import (
    get_skills_target_dir,
    install_skills,
)
from trellis_cli.main import app
from trellis_cli.prompts import agent_rule_text
from trellis_cli.skills import SKILL_NAMES

runner = CliRunner()


# ---------------------------------------------------------------------------
# Package data is readable via importlib.resources (works from a wheel)
# ---------------------------------------------------------------------------


class TestPackageData:
    def test_skill_names_are_four(self):
        assert set(SKILL_NAMES) == {
            "retrieve-before-task",
            "record-after-task",
            "link-evidence",
            "trellis-loop",
        }

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_each_skill_md_readable_as_resource(self, name):
        root = files("trellis_cli.skills")
        text = (root / name / "SKILL.md").read_text()
        # Frontmatter + heading, so the file is a real SKILL.md not a stub.
        assert text.startswith("---")
        assert f"name: {name}" in text

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_frontmatter_keeps_packaged_fields(self, name):
        text = (files("trellis_cli.skills") / name / "SKILL.md").read_text()
        frontmatter = text.split("---")[1]
        fields = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
        assert {"name", "description", "version"} <= set(fields)
        # The skills are the supported path, so they carry no preview marker,
        # in the frontmatter or as a banner an agent would read as "in flux".
        assert "status" not in fields
        assert "preview" not in text.lower()
        # Hosts that list skills truncate or refuse a longer description.
        assert len(fields["description"]) < 200

    def test_record_after_task_example_traces_validate(self):
        # An agent copies the example. Trace validation is all-or-nothing,
        # so an example the schema rejects teaches every reader to lose
        # the trace.
        text = (
            files("trellis_cli.skills") / "record-after-task" / "SKILL.md"
        ).read_text()
        examples = re.findall(
            r"save_experience\(trace_json='(\{.*?\})'\)", text, flags=re.DOTALL
        )
        assert examples
        for example in examples:
            Trace.model_validate_json(example)


# ---------------------------------------------------------------------------
# get_skills_target_dir
# ---------------------------------------------------------------------------


class TestGetSkillsTargetDir:
    def test_user_scope(self):
        assert get_skills_target_dir("user") == Path.home() / ".claude" / "skills"

    def test_project_scope_with_dir(self, tmp_path):
        assert (
            get_skills_target_dir("project", project_dir=tmp_path)
            == tmp_path / ".claude" / "skills"
        )

    def test_project_scope_defaults_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_skills_target_dir("project") == tmp_path / ".claude" / "skills"

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="unknown skills scope"):
            get_skills_target_dir("bogus")


# ---------------------------------------------------------------------------
# install_skills helper
# ---------------------------------------------------------------------------


class TestInstallSkillsHelper:
    def test_fresh_install_copies_all(self, tmp_path):
        target = tmp_path / "skills"
        results = install_skills(target)
        assert {r["status"] for r in results} == {"installed"}
        for name in SKILL_NAMES:
            assert (target / name / "SKILL.md").is_file()

    def test_idempotent_skips_existing(self, tmp_path):
        target = tmp_path / "skills"
        install_skills(target)
        results = install_skills(target)
        assert {r["status"] for r in results} == {"skipped"}

    def test_force_overwrites(self, tmp_path):
        target = tmp_path / "skills"
        install_skills(target)
        # Corrupt one skill to prove force re-copies the real content.
        victim = target / "retrieve-before-task" / "SKILL.md"
        victim.write_text("stale")
        results = install_skills(target, force=True)
        assert {r["status"] for r in results} == {"overwritten"}
        assert victim.read_text() != "stale"

    def test_partial_install_only_fills_missing(self, tmp_path):
        target = tmp_path / "skills"
        install_skills(target)
        # Remove one skill; a re-run should install just that one.
        import shutil

        shutil.rmtree(target / "link-evidence")
        results = install_skills(target)
        by_name = {r["name"]: r["status"] for r in results}
        assert by_name["link-evidence"] == "installed"
        assert by_name["retrieve-before-task"] == "skipped"

    def test_creates_target_dir(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "skills"
        install_skills(target)
        assert target.is_dir()


# ---------------------------------------------------------------------------
# `trellis admin install-skills` CLI command
# ---------------------------------------------------------------------------


class TestInstallSkillsCommand:
    @pytest.fixture(autouse=True)
    def _setup_env(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def test_user_json_output(self):
        result = runner.invoke(
            app, ["admin", "install-skills", "user", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["scope"] == "user"
        assert len(data["skills"]) == 4
        assert {s["status"] for s in data["skills"]} == {"installed"}
        skills_dir = self.tmp / "home" / ".claude" / "skills"
        assert data["skills_dir"] == str(skills_dir)
        assert (skills_dir / "retrieve-before-task" / "SKILL.md").exists()

    def test_default_scope_is_user(self):
        result = runner.invoke(app, ["admin", "install-skills", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["scope"] == "user"

    def test_project_scope(self, monkeypatch):
        project_dir = self.tmp / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        result = runner.invoke(
            app, ["admin", "install-skills", "project", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["skills_dir"] == str(project_dir / ".claude" / "skills")
        assert (
            project_dir / ".claude" / "skills" / "record-after-task" / "SKILL.md"
        ).exists()

    def test_idempotent_then_force(self):
        runner.invoke(app, ["admin", "install-skills", "user"])
        result = runner.invoke(
            app, ["admin", "install-skills", "user", "--format", "json"]
        )
        data = json.loads(result.stdout.strip())
        assert {s["status"] for s in data["skills"]} == {"skipped"}

        forced = runner.invoke(
            app, ["admin", "install-skills", "user", "--force", "--format", "json"]
        )
        fdata = json.loads(forced.stdout.strip())
        assert {s["status"] for s in fdata["skills"]} == {"overwritten"}

    def test_text_output(self):
        result = runner.invoke(app, ["admin", "install-skills", "user"])
        assert result.exit_code == 0
        assert "Skills install complete" in result.stdout
        assert "link-evidence" in result.stdout

    def test_json_carries_the_agent_rule(self):
        """An orchestrator splices ``agent_rule`` into a subagent's brief."""
        result = runner.invoke(
            app, ["admin", "install-skills", "user", "--format", "json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip())["agent_rule"] == agent_rule_text()

    @pytest.mark.parametrize("colour", [False, True])
    def test_text_output_ends_with_the_rule_and_the_pointer(self, monkeypatch, colour):
        """The paragraph is pasted, so it must arrive as shipped.

        It holds ``[...]`` placeholders that Rich deletes as markup and
        lines that it folds at the console width; the colour pass proves
        neither happens when the run is coloured either.
        """
        if colour:
            force_colour(monkeypatch, admin)
        monkeypatch.setenv("COLUMNS", "40")
        result = runner.invoke(app, ["admin", "install-skills", "user"])
        assert result.exit_code == 0
        assert plain(result.stdout).endswith(
            agent_rule_text()
            + "\n\nSystem prompt for API-key and non-Claude-Code clients: "
            "trellis admin show-prompt\n"
        )

    def _fail_every_copy(self, monkeypatch):
        def raiser(_src, _dest):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr("trellis_cli.claude_integration.shutil.copytree", raiser)

    def test_a_failed_copy_prints_no_rule_in_text(self, monkeypatch):
        """Text promises the rule only after a clean install."""
        self._fail_every_copy(monkeypatch)
        result = runner.invoke(app, ["admin", "install-skills", "user"])
        assert result.exit_code == 1, result.stdout
        assert "show-prompt" not in result.stdout
        assert agent_rule_text() not in plain(result.stdout)

    def test_a_failed_copy_keeps_the_json_key(self, monkeypatch):
        """JSON keeps its shape, so a consumer never branches on the key."""
        self._fail_every_copy(monkeypatch)
        result = runner.invoke(
            app, ["admin", "install-skills", "user", "--format", "json"]
        )
        assert result.exit_code == 1, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["status"] == "partial"
        assert data["agent_rule"] == agent_rule_text()

    def test_invalid_scope(self):
        result = runner.invoke(
            app, ["admin", "install-skills", "nope", "--format", "json"]
        )
        assert result.exit_code == 2
        assert json.loads(result.stdout) == {
            "status": "error",
            "error": "scope must be 'user' or 'project', got 'nope'",
        }

    def test_invalid_scope_text_keeps_its_brackets(self):
        """The refusal quotes the value as typed, brackets included.

        Rich reads a ``[word]`` as a style tag and deletes it unless the
        message is escaped, which printed ``got ''``. Comparing the whole
        line also pins the rendered ``Error:`` prefix, so printing the
        markup literally fails too.
        """
        result = runner.invoke(app, ["admin", "install-skills", "[user]"])
        assert result.exit_code == 2
        assert (
            plain(result.stdout).strip()
            == "Error: scope must be 'user' or 'project', got '[user]'"
        )

    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_failed_copy_error_keeps_its_brackets(self, monkeypatch, output_format):
        error = OSError(13, "Permission denied", "/srv/demo/[work]/skills")

        def raiser(_src, _dest):
            raise error

        monkeypatch.setattr("trellis_cli.claude_integration.shutil.copytree", raiser)

        result = runner.invoke(
            app, ["admin", "install-skills", "user", "--format", output_format]
        )

        assert result.exit_code == 1, result.stdout
        assert str(error) in plain(result.stdout)
