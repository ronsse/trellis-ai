"""Tests for retrieve CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

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


class TestRetrieveHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["retrieve", "--help"])
        assert result.exit_code == 0
        for cmd in ["pack", "search", "trace", "entity", "precedents"]:
            assert cmd in result.stdout
