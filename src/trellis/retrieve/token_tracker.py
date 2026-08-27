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
    pack_id: str | None = None,
) -> None:
    """Record token usage event.

    ``pack_id`` is the join key that makes response cost attributable.
    Without it a ``TOKEN_TRACKED`` event records *that* a retrieval cost
    N tokens but not *which* retrieval, so the per-call half of
    :mod:`trellis.retrieve.pack_value` cannot price a specific pack
    against the items it later got cited for. It is optional because most
    tracked operations legitimately have no pack — ``get_graph``,
    ``get_lessons``, ``get_items`` and ``get_file_context`` return
    formatted results that never went through ``PackBuilder``. Supply it
    only where a pack actually exists; a fabricated id would be worse
    than the absence it replaces.

    Args:
        event_log: Event log to write to.
        layer: Response layer — "cli", "mcp", or "sdk".
        operation: Tool or command name.
        response_tokens: Estimated tokens in the response.
        budget_tokens: Token budget that was requested, if any.
        trimmed: Whether the response was auto-trimmed.
        agent_id: Optional agent identifier.
        pack_id: The ``PACK_ASSEMBLED`` id this response rendered, when
            the operation assembled a pack. ``None`` for pack-free
            operations.
    """
    payload: dict[str, Any] = {
        "layer": layer,
        "operation": operation,
        "response_tokens": response_tokens,
        "budget_tokens": budget_tokens,
        "trimmed": trimmed,
        "agent_id": agent_id,
        "pack_id": pack_id,
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
        pack_id=pack_id,
    )
