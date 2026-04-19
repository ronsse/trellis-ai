"""Feedback recording — JSONL append-log for pack feedback signals."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from trellis.feedback.models import PackFeedback
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.outcome import OutcomeStore

logger = structlog.get_logger(__name__)

#: Default component id used when bridging PackFeedback into an OutcomeEvent.
#: Callers can override per-call via the ``component_id`` kwarg on
#: :func:`record_feedback`.
_DEFAULT_COMPONENT_ID = "retrieve.pack_builder.PackBuilder"


def record_feedback(
    feedback: PackFeedback,
    *,
    log_dir: Path | str,
    event_log: EventLog | None = None,
    outcome_store: OutcomeStore | None = None,
    pack_id: str | None = None,
    component_id: str = _DEFAULT_COMPONENT_ID,
) -> Path:
    """Append a feedback signal to the JSONL log.

    Creates the log directory and file if they don't exist.  When
    ``event_log`` is provided, also emits a ``FEEDBACK_RECORDED`` event
    so :class:`~trellis.retrieve.advisory_generator.AdvisoryGenerator`
    and :func:`~trellis.retrieve.effectiveness.analyze_effectiveness`
    pick up the signal.  When ``outcome_store`` is also provided, an
    :class:`~trellis.schemas.outcome.OutcomeEvent` is appended to the
    ops-tier store so tuners can consume it.

    The JSONL append is the authoritative file record; event and
    outcome emission bridge the feedback into the governed analytics
    and ops paths respectively.  Both emissions fail soft — log-only,
    never raise — since the file write is the durability guarantee.

    Args:
        feedback: The feedback signal to record.
        log_dir: Directory for the feedback log
            (e.g. ``artifacts/runs/{run_id}/experience/``).
        event_log: Optional event log to also emit ``FEEDBACK_RECORDED``
            to.  When ``None`` (default), behavior is file-only —
            matching fd-poc-style workflows that consume the JSONL log
            directly.
        outcome_store: Optional :class:`OutcomeStore` to dual-emit an
            ``OutcomeEvent`` bridging PackFeedback into the ops tier.
        pack_id: Pack identifier for the event.  Used as both the
            event's ``entity_id`` and ``payload.pack_id`` so
            AdvisoryGenerator can join with ``PACK_ASSEMBLED`` events,
            and also stored on the OutcomeEvent's ``pack_id`` field.
            Ignored when neither emission sink is provided.
        component_id: Stable component identifier written onto the
            :class:`OutcomeEvent`.  Defaults to the PackBuilder.

    Returns:
        Path to the log file.
    """
    log_path = Path(log_dir) / "pack_feedback.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = asdict(feedback)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")

    if event_log is not None:
        try:
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="feedback.record",
                entity_id=pack_id,
                entity_type="pack" if pack_id else None,
                payload=feedback.to_event_payload(pack_id=pack_id),
            )
        except Exception:
            logger.exception(
                "feedback_event_emit_failed",
                run_id=feedback.run_id,
                pack_id=pack_id,
            )

    if outcome_store is not None:
        try:
            _emit_outcome(
                feedback,
                outcome_store=outcome_store,
                pack_id=pack_id,
                component_id=component_id,
            )
        except Exception:
            logger.exception(
                "feedback_outcome_emit_failed",
                run_id=feedback.run_id,
                pack_id=pack_id,
            )

    logger.debug(
        "feedback_recorded",
        run_id=feedback.run_id,
        phase=feedback.phase,
        outcome=feedback.outcome,
        items_served=len(feedback.items_served),
        log_path=str(log_path),
        event_log_emitted=event_log is not None,
        outcome_emitted=outcome_store is not None,
    )
    return log_path


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` on failure."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _emit_outcome(
    feedback: PackFeedback,
    *,
    outcome_store: OutcomeStore,
    pack_id: str | None,
    component_id: str,
) -> None:
    """Bridge a :class:`PackFeedback` into an :class:`OutcomeEvent`."""
    # Imports deferred to avoid importing ops/schemas in the hot path
    # for callers that never pass an outcome_store.
    from trellis.ops import record_outcome  # noqa: PLC0415

    occurred_at = _parse_timestamp(feedback.timestamp_utc)
    items_served = len(feedback.items_served)
    items_referenced = len(feedback.items_referenced)
    success = feedback.outcome in {"success", "completed"}

    metadata: dict[str, object] = {
        "pack_outcome": feedback.outcome,
        "intent": feedback.intent,
    }
    if feedback.relevance_scores:
        metadata["relevance_scores"] = dict(feedback.relevance_scores)
    if feedback.metadata:
        metadata["feedback_metadata"] = dict(feedback.metadata)

    record_outcome(
        outcome_store,
        component_id=component_id,
        success=success,
        latency_ms=0.0,
        intent_family=feedback.intent_family or None,
        phase=feedback.phase or None,
        agent_id=feedback.agent_id,
        run_id=feedback.run_id,
        pack_id=pack_id,
        items_served=items_served,
        items_referenced=items_referenced,
        occurred_at=occurred_at,
        metadata=metadata,
    )


def load_feedback_log(log_dir: Path | str) -> list[PackFeedback]:
    """Load all feedback signals from a JSONL log.

    Args:
        log_dir: Directory containing pack_feedback.jsonl.

    Returns:
        List of PackFeedback objects in chronological order.
    """
    log_path = Path(log_dir) / "pack_feedback.jsonl"
    if not log_path.exists():
        return []

    signals: list[PackFeedback] = []
    with log_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            signals.append(
                PackFeedback(
                    run_id=data["run_id"],
                    phase=data["phase"],
                    intent=data["intent"],
                    outcome=data["outcome"],
                    items_served=data.get("items_served", []),
                    items_referenced=data.get("items_referenced", []),
                    relevance_scores=data.get("relevance_scores", {}),
                    intent_family=data.get("intent_family", ""),
                    timestamp_utc=data.get("timestamp_utc", ""),
                    agent_id=data.get("agent_id"),
                    metadata=data.get("metadata", {}),
                )
            )

    logger.debug("feedback_log_loaded", count=len(signals), log_path=str(log_path))
    return signals
