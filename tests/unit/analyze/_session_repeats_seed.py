"""Seed helpers for the session-repeats tests: documents, packs and a clock.

The texts are chosen by measurement, not by eye: ``NEAR`` differs from ``BASE``
by one word (3-shingle Jaccard about 0.96), ``LOOSE`` rewords the tail
(about 0.75, under the builder's 0.85 threshold and over 0.5), and ``FRESH``
shares nothing. The loose pair is what tells a threshold the builder uses from
one it does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.stores.base.event_log import Event, EventLog, EventType

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
T0 = NOW - timedelta(hours=3)

BASE = (
    "The failover runbook says to drain the primary node, promote the replica, "
    "verify replication lag is under five seconds, then repoint the service "
    "alias and page the on-call engineer if lag stays above the threshold. "
)
NEAR = BASE.replace("five seconds", "five secs")
LOOSE = BASE.replace(
    "page the on-call engineer if lag stays above the threshold",
    "notify the duty engineer when lag stays high",
)
FRESH = (
    "Quarterly budget review covers hiring plans, cloud spend by team, vendor "
    "renewals and the forecast for the next two quarters of travel costs."
)
OTHER = (
    "Onboarding checklist for new engineers: request laptop, join the chat "
    "workspace, read the style guide and shadow one release before shipping."
)
THIRD = (
    "Capacity planning notes: the storage tier grows about four percent a month, "
    "so the next purchase order should land before the autumn freeze begins."
)
CHUNK_0 = (
    "Part one of the migration notes describes the schema changes, the new "
    "indexes and the order in which the tables must be altered in production."
)
CHUNK_1 = (
    "Part two of the migration notes lists the rollback steps, the verification "
    "queries and the people who must sign off before the cutover window opens."
)


def put_doc(
    stores: Any, doc_id: str, content: str, metadata: dict[str, Any] | None = None
) -> None:
    stores.knowledge.document_store.put(doc_id, content, metadata or {})


def pack(
    log: EventLog,
    pack_id: str,
    session_id: str | None,
    at: datetime,
    items: list[tuple[str, int | None]],
    *,
    holdout: bool = False,
    sectioned: bool = False,
    rows: bool = True,
) -> None:
    """Append one ``PACK_ASSEMBLED`` serving ``items`` (``(item_id, tokens)``)."""
    payload: dict[str, Any] = {
        "intent": "synthetic",
        "injected_item_ids": [item_id for item_id, _ in items],
    }
    if rows:
        payload["injected_items"] = [
            {
                "item_id": item_id,
                **({} if tokens is None else {"estimated_tokens": tokens}),
                "rank": rank,
            }
            for rank, (item_id, tokens) in enumerate(items, start=1)
        ]
    if session_id is not None:
        payload["session_id"] = session_id
    if sectioned:
        payload["sections"] = [
            {"name": "main", "item_ids": [item_id for item_id, _ in items]}
        ]
        if rows:
            for row in payload["injected_items"]:
                row["section"] = "main"
    if holdout:
        payload[HOLDOUT_KEY] = True
    log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            occurred_at=at,
            recorded_at=at,
            payload=payload,
            metadata={},
        )
    )
