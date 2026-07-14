"""Tests for the judged-memory-operation (training-pair) payload schema.

Covers the #264 schema slice: the ``MemoryOpJudgedPayload`` contract
(``docs/design/plan-memory-lifecycle.md`` §0.1). Emission wiring lands
with #263 — these tests exercise the payload shape, its leak-safety
guarantees, and one full round-trip through a real ``EventLog`` backend
to prove the model survives serialization to and from the log.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trellis.core.hashing import content_hash
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

# The guide's binding field contract for the payload
# (Fable, 2026-07-13): {op_type, model_id, input_digest, decision,
# confidence, subject_ref} — no more, no fewer.
EXPECTED_FIELDS = {
    "op_type",
    "model_id",
    "input_digest",
    "decision",
    "confidence",
    "subject_ref",
}


def _payload() -> MemoryOpJudgedPayload:
    return MemoryOpJudgedPayload(
        op_type=JudgedOpType.RECONCILIATION,
        model_id="hermes3:8b",
        input_digest=InputDigest(
            hash=content_hash("some judged memory content"),
            length=len("some judged memory content"),
            source_refs=["doc_abc", "entity_xyz"],
        ),
        decision="supersede",
        confidence=0.82,
        subject_ref=SubjectRef(ref_type="doc", ref_id="doc_abc"),
    )


def test_field_set_matches_guide_contract() -> None:
    """Leak-safety: the payload's field set is exactly the guide's spec.

    The contract is a whitelist — asserting the exact field set is what
    keeps a raw-content field from ever being bolted on "because it's
    just telemetry". No field carries free-prose memory content.
    """
    assert set(MemoryOpJudgedPayload.model_fields) == EXPECTED_FIELDS
    # Explicit leak-safety guard: none of the obvious free-content field
    # names exist on the model.
    forbidden = {"content", "text", "body", "prose", "raw", "summary"}
    assert forbidden.isdisjoint(MemoryOpJudgedPayload.model_fields)


def test_extra_fields_rejected() -> None:
    """``extra="forbid"`` (TrellisModel) rejects unknown top-level keys."""
    with pytest.raises(ValidationError):
        MemoryOpJudgedPayload(
            op_type=JudgedOpType.CURATION,
            model_id="deterministic",
            input_digest=InputDigest(hash="abcd", length=4),
            decision="keep",
            confidence=1.0,
            subject_ref=SubjectRef(ref_type="entity", ref_id="e1"),
            content="the raw memory prose that must never be logged",  # type: ignore[call-arg]
        )


def test_nested_models_reject_extra_fields() -> None:
    """Nested digest / ref models are ``extra="forbid"`` too."""
    with pytest.raises(ValidationError):
        InputDigest(hash="abcd", length=4, content="leak")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SubjectRef(ref_type="doc", ref_id="d1", title="leak")  # type: ignore[call-arg]


def test_confidence_bounds_enforced() -> None:
    """Confidence is constrained to ``[0.0, 1.0]``."""
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            MemoryOpJudgedPayload(
                op_type=JudgedOpType.EXTRACTION,
                model_id="hermes3:8b",
                input_digest=InputDigest(hash="abcd", length=4),
                decision="accept",
                confidence=bad,
                subject_ref=SubjectRef(ref_type="doc", ref_id="d1"),
            )


def test_negative_input_length_rejected() -> None:
    """Digest length is a non-negative character count."""
    with pytest.raises(ValidationError):
        InputDigest(hash="abcd", length=-1)


def test_op_type_wire_values() -> None:
    """The four judged-stage slugs are stable wire values."""
    assert JudgedOpType.EXTRACTION.value == "extraction"
    assert JudgedOpType.RECONCILIATION.value == "reconciliation"
    assert JudgedOpType.DISTILLATION.value == "distillation"
    assert JudgedOpType.CURATION.value == "curation"


def test_round_trip_through_event_log(tmp_path: Path) -> None:
    """The payload survives a full round-trip through a real EventLog.

    Emit a ``MEMORY_OP_JUDGED`` event carrying the JSON-mode dump of the
    payload, read it back from SQLite, and re-validate the stored dict
    through the model — proving the schema is durable on the wire the
    #263 emit path will use.
    """
    log = SQLiteEventLog(tmp_path / "events.db")
    try:
        payload = _payload()
        emitted = log.emit(
            EventType.MEMORY_OP_JUDGED,
            source="reconciler",
            entity_id=payload.subject_ref.ref_id,
            entity_type=payload.subject_ref.ref_type,
            payload=payload.model_dump(mode="json"),
        )
        assert emitted.event_type is EventType.MEMORY_OP_JUDGED

        stored = log.get_events(event_type=EventType.MEMORY_OP_JUDGED)
        assert len(stored) == 1
        event = stored[0]
        assert isinstance(event, Event)
        assert event.event_type is EventType.MEMORY_OP_JUDGED

        # Re-validate the persisted payload dict back into the model.
        restored = MemoryOpJudgedPayload.model_validate(event.payload)
        assert restored == payload
        assert restored.op_type is JudgedOpType.RECONCILIATION
        assert restored.subject_ref.ref_id == "doc_abc"
        # No raw content ever touched the log.
        assert "content" not in event.payload
    finally:
        log.close()
