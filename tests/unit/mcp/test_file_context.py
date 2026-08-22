"""Tests for the ``get_file_context`` MCP tool (#307, server-side half)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_file_context = unwrap_tool(server_mod.get_file_context)


def _seed_file_memory(registry: StoreRegistry) -> None:
    registry.knowledge.document_store.put(
        "corpus:vault:abc",
        "Gotcha: strategies.py hard-excludes unconfirmed mints.",
        metadata={"source_path": "src/trellis/retrieve/strategies.py"},
    )
    registry.knowledge.graph_store.upsert_node(
        "ent-graphsearch",
        "concept",
        {"name": "GraphSearch", "description": "Graph traversal strategy"},
        document_ids=["corpus:vault:abc"],
    )


class TestGetFileContextTool:
    def test_empty_paths_raises_invalid_params(self) -> None:
        with pytest.raises(McpError, match="paths"):
            get_file_context(paths=[])

    def test_blank_paths_raise_invalid_params(self) -> None:
        with pytest.raises(McpError, match="paths"):
            get_file_context(paths=["", "   "])

    def test_returns_documents_and_linked_entities(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_file_memory(temp_registry)
        result = get_file_context(
            paths=["/home/n/projects/trellis-ai/src/trellis/retrieve/strategies.py"]
        )
        assert "corpus:vault:abc" in result
        assert "GraphSearch" in result
        assert "Newest memory:" in result

    def test_no_context_message_per_path(self, temp_registry: StoreRegistry) -> None:
        result = get_file_context(paths=["never/ingested.md"])
        assert "never/ingested.md" in result
        assert "No stored context for this path." in result

    def test_unconfirmed_mints_gated_by_default(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        temp_registry.knowledge.graph_store.upsert_node(
            "ent-mint",
            "Product",
            {"name": "WhoopBand", "extraction_status": "unconfirmed"},
            document_ids=["doc-1"],
        )
        assert "WhoopBand" not in get_file_context(paths=["notes/foo.md"])
        assert "WhoopBand" in get_file_context(
            paths=["notes/foo.md"], include_unconfirmed=True
        )

    def test_emits_token_usage_telemetry(self, temp_registry: StoreRegistry) -> None:
        _seed_file_memory(temp_registry)
        get_file_context(paths=["src/trellis/retrieve/strategies.py"])
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.TOKEN_TRACKED
        )
        operations = [e.payload.get("operation") for e in events]
        assert "get_file_context" in operations
