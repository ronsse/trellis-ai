"""Feedback recording — JSONL append-log for pack feedback signals."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from trellis.feedback.models import PackFeedback
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)


def record_feedback(
    feedback: PackFeedback,
    *,
    log_dir: Path | str,
    event_log: EventLog | None = None,
    pack_id: str | None = None,
) -> Path:
    """Append a feedback signal to the JSONL log.

    Creates the log directory and file if they don't exist.  When
    ``event_log`` is provided, also emits a ``FEEDBACK_RECORDED`` event
    so :class:`~trellis.retrieve.advisory_generator.AdvisoryGenerator`
    and :func:`~trellis.retrieve.effectiveness.analyze_effectiveness`
    pick up the signal.  The JSONL append is the authoritative file
    record; event emission bridges this feedback into the governed
    analytics path.  Event-emit failures are logged but do not raise —
    the file write is the durability guarantee.

    Args:
        feedback: The feedback signal to record.
        log_dir: Directory for the feedback log
            (e.g. ``artifacts/runs/{run_id}/experience/``).
        event_log: Optional event log to also emit ``FEEDBACK_RECORDED``
            to.  When ``None`` (default), behavior is file-only —
            matching fd-poc-style workflows that consume the JSONL log
            directly.
        pack_id: Pack identifier for the event.  Used as both the
            event's ``entity_id`` and ``payload.pack_id`` so
            AdvisoryGenerator can join with ``PACK_ASSEMBLED`` events.
            Ignored when ``event_log`` is ``None``.

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

    logger.debug(
        "feedback_recorded",
        run_id=feedback.run_id,
        phase=feedback.phase,
        outcome=feedback.outcome,
        items_served=len(feedback.items_served),
        log_path=str(log_path),
        event_log_emitted=event_log is not None,
    )
    return log_path


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
