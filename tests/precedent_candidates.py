"""Fixtures for candidate precedents: a distilling trace and the governed record."""

from __future__ import annotations

from trellis.learning.precedent_distill import distill_trace
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.schemas.enums import OutcomeStatus, TraceSource
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
