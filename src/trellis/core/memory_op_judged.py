"""Shared emitter for leak-safe judged-memory-operation events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)


def emit_memory_op_judged(
    event_log: EventLog,
    *,
    op_type: JudgedOpType,
    source: str,
    model_id: str,
    input_digest: InputDigest,
    decision: str,
    confidence: float,
    subject_ref: SubjectRef,
    entity_id: str | None = None,
    entity_type: str | None = None,
) -> None:
    """Emit one judged operation after validating its shared contract.

    ``model_id`` must identify the actual configured or reported judge. Event
    persistence is best-effort because the judged memory item is already
    durable when callers reach this post-success telemetry seam.
    """
    if not model_id.strip():
        msg = "model_id must be a non-empty string"
        raise ValueError(msg)

    payload = MemoryOpJudgedPayload(
        op_type=op_type,
        model_id=model_id,
        input_digest=input_digest,
        decision=decision,
        confidence=max(0.0, min(1.0, float(confidence))),
        subject_ref=subject_ref,
    )
    try:
        event_log.emit(
            EventType.MEMORY_OP_JUDGED,
            source=source,
            entity_id=entity_id or subject_ref.ref_id,
            entity_type=entity_type or subject_ref.ref_type,
            payload=payload.model_dump(mode="json"),
        )
    except Exception:
        logger.exception(
            "memory_op_judged_emit_failed",
            op_type=op_type.value,
            subject_ref_type=subject_ref.ref_type,
            subject_ref_id=subject_ref.ref_id,
        )


__all__ = ["emit_memory_op_judged"]
