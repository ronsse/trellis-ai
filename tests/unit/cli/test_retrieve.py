"""Tests for retrieve CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.chunk_corpus import seed_chunk_favouring, seed_chunked
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


class TestRetrievePack:
    def test_pack_request(self) -> None:
        result = runner.invoke(
            app,
            ["retrieve", "pack", "--intent", "deploy checklist"],
        )
        assert result.exit_code == 0

    def test_pack_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "pack",
                "--intent",
                "deploy",
                "--domain",
                "platform",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["intent"] == "deploy"
        assert data["status"] == "ok"


class TestRetrieveSearch:
    def test_search(self) -> None:
        result = runner.invoke(app, ["retrieve", "search", "kubernetes"])
        assert result.exit_code == 0

    def test_search_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "search",
                "kubernetes",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["query"] == "kubernetes"
        assert data["status"] == "ok"


class TestRetrieveChunkVisibility:
    """``retrieve search`` / ``retrieve pack`` hand back whole rows (#396).

    Both print a ``doc_id`` per result, so a ``<parent>#chunk-N`` row is a
    fragment of a document the same output already lists. The REST siblings
    (``GET /api/v1/documents``, ``GET /api/v1/search``) exclude chunks by
    default; these did not, and an operator running ``trellis retrieve
    search`` against the reference deployment (56% chunk rows) saw the same
    noise the REST fix removed.
    """

    #: The production id shape, with a source system Rich does not render
    #: as an emoji — see :func:`tests.chunk_corpus.seed_chunked` and #403.
    _ID_PREFIX = "corpus:obsidian"

    @staticmethod
    def _seed(parents: int = 3, per_parent: int = 3) -> list[str]:
        from trellis_cli.stores import get_document_store

        return seed_chunked(
            get_document_store(),
            parents=parents,
            per_parent=per_parent,
            id_prefix=TestRetrieveChunkVisibility._ID_PREFIX,
        )

    @staticmethod
    def _seed_chunk_favouring(parents: int = 25) -> list[str]:
        from trellis_cli.stores import get_document_store

        return seed_chunk_favouring(
            get_document_store(),
            parents=parents,
            id_prefix=TestRetrieveChunkVisibility._ID_PREFIX,
        )

    @staticmethod
    def _json(*args: str) -> dict:
        """Invoke the CLI on the machine-safe path and parse the payload.

        ``--quiet`` is not stylistic: without it the payload prints through
        ``console.print``, and Rich both rewrites ``:name:`` emoji
        shortcodes inside string values and line-wraps at the console
        width, putting literal newlines inside JSON strings so a wide
        payload does not parse at all (#403). Only the *printing* differs
        between the two paths — both run the same
        ``store.search(..., include_chunks=...)`` call — so chunk
        visibility is tested faithfully here while #403 owns the channel.
        """
        result = runner.invoke(app, [*args, "--format", "json", "--quiet"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout.strip())

    def test_search_excludes_chunks_by_default(self) -> None:
        parent_ids = self._seed()
        data = self._json("retrieve", "search", "distinctive")
        assert {r["doc_id"] for r in data["results"]} == set(parent_ids)

    def test_search_include_chunks_opt_in(self) -> None:
        self._seed()
        data = self._json("retrieve", "search", "distinctive", "--include-chunks")
        assert data["count"] == 12
        assert any("#chunk-" in r["doc_id"] for r in data["results"])

    def test_search_limit_is_not_shortened_by_the_chunk_filter(self) -> None:
        """``--limit N`` returns N documents, not N minus the chunks.

        Pins the store-level pushdown from the CLI side: with chunks
        ranking above parents, a post-hoc filter would print *nothing* for
        ``--limit 20`` and the operator would read that as "no matches".
        """
        self._seed_chunk_favouring()

        # Precondition: the fixture really does rank chunks first, so the
        # assertion below tests the pushdown rather than the seed data.
        unfiltered = self._json(
            "retrieve", "search", "distinctive", "--limit", "20", "--include-chunks"
        )
        assert all("#chunk-" in r["doc_id"] for r in unfiltered["results"])

        data = self._json("retrieve", "search", "distinctive", "--limit", "20")
        assert data["count"] == 20
        assert not [r for r in data["results"] if "#chunk-" in r["doc_id"]]

    def test_search_text_output_omits_chunk_ids(self) -> None:
        """The operator's actual invocation: no flags at all.

        Every other assertion here reads ``--format json --quiet``, which is
        the machine path. The default text path renders one id per line and
        is unaffected by #403, but nothing covered it with results in the
        store — the pre-existing ``test_search`` runs against an empty one
        and so proves only an exit code.
        """
        self._seed()
        result = runner.invoke(app, ["retrieve", "search", "distinctive"])
        assert result.exit_code == 0, result.output
        assert "#chunk-" not in result.stdout
        assert "corpus:obsidian:doc0" in result.stdout

    def test_pack_excludes_chunks_by_default(self) -> None:
        parent_ids = self._seed()
        data = self._json("retrieve", "pack", "--intent", "distinctive")
        assert set(data["items"]) == set(parent_ids)

    def test_pack_include_chunks_opt_in(self) -> None:
        self._seed()
        data = self._json(
            "retrieve", "pack", "--intent", "distinctive", "--include-chunks"
        )
        assert data["count"] == 12
        assert any("#chunk-" in item for item in data["items"])


class TestRetrieveTrace:
    def test_trace_not_found(self) -> None:
        result = runner.invoke(app, ["retrieve", "trace", "nonexistent"])
        assert result.exit_code == 1

    def test_trace_not_found_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "trace",
                "nonexistent",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["status"] == "not_found"


class TestRetrieveEntity:
    def test_entity_not_found(self) -> None:
        result = runner.invoke(app, ["retrieve", "entity", "ent_456"])
        assert result.exit_code == 1

    def test_entity_resolves_via_local_alias(self) -> None:
        from trellis_cli.stores import LOCAL_SOURCE_SYSTEM, get_graph_store

        graph = get_graph_store()
        graph.upsert_node("ulid_for_api", "service", {"name": "user-api"})
        graph.upsert_alias(
            entity_id="ulid_for_api",
            source_system=LOCAL_SOURCE_SYSTEM,
            raw_id="user-api",
            raw_name="user-api",
            is_primary=True,
        )

        result = runner.invoke(app, ["retrieve", "entity", "user-api"])
        assert result.exit_code == 0
        assert "service" in result.stdout

    def test_entity_resolves_via_local_alias_json(self) -> None:
        from trellis_cli.stores import LOCAL_SOURCE_SYSTEM, get_graph_store

        graph = get_graph_store()
        graph.upsert_node("ulid_for_api", "service", {"name": "user-api"})
        graph.upsert_alias(
            entity_id="ulid_for_api",
            source_system=LOCAL_SOURCE_SYSTEM,
            raw_id="user-api",
            raw_name="user-api",
            is_primary=True,
        )

        result = runner.invoke(
            app, ["retrieve", "entity", "user-api", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data.get("status") != "not_found"
        assert data["node_type"] == "service"


class TestDocPreview:
    def test_uses_snippet_when_present(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        doc = {"snippet": "short snippet", "content": "long content here"}
        assert _doc_preview(doc, 80) == "short snippet"

    def test_falls_back_to_content_when_snippet_missing(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        assert _doc_preview({"content": "full document"}, 80) == "full document"
        assert _doc_preview({"snippet": "", "content": "fallback"}, 80) == "fallback"
        assert _doc_preview({"snippet": None, "content": "fallback"}, 80) == "fallback"

    def test_returns_empty_when_no_text(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        assert _doc_preview({}, 80) == ""
        assert _doc_preview({"snippet": "", "content": ""}, 80) == ""
        assert _doc_preview({"snippet": None, "content": None}, 80) == ""

    def test_collapses_whitespace_and_newlines(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        doc = {"content": "# Heading\n\nParagraph\twith\ttabs   and   spaces"}
        assert _doc_preview(doc, 80) == "# Heading Paragraph with tabs and spaces"

    def test_truncates_to_width(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        doc = {"content": "abcdefghij" * 20}
        result = _doc_preview(doc, 50)
        assert len(result) == 50
        assert result == "abcdefghij" * 5


class TestRetrievePrecedents:
    def test_precedents(self) -> None:
        result = runner.invoke(app, ["retrieve", "precedents"])
        assert result.exit_code == 0

    def test_precedents_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "precedents",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["count"] == 0


class TestRetrieveFileContext:
    @staticmethod
    def _seed(*, extraction_status: str | None = None) -> None:
        from trellis_cli.stores import get_document_store, get_graph_store

        get_document_store().put(
            "corpus:vault:abc",
            "Gotcha: the pack builder truncates before scoring.",
            metadata={"source_path": "src/trellis/retrieve/pack_builder.py"},
        )
        props: dict[str, str] = {"name": "PackBuilder"}
        if extraction_status is not None:
            props["extraction_status"] = extraction_status
        get_graph_store().upsert_node(
            "ent-packbuilder",
            "concept",
            props,
            document_ids=["corpus:vault:abc"],
        )

    def test_absolute_query_finds_stored_relpath(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "retrieve",
                "file-context",
                "/home/n/projects/trellis-ai/src/trellis/retrieve/pack_builder.py",
            ],
        )
        assert result.exit_code == 0
        assert "corpus:vault:abc" in result.stdout
        assert "PackBuilder" in result.stdout

    def test_json_output_carries_timestamps_for_the_staleness_gate(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "retrieve",
                "file-context",
                "src/trellis/retrieve/pack_builder.py",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["count"] == 1
        (entry,) = data["paths"]
        assert entry["path"] == "src/trellis/retrieve/pack_builder.py"
        assert entry["documents"][0]["doc_id"] == "corpus:vault:abc"
        assert entry["entities"][0]["entity_id"] == "ent-packbuilder"
        assert entry["newest_item_at"] is not None

    def test_unknown_path_is_a_clean_empty_answer(self) -> None:
        result = runner.invoke(
            app,
            ["retrieve", "file-context", "never/ingested.py", "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["paths"] == [
            {
                "path": "never/ingested.py",
                "documents": [],
                "entities": [],
                "newest_item_at": None,
            }
        ]

    def test_unconfirmed_mints_gated_unless_requested(self) -> None:
        self._seed(extraction_status="unconfirmed")
        args = ["retrieve", "file-context", "src/trellis/retrieve/pack_builder.py"]
        assert "PackBuilder" not in runner.invoke(app, args).stdout
        assert (
            "PackBuilder" in runner.invoke(app, [*args, "--include-unconfirmed"]).stdout
        )

    def test_quiet_leaves_long_paths_intact(self) -> None:
        """Rich wraps at 80 columns when piped, splitting absolute paths.

        A ``PreToolUse`` hook parses this output, so the raw writer is
        the one that matters for the command's actual consumer.
        """
        self._seed()
        long_path = (
            "/home/nronsse/projects/trellis-ai/src/trellis/retrieve/pack_builder.py"
        )
        result = runner.invoke(app, ["retrieve", "file-context", long_path, "-q"])
        assert result.exit_code == 0
        assert long_path in result.stdout.splitlines()

    def test_jsonl_emits_one_object_per_path(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "retrieve",
                "file-context",
                "src/trellis/retrieve/pack_builder.py",
                "never/ingested.py",
                "--format",
                "jsonl",
            ],
        )
        assert result.exit_code == 0
        rows = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert [r["path"] for r in rows] == [
            "src/trellis/retrieve/pack_builder.py",
            "never/ingested.py",
        ]
        assert rows[0]["documents"][0]["doc_id"] == "corpus:vault:abc"

    def test_unsupported_format_is_refused_not_silently_degraded(self) -> None:
        result = runner.invoke(
            app,
            ["retrieve", "file-context", "notes/foo.md", "--format", "tsv"],
        )
        assert result.exit_code == EXIT_VALIDATION
        assert "tsv" in result.stdout


class TestRetrieveHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["retrieve", "--help"])
        assert result.exit_code == 0
        for cmd in ["pack", "search", "trace", "entity", "precedents", "file-context"]:
            assert cmd in result.stdout
