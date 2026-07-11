"""Walker determinism, filtering, and single-file support."""

from __future__ import annotations

from pathlib import Path

from trellis.ingest_corpus.walker import walk_corpus

_EXTS = (".md",)


def _make_vault(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "b.md").write_text("b")
    (root / "a.md").write_text("a")
    (root / "sub" / "c.md").write_text("c")
    (root / "notes.txt").write_text("t")
    (root / ".hidden.md").write_text("h")
    (root / ".obsidian" / "config.md").write_text("cfg")


class TestWalk:
    def test_sorted_relpaths_and_unsupported(self, tmp_path: Path):
        _make_vault(tmp_path)
        supported, unsupported = walk_corpus(tmp_path, extensions=_EXTS)
        assert [rel for rel, _ in supported] == ["a.md", "b.md", "sub/c.md"]
        assert unsupported == ["notes.txt"]

    def test_dot_files_and_dirs_are_skipped(self, tmp_path: Path):
        _make_vault(tmp_path)
        supported, unsupported = walk_corpus(tmp_path, extensions=_EXTS)
        names = [rel for rel, _ in supported] + unsupported
        assert not any(".hidden" in n or ".obsidian" in n for n in names)

    def test_walk_is_deterministic(self, tmp_path: Path):
        _make_vault(tmp_path)
        assert walk_corpus(tmp_path, extensions=_EXTS) == walk_corpus(
            tmp_path, extensions=_EXTS
        )

    def test_single_file_root(self, tmp_path: Path):
        target = tmp_path / "solo.md"
        target.write_text("solo")
        supported, unsupported = walk_corpus(target, extensions=_EXTS)
        assert supported == [("solo.md", target)]
        assert unsupported == []


class TestIncludeFilter:
    def test_include_glob_filters_paths(self, tmp_path: Path):
        _make_vault(tmp_path)
        supported, unsupported = walk_corpus(
            tmp_path, include=("sub/*",), extensions=_EXTS
        )
        assert [rel for rel, _ in supported] == ["sub/c.md"]
        assert unsupported == []

    def test_bare_filename_glob_matches_any_depth(self, tmp_path: Path):
        _make_vault(tmp_path)
        supported, _ = walk_corpus(tmp_path, include=("c.md",), extensions=_EXTS)
        assert [rel for rel, _ in supported] == ["sub/c.md"]
