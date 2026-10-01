"""Tests for :mod:`trellis.core.project` — which project a session belongs to.

Every repository here is a hand-built ``.git`` layout under ``tmp_path`` with
a synthetic name, mirroring what ``git worktree add`` writes (a ``gitdir:``
file, then a ``commondir`` of ``../..``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trellis.core.path_presence import path_is_present
from trellis.core.project import PROJECT_ENV, resolve_project


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _linked_worktree(common: Path, worktree: Path, *, relative: bool = False) -> Path:
    """A linked worktree of the repository whose git directory is ``common``."""
    gitdir = common / "worktrees" / worktree.name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree.mkdir(parents=True)
    target = os.path.relpath(gitdir, worktree) if relative else str(gitdir)
    (worktree / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    return worktree


def _resolve_from(path: Path, monkeypatch: pytest.MonkeyPatch) -> str | None:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(path)
    return resolve_project()


class TestRepositoryName:
    def test_names_the_repository_from_a_nested_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, "alpha-repo")
        assert _resolve_from(repo / "src" / "pkg", monkeypatch) == "alpha-repo"

    @pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
    def test_a_linked_worktree_reports_its_main_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
    ) -> None:
        main = _repo(tmp_path, "alpha-repo")
        worktree = _linked_worktree(
            main / ".git", tmp_path / "elsewhere" / "wt-7-checkout", relative=relative
        )
        assert _resolve_from(worktree / "src", monkeypatch) == "alpha-repo"

    def test_a_bare_repositorys_worktree_drops_the_git_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        common = tmp_path / "beta.git"
        common.mkdir()
        worktree = _linked_worktree(common, tmp_path / "w1")
        assert _resolve_from(worktree, monkeypatch) == "beta"

    def test_no_repository_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if any(path_is_present(d / ".git") for d in (tmp_path, *tmp_path.parents)):
            pytest.skip("tmp_path sits inside a git repository on this host")
        assert _resolve_from(tmp_path / "plain" / "dir", monkeypatch) is None


class TestOverride:
    def test_the_override_wins_over_the_repository_and_is_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, "alpha-repo")
        monkeypatch.setenv(PROJECT_ENV, " gamma ")
        assert _resolve_from(repo, monkeypatch) == "gamma"

    def test_a_blank_override_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, "alpha-repo")
        monkeypatch.setenv(PROJECT_ENV, "   ")
        assert _resolve_from(repo, monkeypatch) == "alpha-repo"


class TestFailSoft:
    @pytest.mark.parametrize("kind", ["symlink-loop", "malformed", "dangling"])
    def test_a_broken_dotgit_answers_none_not_the_enclosing_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        sub = _repo(tmp_path, "alpha-repo") / "vendor" / "sub"
        sub.mkdir(parents=True)
        dotgit = sub / ".git"
        if kind == "symlink-loop":
            dotgit.symlink_to(dotgit)
        elif kind == "malformed":
            dotgit.write_text("not a gitdir line\n", encoding="utf-8")
        else:
            gone = tmp_path / "gone" / ".git" / "worktrees" / "sub"
            dotgit.write_text(f"gitdir: {gone}\n", encoding="utf-8")
        assert _resolve_from(sub, monkeypatch) is None

    def test_a_deleted_working_directory_answers_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doomed = tmp_path / "doomed"
        doomed.mkdir()
        monkeypatch.chdir(doomed)
        doomed.rmdir()
        assert resolve_project() is None


def test_resolved_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _repo(tmp_path, "alpha-repo")
    second = _repo(tmp_path, "beta-repo")
    assert _resolve_from(first, monkeypatch) == "alpha-repo"

    monkeypatch.chdir(second)
    assert resolve_project() == "alpha-repo"

    resolve_project.cache_clear()
    assert resolve_project() == "beta-repo"
