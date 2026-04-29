"""Bridge from the EventLog into the observation shape consumed by
:func:`trellis.learning.scoring.analyze_learning_observations`.

The Trellis dual-loop has two halves (CLAUDE.md): the EventLog-authoritative
demote path (effectiveness analysis + advisory fitness) and the JSONL-based
promote path (per-precedent learning scoring). The demote half has full
end-to-end wiring through ``run_effectiveness_feedback`` /
``run_advisory_fitness_loop``; until 2026-04-29, the promote half had a
fully implemented :func:`analyze_learning_observations` in
``trellis.learning.scoring`` but **no caller** producing real
observations from a live feedback loop. Only synthetic unit-test
fixtures fed it.

This module closes that gap by joining ``PACK_ASSEMBLED`` and
``FEEDBACK_RECORDED`` events on ``pack_id`` and producing the shape
``analyze_learning_observations`` expects. It is the EventLog-authoritative
counterpart to a future file-only ``pack_feedback.jsonl``-driven bridge —
that variant is intentionally deferred (see plan §5.5.2 row 2 follow-up)
because the JSONL alone does not carry the per-item ``item_type`` /
``source_strategy`` details ``learning.scoring`` needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Default scan limit for either event type. Matches the limit used by
#: ``analyze_effectiveness`` for symmetry with the demote half.
_DEFAULT_EVENT_LIMIT = 1000


def build_learning_observations_from_event_log(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> list[dict[str, Any]]:
    """Join PACK_ASSEMBLED + FEEDBACK_RECORDED into learning observations.

    Each returned observation describes one *graded pack*: the agent
    asked for context (PACK_ASSEMBLED), used some subset of it
    (FEEDBACK_RECORDED.helpful_item_ids), and reported a pack-level
    outcome. ``analyze_learning_observations`` then aggregates these
    by ``(intent_family, item_id)`` and proposes promotion / noise
    candidates for human review.

    Joining strategy: pack_id is the canonical join key. Feedback
    events without a ``pack_id`` payload field (legacy / hand-emitted)
    fall back to ``entity_id``. Packs without matching feedback are
    excluded — they have no outcome to attribute.

    Args:
        event_log: Source event log.
        days: Look-back window for both event types.
        limit: Per-event-type scan limit. ``analyze_effectiveness`` uses
            the same default; matching here keeps the two halves of
            the dual-loop symmetric.

    Returns:
        List of observations in the shape consumed by
        :func:`~trellis.learning.scoring.analyze_learning_observations`.
        Empty when no feedback exists, or when no pack feedback can be
        joined to a pack assembly event.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)

    pack_events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        limit=limit,
    )
    feedback_events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        since=since,
        limit=limit,
    )

    # pack_id → PACK_ASSEMBLED.payload
    pack_payloads: dict[str, dict[str, Any]] = {}
    for event in pack_events:
        pack_id = event.entity_id
        if pack_id:
            pack_payloads[pack_id] = event.payload

    observations: list[dict[str, Any]] = []
    for event in feedback_events:
        payload = event.payload or {}
        pack_id = str(payload.get("pack_id") or event.entity_id or "").strip()
        if not pack_id:
            continue
        pack_payload = pack_payloads.get(pack_id)
        if pack_payload is None:
            continue
        observations.append(_join_one(payload, pack_payload))

    logger.debug(
        "learning_observations_built",
        observations=len(observations),
        pack_events=len(pack_events),
        feedback_events=len(feedback_events),
        days=days,
    )
    return observations


def _join_one(
    feedback_payload: Mapping[str, Any],
    pack_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine one feedback payload + its pack payload into one observation."""
    items: list[dict[str, Any]] = []
    for raw in pack_payload.get("injected_items", []) or []:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {
            "item_id": raw.get("item_id"),
            "item_type": raw.get("item_type"),
        }
        # ``analyze_learning_observations`` reads ``source_strategy``;
        # the PackBuilder telemetry stamps the same concept under
        # ``strategy_source``. Map for the bridge so downstream
        # candidate fields populate.
        strategy_source = raw.get("strategy_source")
        if strategy_source:
            item["source_strategy"] = strategy_source
        title = raw.get("title")
        if title:
            item["title"] = title
        category = raw.get("category")
        if category:
            item["category"] = category
        domain_system = raw.get("domain_system")
        if domain_system:
            item["domain_system"] = domain_system
        items.append(item)

    observation: dict[str, Any] = {
        "run_id": feedback_payload.get("run_id") or "unknown-run",
        "intent_family": (
            feedback_payload.get("intent_family")
            or pack_payload.get("intent_family")
            or ""
        ),
        "outcome": feedback_payload.get("outcome") or (
            "success" if feedback_payload.get("success") else "failure"
        ),
        "phase": feedback_payload.get("phase") or "",
        "items": items,
    }

    # Optional fields — only set when present so analyze sees defaults.
    domain = pack_payload.get("domain") or feedback_payload.get("domain")
    if domain:
        observation["domain"] = domain
    seed_entity_ids = pack_payload.get("seed_entity_ids") or pack_payload.get(
        "target_entity_ids"
    )
    if seed_entity_ids:
        observation["seed_entity_ids"] = list(seed_entity_ids)
    if "had_retry" in feedback_payload:
        observation["had_retry"] = bool(feedback_payload["had_retry"])
    if "injected" in feedback_payload:
        observation["injected"] = bool(feedback_payload["injected"])
    selection_efficiency = pack_payload.get("selection_efficiency")
    if isinstance(selection_efficiency, int | float):
        observation["selection_efficiency"] = float(selection_efficiency)

    return observation


__all__ = ["build_learning_observations_from_event_log"]
