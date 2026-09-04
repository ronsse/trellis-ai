"""Tests for the shared judged-operation event emitter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import EventType


def test_emits_the_typed_payload_without_raw_content() -> None:
    event_log = MagicMock()
    digest = InputDigest(hash="abc123", length=17, source_refs=["doc-1"])
    subject = SubjectRef(ref_type="doc", ref_id="doc-2")

    emit_memory_op_judged(
        event_log,
        op_type=JudgedOpType.CLASSIFICATION,
        source="worker:enrich",
        model_id="test-model-v1",
        input_digest=digest,
        decision="reference",
        confidence=0.8,
        subject_ref=subject,
    )

    event_log.emit.assert_called_once()
    args, kwargs = event_log.emit.call_args
    assert args == (EventType.MEMORY_OP_JUDGED,)
    assert kwargs["source"] == "worker:enrich"
    assert kwargs["entity_id"] == "doc-2"
    assert kwargs["entity_type"] == "doc"
    assert MemoryOpJudgedPayload.model_validate(kwargs["payload"]) == (
        MemoryOpJudgedPayload(
            op_type=JudgedOpType.CLASSIFICATION,
            model_id="test-model-v1",
            input_digest=digest,
            decision="reference",
            confidence=0.8,
            subject_ref=subject,
        )
    )


def test_rejects_empty_model_id_before_emit() -> None:
    event_log = MagicMock()
    with pytest.raises(ValueError, match="model_id"):
        emit_memory_op_judged(
            event_log,
            op_type=JudgedOpType.CLASSIFICATION,
            source="worker:enrich",
            model_id=" ",
            input_digest=InputDigest(hash="abc", length=3),
            decision="notes",
            confidence=0.0,
            subject_ref=SubjectRef(ref_type="doc", ref_id="doc-1"),
        )
    event_log.emit.assert_not_called()


def test_event_log_failure_is_soft() -> None:
    event_log = MagicMock()
    event_log.emit.side_effect = RuntimeError("down")

    emit_memory_op_judged(
        event_log,
        op_type=JudgedOpType.EXTRACTION,
        source="save_memory.extract",
        model_id="test-model-v1",
        input_digest=InputDigest(hash="abc", length=3),
        decision="Person",
        confidence=0.7,
        subject_ref=SubjectRef(ref_type="entity", ref_id="entity-1"),
    )
