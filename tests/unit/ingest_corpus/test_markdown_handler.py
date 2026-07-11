"""Markdown handler — frontmatter, wikilinks, malformed input."""

from __future__ import annotations

from trellis.ingest_corpus.handlers import handler_for, supported_extensions
from trellis.ingest_corpus.handlers.markdown import MarkdownHandler

HANDLER = MarkdownHandler()


class TestRegistry:
    def test_markdown_extensions_are_registered(self):
        assert ".md" in supported_extensions()
        assert handler_for("notes/foo.md") is not None
        assert handler_for("notes/FOO.MD") is not None

    def test_unknown_extension_has_no_handler(self):
        assert handler_for("audio.mp3") is None
        assert handler_for("no_extension") is None


class TestFrontmatter:
    def test_frontmatter_becomes_metadata(self):
        text = "---\ntitle: My Note\ntags: [a, b]\n---\n\nBody text.\n"
        metadata, warnings = HANDLER.parse("note.md", text)
        assert warnings == []
        assert metadata["title"] == "My Note"
        assert metadata["tags"] == ["a", "b"]

    def test_no_frontmatter_yields_empty_metadata(self):
        metadata, warnings = HANDLER.parse("note.md", "Just a body.\n")
        assert metadata == {}
        assert warnings == []

    def test_yaml_dates_are_json_safe(self):
        text = "---\ndate: 2026-07-11\nwhen: 2026-07-11 09:00:00\n---\nBody.\n"
        metadata, _ = HANDLER.parse("note.md", text)
        assert metadata["date"] == "2026-07-11"
        assert isinstance(metadata["when"], str)

    def test_reserved_keys_are_namespaced(self):
        text = "---\nsource_path: /evil\nchunk_count: 99\n---\nBody.\n"
        metadata, _ = HANDLER.parse("note.md", text)
        assert "source_path" not in metadata
        assert metadata["frontmatter_source_path"] == "/evil"
        assert metadata["frontmatter_chunk_count"] == 99

    def test_malformed_yaml_warns_and_continues(self):
        text = "---\ntitle: [unclosed\n---\nBody.\n"
        metadata, warnings = HANDLER.parse("note.md", text)
        assert metadata == {}
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "malformed_frontmatter"

    def test_non_mapping_frontmatter_warns(self):
        text = "---\n- just\n- a list\n---\nBody.\n"
        metadata, warnings = HANDLER.parse("note.md", text)
        assert metadata == {}
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "malformed_frontmatter"


class TestWikilinks:
    def test_wikilinks_are_collected_deduped_in_order(self):
        text = "See [[Alpha]], [[Beta|the beta]], [[Alpha]] and [[Gamma#sec]].\n"
        metadata, _ = HANDLER.parse("note.md", text)
        assert metadata["wikilinks"] == ["Alpha", "Beta", "Gamma"]

    def test_no_wikilinks_key_when_none_found(self):
        metadata, _ = HANDLER.parse("note.md", "No links here.\n")
        assert "wikilinks" not in metadata

    def test_wikilinks_in_frontmatter_are_not_body_links(self):
        text = "---\ntitle: t\n---\nBody [[Real Link]].\n"
        metadata, _ = HANDLER.parse("note.md", text)
        assert metadata["wikilinks"] == ["Real Link"]
