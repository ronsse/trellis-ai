"""Shared operations for MCP servers."""

from __future__ import annotations

from typing import Any

from trellis.stores.registry import StoreRegistry


def search_documents(
    registry: StoreRegistry,
    query: str,
    *,
    limit: int = 10,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Search documents and return pack-style items."""
    filters: dict[str, Any] = {}
    if domain:
        filters["domain"] = domain
    results = registry.knowledge.document_store.search(
        query, limit=limit, filters=filters
    )
    return [
        {
            "item_id": doc["doc_id"],
            "item_type": "document",
            "excerpt": doc.get("content", "")[:500],
            "relevance_score": abs(doc.get("rank", 0.0)),
            "metadata": doc.get("metadata", {}),
            "source_strategy": "keyword",
        }
        for doc in results
    ]


def search_graph_nodes(
    registry: StoreRegistry,
    query: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search graph nodes by name/description substring match."""
    nodes = registry.knowledge.graph_store.query(limit=limit)
    q_lower = query.lower()
    items: list[dict[str, Any]] = []
    for node in nodes:
        props = node.get("properties", {})
        name = str(props.get("name", "")).lower()
        desc = str(props.get("description", "")).lower()
        if q_lower in name or q_lower in desc:
            excerpt = props.get("name", "") or props.get("description", "")
            items.append(
                {
                    "item_id": node["node_id"],
                    "item_type": "entity",
                    "excerpt": excerpt,
                    "relevance_score": 0.5,
                    "metadata": {
                        "node_type": node.get("node_type", ""),
                        **props,
                    },
                    "source_strategy": "graph",
                }
            )
    return items


def fetch_recent_traces(
    registry: StoreRegistry,
    *,
    limit: int = 3,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent traces and return pack-style items."""
    traces = registry.operational.trace_store.query(domain=domain, limit=limit)
    return [
        {
            "item_id": t.trace_id,
            "item_type": "trace",
            "excerpt": t.intent[:300],
            "relevance_score": 0.3,
            "metadata": {
                "source": t.source.value,
                "outcome": t.outcome.status.value if t.outcome else None,
            },
            "source_strategy": "trace_recency",
        }
        for t in traces
    ]
