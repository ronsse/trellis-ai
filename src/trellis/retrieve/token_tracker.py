"""Token usage tracking and reporting."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token)."""
    return len(text) // 4 + 1


def track_token_usage(
    event_log: EventLog,
    *,
    layer: str,
    operation: str,
    response_tokens: int,
    budget_tokens: int | None = None,
    trimmed: bool = False,
    agent_id: str | None = None,
) -> None:
    """Record token usage event.

    Args:
        event_log: Event log to write to.
        layer: Response layer — "cli", "mcp", or "sdk".
        operation: Tool or command name.
        response_tokens: Estimated tokens in the response.
        budget_tokens: Token budget that was requested, if any.
        trimmed: Whether the response was auto-trimmed.
        agent_id: Optional agent identifier.
    """
    payload: dict[str, Any] = {
        "layer": layer,
        "operation": operation,
        "response_tokens": response_tokens,
        "budget_tokens": budget_tokens,
        "trimmed": trimmed,
        "agent_id": agent_id,
    }
    event_log.emit(
        EventType.TOKEN_TRACKED,
        source=f"{layer}:{operation}",
        payload=payload,
    )
    logger.debug(
        "token_usage_tracked",
        layer=layer,
        operation=operation,
        response_tokens=response_tokens,
        budget_tokens=budget_tokens,
        trimmed=trimmed,
    )
