"""Retrieve routes -- search, packs, entities, traces."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from trellis.retrieve.builder_factory import build_pack_builder, describe_axes
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.precedents import list_precedents as _list_precedents
from trellis.schemas.pack import PackBudget, SectionRequest
from trellis.schemas.trace import Trace
from trellis.stores.base.graph import NODE_SEARCH_SORTS
from trellis_api.app import get_registry
from trellis_wire.dtos import (
    AxisReportResponse,
    PackRequest,
    PackResponse,
    SectionedPackRequest,
    SectionedPackResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


def _build_pack_builder(registry: Any) -> PackBuilder:
    """Wire a PackBuilder the same way every other pack surface does.

    Delegates to :func:`~trellis.retrieve.builder_factory.build_pack_builder`
    (#410). "The same way the MCP server does" was previously asserted by a
    docstring over a hand-copied argument list; it is now the same call.
    """
    return build_pack_builder(registry, surface="api.retrieve")


# Chunk rows are excluded from this route by default (#396) — the sibling
# of the same change on ``GET /api/v1/documents`` (#385/#391), which left
# this one behind.
#
# ``search`` hands back whole document rows, and a ``<parent>#chunk-N`` row
# is a slice of a parent the *same result set* already ranks. On the
# reference deployment (2026-08-29, 1,319 documents, 740 of them chunks)
# eight representative queries at ``limit=20`` returned 155 rows of which 39
# (25.2%) were chunks; re-running them with ``include_chunks=False`` surfaced
# **34 whole documents the unfiltered view never showed** — one per
# displaced fragment. The eight, so the figure is re-derivable rather than
# quoted: "how does retrieval work", "hunting", "memory system", "backup",
# "kids school", "postgres", "pack budget", "vector store" — a spread of
# technical and personal intents, since the corpus is majority personal.
#
# That refill is the point, and it is why the exclusion is pushed into the
# store rather than applied to the response — see
# :meth:`~trellis.stores.base.document.DocumentStore.list_documents` for the
# argument, which this route does not restate.
#
# What is worth saying here: the result set can still come back shorter than
# ``limit``, and that is not the defect the pushdown avoids. One of the eight
# queries (``postgres``) went 15 → 10 because the corpus genuinely holds
# fewer than 20 non-chunk matches for it. A pushdown short page means "that
# is all there is"; a post-hoc filter's short page means "there was more, off
# the end of the window you asked for".
#
# Deliberately *not* scoped to browser operators. ``TrellisClient.search``
# targets this route, so the default changes for SDK agents too — correctly:
# an agent reading whole rows gets strictly more content per row from the
# parent. The surface that must keep seeing chunks is the pack's keyword
# axis (``retrieve/strategies.py``), where the excerpt is what the token
# budget prices and the chunk is the retrievable unit. See
# :data:`trellis.ingest_corpus.models.CHUNK_ID_SEPARATOR` for the rule and
# ``tests/unit/test_chunk_visibility_rule.py`` for its enforcement.
@router.get("/search")
def search(
    q: str = Query(..., description="Search query"),
    domain: str | None = Query(None, description="Domain filter"),
    limit: int = Query(20, description="Max results"),
    include_chunks: bool = Query(
        False,
        description=(
            "Include <parent>#chunk-N fragment rows. Excluded by default:"
            " they are slices of documents the same search already ranks."
        ),
    ),
) -> dict[str, Any]:
    """Full-text search across documents.

    ``<parent>#chunk-N`` fragment rows are excluded by default; pass
    ``include_chunks=true`` for the unfiltered result set. The applied
    setting is echoed back on the response.
    """
    registry = get_registry()
    filters: dict[str, Any] = {}
    if domain:
        filters["domain"] = domain
    results = registry.knowledge.document_store.search(
        q, limit=limit, filters=filters, include_chunks=include_chunks
    )
    return {
        "status": "ok",
        "query": q,
        "count": len(results),
        "include_chunks": include_chunks,
        "results": results,
    }


@router.post("/packs", response_model=PackResponse)
def assemble_pack(req: PackRequest) -> PackResponse:
    """Assemble a context pack."""
    registry = get_registry()
    builder = _build_pack_builder(registry)

    budget = PackBudget(max_items=req.max_items, max_tokens=req.max_tokens)
    # Pass domain as a filter so strategies can use it for scoping
    filters: dict[str, Any] | None = None
    if req.domain:
        filters = {"domain": req.domain}
    pack = builder.build(
        intent=req.intent,
        domain=req.domain,
        agent_id=req.agent_id,
        run_id=req.run_id,
        intent_family=req.intent_family,
        budget=budget,
        filters=filters,
        tag_filters=req.tag_filters,
    )

    # Which axes this deployment has, which ran, and which did not — the
    # same call `trellis retrieve pack --format json` has made since #410
    # (builder_factory.describe_axes), so a REST caller stops mistaking a
    # degraded, keyword-only pack for a full one (#761 follow-up A).
    axes = describe_axes(
        builder,
        pack.retrieval_report.strategies_used,
        embedder_configured=registry.embedding_fn is not None,
    )

    return PackResponse(
        pack_id=pack.pack_id,
        intent=pack.intent,
        domain=pack.domain,
        agent_id=pack.agent_id,
        count=len(pack.items),
        items=[item.model_dump() for item in pack.items],
        advisories=[a.model_dump(mode="json") for a in pack.advisories],
        retrieval_report=pack.retrieval_report.model_dump(),
        withholding=pack.metadata.get("withholding"),
        axes=AxisReportResponse(**axes),
    )


@router.post("/packs/sectioned", response_model=SectionedPackResponse)
def assemble_sectioned_pack(req: SectionedPackRequest) -> SectionedPackResponse:
    """Assemble a sectioned pack with independently budgeted sections.

    The SDK's ``assemble_sectioned_pack()`` targets this route; section
    dicts are validated into ``SectionRequest`` models here (the wire
    DTO keeps them untyped so trellis_wire stays core-free).
    """
    registry = get_registry()
    try:
        sections = [SectionRequest(**s) for s in req.sections]
    except (ValidationError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid section request: {exc}"
        ) from exc

    builder = _build_pack_builder(registry)
    filters: dict[str, Any] | None = None
    if req.domain:
        filters = {"domain": req.domain}
    pack = builder.build_sectioned(
        req.intent,
        sections=sections,
        domain=req.domain,
        agent_id=req.agent_id,
        run_id=req.run_id,
        intent_family=req.intent_family,
        filters=filters,
    )
    return SectionedPackResponse(
        pack_id=pack.pack_id,
        intent=pack.intent,
        domain=pack.domain,
        agent_id=pack.agent_id,
        sections=[s.model_dump(mode="json") for s in pack.sections],
        advisories=[a.model_dump(mode="json") for a in pack.advisories],
        withholding=pack.metadata.get("withholding"),
    )


@router.get("/graph/search", summary="Search graph entities")
def search_entities(
    q: str | None = Query(
        None,
        description="A case-insensitive substring of the name, node_id or node_type",
    ),
    node_type: str | None = Query(None, description="Filter by node type"),
    sort: str = Query(
        "created_at", description="Sort field: created_at, name, node_type"
    ),
    order: str = Query("desc", description="Sort order: asc or desc"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> dict[str, Any]:
    """Search graph nodes by name or type."""
    store = get_registry().knowledge.graph_store
    # Lenient: an unknown sort is created_at, and any order but asc (in any
    # case) descends.
    rows, total = store.search_nodes(
        search=q,
        node_type=node_type,
        sort=sort if sort in NODE_SEARCH_SORTS else "created_at",
        descending=order.lower() != "asc",
        limit=limit,
        offset=offset,
    )
    results = [
        {
            "entity_id": row["node_id"],
            "node_type": row["node_type"],
            "name": row["properties"].get("name", row["node_id"]),
            "properties": row["properties"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return {
        "status": "ok",
        "total": total,
        "count": len(results),
        "offset": offset,
        "results": results,
    }


@router.get("/graph/search/facets", summary="Count graph search matches per node type")
def search_entity_facets(
    q: str | None = Query(
        None,
        description=(
            "The /graph/search q filter: a case-insensitive substring of the"
            " name, node_id or node_type"
        ),
    ),
) -> dict[str, Any]:
    """Count current graph nodes per stored ``node_type`` under ``q``.

    The graph page's type chips. Every type is counted server-side, in its
    stored case, by count descending then type, and the counts sum to
    ``/graph/search``'s ``total`` for the same ``q``. ``node_type`` is not a
    parameter because it is the dimension being counted.
    """
    store = get_registry().knowledge.graph_store
    counts = store.count_nodes_by_type(search=q or None)
    node_types = [
        {"node_type": node_type, "count": count}
        for node_type, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {"status": "ok", "total": sum(counts.values()), "node_types": node_types}


@router.get("/entities/{entity_id:path}")
def get_entity(
    entity_id: str,
    depth: int = Query(1, description="Subgraph traversal depth"),
) -> dict[str, Any]:
    """Get an entity and its neighborhood."""
    registry = get_registry()
    node = registry.knowledge.graph_store.get_node(entity_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

    subgraph = registry.knowledge.graph_store.get_subgraph(
        seed_ids=[entity_id], depth=depth
    )
    return {"status": "ok", "entity": node, "subgraph": subgraph}


@router.get("/traces")
def list_traces(
    domain: str | None = Query(None),
    agent: str | None = Query(None, alias="agent_id"),
    limit: int = Query(20),
) -> dict[str, Any]:
    """List recent traces."""
    registry = get_registry()
    traces = registry.operational.trace_store.query(
        domain=domain, agent_id=agent, limit=limit
    )
    total = registry.operational.trace_store.count(domain=domain)

    items = [t.to_summary_dict() for t in traces]
    return {"status": "ok", "total": total, "count": len(items), "traces": items}


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    """Get a trace and what can show each of its evidence refs.

    ``evidence_links`` maps an ``evidence_id`` to ``{"document_id": ...}``
    when a document holds that evidence record, else to
    ``{"entity_id": "evidence:<id>"}`` when that graph node exists. A ref with
    neither, or with an empty ``evidence_id``, has no entry.
    ``evidence_links`` is ``null`` when the knowledge stores cannot be read.
    """
    registry = get_registry()
    trace = registry.operational.trace_store.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
    links: dict[str, dict[str, str]] | None
    try:
        links = _evidence_links(registry, trace)
    # GRACEFUL-DEGRADATION: the links only point at views of the evidence. A
    # knowledge store that cannot answer costs the links, never the trace,
    # which lives in the operational plane.
    except Exception:
        logger.warning("trace_evidence_links_failed", trace_id=trace_id, exc_info=True)
        links = None
    return {
        "status": "ok",
        "trace": trace.model_dump(mode="json"),
        "evidence_links": links,
    }


def _evidence_links(registry: Any, trace: Trace) -> dict[str, dict[str, str]]:
    """Where each evidence ref can be viewed, keyed by its ``evidence_id``.

    The evidence writers (``POST /evidence``, ``trellis ingest evidence``, the
    demo) store each record as a document under its ``evidence_id``. Failing
    a document, trace extraction, when it ran, wrote an ``evidence:<id>``
    graph node. A ref with neither has no entry, nor does one with an empty
    ``evidence_id``: it names no record, even where a graph holds a node
    ``evidence:``.
    """
    documents = registry.knowledge.document_store
    graph = registry.knowledge.graph_store
    links: dict[str, dict[str, str]] = {}
    for ref in trace.evidence_used:
        if not ref.evidence_id:
            continue
        node_id = f"evidence:{ref.evidence_id}"
        if documents.get(ref.evidence_id) is not None:
            links[ref.evidence_id] = {"document_id": ref.evidence_id}
        elif graph.get_node(node_id) is not None:
            links[ref.evidence_id] = {"entity_id": node_id}
    return links


@router.get("/precedents")
def list_precedents(
    domain: str | None = Query(None),
    limit: int = Query(20),
) -> dict[str, Any]:
    """List promoted precedents."""
    registry = get_registry()
    items = _list_precedents(registry.operational.event_log, domain=domain, limit=limit)
    return {"status": "ok", "count": len(items), "precedents": items}
