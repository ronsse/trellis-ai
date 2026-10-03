"""Tests for ``trellis ingest conversations``."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
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
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.main import app
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


@pytest.fixture
def export(tmp_path: Path) -> Path:
    conversations = [
        {
            "uuid": "c1",
            "name": "Kids and savings",
            "chat_messages": [
                {"sender": "human", "text": "My kids are 7 and 4."},
                {
                    "sender": "assistant",
                    "text": "Noted — planning for two young children.",
                },
            ],
        }
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps(conversations))
    return src


class TestIngestConversations:
    def test_sync_json_output(self, export: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "conversations", str(export), "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "synced"
        assert data["counts"]["ingested"] == 1
        assert data["files"][0]["doc_id"] == "conversation:claude-ai:c1"

    def test_second_run_skips_unchanged(self, export: Path) -> None:
        args = ["ingest", "conversations", str(export), "--format", "json"]
        assert runner.invoke(app, args).exit_code == 0
        data = json.loads(runner.invoke(app, args).stdout.strip())
        assert data["counts"]["skipped_unchanged"] == 1
        assert data["counts"]["ingested"] == 0

    def test_dry_run_reports_plan(self, export: Path) -> None:
        result = runner.invoke(
            app,
            ["ingest", "conversations", str(export), "--dry-run", "--format", "json"],
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip())["status"] == "planned"

    def test_tags_land_in_metadata(self, export: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest",
                "conversations",
                str(export),
                "--domain",
                "personal",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        from trellis_cli.stores import _get_registry, _reset_registry

        _reset_registry()
        doc = _get_registry().knowledge.document_store.get("conversation:claude-ai:c1")
        assert doc["metadata"]["domain"] == "personal"

    def test_missing_path_errors(self) -> None:
        result = runner.invoke(
            app, ["ingest", "conversations", "/nope/missing.json", "--format", "json"]
        )
        assert result.exit_code == 2
        assert json.loads(result.stdout.strip())["status"] == "error"

    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_an_unreadable_root_exits_2_and_says_why(
        self, tmp_path: Path, shape: UnreadablePathShape, fmt: str
    ) -> None:
        root = tmp_path / "in" / "root"
        args = [
            "ingest",
            "conversations",
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

    def test_text_output_mentions_counts(self, export: Path) -> None:
        result = runner.invoke(app, ["ingest", "conversations", str(export)])
        assert result.exit_code == 0
        assert "new=1" in plain(result.stdout)

    @pytest.mark.parametrize(
        "source_system", ["x[/y]", "ob[bold]sidian", "abc\\", "a\\[b]c"]
    )
    def test_source_system_renders_verbatim(
        self, export: Path, source_system: str
    ) -> None:
        # The namespace inside every doc id (``conversation:<source_system>:``).
        # Rich raised on the unmatched ``[/y]`` after the sync had committed,
        # and deleted ``[bold]``. A backslash must survive before ``[b]`` and
        # must not double at the end.
        result = runner.invoke(
            app,
            ["ingest", "conversations", str(export), "--source-system", source_system],
        )
        assert result.exit_code == 0, result.output
        assert f"({source_system})" in " ".join(plain(result.stdout).split())

    def test_failure_message_renders_verbatim(
        self, export: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same shape as ingest corpus: the raw exception inside ``[red]``
        # raised on ``[/x]`` and the reason never reached the operator.
        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "[bold]b[/x]"
            raise RuntimeError(msg)

        monkeypatch.setattr("trellis.ingest_corpus.sync_conversations", _fail)
        result = runner.invoke(app, ["ingest", "conversations", str(export)])
        assert result.exit_code == EXIT_INTERNAL, result.output
        assert "Conversation ingest failed: [bold]b[/x]" in " ".join(
            plain(result.stdout).split()
        )


# --- #633: ``--prune`` needs the whole export ----------------------------------


def _conversation(uuid: str, name: str, *texts: str) -> dict[str, Any]:
    senders = ("human", "assistant")
    return {
        "uuid": uuid,
        "name": name,
        "chat_messages": [
            {"sender": senders[index % 2], "text": text}
            for index, text in enumerate(texts)
        ],
    }


_C1 = (
    "c1",
    "Bread notes",
    "How long should dough proof?",
    "Overnight in the fridge works well.",
)
_C2 = ("c2", "Tide notes", "Why do tides repeat?", "They follow the lunar cycle.")
_C3 = (
    "c3",
    "Heap notes",
    "Where is the minimum in a heap?",
    "At the root of a binary min-heap.",
)
_TITLES = {"c1": _C1[1], "c2": _C2[1], "c3": _C3[1]}


def _doc_id(uuid: str) -> str:
    return f"conversation:claude-ai:{uuid}"


def _write_export(path: Path, conversations: list[Any]) -> Path:
    path.write_text(json.dumps(conversations), encoding="utf-8")
    return path


def _run(path: Path, *args: str) -> Result:
    return runner.invoke(app, ["ingest", "conversations", str(path), *args])


def _seed(tmp_path: Path) -> None:
    seed = _write_export(
        tmp_path / "seed.json", [_conversation(*c) for c in (_C1, _C2, _C3)]
    )
    result = _run(seed, "--format", "json")
    assert result.exit_code == 0, result.output


def _stored(uuid: str) -> bool:
    return _get_registry().knowledge.document_store.get(_doc_id(uuid)) is not None


def _empty_directory(tmp_path: Path) -> Path:
    path = tmp_path / "export-dir"
    path.mkdir()
    return path


def _zip_without_member(tmp_path: Path) -> Path:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("other.json", "[]")
    return path


def _truncated_json(tmp_path: Path) -> Path:
    path = tmp_path / "conversations.json"
    path.write_text('[{"uuid": "c1", ', encoding="utf-8")
    return path


def _not_a_conversation_list(tmp_path: Path) -> Path:
    path = tmp_path / "conversations.json"
    path.write_text("42", encoding="utf-8")
    return path


def _conversation_without_id(tmp_path: Path) -> Path:
    no_id = _conversation("", "No id", "Is anyone there?", "Nobody named.")
    del no_id["uuid"]
    return _write_export(tmp_path / "conversations.json", [_conversation(*_C1), no_id])


#: One export per reader warning that means "this is not the whole export".
#: The third member is the conversations prune could not check.
_INCOMPLETE_EXPORTS = [
    pytest.param(
        "unreadable_export", _empty_directory, ("c1", "c2", "c3"), id="unreadable"
    ),
    pytest.param(
        "export_member_missing",
        _zip_without_member,
        ("c1", "c2", "c3"),
        id="member_missing",
    ),
    pytest.param(
        "malformed_export", _truncated_json, ("c1", "c2", "c3"), id="malformed"
    ),
    pytest.param(
        "unrecognized_export_shape",
        _not_a_conversation_list,
        ("c1", "c2", "c3"),
        id="unrecognized_shape",
    ),
    pytest.param(
        "conversation_missing_id",
        _conversation_without_id,
        ("c2", "c3"),
        id="missing_id",
    ),
]


class TestPruneNeedsTheWholeExport:
    """Prune deletes a conversation only when the export was read whole (#633).

    An export that could not be read, or was read with a conversation the
    reader had to drop, does not say which conversations are gone — so
    prune keeps every candidate, lists it under ``prune_withheld``, and the
    run exits 5 with ``status: "partial"``.
    """

    @pytest.mark.parametrize("output_format", ["json", "text"])
    @pytest.mark.parametrize(("kind", "build", "withheld"), _INCOMPLETE_EXPORTS)
    def test_incomplete_export_withholds_prune(
        self,
        tmp_path: Path,
        kind: str,
        build: Callable[[Path], Path],
        withheld: tuple[str, ...],
        output_format: str,
    ) -> None:
        _seed(tmp_path)
        export_path = build(tmp_path)

        if output_format == "json":
            result = _run(export_path, "--prune", "--format", "json")
            assert result.exit_code == 5, result.output
            data = json.loads(result.stdout.strip())
            assert data["status"] == "partial"
            assert data["pruned"] == []
            assert sorted(e["doc_id"] for e in data["prune_withheld"]) == [
                _doc_id(uuid) for uuid in withheld
            ]
            for entry in data["prune_withheld"]:
                assert kind in entry["detail"]
            assert data["counts"]["prune_withheld"] == len(withheld)
        else:
            result = _run(export_path, "--prune")
            assert result.exit_code == 5, result.output
            text = " ".join(plain(result.stdout).split())
            for uuid in withheld:
                line = f"withheld {_TITLES[uuid]}: export not fully read: {kind}"
                assert line in text, text
        for uuid in ("c1", "c2", "c3"):
            assert _stored(uuid), uuid

    def test_sync_warnings_do_not_make_the_export_incomplete(
        self, tmp_path: Path
    ) -> None:
        # Completeness is the reader's verdict, taken before sync runs. Sync
        # adds warnings of its own (here a near-duplicate); reading those as
        # an incomplete export would withhold a prune that is sound.
        _seed(tmp_path)
        duplicate = _conversation("c4", *_C1[1:])
        export_path = _write_export(
            tmp_path / "conversations.json",
            [_conversation(*_C1), _conversation(*_C3), duplicate],
        )

        result = _run(export_path, "--prune", "--format", "json")

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "synced"
        assert "near_duplicate" in {w["kind"] for w in data["warnings"]}
        assert [e["doc_id"] for e in data["pruned"]] == [_doc_id("c2")]
        assert not _stored("c2")
        assert _stored("c4")

    def test_an_empty_conversation_is_still_in_the_export(self, tmp_path: Path) -> None:
        # An empty conversation yields no record but is not gone, and the
        # reader counts it as read: it neither withholds the prune nor is
        # pruned.
        _seed(tmp_path)
        emptied = {"uuid": "c2", "name": _C2[1], "chat_messages": []}
        export_path = _write_export(
            tmp_path / "conversations.json", [_conversation(*_C1), emptied]
        )

        result = _run(export_path, "--prune", "--format", "json")

        data = json.loads(result.stdout.strip())
        assert [e["doc_id"] for e in data["pruned"]] == [_doc_id("c3")]
        assert data["prune_withheld"] == []
        assert result.exit_code == 0, result.output
        assert data["status"] == "synced"
        assert _stored("c2")
        assert not _stored("c3")

    def test_bracketed_titles_and_paths_render_verbatim(self, tmp_path: Path) -> None:
        # A title and an export path are the caller's text. Rich deletes
        # ``[wip]`` as a style tag and raises on ``[/x]`` — a closing tag
        # with nothing to close — after the sync has committed.
        seed = _write_export(
            tmp_path / "seed.json",
            [
                _conversation("c1", "[wip] Bread notes", *_C1[2:]),
                _conversation("c2", "[/x] Tide notes", *_C2[2:]),
            ],
        )
        assert _run(seed, "--format", "json").exit_code == 0
        # No conversations.json inside, so the reader warns
        # ``unreadable_export`` with a path and detail that both hold ``[/x]``.
        export_dir = tmp_path / "[" / "x]"
        export_dir.mkdir(parents=True)

        result = _run(export_dir, "--prune")

        assert result.exit_code == 5, result.output
        text = " ".join(plain(result.stdout).split())
        assert "withheld [wip] Bread notes: export not fully read" in text, text
        assert "withheld [/x] Tide notes: export not fully read" in text, text
        assert "warning unreadable_export: path=" in text, text
