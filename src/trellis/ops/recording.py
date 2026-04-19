"""record_outcome — append a governed-call signal to the OutcomeStore.

Thin wrapper that constructs an :class:`OutcomeEvent` from kwargs and
persists it.  Uses the sentinel pattern so callers never need to import
:class:`ComponentOutcome` directly in the hot path — they pass the
call's measurements as kwargs.

The helper is deliberately small: tuneable components should measure
around the governed work, then call ``record_outcome(...)`` once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.stores.base.outcome import OutcomeStore

logger = structlog.get_logger(__name__)


def record_outcome(
    store: OutcomeStore,
    *,
    component_id: str,
    success: bool,
    latency_ms: float,
    params_version: str | None = None,
    domain: str | None = None,
    intent_family: str | None = None,
    tool_name: str | None = None,
    phase: str | None = None,
    agent_role: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    pack_id: str | None = None,
    trace_id: str | None = None,
    items_served: int | None = None,
    items_referenced: int | None = None,
    metrics: dict[str, float] | None = None,
    error: str | None = None,
    occurred_at: datetime | None = None,
    cohort: str | None = None,
    segment: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OutcomeEvent:
    """Build an :class:`OutcomeEvent` and append it to ``store``.

    Returns the stored event so callers can reference its ``event_id``
    or link it in their own telemetry.  Never raises on store errors —
    recording is advisory and must not break the hot path; errors are
    logged and a placeholder event is returned.
    """
    outcome = ComponentOutcome(
        success=success,
        latency_ms=latency_ms,
        items_served=items_served,
        items_referenced=items_referenced,
        metrics=dict(metrics) if metrics else {},
        error=error,
    )
    kwargs: dict[str, Any] = {
        "component_id": component_id,
        "params_version": params_version,
        "domain": domain,
        "intent_family": intent_family,
        "tool_name": tool_name,
        "phase": phase,
        "agent_role": agent_role,
        "agent_id": agent_id,
        "run_id": run_id,
        "session_id": session_id,
        "pack_id": pack_id,
        "trace_id": trace_id,
        "cohort": cohort,
        "segment": segment,
        "metadata": dict(metadata) if metadata else {},
        "outcome": outcome,
    }
    if occurred_at is not None:
        kwargs["occurred_at"] = occurred_at

    event = OutcomeEvent(**kwargs)
    try:
        store.append(event)
    except Exception:
        logger.exception(
            "record_outcome.append_failed",
            component_id=component_id,
            event_id=event.event_id,
        )
    return event
