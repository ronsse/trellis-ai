"""Token usage analysis."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import EventLog, EventType


class TokenUsageReport(TrellisModel):
    """Aggregated token usage report."""

    total_responses: int
    total_tokens: int
    avg_tokens_per_response: float
    by_layer: dict[str, dict[str, Any]]
    by_operation: list[dict[str, Any]]
    over_budget: list[dict[str, Any]]


def analyze_token_usage(
    event_log: EventLog,
    *,
    days: int = 7,
) -> TokenUsageReport:
    """Analyze token usage across all layers.

    Returns breakdown by layer (CLI/MCP/SDK), by operation,
    and highlights responses that exceeded their token budget.

    Args:
        event_log: Event log to query.
        days: Number of days of history to analyze.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)
    events = event_log.get_events(
        event_type=EventType.TOKEN_TRACKED,
        since=since,
        limit=5000,
    )

    # Aggregate by layer
    layer_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total_tokens": 0}
    )
    # Aggregate by operation
    op_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total_tokens": 0}
    )
    # Over-budget responses
    over_budget: list[dict[str, Any]] = []

    total_tokens = 0
    for event in events:
        payload = event.payload
        layer = payload.get("layer", "unknown")
        operation = payload.get("operation", "unknown")
        response_tokens = payload.get("response_tokens", 0)
        budget_tokens = payload.get("budget_tokens")

        total_tokens += response_tokens

        layer_stats[layer]["count"] += 1
        layer_stats[layer]["total_tokens"] += response_tokens

        op_key = f"{layer}:{operation}"
        op_stats[op_key]["count"] += 1
        op_stats[op_key]["total_tokens"] += response_tokens
        op_stats[op_key]["layer"] = layer
        op_stats[op_key]["operation"] = operation

        if budget_tokens is not None and response_tokens > budget_tokens:
            over_budget.append(
                {
                    "layer": layer,
                    "operation": operation,
                    "response_tokens": response_tokens,
                    "budget_tokens": budget_tokens,
                    "occurred_at": event.occurred_at.isoformat(),
                }
            )

    total_responses = len(events)
    avg_tokens = total_tokens / total_responses if total_responses > 0 else 0.0

    # Compute per-layer averages
    by_layer: dict[str, dict[str, Any]] = {}
    for layer, stats in layer_stats.items():
        count = stats["count"]
        by_layer[layer] = {
            "count": count,
            "total_tokens": stats["total_tokens"],
            "avg_tokens": round(stats["total_tokens"] / count, 1) if count else 0.0,
        }

    # Sort operations by total tokens descending, take top 10
    by_operation = sorted(
        [
            {
                "key": key,
                "layer": info["layer"],
                "operation": info["operation"],
                "count": info["count"],
                "total_tokens": info["total_tokens"],
                "avg_tokens": round(info["total_tokens"] / info["count"], 1)
                if info["count"]
                else 0.0,
            }
            for key, info in op_stats.items()
        ],
        key=lambda x: x["total_tokens"],
        reverse=True,
    )[:10]

    return TokenUsageReport(
        total_responses=total_responses,
        total_tokens=total_tokens,
        avg_tokens_per_response=avg_tokens,
        by_layer=by_layer,
        by_operation=by_operation,
        over_budget=over_budget,
    )
