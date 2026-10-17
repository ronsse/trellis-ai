"""Explore routes -- read-only browsing of documents, events, packs, history.

The Memory Explorer surface: everything here is a GET over data the
other routers wrote. No mutations, but not every route here is open to
the same caller: the document and graph-history routes mount under the
``read`` scope same as the rest of this router, while the event and
pack-telemetry routes (``/events``, ``/events/{event_id}``, ``/packs``,
``/packs/{pack_id}``) carry their own route-level ``admin`` requirement
below, because they replay other callers' pack telemetry -- intents,
served item ids and titles, attribution -- which a ``read`` key (what an
agent or connector holds) should not be able to read back wholesale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from trellis.auth import SCOPE_ADMIN
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT, Event, EventType
from trellis_api.app import get_registry
from trellis_api.auth import require_scope
from trellis_wire.dtos import DefaultValidationError, StringDetailError

router = APIRouter()

#: Route-level override for the four pack-telemetry routes below
#: (``/events``, ``/events/{event_id}``, ``/packs``, ``/packs/{pack_id}``).
#: One name shared by all four so the requirement reads in one place
#: rather than as four separately-typed decorators.
_ADMIN_ONLY = [Depends(require_scope(SCOPE_ADMIN))]

#: Small, always-cheap payload fields surfaced in event list rows when
#: the full payload is suppressed (``include_payload=false``).
_PAYLOAD_SUMMARY_KEYS = (
    "intent",
    "domain",
    "agent_id",
    "items_count",
    "candidates_found",
    "rating",
    "pack_id",
    "target_id",
    "doc_id",
    "operation",
)

_PREVIEW_CHARS = 300

#: ``DocumentStore.search`` takes no offset and nothing counts a query's
#: total matches, unlike ``list_documents``/``count`` for the unfiltered
#: listing. Paging a search the same way the list is paged means capturing
#: a bounded window of matches once and slicing it in Python; this is that
#: bound. A query that matches at least this many documents gets
#: ``total_exact: false`` rather than a silently wrong exact total.
_SEARCH_SCAN_LIMIT = 2000


def _document_row(doc: dict[str, Any]) -> dict[str, Any]:
    """Shape a stored document into a list row: preview, never full content."""
    content = doc.get("content") or ""
    row = {
        "doc_id": doc.get("doc_id"),
        "preview": content[:_PREVIEW_CHARS],
        "content_length": len(content),
        "metadata": doc.get("metadata") or {},
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
    if "rank" in doc:
        row["rank"] = doc["rank"]
    return row


# Chunks are excluded from this view by default (#385). Corpus ingestion
# writes a ``<parent>#chunk-N`` row per embeddable slice alongside the
# parent it was cut from, and on the reference deployment those are 740 of
# 1,317 rows (56%) — so the unfiltered listing an operator sees is mostly
# fragments of documents the same listing already shows.
#
# Three things this deliberately is not:
#
# * Not a filter over the returned page. The exclusion is pushed into the
#   store (``include_chunks``) because a page filtered after the fact would
#   return ~22 rows for ``limit=50`` and the caller could not tell a short
#   page from the end of the data. Chunks are also not evenly spread: the
#   newest 50 rows on the reference deployment contain none, so the page
#   composition swings with the offset.
# * Not a recall loss on ``q``. The parent row holds the full content its
#   chunks were sliced from, so it matches whatever they match.
# * Not hidden. Chunks stay addressable at ``GET /documents/{doc_id}``
#   (percent-encode the ``#`` — an unencoded chunk id is truncated at the
#   fragment delimiter before the request leaves the client; the Memory
#   Explorer already calls ``encodeURIComponent``), every other
#   ``list_documents`` caller still sees them (the store default is
#   ``include_chunks=True``), and ``?include_chunks=true`` restores the
#   old response here.
@router.get("/documents")
def list_documents(
    q: str | None = Query(None, description="Full-text search query"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset, applied to search results too"),
    include_chunks: bool = Query(
        False,
        description=(
            "Include <parent>#chunk-N fragment rows. Excluded by default:"
            " they are slices of documents already in the list."
        ),
    ),
) -> dict[str, Any]:
    """List or search documents, returning previews only.

    ``<parent>#chunk-N`` fragment rows are excluded by default; pass
    ``include_chunks=true`` for the unfiltered listing. ``total`` is
    counted under the same filter as the rows, and the applied setting is
    echoed back on the response.

    A search (``q`` set) is paged the same way the unfiltered list is:
    ``offset`` and ``limit`` both apply to the matches. Nothing in
    ``DocumentStore`` counts a query's total matches or offsets into them,
    so this captures up to :data:`_SEARCH_SCAN_LIMIT` matches once and
    pages that capture in Python; ``total`` and ``total_exact`` describe
    the capture, so a query matching at least the scan bound reports
    ``total_exact: false`` instead of a silently wrong exact total.
    """
    registry = get_registry()
    store = registry.knowledge.document_store
    if q:
        matches = store.search(
            q, limit=_SEARCH_SCAN_LIMIT, include_chunks=include_chunks
        )
        total = len(matches)
        total_exact = total < _SEARCH_SCAN_LIMIT
        docs = matches[offset : offset + limit]
    else:
        docs = store.list_documents(
            limit=limit, offset=offset, include_chunks=include_chunks
        )
        total = store.count(include_chunks=include_chunks)
        total_exact = True
    rows = [_document_row(d) for d in docs]
    return {
        "status": "ok",
        "total": total,
        "total_exact": total_exact,
        "count": len(rows),
        "offset": offset,
        "include_chunks": include_chunks,
        "documents": rows,
    }


@router.get("/documents/{doc_id:path}")
def get_document(doc_id: str) -> dict[str, Any]:
    """Get a single document with full content and metadata."""
    registry = get_registry()
    doc = registry.knowledge.document_store.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")
    return {"status": "ok", "document": doc}


def _event_row(event: Event, *, include_payload: bool) -> dict[str, Any]:
    """Serialize an event; strip large payloads to a summary unless asked.

    Every route that calls this already requires the ``admin`` scope
    (``_ADMIN_ONLY``), so the would-be pack on a held-out row is never
    blinded here -- there is no caller of this function without admin.
    """
    row = event.model_dump(mode="json")
    if not include_payload:
        payload = row.pop("payload", {}) or {}
        row["payload_keys"] = sorted(payload)
        row["payload_summary"] = {
            k: payload[k] for k in _PAYLOAD_SUMMARY_KEYS if k in payload
        }
    return row


def _parse_event_type(event_type: str | None) -> EventType | None:
    if event_type is None:
        return None
    try:
        return EventType(event_type)
    except ValueError:
        valid = ", ".join(e.value for e in EventType)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event_type '{event_type}'. Valid values: {valid}",
        ) from None


@router.get(
    "/events",
    dependencies=_ADMIN_ONLY,
    responses={
        403: {"description": "The caller's key lacks the admin scope."},
        422: {
            "description": (
                "A query parameter fails its declared type "
                "(DefaultValidationError -- ``detail`` is a list of field "
                "errors), or an unknown `event_type` or an `order` outside "
                "{'asc', 'desc'} (StringDetailError -- ``detail`` is a "
                "plain string naming the bad value)."
            ),
            "model": DefaultValidationError | StringDetailError,
        },
    },
)
def list_events(
    event_type: str | None = Query(None, description="Filter by event type value"),
    entity_id: str | None = Query(None, description="Filter by subject entity"),
    source: str | None = Query(None, description="Filter by emitting component"),
    since: datetime | None = Query(None, description="Lower bound (inclusive)"),  # noqa: B008 — FastAPI DI idiom
    until: datetime | None = Query(None, description="Upper bound (inclusive)"),  # noqa: B008 — FastAPI DI idiom
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    order: str = Query("desc", description="asc or desc (default desc = tail view)"),
    include_payload: bool = Query(
        False, description="Include full payloads (large for pack.assembled)"
    ),
) -> dict[str, Any]:
    """Tail or filter the event log. Requires the ``admin`` scope.

    ``EventLog.get_events`` has no offset — page older history by
    passing the oldest ``occurred_at`` you have as ``until``. Every
    caller who reaches this route already has ``admin``, so the
    would-be pack on a withheld pack's row is never blinded here.
    """
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="order must be 'asc' or 'desc'")
    parsed_type = _parse_event_type(event_type)
    registry = get_registry()
    event_log = registry.operational.event_log
    events = event_log.get_events(
        event_type=parsed_type,
        entity_id=entity_id,
        source=source,
        since=since,
        until=until,
        limit=limit,
        order=order,  # type: ignore[arg-type]
    )
    return {
        "status": "ok",
        "total": event_log.count(event_type=parsed_type, since=since),
        "count": len(events),
        "events": [_event_row(e, include_payload=include_payload) for e in events],
        "event_types": [e.value for e in EventType],
    }


@router.get(
    "/events/{event_id}",
    dependencies=_ADMIN_ONLY,
    responses={403: {"description": "The caller's key lacks the admin scope."}},
)
def get_event(event_id: str) -> dict[str, Any]:
    """Fetch one event by id, reaching the whole log, not just a window.
    Requires the ``admin`` scope.

    ``GET /events`` pages a recency window via ``until``; an event older
    than whatever window a caller has loaded cannot be reached through
    it by id alone. This route does a direct lookup instead. Every
    caller who reaches this route already has ``admin``, so the
    would-be pack on a withheld pack's row is never blinded here, same
    as the list and pack-detail views.
    """
    registry = get_registry()
    event_log = registry.operational.event_log
    event = event_log.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event not found: {event_id}")
    return {
        "status": "ok",
        "event": _event_row(event, include_payload=True),
    }


def _is_sectioned(payload: dict[str, Any]) -> bool:
    """A sectioned pack (``PackBuilder.build_sectioned``) emits ``sections``
    and no ``injected_items[]`` — the same discriminator ``pack_value.py``
    uses to exclude sectioned packs from per-item attribution. A flat pack
    contributes ``injected_items[]`` and, before #371, could legitimately
    have none (zero candidates); checking ``sections`` first keeps that
    case flagged as flat, not sectioned.
    """
    return bool(payload.get("sections")) and not payload.get("injected_items")


def _pack_summary(event: Event) -> dict[str, Any]:
    payload = event.payload or {}
    return {
        "pack_id": event.entity_id,
        "created_at": event.occurred_at.isoformat(),
        "intent": payload.get("intent"),
        "domain": payload.get("domain"),
        "agent_id": payload.get("agent_id"),
        "session_id": payload.get("session_id"),
        "items_count": payload.get("items_count"),
        "candidates_found": payload.get("candidates_found"),
        "strategies_used": payload.get("strategies_used", []),
        "reranker": payload.get("reranker"),
        "sectioned": _is_sectioned(payload),
    }


@router.get(
    "/packs",
    dependencies=_ADMIN_ONLY,
    responses={403: {"description": "The caller's key lacks the admin scope."}},
)
def list_packs(
    until: datetime | None = Query(  # noqa: B008 — FastAPI DI idiom
        None,
        description=(
            "Upper bound (inclusive) on assembly time. Page older history"
            " by passing the oldest created_at you have."
        ),
    ),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
) -> dict[str, Any]:
    """List assembled context packs (newest first), summary fields only.

    Requires the ``admin`` scope: summaries still carry each pack's
    ``intent`` and other callers' session metadata.
    """
    registry = get_registry()
    event_log = registry.operational.event_log
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, until=until, limit=limit, order="desc"
    )
    return {
        "status": "ok",
        "total": event_log.count(event_type=EventType.PACK_ASSEMBLED),
        "count": len(events),
        "packs": [_pack_summary(e) for e in events],
    }


@router.get(
    "/packs/{pack_id}",
    dependencies=_ADMIN_ONLY,
    responses={403: {"description": "The caller's key lacks the admin scope."}},
)
def get_pack(pack_id: str) -> dict[str, Any]:
    """Full pack telemetry: what was injected, rejected, and why.
    Requires the ``admin`` scope.

    Joins related feedback the same way the learning loop does —
    ``FEEDBACK_RECORDED.payload["pack_id"]`` (see
    ``trellis.learning.pack_observations.join_pack_feedback``). Every
    caller who reaches this route already has ``admin``, so the
    would-be pack on a withheld pack's row is never blinded here.

    ``feedback`` stays capped at 50 rows, but ``feedback_total`` (the
    true count, read via a bounded ``DEFAULT_SCAN_LIMIT`` over-fetch
    since :meth:`EventLog.count` cannot filter by ``payload_filters``)
    and ``feedback_truncated`` (``feedback_total > len(feedback)``) now
    say when the 50-row list is not the whole picture (P1-9).
    """
    registry = get_registry()
    event_log = registry.operational.event_log
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=1
    )
    if not events:
        raise HTTPException(status_code=404, detail=f"Pack not found: {pack_id}")
    event = events[0]
    # One bounded fetch serves both the displayed slice and the true
    # total, so the two can never disagree about which 50 rows they
    # describe. A pack accumulating >= DEFAULT_SCAN_LIMIT feedback rows
    # is not expected in practice; ``feedback_total`` would then read as
    # a floor rather than an exact count, same as any other capped scan.
    feedback_scan = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        payload_filters={"pack_id": pack_id},
        limit=DEFAULT_SCAN_LIMIT,
    )
    feedback_total = len(feedback_scan)
    feedback = feedback_scan[:50]
    payload = event.payload or {}
    return {
        "status": "ok",
        "pack": {
            **_pack_summary(event),
            "payload": payload,
        },
        # Always False: this route requires the ``admin`` scope
        # (``_ADMIN_ONLY``), and only a caller without it ever gets a
        # held-out pack's fields stripped. The key stays in the response
        # (the UI reads it) so a future non-admin reader of this route
        # would not need a wire-shape change to carry the real answer.
        "holdout_blinded": False,
        "feedback": [f.model_dump(mode="json") for f in feedback],
        "feedback_total": feedback_total,
        "feedback_truncated": feedback_total > len(feedback),
    }


@router.get("/graph/history")
def get_node_history(
    entity_id: str = Query(..., description="Node ID to fetch SCD-2 history for"),
) -> dict[str, Any]:
    """All versions of a graph node, newest first (SCD-2 audit trail).

    Query param rather than a path segment because
    ``GET /entities/{entity_id:path}`` greedily matches any suffix.
    """
    registry = get_registry()
    versions = registry.knowledge.graph_store.get_node_history(entity_id)
    if not versions:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")
    return {
        "status": "ok",
        "entity_id": entity_id,
        "count": len(versions),
        "versions": versions,
    }
