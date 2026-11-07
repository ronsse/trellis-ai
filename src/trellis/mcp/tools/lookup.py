"""Read-by-id tools.

``get_lessons``, ``get_graph``, ``get_items`` and ``get_file_context``.
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.shared.exceptions import McpError

from trellis.auth import SCOPE_READ
from trellis.core.access_log import note_served_items
from trellis.mcp.app import PACK_TOOL_TAG, mcp
from trellis.mcp.auth import current_clearance, trellis_scope
from trellis.mcp.boundary import (
    _exception_detail,
    _raise_internal,
    _raise_invalid_params,
    _raise_not_found,
    _refuse_public_caller,
)
from trellis.mcp.grading_trailer import with_grading_trailer
from trellis.mcp.tools.context import _track_tokens
from trellis.mcp.wiring import _get_registry
from trellis.retrieve.builder_factory import _get_lessons_pack_event_enabled
from trellis.retrieve.file_context import build_file_context
from trellis.retrieve.formatters import (
    format_entity_as_markdown,
    format_fetched_items_as_markdown,
    format_file_context_as_markdown,
    format_lessons_as_markdown,
    format_subgraph_as_markdown,
)
from trellis.retrieve.lessons_pack import record_lessons_pack
from trellis.retrieve.precedents import PrecedentClearance
from trellis.retrieve.sensitivity import (
    DocumentClearance,
    conceal_node_pointers,
    is_public_only,
    read_document_row,
)
from trellis.retrieve.token_tracker import estimate_tokens, track_token_usage
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import load_settings_values

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Macro Tool 5: get_lessons
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def get_lessons(
    domain: str | None = None,
    limit: int = 10,
    max_tokens: int = 2000,
) -> str:
    """Get lessons learned (promoted precedents) from past experiences.

    A lesson whose own document row or source the caller's clearance cannot read
    is left out.

    Args:
        domain: Optional domain filter.
        limit: Maximum lessons to return (default 10).
        max_tokens: Maximum response size in tokens (default 2000).

    With ``TRELLIS_GET_LESSONS_PACK_EVENT`` on, the reply ends with a
    ``pack_id`` and the served ids so ``record_feedback`` can grade them.
    """
    from trellis.retrieve.precedents import list_precedents as _list_prec  # noqa: PLC0415

    _refuse_public_caller("get_lessons")
    registry = _get_registry()
    lessons = _list_prec(
        registry.operational.event_log,
        domain=domain,
        limit=limit,
        graph_store=registry.knowledge.graph_store,
        document_store=registry.knowledge.document_store,
        clearance=current_clearance(),
    )

    served: list[dict[str, Any]] = []
    result = format_lessons_as_markdown(
        lessons, max_tokens=max_tokens, served_out=served
    )
    if served and _get_lessons_pack_event_enabled(
        load_settings_values(registry.stores_dir)
    ):
        result = record_lessons_pack(
            registry.operational.event_log,
            served,
            rendered=result,
            domain=domain,
            max_tokens=max_tokens,
        )
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_lessons",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="get_lessons")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 6: get_graph
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_graph(
    entity_id: str,
    depth: int = 1,
    max_tokens: int = 2000,
) -> str:
    """Get an entity and its neighborhood from the knowledge graph.

    Args:
        entity_id: The entity ID to explore.
        depth: How many relationship hops to traverse (default 1).
        max_tokens: Maximum response size in tokens (default 2000).
    """
    _refuse_public_caller("get_graph")
    if not entity_id or not entity_id.strip():
        _raise_invalid_params(
            "entity_id must not be empty",
            data={"field": "entity_id"},
        )

    registry = _get_registry()
    node = registry.knowledge.graph_store.get_node(entity_id)
    # A promoted precedent is judged by its source's label as well as its own
    # (#1080). One predicate for the entity and every neighbour: a withheld
    # entity is the not-found a missing one gets, a withheld neighbour and each
    # edge that touches it are left out.
    precedents = PrecedentClearance(
        registry.knowledge.document_store,
        current_clearance(),
        registry.knowledge.graph_store,
    )
    if node is None or precedents.verdict_of(entity_id, node) is not None:
        _raise_not_found(
            f"entity not found: {entity_id}",
            data={"entity_id": entity_id},
        )

    subgraph = registry.knowledge.graph_store.get_subgraph(
        seed_ids=[entity_id], depth=depth
    )
    withheld = {
        n["node_id"]
        for n in subgraph.get("nodes", [])
        if precedents.verdict_of(n["node_id"], n) is not None
    }
    if withheld:
        subgraph = {
            **subgraph,
            "nodes": [n for n in subgraph["nodes"] if n["node_id"] not in withheld],
            "edges": [
                e
                for e in subgraph.get("edges", [])
                if e["source_id"] not in withheld and e["target_id"] not in withheld
            ],
        }
    # A node carries no label, so the root and every neighbour are served; what
    # is dropped is a pointer naming a document this caller may not read. One
    # judgement for the whole response (root first, then the neighbourhood).
    root, *neighbours = conceal_node_pointers(
        [node, *subgraph.get("nodes", [])],
        registry.knowledge.document_store,
        current_clearance(),
    )
    subgraph = {**subgraph, "nodes": neighbours}
    result = format_subgraph_as_markdown(root, subgraph, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_graph",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="get_graph")
    return result


# ---------------------------------------------------------------------------
# Macro Tool: get_items — batch fetch-by-id (#305)
# ---------------------------------------------------------------------------

#: Hard cap on ids per ``get_items`` call. A pack serves at most
#: ``_FLAT_MAX_ITEMS`` items, so one call can fetch a whole index pack;
#: anything larger is a bulk export, which is the REST/CLI surface's job.
_GET_ITEMS_MAX_IDS = 50


def _render_fetched_item(
    registry: StoreRegistry,
    item_id: str,
    precedents: PrecedentClearance | None = None,
) -> tuple[str, str] | None:
    """Resolve ``item_id`` to ``(kind, markdown body)``, or ``None``.

    Resolution order mirrors where pack item ids point: the document
    store (keyword/semantic items — vector ``item_id``s are doc ids),
    then the graph store (entity/observation items), then the trace
    store. A graph node goes through the same
    :func:`~trellis.retrieve.formatters.format_entity_as_markdown` block
    ``get_graph`` renders, so a fetched entity carries the ``document_ids``
    evidence pointers that make it a hop rather than a dead end.

    ``precedents`` is the per-call :class:`PrecedentClearance` (#1080), so a
    caller fetching many ids judges each precedent once; one is built for this
    id when it is left out.
    """
    if precedents is None:
        precedents = PrecedentClearance(
            registry.knowledge.document_store,
            current_clearance(),
            registry.knowledge.graph_store,
        )
    doc = read_document_row(
        registry.knowledge.document_store, item_id, current_clearance()
    )
    if doc is not None:
        # Above the caller's clearance answers exactly as not-found: no other
        # kind is tried, so the reply cannot say the document exists (#194).
        if not DocumentClearance(
            registry.knowledge.document_store, current_clearance()
        ).permits(item_id, doc.get("metadata")):
            return None
        # A precedent's own row holds the source's excerpt and carries no
        # label, so its source's label decides.
        if precedents.verdict_of(item_id) is not None:
            return None
        return "document", doc.get("content", "")

    if is_public_only(current_clearance()):
        # A graph node or a trace has no label to compare, so a public-only
        # caller is never shown one, and the reply is the not-found a missing
        # id gets: no other kind is tried, so it cannot say the id exists.
        return None

    node = registry.knowledge.graph_store.get_node(item_id)
    if node is not None:
        if precedents.verdict_of(item_id, node) is not None:
            return None
        # The node is served; a pointer to a document above the caller's
        # clearance is not.
        (node,) = conceal_node_pointers(
            [node], registry.knowledge.document_store, current_clearance()
        )
        return "entity", format_entity_as_markdown(node)

    trace = registry.operational.trace_store.get(item_id)
    if trace is not None:
        return "trace", f"```json\n{trace.model_dump_json()}\n```"

    return None


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def get_items(
    item_ids: list[str],
    pack_id: str = "",
    max_tokens: int = 4000,
) -> str:
    """Fetch full bodies for known item ids, token-budgeted (#305).

    The fetch layer of progressive disclosure: survey an index pack
    (``get_context(index=True)`` / ``search(index=True)``), follow
    ``get_graph`` evidence pointers, then batch-fetch the ids worth
    reading. Ids resolve against the document store, the knowledge
    graph, and the trace store — a fetched entity includes its
    ``document_ids`` pointers so you can keep following evidence.

    Items that do not fit ``max_tokens`` are omitted whole and reported
    as omitted — never truncated — so re-fetch them with a larger
    ``max_tokens`` (the index line's ``~N tok`` is the cost to budget
    for); unknown ids are listed as not found. Every call is recorded as
    a
    ``PACK_ITEMS_FETCHED`` event carrying the served ids, so pass the
    ``pack_id`` of the pack that surfaced the ids — that keeps the fetch,
    and your later ``record_feedback(pack_id=..., helpful_item_ids=...)``,
    attributable to the serving pack.

    Args:
        item_ids: Item ids to fetch (max 50). Copy them verbatim from
            index lines, pack items, or graph evidence pointers.
        pack_id: The pack that surfaced these ids (strongly recommended
            after an index retrieval; empty when there is none).
        max_tokens: Maximum response size in tokens (default 4000).
    """
    if not item_ids:
        _raise_invalid_params(
            "item_ids must not be empty",
            data={"field": "item_ids"},
        )
    if len(item_ids) > _GET_ITEMS_MAX_IDS:
        _raise_invalid_params(
            f"too many item_ids: {len(item_ids)} (max {_GET_ITEMS_MAX_IDS})",
            data={"field": "item_ids", "count": len(item_ids)},
        )
    if any(not isinstance(i, str) or not i.strip() for i in item_ids):
        _raise_invalid_params(
            "item_ids entries must be non-empty strings",
            data={"field": "item_ids"},
        )

    registry = _get_registry()
    # Dedup while preserving order — a repeated id must not be charged
    # (or recorded as served) twice.
    unique_ids = list(dict.fromkeys(i.strip() for i in item_ids))

    try:
        fetched: list[dict[str, Any]] = []
        not_found: list[str] = []
        # One judgement per precedent for the whole call.
        precedents = PrecedentClearance(
            registry.knowledge.document_store,
            current_clearance(),
            registry.knowledge.graph_store,
        )
        for item_id in unique_ids:
            resolved = _render_fetched_item(registry, item_id, precedents)
            if resolved is None:
                not_found.append(item_id)
            else:
                kind, body = resolved
                fetched.append({"item_id": item_id, "kind": kind, "body": body})

        result, served_ids, omitted_ids = format_fetched_items_as_markdown(
            fetched,
            not_found=not_found,
            max_tokens=max_tokens,
            pack_id=pack_id.strip() or None,
        )
        result = with_grading_trailer(registry, result, pack_id.strip() or None)

        note_served_items(served_ids)

        # The serve record is the point of the tool (#305): a fetch that
        # cannot be recorded is invisible to attribution, so this emit is
        # loud — same posture as PackBuilder's PACK_ASSEMBLED emit on the
        # serving side. Response-token metering below stays fail-soft.
        registry.operational.event_log.emit(
            EventType.PACK_ITEMS_FETCHED,
            source="mcp:get_items",
            entity_id=pack_id.strip() or None,
            entity_type="pack" if pack_id.strip() else None,
            payload={
                "pack_id": pack_id.strip() or None,
                "requested_item_ids": unique_ids,
                "served_item_ids": served_ids,
                "not_found_item_ids": not_found,
                "omitted_item_ids": omitted_ids,
                "response_tokens": estimate_tokens(result),
                "budget_tokens": max_tokens,
            },
        )
    except McpError:
        raise
    except Exception as exc:
        logger.exception("get_items_failed")
        detail = _exception_detail(exc)
        _raise_internal(
            f"failed to fetch items: {detail}",
            cause=exc,
            data={"tool": "get_items", "item_ids": unique_ids},
        )

    _track_tokens(registry, operation="get_items", result=result, budget=max_tokens)
    return result


# ---------------------------------------------------------------------------
# get_file_context — file-scoped retrieval (#307, server-side half)
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_file_context(
    paths: list[str],
    include_unconfirmed: bool = False,
    max_tokens: int = 2000,
) -> str:
    """Get stored context about specific files before reading or editing them.

    For each path, returns documents whose ``source_path`` names that file
    (exact match, or a ``/``-boundary suffix match so absolute paths find
    stored relpaths) plus graph entities doc-linked to those documents.
    Every item carries its store timestamps and each path a ``Newest
    memory`` line, so a caller can staleness-gate: if the file's mtime is
    newer than the newest memory, the context is about an older version
    of the file.

    Args:
        paths: File paths to look up (relative or absolute).
        include_unconfirmed: Also surface unconfirmed extraction mints
            (excluded by default — extraction attests mention, not fact).
        max_tokens: Maximum response size in tokens (default 2000).
    """
    _refuse_public_caller("get_file_context")
    cleaned = [p.strip() for p in paths if p and p.strip()] if paths else []
    if not cleaned:
        _raise_invalid_params(
            "paths must contain at least one non-empty path",
            data={"field": "paths"},
        )

    registry = _get_registry()
    file_context = build_file_context(
        registry.knowledge.document_store,
        registry.knowledge.graph_store,
        cleaned,
        include_unconfirmed=include_unconfirmed,
        clearance=current_clearance(),
    )
    result = format_file_context_as_markdown(file_context, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_file_context",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="get_file_context")
    return result
