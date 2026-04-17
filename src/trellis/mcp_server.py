"""MCP server for trellis — tools for Claude Code, OpenClaw, etc."""

from __future__ import annotations

from typing import Any

import structlog
from fastmcp import FastMCP

from trellis.core.ids import generate_ulid
from trellis.schemas.trace import Trace
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

mcp = FastMCP(
    "trellis",
    instructions=("Trellis — structured memory and learning for AI agents"),
)


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


_registry: StoreRegistry | None = None


def _get_registry() -> StoreRegistry:
    """Get or create a cached StoreRegistry singleton."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = StoreRegistry.from_config_dir()
    return _registry


def _error_response(message: str) -> dict[str, Any]:
    """Build a structured error response."""
    return {"status": "error", "message": message}


def _ok_response(**kwargs: Any) -> dict[str, Any]:
    """Build a structured success response."""
    return {"status": "ok", **kwargs}


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_search(
    query: str,
    limit: int = 10,
    mode: str = "keyword",
) -> dict[str, Any]:
    """Search documents in the experience graph.

    Args:
        query: Search query string.
        limit: Maximum results to return (default 10).
        mode: Search mode — "keyword" or "semantic".
    """
    if not query or not query.strip():
        return _error_response("Query must not be empty")

    registry = _get_registry()
    store = registry.document_store
    results = store.search(query, limit=limit)

    items = [
        {
            "doc_id": doc["doc_id"],
            "content": doc.get("content", "")[:500],
            "metadata": doc.get("metadata", {}),
            "rank": doc.get("rank"),
        }
        for doc in results
    ]

    return _ok_response(
        query=query,
        mode=mode,
        count=len(items),
        results=items,
    )


@mcp.tool()
def memory_store(
    content: str,
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """Store a document in the experience graph.

    Args:
        content: Document content to store.
        metadata: Optional metadata dict (tags, source, etc.).
        doc_id: Optional document ID. Auto-generated if not provided.
    """
    if not content or not content.strip():
        return _error_response("Content must not be empty")

    registry = _get_registry()
    store = registry.document_store
    stored_id = store.put(doc_id, content, metadata=metadata or {})

    return _ok_response(doc_id=stored_id)


@mcp.tool()
def memory_delete(doc_id: str) -> dict[str, Any]:
    """Delete a document by ID.

    Args:
        doc_id: The document ID to delete.
    """
    if not doc_id or not doc_id.strip():
        return _error_response("doc_id must not be empty")

    registry = _get_registry()
    store = registry.document_store
    deleted = store.delete(doc_id)

    if not deleted:
        return _error_response(f"Document not found: {doc_id}")

    return _ok_response(doc_id=doc_id, deleted=True)


# ---------------------------------------------------------------------------
# Knowledge tools
# ---------------------------------------------------------------------------


@mcp.tool()
def knowledge_query(
    query: str,
    node_types: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Query graph nodes in the experience graph.

    Args:
        query: Search query. Matches node name property.
        node_types: Optional list of node types to filter by.
        limit: Maximum results (default 20).
    """
    if not query or not query.strip():
        return _error_response("Query must not be empty")

    results: list[dict[str, Any]] = []

    registry = _get_registry()
    store = registry.graph_store

    if node_types:
        for nt in node_types:
            nodes = store.query(
                node_type=nt,
                properties={"name": query},
                limit=limit,
            )
            results.extend(nodes)
    else:
        nodes = store.query(properties={"name": query}, limit=limit)
        results.extend(nodes)

        if not results:
            all_nodes = store.query(limit=limit * 2)
            q_lower = query.lower()
            for node in all_nodes:
                props = node.get("properties", {})
                name = str(props.get("name", "")).lower()
                desc = str(props.get("description", "")).lower()
                if q_lower in name or q_lower in desc:
                    results.append(node)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for node in results:
        nid = node["node_id"]
        if nid not in seen:
            seen.add(nid)
            unique.append(node)

    return _ok_response(
        query=query,
        count=len(unique[:limit]),
        nodes=unique[:limit],
    )


@mcp.tool()
def knowledge_add(
    name: str,
    entity_type: str = "concept",
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an entity node in the knowledge graph.

    Args:
        name: Entity name.
        entity_type: Node type (default "concept").
        properties: Optional additional properties dict.
    """
    if not name or not name.strip():
        return _error_response("Name must not be empty")

    props = dict(properties or {})
    props["name"] = name

    registry = _get_registry()
    store = registry.graph_store
    node_id = store.upsert_node(
        node_id=None,
        node_type=entity_type,
        properties=props,
    )

    return _ok_response(node_id=node_id, entity_type=entity_type, name=name)


@mcp.tool()
def knowledge_relate(
    source_id: str,
    target_id: str,
    edge_kind: str = "entity_related_to",
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a relationship edge between two nodes.

    Args:
        source_id: Source node ID.
        target_id: Target node ID.
        edge_kind: Edge type (default "entity_related_to").
        properties: Optional edge properties.
    """
    if not source_id or not target_id:
        return _error_response("Both source_id and target_id are required")

    registry = _get_registry()
    store = registry.graph_store

    if store.get_node(source_id) is None:
        return _error_response(f"Source node not found: {source_id}")
    if store.get_node(target_id) is None:
        return _error_response(f"Target node not found: {target_id}")

    edge_id = store.upsert_edge(
        source_id=source_id,
        target_id=target_id,
        edge_type=edge_kind,
        properties=properties,
    )

    return _ok_response(
        edge_id=edge_id,
        source_id=source_id,
        target_id=target_id,
        edge_kind=edge_kind,
    )


# ---------------------------------------------------------------------------
# Experience tools
# ---------------------------------------------------------------------------


@mcp.tool()
def experience_cases(
    limit: int = 10,
    domain: str | None = None,
) -> dict[str, Any]:
    """List recent traces as experience cases.

    Args:
        limit: Maximum traces to return (default 10).
        domain: Optional domain filter.
    """
    registry = _get_registry()
    store = registry.trace_store
    traces = store.query(domain=domain, limit=limit)

    cases = [{**t.to_summary_dict(), "steps_count": len(t.steps)} for t in traces]

    return _ok_response(count=len(cases), cases=cases)


@mcp.tool()
def experience_lessons(
    limit: int = 20,
    domain: str | None = None,
) -> dict[str, Any]:
    """List precedents (promoted lessons from traces).

    Args:
        limit: Maximum results (default 20).
        domain: Optional domain filter.
    """
    from trellis.retrieve.precedents import list_precedents as _list_prec  # noqa: PLC0415, I001

    registry = _get_registry()
    lessons = _list_prec(registry.event_log, domain=domain, limit=limit)

    return _ok_response(count=len(lessons), lessons=lessons)


@mcp.tool()
def experience_playbooks(limit: int = 10) -> dict[str, Any]:
    """List published packs/playbooks.

    Args:
        limit: Maximum results (default 10).

    Note: Playbooks are not yet fully implemented.
    """
    registry = _get_registry()
    log = registry.event_log
    events = log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
        limit=limit,
    )

    playbooks = [
        {
            "event_id": e.event_id,
            "entity_id": e.entity_id,
            "payload": e.payload,
            "occurred_at": e.occurred_at.isoformat(),
        }
        for e in events
    ]

    note = "Playbook publishing is a work in progress." if not playbooks else None
    return _ok_response(
        count=len(playbooks),
        playbooks=playbooks,
        note=note,
    )


# ---------------------------------------------------------------------------
# Trace tools
# ---------------------------------------------------------------------------


@mcp.tool()
def trace_ingest(trace_json: str) -> dict[str, Any]:
    """Ingest a trace from a JSON string.

    Args:
        trace_json: JSON string conforming to the Trace schema.
    """
    if not trace_json or not trace_json.strip():
        return _error_response("trace_json must not be empty")

    try:
        trace = Trace.model_validate_json(trace_json)
    except Exception as exc:
        return _error_response(f"Invalid trace JSON: {exc}")

    try:
        registry = _get_registry()
        store = registry.trace_store
        trace_id = store.append(trace)
    except Exception as exc:
        return _error_response(f"Failed to store trace: {exc}")

    return _ok_response(trace_id=trace_id)


@mcp.tool()
def trace_status(limit: int = 10) -> dict[str, Any]:
    """List recent traces with summary info.

    Args:
        limit: Maximum traces to return (default 10).
    """
    registry = _get_registry()
    store = registry.trace_store
    traces = store.query(limit=limit)
    total = store.count()

    items = [
        {
            "trace_id": t.trace_id,
            "source": t.source.value,
            "intent": t.intent[:100],
            "outcome": (t.outcome.status.value if t.outcome else None),
            "created_at": t.created_at.isoformat(),
        }
        for t in traces
    ]

    return _ok_response(total_traces=total, count=len(items), traces=items)


# ---------------------------------------------------------------------------
# Context tools
# ---------------------------------------------------------------------------


def _assemble_doc_items(
    intent: str,
    max_items: int,
    domain: str | None,
) -> list[dict[str, Any]]:
    """Search documents and return pack items."""
    from trellis.mcp._shared import search_documents  # noqa: PLC0415

    return search_documents(
        _get_registry(), intent, limit=max_items // 2, domain=domain
    )


def _assemble_graph_items(
    intent: str,
    max_items: int,
) -> list[dict[str, Any]]:
    """Search graph nodes and return pack items."""
    from trellis.mcp._shared import search_graph_nodes  # noqa: PLC0415

    return search_graph_nodes(_get_registry(), intent, limit=max(5, max_items // 4))


def _assemble_trace_items(
    max_items: int,
    domain: str | None,
) -> list[dict[str, Any]]:
    """Fetch recent traces and return pack items."""
    from trellis.mcp._shared import fetch_recent_traces  # noqa: PLC0415

    return fetch_recent_traces(
        _get_registry(), limit=max(3, max_items // 4), domain=domain
    )


@mcp.tool()
def context_assemble(
    intent: str,
    max_items: int = 50,
    domain: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Assemble a context pack for an agent or workflow.

    Args:
        intent: The intent or query to assemble context for.
        max_items: Maximum items in the pack (default 50).
        domain: Optional domain scope.
        agent_id: Optional agent ID scope.
    """
    if not intent or not intent.strip():
        return _error_response("Intent must not be empty")

    items: list[dict[str, Any]] = []

    try:
        items.extend(_assemble_doc_items(intent, max_items, domain))
    except Exception:
        logger.exception("context_assemble_doc_search_failed")

    try:
        items.extend(_assemble_graph_items(intent, max_items))
    except Exception:
        logger.exception("context_assemble_graph_search_failed")

    try:
        items.extend(_assemble_trace_items(max_items, domain))
    except Exception:
        logger.exception("context_assemble_trace_search_failed")

    # Deduplicate and trim
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        iid = item["item_id"]
        if iid not in seen:
            seen.add(iid)
            unique.append(item)

    unique.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)
    selected = unique[:max_items]

    return _ok_response(
        pack_id=generate_ulid(),
        intent=intent,
        domain=domain,
        agent_id=agent_id,
        count=len(selected),
        items=selected,
        strategies_used=["keyword", "graph", "trace_recency"],
    )


# ---------------------------------------------------------------------------
# Graph tools
# ---------------------------------------------------------------------------


@mcp.tool()
def context_graph(
    entity_id: str,
    depth: int = 1,
) -> dict[str, Any]:
    """Get an entity and its neighborhood from the graph.

    Args:
        entity_id: The node ID to start from.
        depth: How many hops to traverse (default 1).
    """
    if not entity_id or not entity_id.strip():
        return _error_response("entity_id must not be empty")

    registry = _get_registry()
    store = registry.graph_store

    node = store.get_node(entity_id)
    if node is None:
        return _error_response(f"Entity not found: {entity_id}")

    subgraph = store.get_subgraph(seed_ids=[entity_id], depth=depth)

    return _ok_response(
        entity=node,
        neighbors=subgraph,
    )


# ---------------------------------------------------------------------------
# Skill tools (agent-kernel MCP compatibility stubs)
# ---------------------------------------------------------------------------


@mcp.tool()
def skill_list() -> dict[str, Any]:
    """List available skills (stub — no skill registry yet)."""
    return _ok_response(skills=[], note="No skill registry in trellis yet.")


@mcp.tool()
def skill_search(query: str) -> dict[str, Any]:
    """Search skills (stub — no skill registry yet).

    Args:
        query: Search query for skills.
    """
    return _ok_response(
        query=query,
        skills=[],
        note="No skill registry in trellis yet.",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
