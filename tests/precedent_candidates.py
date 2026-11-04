"""Fixtures for candidate precedents: a distilling trace and the governed record."""

from __future__ import annotations

from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.learning.precedent_distill import distill_trace
from trellis.learning.precedent_judged import (
    DISTILL_PROMPT_VERSION,
    MERGE_PROMPT_VERSION,
    judged_source,
    pair_ref_id,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.memory_op import (
    REF_TYPE_PRECEDENT_PAIR,
    REF_TYPE_TRACE,
    InputDigest,
    JudgedOpType,
    SubjectRef,
)
from trellis.schemas.precedent_record import PrecedentRecord
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep
from trellis.stores.registry import StoreRegistry

#: A token no other fixture, store row or pack contains.
SENTINEL = "zqxv-candidate-sentinel"


def recovering_trace(
    *, trace_id: str = "trace-recover", sentinel: str = SENTINEL, domain: str = "data"
) -> Trace:
    """A trace whose first call fails and whose retry with other args works."""
    return Trace(
        trace_id=trace_id,
        source=TraceSource.AGENT,
        intent="load the quarterly export",
        steps=[
            TraceStep(
                step_type="tool_call",
                name="load_export",
                args={"mode": "strict"},
                error=f"schema mismatch {sentinel}",
            ),
            TraceStep(
                step_type="tool_call",
                name="load_export",
                args={"mode": "lenient"},
                result={"rows": 3},
            ),
        ],
        outcome=Outcome(status=OutcomeStatus.SUCCESS),
        context=TraceContext(domain=domain),
    )


def candidates_of(trace: Trace) -> list[PrecedentRecord]:
    return list(distill_trace(trace).records)


def record_candidate(registry: StoreRegistry, record: PrecedentRecord):
    """Record one candidate through the governed executor; return the result."""
    return build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=Operation.PRECEDENT_CANDIDATE,
            args={"record": record.model_dump(mode="json")},
            requested_by="test:candidate",
            idempotency_key=record.candidate_id,
        )
    )


def record_candidates(registry: StoreRegistry, trace: Trace) -> list[PrecedentRecord]:
    records = candidates_of(trace)
    assert records, "fixture trace must distil to candidates"
    for record in records:
        assert record_candidate(registry, record).status == CommandStatus.SUCCESS
    return records


def local_model_copy(record: PrecedentRecord) -> PrecedentRecord:
    """``record`` re-stamped ``local_model`` under the id its content now derives."""
    claimed = record.model_copy(update={"provenance_tier": "local_model"})
    return claimed.model_copy(update={"candidate_id": claimed.expected_candidate_id()})


def local_model_record_payload() -> dict:
    """A ``local_model`` record for :func:`recovering_trace`, as a command sends it."""
    [real, *_] = candidates_of(recovering_trace())
    return local_model_copy(real).model_dump(mode="json")


def log_judged_lesson(
    event_log,
    trace_id: str,
    *,
    decision: str = "lesson",
    source: str | None = None,
) -> None:
    """The judged event the local distiller logs for a call about ``trace_id``."""
    emit_memory_op_judged(
        event_log,
        op_type=JudgedOpType.DISTILLATION,
        source=source or judged_source(DISTILL_PROMPT_VERSION),
        model_id="test-model",
        input_digest=InputDigest(hash="0123abcd", length=10, source_refs=[trace_id]),
        decision=decision,
        confidence=0.5,
        subject_ref=SubjectRef(ref_type=REF_TYPE_TRACE, ref_id=trace_id),
    )


def log_judged_merge(
    event_log,
    candidate_ids: list[str],
    *,
    decision: str = "same",
    op_type: JudgedOpType = JudgedOpType.RECONCILIATION,
) -> None:
    """The judged event the local merger logs for a call about the candidate pair."""
    emit_memory_op_judged(
        event_log,
        op_type=op_type,
        source=judged_source(MERGE_PROMPT_VERSION),
        model_id="test-model",
        input_digest=InputDigest(
            hash="0123abcd", length=10, source_refs=list(candidate_ids)
        ),
        decision=decision,
        confidence=1.0,
        subject_ref=SubjectRef(
            ref_type=REF_TYPE_PRECEDENT_PAIR, ref_id=pair_ref_id(candidate_ids)
        ),
    )
