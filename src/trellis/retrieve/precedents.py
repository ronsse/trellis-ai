"""Shared precedent listing logic."""

from __future__ import annotations

from typing import Any

from trellis.stores.base.event_log import EventLog, EventType


def list_precedents(
    event_log: EventLog,
    *,
    domain: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Query PRECEDENT_PROMOTED events, optionally filtered by domain.

    Returns a list of dicts (newest precedent first) with event_id,
    entity_id, title, description, domain, and occurred_at. The
    underlying ``get_events`` call uses ``order="desc"`` so the
    ``limit`` cap shows the most recently promoted precedents — with
    the default ``order="asc"`` the listing would show the *oldest*
    precedents and miss any new ones once the table grows past
    ``limit`` rows.

    Note: when ``domain`` is set the post-filter applies *after*
    truncation, so a small ``limit`` combined with a domain filter
    can return fewer than ``limit`` rows even if more matches exist.
    Callers needing strict per-domain pagination should add a
    backend-side ``domain`` predicate.
    """
    events = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED,
        limit=limit,
        order="desc",
    )

    if domain:
        events = [e for e in events if e.payload.get("domain") == domain]

    return [
        {
            "event_id": e.event_id,
            "entity_id": e.entity_id,
            "title": e.payload.get("title", ""),
            "description": e.payload.get("description", ""),
            "domain": e.payload.get("domain"),
            "occurred_at": e.occurred_at.isoformat(),
            "payload": e.payload,
        }
        for e in events
    ]
