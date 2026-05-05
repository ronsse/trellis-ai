"""Feedback recording — JSONL append-log for pack feedback signals."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class FeedbackRecordResult:
    """Outcome of a :func:`record_feedback` call.

    The JSONL append is always attempted and is the durability
    guarantee; ``log_path`` is populated even when downstream emissions
    fail. ``event_log_emitted`` / ``outcome_emitted`` tell callers
    whether the bridged sinks actually received the signal, so a
    retry or reconciliation can be scheduled without scanning logs.

    The error fields surface the last exception caught so callers can
    distinguish "sink not configured" (``*_error is None``) from
    "sink failed" (``*_error is not None``). Emissions still fail
    soft; callers opt into strict mode by checking these fields.
    """

    log_path: Path
    feedback_id: str
    event_log_emitted: bool = False
    outcome_emitted: bool = False
    event_log_error: Exception | None = None
    outcome_error: Exception | None = None
    event_log_skipped_as_duplicate: bool = False

    @property
    def event_log_in_sync(self) -> bool:
        """True when the EventLog has a matching feedback entry.

        Either we emitted successfully this call, or a prior call /
        reconciliation already persisted it (duplicate-skip path).
        """
        return self.event_log_emitted or self.event_log_skipped_as_duplicate


@dataclass
class ReconcileResult:
    """Outcome of :func:`reconcile_feedback_log_to_event_log`."""

    scanned: int = 0
    already_present: int = 0
    emitted: int = 0
    failed: int = 0
    missing_feedback_ids: list[str] = field(default_factory=list)


def _feedback_id_in_event_log(event_log: EventLog, feedback_id: str) -> bool:
    """Return True when the EventLog already has a FEEDBACK_RECORDED event
    with this ``feedback_id`` in its payload.

    Pushes the ``feedback_id`` predicate into the backend via
    ``payload_filters`` so the lookup is a SQL ``WHERE`` against
    ``payload->>'feedback_id'`` (Postgres) / ``json_extract`` (SQLite),
    not a Python scan over the most-recent 10K events. ``limit=1`` is
    enough — the predicate identifies the row, ``order="desc"`` is
    retained for backends that don't honour limit-with-predicate
    semantics deterministically.
    """
    events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        limit=1,
        order="desc",
        payload_filters={"feedback_id": feedback_id},
    )
    return bool(events)


def record_feedback(
    feedback: PackFeedback,
    *,
    log_dir: Path | str,
    event_log: EventLog | None = None,
    outcome_store: OutcomeStore | None = None,
    pack_id: str | None = None,
    component_id: str = _DEFAULT_COMPONENT_ID,
) -> FeedbackRecordResult:
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

    ``feedback.feedback_id`` is used as the idempotency key against the
    EventLog: if a prior call or replay already emitted this feedback_id,
    the event emission is skipped and the result reports
    ``event_log_skipped_as_duplicate=True``. The JSONL append is still
    performed (it is the authoritative file record). Callers who want
    the JSONL file itself to be de-duplicated should check by
    ``feedback_id`` before calling.

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
        :class:`FeedbackRecordResult` carrying the log path,
        ``feedback_id``, per-sink emission status, and any captured
        errors.
    """
    log_path = Path(log_dir) / "pack_feedback.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = asdict(feedback)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")

    event_log_emitted = False
    event_log_skipped_as_duplicate = False
    event_log_error: Exception | None = None
    if event_log is not None:
        try:
            if _feedback_id_in_event_log(event_log, feedback.feedback_id):
                event_log_skipped_as_duplicate = True
                logger.debug(
                    "feedback_event_skipped_duplicate",
                    feedback_id=feedback.feedback_id,
                    run_id=feedback.run_id,
                    pack_id=pack_id,
                )
            else:
                event_log.emit(
                    EventType.FEEDBACK_RECORDED,
                    source="feedback.record",
                    entity_id=pack_id,
                    entity_type="pack" if pack_id else None,
                    payload=feedback.to_event_payload(pack_id=pack_id),
                )
                event_log_emitted = True
        except Exception as exc:
            event_log_error = exc
            logger.exception(
                "feedback_event_emit_failed",
                run_id=feedback.run_id,
                pack_id=pack_id,
                feedback_id=feedback.feedback_id,
            )

    outcome_emitted = False
    outcome_error: Exception | None = None
    if outcome_store is not None:
        try:
            _emit_outcome(
                feedback,
                outcome_store=outcome_store,
                pack_id=pack_id,
                component_id=component_id,
            )
            outcome_emitted = True
        except Exception as exc:
            outcome_error = exc
            logger.exception(
                "feedback_outcome_emit_failed",
                run_id=feedback.run_id,
                pack_id=pack_id,
                feedback_id=feedback.feedback_id,
            )

    logger.debug(
        "feedback_recorded",
        feedback_id=feedback.feedback_id,
        run_id=feedback.run_id,
        phase=feedback.phase,
        outcome=feedback.outcome,
        items_served=len(feedback.items_served),
        log_path=str(log_path),
        event_log_emitted=event_log_emitted,
        event_log_skipped_as_duplicate=event_log_skipped_as_duplicate,
        outcome_emitted=outcome_emitted,
    )
    return FeedbackRecordResult(
        log_path=log_path,
        feedback_id=feedback.feedback_id,
        event_log_emitted=event_log_emitted,
        outcome_emitted=outcome_emitted,
        event_log_error=event_log_error,
        outcome_error=outcome_error,
        event_log_skipped_as_duplicate=event_log_skipped_as_duplicate,
    )


def reconcile_feedback_log_to_event_log(
    log_dir: Path | str,
    event_log: EventLog,
    *,
    pack_id_lookup: dict[str, str] | None = None,
) -> ReconcileResult:
    """Emit any JSONL feedback entries missing from the EventLog.

    Closes the divergence path where JSONL was written but the
    ``FEEDBACK_RECORDED`` event was not (sink unavailable, process
    crashed between writes, file-only capture being promoted into the
    governed pipeline, etc.).

    Safe to run repeatedly: each JSONL row is matched against the
    EventLog by ``feedback_id``; entries that are already present are
    left alone.

    Args:
        log_dir: Directory containing ``pack_feedback.jsonl``.
        event_log: EventLog to backfill.
        pack_id_lookup: Optional ``feedback_id -> pack_id`` map for
            entries that carry a pack association. ``pack_id`` is not
            stored in ``PackFeedback`` itself, so reconciliation can
            only restore it when the caller provides this mapping
            (otherwise the emitted event has ``entity_id=None``).

    Returns:
        :class:`ReconcileResult` with counts and the list of
        ``feedback_id``s that failed to emit.
    """
    signals = load_feedback_log(log_dir)
    result = ReconcileResult(scanned=len(signals))
    lookup = pack_id_lookup or {}

    for fb in signals:
        if _feedback_id_in_event_log(event_log, fb.feedback_id):
            result.already_present += 1
            continue
        pack_id = lookup.get(fb.feedback_id)
        try:
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="feedback.reconcile",
                entity_id=pack_id,
                entity_type="pack" if pack_id else None,
                payload=fb.to_event_payload(pack_id=pack_id),
            )
            result.emitted += 1
        except Exception:
            result.failed += 1
            result.missing_feedback_ids.append(fb.feedback_id)
            logger.exception(
                "feedback_reconcile_emit_failed",
                feedback_id=fb.feedback_id,
                run_id=fb.run_id,
            )

    logger.info(
        "feedback_reconcile_completed",
        log_dir=str(log_dir),
        scanned=result.scanned,
        already_present=result.already_present,
        emitted=result.emitted,
        failed=result.failed,
    )
    return result


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
        "feedback_id": feedback.feedback_id,
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
            kwargs: dict[str, object] = {
                "run_id": data["run_id"],
                "phase": data["phase"],
                "intent": data["intent"],
                "outcome": data["outcome"],
                "items_served": data.get("items_served", []),
                "items_referenced": data.get("items_referenced", []),
                "relevance_scores": data.get("relevance_scores", {}),
                "intent_family": data.get("intent_family", ""),
                "timestamp_utc": data.get("timestamp_utc", ""),
                "agent_id": data.get("agent_id"),
                "metadata": data.get("metadata", {}),
            }
            # Older JSONL rows pre-date feedback_id; only pass through when
            # present so the dataclass default (fresh ULID) doesn't stomp
            # an existing id and break reconciliation idempotency.
            if data.get("feedback_id"):
                kwargs["feedback_id"] = data["feedback_id"]
            signals.append(PackFeedback(**kwargs))  # type: ignore[arg-type]

    logger.debug("feedback_log_loaded", count=len(signals), log_path=str(log_path))
    return signals
