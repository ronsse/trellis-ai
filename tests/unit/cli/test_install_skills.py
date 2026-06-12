"""Tests for skill installation: helpers, package data, and the CLI command."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.claude_integration import (
    get_skills_target_dir,
    install_skills,
)
from trellis_cli.main import app
from trellis_cli.skills import SKILL_NAMES

runner = CliRunner()


# ---------------------------------------------------------------------------
# Package data is readable via importlib.resources (works from a wheel)
# ---------------------------------------------------------------------------


class TestPackageData:
    def test_skill_names_are_three(self):
        assert set(SKILL_NAMES) == {
            "retrieve-before-task",
            "record-after-task",
            "link-evidence",
        }

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_each_skill_md_readable_as_resource(self, name):
        root = files("trellis_cli.skills")
        text = (root / name / "SKILL.md").read_text()
        # Frontmatter + heading, so the file is a real SKILL.md not a stub.
        assert text.startswith("---")
        assert f"name: {name}" in text


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
        assert len(data["skills"]) == 3
        assert {s["status"] for s in data["skills"]} == {"installed"}
        skills_dir = self.tmp / "home" / ".claude" / "skills"
        assert data["skills_dir"] == str(skills_dir)
        assert (skills_dir / "retrieve-before-task" / "SKILL.md").exists()

    def test_default_scope_is_user(self):
        result = runner.invoke(
            app, ["admin", "install-skills", "--format", "json"]
        )
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

    def test_invalid_scope(self):
        result = runner.invoke(
            app, ["admin", "install-skills", "nope", "--format", "json"]
        )
        assert result.exit_code != 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "error"
