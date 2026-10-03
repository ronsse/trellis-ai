"""Tests for ``trellis ingest corpus``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unreadable_paths import (
    UNREADABLE_PATH_IDS,
    UNREADABLE_PATH_SHAPES,
    UnreadablePathShape,
    unreadable,
)
from trellis.ingest_corpus.handlers import supported_extensions
from trellis.ingest_corpus.models import corpus_doc_id
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.main import app
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("---\ntitle: T\n---\n\nBody [[Link]].\n")
    (root / "other.txt").write_text("plain\n")
    return root


class TestIngestCorpus:
    def test_sync_json_output(self, vault: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest",
                "corpus",
                str(vault),
                "--source-system",
                "obsidian",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "synced"
        assert data["counts"]["ingested"] == 1
        assert data["counts"]["skipped_unsupported"] == 1
        assert data["files"][0]["doc_id"].startswith("corpus:obsidian:")

    def test_second_run_skips_unchanged(self, vault: Path) -> None:
        args = ["ingest", "corpus", str(vault), "--format", "json"]
        assert runner.invoke(app, args).exit_code == 0
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["counts"]["skipped_unchanged"] == 1
        assert data["counts"]["ingested"] == 0

    def test_dry_run_reports_plan(self, vault: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "planned"
        assert data["dry_run"] is True
        follow_up = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--dry-run", "--format", "json"]
        )
        # Dry runs never write: the plan is identical the second time.
        assert json.loads(follow_up.stdout.strip())["counts"]["ingested"] == 1

    def test_tags_and_domain_land_in_metadata(self, vault: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest",
                "corpus",
                str(vault),
                "--domain",
                "ops",
                "--tag",
                "team=core",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        from trellis_cli.stores import _get_registry, _reset_registry

        _reset_registry()
        doc_id = json.loads(result.stdout.strip())["files"][0]["doc_id"]
        stored = _get_registry().knowledge.document_store.get(doc_id)
        assert stored["metadata"]["domain"] == "ops"
        assert stored["metadata"]["team"] == "core"

    def test_invalid_tag_exits_with_validation_code(self, vault: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--tag", "notkv", "--format", "json"]
        )
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "error"

    def test_missing_path_errors(self) -> None:
        result = runner.invoke(
            app, ["ingest", "corpus", "/nope/missing", "--format", "json"]
        )
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "error"

    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_an_unreadable_root_exits_2_and_says_why(
        self, tmp_path: Path, shape: UnreadablePathShape, fmt: str
    ) -> None:
        root = tmp_path / "in" / "root"
        args = [
            "ingest",
            "corpus",
            str(root),
            *(["--format", "json"] if fmt == "json" else []),
        ]
        with unreadable(shape, root):
            result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        if fmt == "json":
            data = json.loads(result.stdout.strip())
            assert data["status"] == "error"
            assert shape.message_fragment in data["message"]
        else:
            assert shape.message_fragment in " ".join(plain(result.output).split())

    def test_text_output_mentions_counts(self, vault: Path) -> None:
        result = runner.invoke(app, ["ingest", "corpus", str(vault)])
        assert result.exit_code == 0
        assert "new=1" in plain(result.stdout)

    @pytest.mark.parametrize(
        "source_system", ["x[/y]", "ob[bold]sidian", "abc\\", "a\\[b]c"]
    )
    def test_source_system_renders_verbatim(
        self, vault: Path, source_system: str
    ) -> None:
        # The namespace inside every doc id (``corpus:<source_system>:``), so
        # the operator re-types it to re-sync. Rich raised on the unmatched
        # ``[/y]`` after the sync had committed, and deleted ``[bold]``. A
        # backslash must survive before ``[b]`` and must not double at the end.
        result = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--source-system", source_system]
        )
        assert result.exit_code == 0, result.output
        assert f"({source_system})" in _text(result)

    @pytest.mark.parametrize("raw", ["x[/y]", "k[bold]v"])
    def test_invalid_tag_renders_verbatim(self, vault: Path, raw: str) -> None:
        # The text arm of test_invalid_tag_exits_with_validation_code: Rich
        # raised on ``[/y]``, so it exited 1 rather than 2, and ate ``[bold]``.
        result = runner.invoke(app, ["ingest", "corpus", str(vault), "--tag", raw])
        assert result.exit_code == 2, result.output
        assert f"Invalid --tag {raw!r}: expected k=v" in _text(result)

    def test_failure_message_renders_verbatim(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The text arm printed the raw exception inside ``[red]``, so an
        # unmatched ``[/x]`` in its message raised out of the ``except`` arm
        # and the operator got a traceback in place of the reason.
        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "[bold]b[/x]"
            raise RuntimeError(msg)

        monkeypatch.setattr("trellis.ingest_corpus.sync_corpus", _fail)
        result = runner.invoke(app, ["ingest", "corpus", str(vault)])
        assert result.exit_code == EXIT_INTERNAL, result.output
        assert "Corpus ingest failed: [bold]b[/x]" in _text(result)


class TestIngestCorpusHelp:
    """``--help`` states the input contract (ADR §8, #257)."""

    def test_help_states_input_contract(self) -> None:
        result = runner.invoke(app, ["ingest", "corpus", "--help"])
        assert result.exit_code == 0
        # Whitespace-normalised so a phrase Rich wraps still matches.
        text = " ".join(plain(result.stdout).split())
        # Read from the registry, not typed here: registering a handler
        # without stating its extension in the help fails this test.
        extensions = supported_extensions()
        assert extensions
        for ext in extensions:
            # Whole token: ".mdx" in the help must not satisfy ".md".
            token = rf"(?<!\w){re.escape(ext)}(?!\w)"
            assert re.search(token, text), f"{ext!r} missing from corpus --help"
        assert "unsupported" in text
        assert "trellis ingest conversations" in text


# --- #633: ``--prune`` deletes only what it saw vanish ----------------------

#: Five small documents in two directories plus the root, so a test can
#: break one directory and still have documents elsewhere that must survive.
_TREE = {
    "notes/a.md": "Sourdough wants a long cold proof.\n",
    "notes/b.md": "Tide tables repeat on a lunar cycle.\n",
    "sub/c.md": "A binary heap keeps its minimum at the root.\n",
    "sub/d.md": "Granite is an igneous rock that cooled slowly underground.\n",
    "top.md": "Morse code spells SOS as three dots, three dashes, three dots.\n",
}


def _shape(shape_id: str) -> UnreadablePathShape:
    return next(shape for shape in UNREADABLE_PATH_SHAPES if shape.id == shape_id)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    for relpath, text in _TREE.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _ingest(*args: str) -> Result:
    return runner.invoke(app, ["ingest", "corpus", *args, "--source-system", "t"])


def _ingest_json(*args: str) -> tuple[int, dict[str, Any]]:
    result = _ingest(*args, "--format", "json")
    return result.exit_code, json.loads(result.stdout.strip())


def _stored(relpath: str) -> bool:
    store = _get_registry().knowledge.document_store
    return store.get(corpus_doc_id("t", relpath)) is not None


def _text(result: Result) -> str:
    """Rendered output as the operator reads it, whitespace-normalised."""
    return " ".join(plain(result.stdout).split())


def _warned(data: dict[str, Any]) -> set[tuple[str, Any]]:
    return {(warning["kind"], warning.get("path")) for warning in data["warnings"]}


class TestPruneFailsClosed:
    """A path prune cannot check is kept and reported, never deleted (#633).

    Prune used to delete every document whose file the walk did not yield,
    and the walk silently skips what it cannot read — so a directory that
    lost its permissions, a clobbered path component or a symlink loop
    deleted the documents it hid. Each candidate is now checked against the
    filesystem: only a path that is *verifiably* gone is pruned; one that
    cannot be checked is listed under ``prune_withheld`` and the run exits
    5 with ``status: "partial"``.
    """

    @pytest.mark.parametrize("output_format", ["json", "text"])
    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_unverifiable_source_is_withheld_not_pruned(
        self, corpus: Path, shape: UnreadablePathShape, output_format: str
    ) -> None:
        assert _ingest_json(str(corpus))[0] == 0
        (corpus / "notes" / "b.md").unlink()
        # A loop breaks only its own path. The other two shapes break the
        # directory, so the sibling in it cannot be checked either.
        if shape.id == "symlink_loop":
            expected = {"sub/c.md"}
        else:
            expected = {"sub/c.md", "sub/d.md"}
        args = [str(corpus), "--prune"]
        if output_format == "json":
            args += ["--format", "json"]

        with unreadable(shape, corpus / "sub" / "c.md"):
            result = _ingest(*args)

        assert result.exit_code == 5, result.output
        if output_format == "json":
            data = json.loads(result.stdout.strip())
            assert data["status"] == "partial"
            assert [e["source_path"] for e in data["pruned"]] == ["notes/b.md"]
            assert [e["doc_id"] for e in data["pruned"]] == [
                corpus_doc_id("t", "notes/b.md")
            ]
            assert {e["source_path"] for e in data["prune_withheld"]} == expected
            for entry in data["prune_withheld"]:
                # The OS's own reason, so the operator can act on it.
                assert shape.message_fragment in entry["detail"]
            assert data["counts"]["prune_withheld"] == len(expected)
            assert data["counts"]["pruned"] == 1
        else:
            text = _text(result)
            assert re.search(r"withheld sub/c\.md", text), text
            assert shape.message_fragment in text
            assert f"withheld={len(expected)}" in text
            assert re.search(r"prune notes/b\.md", text), text
        assert not _stored("notes/b.md")
        for relpath in ("notes/a.md", "sub/c.md", "sub/d.md", "top.md"):
            assert _stored(relpath), relpath

    def test_include_filter_does_not_make_unmatched_files_vanish(
        self, corpus: Path
    ) -> None:
        assert _ingest_json(str(corpus))[0] == 0
        (corpus / "notes" / "b.md").unlink()

        exit_code, data = _ingest_json(str(corpus), "--include", "notes/*", "--prune")

        assert [e["source_path"] for e in data["pruned"]] == ["notes/b.md"]
        assert data["prune_withheld"] == []
        assert exit_code == 0
        assert data["status"] == "synced"
        for relpath in ("notes/a.md", "sub/c.md", "sub/d.md", "top.md"):
            assert _stored(relpath), relpath

    def test_symlinked_directory_is_not_read_as_vanished(
        self, corpus: Path, tmp_path: Path
    ) -> None:
        assert _ingest_json(str(corpus))[0] == 0
        elsewhere = tmp_path / "sub-elsewhere"
        (corpus / "sub").rename(elsewhere)
        (corpus / "sub").symlink_to(elsewhere, target_is_directory=True)
        (elsewhere / "e.md").write_text("Basalt forms when lava cools fast.\n")

        exit_code, data = _ingest_json(str(corpus), "--prune")

        assert data["pruned"] == []
        assert data["prune_withheld"] == []
        assert exit_code == 0
        assert _stored("sub/c.md")
        assert _stored("sub/d.md")
        # The walk still does not follow the link: that contract is unchanged.
        assert not _stored("sub/e.md")

    def test_dangling_symlink_is_kept_with_an_unreadable_file_warning(
        self, corpus: Path, tmp_path: Path
    ) -> None:
        assert _ingest_json(str(corpus))[0] == 0
        link = corpus / "sub" / "c.md"
        link.unlink()
        link.symlink_to(tmp_path / "nowhere.md")

        exit_code, data = _ingest_json(str(corpus), "--prune")

        assert data["pruned"] == []
        assert data["prune_withheld"] == []
        assert exit_code == 0
        assert _stored("sub/c.md")
        assert ("unreadable_file", "sub/c.md") in _warned(data)

    def test_unreadable_root_withholds_every_document(self, corpus: Path) -> None:
        assert _ingest_json(str(corpus))[0] == 0
        unsearchable = _shape("unsearchable_parent")

        with unreadable(unsearchable, corpus / "top.md"):
            exit_code, data = _ingest_json(str(corpus), "--prune")

        assert data["pruned"] == []
        assert exit_code == 5
        assert data["status"] == "partial"
        assert sorted(e["source_path"] for e in data["prune_withheld"]) == sorted(_TREE)
        for entry in data["prune_withheld"]:
            assert unsearchable.message_fragment in entry["detail"]
        assert ("unreadable_directory", ".") in _warned(data)
        for relpath in _TREE:
            assert _stored(relpath), relpath

    def test_unreadable_directories_are_reported_not_silently_skipped(
        self, corpus: Path
    ) -> None:
        unsearchable = _shape("unsearchable_parent")

        with (
            unreadable(unsearchable, corpus / "sub" / "c.md"),
            unreadable(unsearchable, corpus / "notes" / "deep" / "e.md"),
        ):
            exit_code, data = _ingest_json(str(corpus))

        assert exit_code == 0
        assert data["status"] == "synced"
        skipped = [w for w in data["warnings"] if w["kind"] == "unreadable_directory"]
        assert {w["path"] for w in skipped} == {"sub", "notes/deep"}
        for warning in skipped:
            assert unsearchable.message_fragment in warning["detail"]
        assert data["counts"]["ingested"] == 3

    def test_dry_run_reports_withheld_documents_with_exit_5(self, corpus: Path) -> None:
        assert _ingest_json(str(corpus))[0] == 0
        (corpus / "notes" / "b.md").unlink()

        with unreadable(_shape("symlink_loop"), corpus / "sub" / "c.md"):
            exit_code, data = _ingest_json(str(corpus), "--dry-run", "--prune")

        assert exit_code == 5
        assert data["status"] == "partial"
        assert data["dry_run"] is True
        assert [e["source_path"] for e in data["pruned"]] == ["notes/b.md"]
        assert [e["source_path"] for e in data["prune_withheld"]] == ["sub/c.md"]
        for relpath in _TREE:
            assert _stored(relpath), relpath

    def test_bracketed_paths_render_verbatim(self, corpus: Path) -> None:
        # ``[wip]`` is a style tag Rich deletes; ``[/x]`` is a closing tag
        # with nothing to close, which Rich raises on.
        (corpus / "notes" / "[wip].md").write_text("Quartz outlasts feldspar.\n")
        (corpus / "notes" / "[").mkdir()
        (corpus / "notes" / "[" / "x].md").write_text("Owls turn their heads far.\n")
        assert _ingest_json(str(corpus))[0] == 0
        loop = _shape("symlink_loop")

        with (
            unreadable(loop, corpus / "notes" / "[wip].md"),
            unreadable(loop, corpus / "notes" / "[" / "x].md"),
        ):
            result = _ingest(str(corpus), "--prune")

        assert result.exit_code == 5, result.output
        text = _text(result)
        assert re.search(r"withheld\s+notes/\[wip\]\.md", text), text
        assert "path=notes/[/x].md" in text

    def test_single_file_root_withholds_the_rest_of_the_corpus(
        self, corpus: Path
    ) -> None:
        assert _ingest_json(str(corpus))[0] == 0

        exit_code, data = _ingest_json(str(corpus / "top.md"), "--prune")

        assert data["pruned"] == []
        assert exit_code == 5
        assert data["status"] == "partial"
        assert sorted(e["source_path"] for e in data["prune_withheld"]) == sorted(
            set(_TREE) - {"top.md"}
        )
        for entry in data["prune_withheld"]:
            assert "root is not a directory" in entry["detail"]
        for relpath in _TREE:
            assert _stored(relpath), relpath
