"""``save_memory(summary=, uri=)`` and ``get_items`` for reference documents (#1250).

Text here is invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
get_items = unwrap_tool(server_mod.get_items)

BODY = "The kiln handbook opens here. " + ("Firing schedules go on at length. " * 600)
SUMMARY = "Invented handbook for the quasar kiln: schedules and safety checks."
URI = "https://docs.example.test/kiln/handbook"


def _saved_id(reply: str) -> str:
    return reply.split("Memory saved: ", maxsplit=1)[1].split(maxsplit=1)[0]


def _count(registry: StoreRegistry) -> int:
    return registry.knowledge.document_store.count(include_chunks=True)


class TestSaveMemory:
    def test_stores_both_keys_and_stamps_the_form(
        self, temp_registry: StoreRegistry
    ) -> None:
        assert len(BODY) > 20_000 - 5_000  # a long body, kept whole
        reply = save_memory(BODY, summary=SUMMARY, uri=URI)
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["content"] == BODY
        assert doc["metadata"]["summary"] == SUMMARY
        assert doc["metadata"]["uri"] == URI
        assert doc["metadata"]["document_form"] == "reference"

    def test_a_named_form_is_not_overwritten(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory(BODY, summary=SUMMARY, metadata={"document_form": "note"})
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["document_form"] == "note"
        assert doc["metadata"]["summary"] == SUMMARY

    def test_a_uri_alone_does_not_make_a_reference(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory("a plain note about kilns", uri=URI)
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["uri"] == URI
        assert "document_form" not in doc["metadata"]

    def test_a_parameter_that_agrees_with_metadata_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory(
            BODY, summary=SUMMARY, uri=URI, metadata={"summary": SUMMARY, "uri": URI}
        )
        assert "Memory saved" in reply

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            (
                {"summary": SUMMARY, "metadata": {"summary": "another summary"}},
                "summary",
            ),
            ({"uri": URI, "metadata": {"uri": "https://other.example.test/"}}, "uri"),
        ],
    )
    def test_a_parameter_and_a_metadata_key_that_disagree_are_refused(
        self, temp_registry: StoreRegistry, kwargs: dict[str, Any], field: str
    ) -> None:
        with pytest.raises(MCPError, match=field):
            save_memory(BODY, **kwargs)
        assert _count(temp_registry) == 0

    @pytest.mark.parametrize(
        ("summary", "match"),
        [
            ("   ", "empty"),
            ("", "empty"),
            ("s" * (EMBED_INPUT_CHAR_CAP + 1), str(EMBED_INPUT_CHAR_CAP)),
        ],
    )
    def test_an_unusable_summary_is_refused_and_nothing_is_written(
        self, temp_registry: StoreRegistry, summary: str, match: str
    ) -> None:
        with pytest.raises(MCPError, match=match):
            save_memory(BODY, summary=summary)
        assert _count(temp_registry) == 0

    def test_a_summary_inside_metadata_is_judged_too(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(MCPError, match="string"):
            save_memory(BODY, metadata={"document_form": "reference", "summary": 7})
        assert _count(temp_registry) == 0

    def test_without_the_new_parameters_nothing_changes(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory("a plain note", metadata={"summary": 7, "topic": "kilns"})
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["summary"] == 7
        assert "document_form" not in doc["metadata"]


class TestGetItems:
    def test_returns_the_full_body_and_the_link(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply_id = _saved_id(save_memory(BODY, summary=SUMMARY, uri=URI))
        reply = get_items([reply_id], max_tokens=50_000)
        assert BODY.strip()[:200] in reply
        assert "Firing schedules go on at length." in reply
        assert f"Source: `{URI}`" in reply
        assert reply.index("Source:") < reply.index("Firing schedules")

    def test_a_document_that_merely_has_a_uri_renders_as_before(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-uri", BODY, {"uri": URI, "summary": SUMMARY}
        )
        reply = get_items(["doc-uri"], max_tokens=50_000)
        assert "Firing schedules go on at length." in reply
        assert "Source:" not in reply

    def test_a_hostile_uri_does_not_break_the_markdown(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-hostile",
            BODY,
            {
                "document_form": "reference",
                "summary": SUMMARY,
                "uri": "x`\n\n## injected heading\n`y",
            },
        )
        reply = get_items(["doc-hostile"], max_tokens=50_000)
        source_lines = [ln for ln in reply.splitlines() if ln.startswith("Source:")]
        assert len(source_lines) == 1
        assert source_lines[0].count("`") == 2
        assert "\n## injected heading" not in reply
