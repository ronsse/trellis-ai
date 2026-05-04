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

    When ``domain`` is set the predicate is pushed into the backend via
    :paramref:`EventLog.get_events.payload_filters`, so the ``limit``
    cap applies *after* the filter. The previous post-fetch shape would
    truncate the SQL window and then drop non-matching rows in Python,
    silently returning fewer than ``limit`` matches even when more
    matches existed beyond the cap.
    """
    payload_filters = {"domain": domain} if domain else None
    events = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED,
        limit=limit,
        order="desc",
        payload_filters=payload_filters,
    )

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
