"""Retrieve routes -- search, packs, entities, traces."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.precedents import list_precedents as _list_precedents
from trellis.retrieve.rerankers import RRFReranker
from trellis.retrieve.strategies import build_strategies
from trellis.schemas.pack import PackBudget
from trellis_api.app import get_registry
from trellis_api.models import PackRequest, PackResponse

router = APIRouter()


@router.get("/search")
def search(
    q: str = Query(..., description="Search query"),
    domain: str | None = Query(None, description="Domain filter"),
    limit: int = Query(20, description="Max results"),
) -> dict[str, Any]:
    """Full-text search across documents."""
    registry = get_registry()
    filters: dict[str, Any] = {}
    if domain:
        filters["domain"] = domain
    results = registry.document_store.search(q, limit=limit, filters=filters)
    return {"status": "ok", "query": q, "count": len(results), "results": results}


@router.post("/packs", response_model=PackResponse)
def assemble_pack(req: PackRequest) -> PackResponse:
    """Assemble a context pack."""
    registry = get_registry()

    builder = PackBuilder(
        strategies=build_strategies(registry),
        event_log=registry.event_log,
        reranker=RRFReranker(),
    )

    budget = PackBudget(max_items=req.max_items, max_tokens=req.max_tokens)
    # Pass domain as a filter so strategies can use it for scoping
    filters: dict[str, Any] | None = None
    if req.domain:
        filters = {"domain": req.domain}
    pack = builder.build(
        intent=req.intent,
        domain=req.domain,
        agent_id=req.agent_id,
        budget=budget,
        filters=filters,
        tag_filters=req.tag_filters,
    )

    return PackResponse(
        pack_id=pack.pack_id,
        intent=pack.intent,
        domain=pack.domain,
        agent_id=pack.agent_id,
        count=len(pack.items),
        items=[item.model_dump() for item in pack.items],
        retrieval_report=pack.retrieval_report.model_dump(),
    )


@router.get("/graph/search", summary="Search graph entities")
def search_entities(
    q: str | None = Query(None, description="Name substring search (case-insensitive)"),
    node_type: str | None = Query(None, description="Filter by node type"),
    sort: str = Query(
        "created_at", description="Sort field: created_at, name, node_type"
    ),
    order: str = Query("desc", description="Sort order: asc or desc"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> dict[str, Any]:
    """Search graph nodes by name or type."""
    registry = get_registry()
    store = registry.graph_store

    # Detect backend by checking for SQLite vs Postgres connection
    is_sqlite = hasattr(store, "_conn") and not hasattr(store, "conn")

    if is_sqlite:
        return _search_entities_sqlite(store, q, node_type, sort, order, limit, offset)
    return _search_entities_postgres(store, q, node_type, sort, order, limit, offset)


def _search_entities_sqlite(
    store: Any,
    q: str | None,
    node_type: str | None,
    sort: str,
    order: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Search graph nodes using SQLite-compatible SQL."""
    import json  # noqa: PLC0415

    conditions = ["valid_to IS NULL"]
    params: list[Any] = []

    if q:
        conditions.append(
            "(json_extract(properties_json, '$.name') LIKE ?"
            " OR node_id LIKE ?"
            " OR node_type LIKE ?)"
        )
        pattern = f"%{q}%"
        params.extend([pattern, pattern, pattern])

    if node_type:
        conditions.append("node_type = ?")
        params.append(node_type)

    where = " AND ".join(conditions)

    # Sort mapping
    sort_col = {
        "name": "json_extract(properties_json, '$.name')",
        "node_type": "node_type",
    }.get(sort, "created_at")
    sort_dir = "ASC" if order.lower() == "asc" else "DESC"

    # Count total matching rows
    count_cursor = store._conn.execute(  # type: ignore[attr-defined]
        f"SELECT COUNT(*) FROM nodes WHERE {where}",  # noqa: S608
        params,
    )
    total = count_cursor.fetchone()[0]

    # Fetch page
    params.extend([limit, offset])
    cursor = store._conn.execute(  # type: ignore[attr-defined]
        f"SELECT node_id, node_type, properties_json, created_at"  # noqa: S608
        f" FROM nodes WHERE {where}"
        f" ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
        params,
    )
    rows = cursor.fetchall()

    results = []
    for r in rows:
        props = json.loads(r["properties_json"]) if r["properties_json"] else {}
        results.append(
            {
                "entity_id": r["node_id"],
                "node_type": r["node_type"],
                "name": props.get("name", r["node_id"]),
                "properties": props,
                "created_at": r["created_at"],
            }
        )
    return {
        "status": "ok",
        "total": total,
        "count": len(results),
        "offset": offset,
        "results": results,
    }


def _search_entities_postgres(
    store: Any,
    q: str | None,
    node_type: str | None,
    sort: str,
    order: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Search graph nodes using PostgreSQL-compatible SQL."""
    conditions = ["valid_to IS NULL"]
    params: list[Any] = []

    if q:
        conditions.append(
            "(properties->>'name' ILIKE %s OR node_id ILIKE %s OR node_type ILIKE %s)"
        )
        pattern = f"%{q}%"
        params.extend([pattern, pattern, pattern])

    if node_type:
        conditions.append("node_type = %s")
        params.append(node_type)

    where = " AND ".join(conditions)

    sort_col = {
        "name": "properties->>'name'",
        "node_type": "node_type",
    }.get(sort, "created_at")
    sort_dir = "ASC" if order.lower() == "asc" else "DESC"

    # Count total
    count_params = list(params)
    with store.conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            f"SELECT COUNT(*) FROM nodes WHERE {where}",  # noqa: S608
            count_params,
        )
        total = cur.fetchone()[0]

    params.extend([limit, offset])
    with store.conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            f"SELECT node_id, node_type, properties, created_at"  # noqa: S608
            f" FROM nodes WHERE {where}"
            f" ORDER BY {sort_col} {sort_dir} LIMIT %s OFFSET %s",
            params,
        )
        rows = cur.fetchall()

    results = [
        {
            "entity_id": r[0],
            "node_type": r[1],
            "name": (r[2] or {}).get("name", r[0]),
            "properties": r[2] or {},
            "created_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "total": total,
        "count": len(results),
        "offset": offset,
        "results": results,
    }


@router.get("/entities/{entity_id:path}")
def get_entity(
    entity_id: str,
    depth: int = Query(1, description="Subgraph traversal depth"),
) -> dict[str, Any]:
    """Get an entity and its neighborhood."""
    registry = get_registry()
    node = registry.graph_store.get_node(entity_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

    subgraph = registry.graph_store.get_subgraph(seed_ids=[entity_id], depth=depth)
    return {"status": "ok", "entity": node, "subgraph": subgraph}


@router.get("/traces")
def list_traces(
    domain: str | None = Query(None),
    agent: str | None = Query(None, alias="agent_id"),
    limit: int = Query(20),
) -> dict[str, Any]:
    """List recent traces."""
    registry = get_registry()
    traces = registry.trace_store.query(domain=domain, agent_id=agent, limit=limit)
    total = registry.trace_store.count(domain=domain)

    items = [t.to_summary_dict() for t in traces]
    return {"status": "ok", "total": total, "count": len(items), "traces": items}


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    """Get a specific trace by ID."""
    registry = get_registry()
    trace = registry.trace_store.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
    return {"status": "ok", "trace": trace.model_dump(mode="json")}


@router.get("/precedents")
def list_precedents(
    domain: str | None = Query(None),
    limit: int = Query(20),
) -> dict[str, Any]:
    """List promoted precedents."""
    registry = get_registry()
    items = _list_precedents(registry.event_log, domain=domain, limit=limit)
    return {"status": "ok", "count": len(items), "precedents": items}
