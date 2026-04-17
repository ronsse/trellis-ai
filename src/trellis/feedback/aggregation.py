"""Feedback aggregation — compute per-item effectiveness from pack signals."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.feedback.models import PackFeedback

logger = structlog.get_logger(__name__)


def compute_item_effectiveness(
    signals: list[PackFeedback],
) -> dict[str, dict[str, Any]]:
    """Aggregate feedback signals to compute per-item effectiveness scores.

    For each item that was served, computes:
    - times_served: how often it appeared in packs
    - times_referenced: how often the agent actually used it
    - success_rate: fraction of deliveries where the phase succeeded
    - reference_rate: fraction of deliveries where the item was referenced
    - intent_families: which retrieval intent families it supported

    Items with high serve count but low reference_rate are candidates for
    demotion. Items with high reference_rate and success_rate should be boosted.

    Args:
        signals: List of PackFeedback signals to aggregate.

    Returns:
        Dict mapping item_id → effectiveness metrics.
    """
    stats: dict[str, dict[str, Any]] = {}

    for signal in signals:
        referenced_set = set(signal.items_referenced)
        is_success = signal.outcome in ("success", "completed")

        for item_id in signal.items_served:
            if item_id not in stats:
                stats[item_id] = {
                    "times_served": 0,
                    "times_referenced": 0,
                    "success_count": 0,
                    "intent_families": set(),
                }
            stats[item_id]["times_served"] += 1
            if item_id in referenced_set:
                stats[item_id]["times_referenced"] += 1
            if is_success:
                stats[item_id]["success_count"] += 1
            intent_family = str(signal.intent_family).strip()
            if intent_family:
                stats[item_id]["intent_families"].add(intent_family)

    for s in stats.values():
        served = s["times_served"]
        s["reference_rate"] = s["times_referenced"] / served if served else 0.0
        s["success_rate"] = s["success_count"] / served if served else 0.0
        s["intent_families"] = sorted(s["intent_families"])

    logger.debug(
        "item_effectiveness_computed",
        signals_processed=len(signals),
        items_scored=len(stats),
    )
    return stats
