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

    Returns a list of dicts with event_id, entity_id, title, description,
    domain, and occurred_at.
    """
    events = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED,
        limit=limit,
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
