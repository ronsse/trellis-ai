"""Explore routes -- read-only browsing of documents, events, packs, history.

The Memory Explorer surface: everything here is a GET over data the
other routers wrote. No mutations, so the whole router mounts under
the ``read`` scope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from trellis.stores.base.event_log import Event, EventType
from trellis_api.app import get_registry

router = APIRouter()

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


@router.get("/documents")
def list_documents(
    q: str | None = Query(None, description="Full-text search query"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset (ignored when q is set)"),
) -> dict[str, Any]:
    """List or search documents, returning previews only."""
    registry = get_registry()
    store = registry.knowledge.document_store
    if q:
        docs = store.search(q, limit=limit)
    else:
        docs = store.list_documents(limit=limit, offset=offset)
    rows = [_document_row(d) for d in docs]
    return {
        "status": "ok",
        "total": store.count(),
        "count": len(rows),
        "offset": offset,
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
    """Serialize an event; strip large payloads to a summary unless asked."""
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


@router.get("/events")
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
    """Tail or filter the event log.

    ``EventLog.get_events`` has no offset — page older history by
    passing the oldest ``occurred_at`` you have as ``until``.
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
    }


@router.get("/packs")
def list_packs(
    limit: int = Query(50, ge=1, le=500, description="Max results"),
) -> dict[str, Any]:
    """List assembled context packs (newest first), summary fields only."""
    registry = get_registry()
    event_log = registry.operational.event_log
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, limit=limit, order="desc"
    )
    return {
        "status": "ok",
        "total": event_log.count(event_type=EventType.PACK_ASSEMBLED),
        "count": len(events),
        "packs": [_pack_summary(e) for e in events],
    }


@router.get("/packs/{pack_id}")
def get_pack(pack_id: str) -> dict[str, Any]:
    """Full pack telemetry: what was injected, rejected, and why.

    Joins related feedback the same way the learning loop does —
    ``FEEDBACK_RECORDED.payload["pack_id"]`` (see
    ``trellis.learning.pack_observations.join_pack_feedback``).
    """
    registry = get_registry()
    event_log = registry.operational.event_log
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=1
    )
    if not events:
        raise HTTPException(status_code=404, detail=f"Pack not found: {pack_id}")
    event = events[0]
    feedback = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        payload_filters={"pack_id": pack_id},
        limit=50,
    )
    return {
        "status": "ok",
        "pack": {
            **_pack_summary(event),
            "payload": event.payload or {},
        },
        "feedback": [f.model_dump(mode="json") for f in feedback],
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
