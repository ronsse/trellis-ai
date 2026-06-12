"""Trellis tools for LangGraph agents — REFERENCE TEMPLATE.

STATUS: PREVIEW — in flux while parallel SDK/store work lands. Expect
breaking changes to the tool signatures before the next minor release.

This file is shipped as a copy-paste starting point, not as an installable
module. Drop it into your own project (e.g. ``myproject/trellis_tools.py``)
and import from there:

    from myproject.trellis_tools import create_trellis_tools

    # The SDK is HTTP-only — pass the base_url of a running trellis-api server.
    tools = create_trellis_tools(base_url="http://127.0.0.1:8420")
    agent = create_react_agent(model, tools)

Provides LangGraph-compatible tool functions that wrap the Trellis SDK,
giving agents structured memory: traces, precedents, knowledge graph,
and context retrieval.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from trellis_sdk import TrellisClient
from trellis_sdk.skills import get_context_for_task, get_recent_activity

_client: TrellisClient | None = None


def _get_client(base_url: str | None = None) -> TrellisClient:
    """Get or create a module-level TrellisClient."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = TrellisClient(base_url=base_url)
    return _client


def create_trellis_tools(
    base_url: str | None = None,
) -> list[Any]:
    """Create LangGraph-compatible Trellis tools.

    Args:
        base_url: REST API URL of a running ``trellis-api`` server. The SDK
            is HTTP-only, so this must point at a live server.

    Returns:
        List of tool functions for use with LangGraph agents.
    """
    client = TrellisClient(base_url=base_url)

    @tool
    def trellis_get_context(
        intent: str,
        domain: str = "",
        max_tokens: int = 2000,
    ) -> str:
        """Search the experience graph for relevant context before starting a task.

        Use this before beginning non-trivial work to find prior art,
        precedents, and relevant knowledge.

        Args:
            intent: What you're trying to do or learn about.
            domain: Optional domain scope (e.g., "backend", "platform").
            max_tokens: Maximum response size in tokens.
        """
        return get_context_for_task(
            client,
            intent,
            domain=domain or None,
            max_tokens=max_tokens,
        )

    @tool
    def trellis_search(
        query: str,
        domain: str = "",
        limit: int = 10,
    ) -> str:
        """Search the experience graph for documents and entities.

        Use for targeted queries when you know what you're looking for.

        Args:
            query: Search query.
            domain: Optional domain filter.
            limit: Maximum results.
        """
        results = client.search(query, domain=domain or None, limit=limit)
        if not results:
            return f"No results found for: {query}"

        lines = [f"# Search Results: {query}", ""]
        for r in results[:limit]:
            doc_id = r.get("doc_id", "unknown")
            content = r.get("content", "")[:200]
            lines.append(f"- **{doc_id}**: {content}")
        return "\n".join(lines)

    @tool
    def trellis_save_trace(trace_json: str) -> str:
        """Save an experience trace recording what you did and what happened.

        Call this after completing meaningful work to build institutional memory.

        Args:
            trace_json: JSON string with keys: source, intent, steps, outcome, context.
                Example: {"source": "agent", "intent": "what you did",
                         "steps": [{"step_type": "tool_call", "name": "...",
                         "result": {...}}],
                         "outcome": {"status": "success", "summary": "..."},
                         "context": {"domain": "backend"}}
        """
        try:
            trace = json.loads(trace_json)
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON — {e}"

        try:
            trace_id = client.ingest_trace(trace)
        except Exception as e:
            return f"Error: Failed to save trace — {e}"

        return f"Trace saved: {trace_id}"

    @tool
    def trellis_save_knowledge(
        name: str,
        entity_type: str = "concept",
        properties_json: str = "{}",
    ) -> str:
        """Create an entity in the knowledge graph.

        Use when you discover or create important entities (services,
        concepts, patterns) that should be tracked.

        Args:
            name: Entity name.
            entity_type: Type (e.g., "concept", "service", "person", "system").
            properties_json: Optional JSON string of additional properties.
        """
        try:
            properties = json.loads(properties_json)
        except json.JSONDecodeError:
            properties = {}

        entity_id = client.create_entity(
            name, entity_type=entity_type, properties=properties
        )
        return f"Entity created: {entity_id} ({entity_type}: {name})"

    @tool
    def trellis_recent_activity(
        domain: str = "",
        limit: int = 10,
        max_tokens: int = 1500,
    ) -> str:
        """Get a summary of recent activity in the experience graph.

        Useful for understanding what has been happening recently.

        Args:
            domain: Optional domain filter.
            limit: Maximum traces to include.
            max_tokens: Maximum response size in tokens.
        """
        return get_recent_activity(
            client,
            domain=domain or None,
            limit=limit,
            max_tokens=max_tokens,
        )

    return [
        trellis_get_context,
        trellis_search,
        trellis_save_trace,
        trellis_save_knowledge,
        trellis_recent_activity,
    ]
