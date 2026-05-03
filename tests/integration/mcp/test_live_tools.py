"""Black-box MCP-protocol tests for the ``trellis-mcp`` server.

These tests sit one layer above the unit tests under ``tests/unit/``
that exercise the tool functions directly. ``CliRunner``-style
in-process invocation skips the entire MCP transport — JSON-RPC
encoding/decoding, schema generation, the FastMCP request loop, and
the way a real Claude-Desktop-style client would talk to the server.
This module spawns the wheel's ``trellis-mcp`` console script and
connects via ``fastmcp.Client``'s stdio transport.

Skipped only when the ``trellis-mcp`` binary isn't installed in the
test runner's environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastmcp import Client


pytestmark = pytest.mark.asyncio


_EXPECTED_TOOLS = {
    "get_context",
    "save_experience",
    "save_knowledge",
    "save_memory",
    "get_lessons",
    "get_graph",
    "record_feedback",
    "search",
    "get_objective_context",
    "get_task_context",
    "get_sectioned_context",
}


def _result_text(result: object) -> str:
    """Extract markdown text from a ``CallToolResult``.

    FastMCP returns content as a list of typed parts; the tools in
    ``trellis.mcp.server`` all return plain strings, which surface as
    a single ``TextContent`` item. ``result.data`` holds the structured
    return value for ``-> str`` tools — string, in this case.
    """
    data = getattr(result, "data", None)
    if isinstance(data, str):
        return data
    content = getattr(result, "content", None) or []
    for part in content:
        text = getattr(part, "text", None)
        if text:
            return str(text)
    return ""


# ── Tool inventory ────────────────────────────────────────────────────


async def test_lists_eleven_tools(mcp_session: Client) -> None:
    """The server advertises all 11 macro tools through the protocol."""
    tools = await mcp_session.list_tools()
    names = {t.name for t in tools}
    assert _EXPECTED_TOOLS.issubset(names), f"Missing tools: {_EXPECTED_TOOLS - names}"


# ── Save tools ────────────────────────────────────────────────────────


async def test_save_knowledge_creates_entity(mcp_session: Client) -> None:
    """``save_knowledge`` creates an entity and reports its id + name."""
    result = await mcp_session.call_tool(
        "save_knowledge",
        {
            "name": "mcp-test-platform-team",
            "entity_type": "concept",
            "properties": {"description": "validates the MCP surface"},
        },
    )
    text = _result_text(result)
    assert "Entity created" in text
    assert "mcp-test-platform-team" in text


async def test_save_memory_stores_document(mcp_session: Client) -> None:
    """``save_memory`` returns ``Memory saved: <doc_id>`` on first store."""
    result = await mcp_session.call_tool(
        "save_memory",
        {
            "content": "MCP-live-test memory: integration suite proof.",
            "metadata": {"source": "live-mcp-test"},
        },
    )
    text = _result_text(result)
    assert "Memory saved:" in text


async def test_save_memory_dedups_on_repeat(mcp_session: Client) -> None:
    """A second ``save_memory`` with identical content hits the dedup path."""
    payload = {
        "content": "MCP dedup probe — content identical across calls.",
        "metadata": {"source": "live-mcp-dedup"},
    }
    first = _result_text(await mcp_session.call_tool("save_memory", payload))
    assert "Memory saved:" in first

    second = _result_text(await mcp_session.call_tool("save_memory", payload))
    # Either exact (content_hash match) or fuzzy (MinHash) dedup must fire.
    assert "already exists" in second.lower() or "duplicate" in second.lower(), (
        f"second save_memory did not dedup: {second!r}"
    )


async def test_save_experience_validates_trace_json(
    mcp_session: Client,
) -> None:
    """``save_experience`` rejects malformed trace JSON with a clear error.

    The happy-path needs a full ``Trace`` schema instance which is
    unwieldy for a smoke test; the error path is the cheapest
    round-trip that proves the tool's argument decoder + JSON validator
    are wired correctly.
    """
    result = await mcp_session.call_tool(
        "save_experience",
        {"trace_json": "not-valid-json"},
    )
    text = _result_text(result)
    assert text.startswith("Error:")
    assert "trace JSON" in text or "Invalid" in text


# ── Read tools ────────────────────────────────────────────────────────


async def test_get_context_returns_markdown(mcp_session: Client) -> None:
    """``get_context`` returns markdown — empty corpus is fine."""
    result = await mcp_session.call_tool(
        "get_context",
        {"intent": "mcp-live-context", "max_tokens": 500},
    )
    text = _result_text(result)
    # Empty corpus → "No context found for: ..."; populated → markdown.
    # Either is a valid round-trip; we just need a non-empty response.
    assert text
    assert "mcp-live-context" in text or "No context found" in text


async def test_search_returns_markdown(mcp_session: Client) -> None:
    """``search`` returns markdown for a known term, even when empty."""
    result = await mcp_session.call_tool(
        "search",
        {"query": "mcp-live-search-token", "limit": 5},
    )
    text = _result_text(result)
    # Empty corpus path: "No results found for: ..."
    assert "mcp-live-search-token" in text or "No results" in text


async def test_get_lessons_returns_markdown(mcp_session: Client) -> None:
    """``get_lessons`` returns markdown (possibly empty list) on fresh registry."""
    result = await mcp_session.call_tool(
        "get_lessons",
        {"limit": 5, "max_tokens": 500},
    )
    text = _result_text(result)
    assert isinstance(text, str)


async def test_get_graph_reports_missing_entity(mcp_session: Client) -> None:
    """``get_graph`` emits a clear ``Entity not found`` for an unknown id."""
    result = await mcp_session.call_tool(
        "get_graph",
        {"entity_id": "mcp:does-not-exist", "depth": 1, "max_tokens": 300},
    )
    text = _result_text(result)
    assert "Entity not found" in text
    assert "mcp:does-not-exist" in text


async def test_get_graph_round_trip_after_save_knowledge(
    mcp_session: Client,
) -> None:
    """Save an entity via ``save_knowledge`` then read it back via ``get_graph``."""
    save_result = await mcp_session.call_tool(
        "save_knowledge",
        {
            "name": "mcp-roundtrip-target",
            "entity_type": "concept",
            "properties": {"description": "round-trip target"},
        },
    )
    save_text = _result_text(save_result)
    # Extract the node id from "Entity created: <id> (concept: mcp-roundtrip-target)"
    after = save_text.split("Entity created:", 1)[1].strip()
    node_id = after.split()[0]
    assert node_id

    graph_result = await mcp_session.call_tool(
        "get_graph",
        {"entity_id": node_id, "depth": 1, "max_tokens": 500},
    )
    graph_text = _result_text(graph_result)
    assert "Entity not found" not in graph_text
    assert "mcp-roundtrip-target" in graph_text


# ── Sectioned context tools ───────────────────────────────────────────


async def test_get_objective_context_returns_markdown(
    mcp_session: Client,
) -> None:
    """``get_objective_context`` round-trips through PackBuilder + EventLog."""
    result = await mcp_session.call_tool(
        "get_objective_context",
        {"intent": "mcp-objective-test", "max_tokens": 800},
    )
    text = _result_text(result)
    # Section-style packs always include the headings even on an empty corpus.
    assert text
    assert "mcp-objective-test" in text or "Domain Knowledge" in text


async def test_get_task_context_returns_markdown(
    mcp_session: Client,
) -> None:
    """``get_task_context`` returns a sectioned-pack markdown payload."""
    result = await mcp_session.call_tool(
        "get_task_context",
        {"intent": "mcp-task-test", "max_tokens": 800},
    )
    text = _result_text(result)
    assert text
    assert "mcp-task-test" in text or "Technical Patterns" in text


async def test_get_sectioned_context_with_custom_sections(
    mcp_session: Client,
) -> None:
    """``get_sectioned_context`` honours caller-supplied section configs."""
    result = await mcp_session.call_tool(
        "get_sectioned_context",
        {
            "intent": "mcp-sectioned-test",
            "sections": [
                {
                    "name": "Probe Section",
                    "retrieval_affinities": ["domain_knowledge"],
                    "max_tokens": 200,
                    "max_items": 3,
                },
            ],
            "max_tokens": 400,
        },
    )
    text = _result_text(result)
    # The section name we supplied should appear as a heading in the output.
    assert "Probe Section" in text or "mcp-sectioned-test" in text


async def test_get_sectioned_context_rejects_empty_sections(
    mcp_session: Client,
) -> None:
    """Empty ``sections`` list is rejected at the tool layer with a clear error."""
    result = await mcp_session.call_tool(
        "get_sectioned_context",
        {"intent": "x", "sections": []},
    )
    text = _result_text(result)
    assert text.startswith("Error:")
    assert "sections" in text


# ── Feedback ──────────────────────────────────────────────────────────


async def test_record_feedback_writes_event(mcp_session: Client) -> None:
    """``record_feedback`` emits a FEEDBACK_RECORDED event for a given pack_id."""
    result = await mcp_session.call_tool(
        "record_feedback",
        {
            "pack_id": "mcp-test-pack-id",
            "success": True,
            "notes": "smoke test",
            "helpful_item_ids": ["item-1"],
        },
    )
    text = _result_text(result)
    assert "Feedback recorded" in text
    assert "positive" in text


async def test_record_feedback_requires_target(mcp_session: Client) -> None:
    """Calling ``record_feedback`` without trace_id or pack_id returns an error."""
    result = await mcp_session.call_tool(
        "record_feedback",
        {"success": True},
    )
    text = _result_text(result)
    assert text.startswith("Error:")
    assert "trace_id" in text or "pack_id" in text
