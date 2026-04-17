"""Trellis tools for LangGraph agents.

Provides LangGraph-compatible tool functions that wrap the XPG SDK,
giving agents structured memory: traces, precedents, knowledge graph,
and context retrieval.

Usage:
    from integrations.langgraph.tools import create_xpg_tools

    tools = create_xpg_tools()  # local mode
    tools = create_xpg_tools(base_url="http://localhost:8420")  # remote

    # Add to your LangGraph agent
    agent = create_react_agent(model, tools)
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


def create_xpg_tools(
    base_url: str | None = None,
) -> list[Any]:
    """Create LangGraph-compatible XPG tools.

    Args:
        base_url: Optional REST API URL. If None, uses local stores.

    Returns:
        List of tool functions for use with LangGraph agents.
    """
    client = TrellisClient(base_url=base_url)

    @tool
    def xpg_get_context(
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
    def xpg_search(
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
    def xpg_save_trace(trace_json: str) -> str:
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
    def xpg_save_knowledge(
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
    def xpg_recent_activity(
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
        xpg_get_context,
        xpg_search,
        xpg_save_trace,
        xpg_save_knowledge,
        xpg_recent_activity,
    ]
