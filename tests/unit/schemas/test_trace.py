"""Tests for trace schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import (
    ArtifactRef,
    EvidenceRef,
    Feedback,
    Outcome,
    Trace,
    TraceContext,
    TraceStep,
)


class TestTraceDefaults:
    """Trace creates with sensible defaults."""

    def test_minimal_trace(self) -> None:
        trace = Trace(
            source=TraceSource.AGENT,
            intent="answer question",
            context=TraceContext(),
        )
        assert trace.trace_id  # ULID generated
        assert len(trace.trace_id) == 26
        assert trace.source == TraceSource.AGENT
        assert trace.intent == "answer question"
        assert trace.steps == []
        assert trace.evidence_used == []
        assert trace.artifacts_produced == []
        assert trace.outcome is None
        assert trace.feedback == []
        assert trace.metadata == {}
        assert trace.created_at is not None
        assert trace.updated_at is not None
        assert trace.schema_version == "0.1.0"


class TestTraceWithSteps:
    """Trace with step records."""

    def test_trace_with_steps(self) -> None:
        step = TraceStep(
            step_type="tool_call",
            name="search",
            args={"query": "test"},
            result={"count": 5},
            duration_ms=120,
        )
        trace = Trace(
            source=TraceSource.HUMAN,
            intent="search docs",
            steps=[step],
            context=TraceContext(agent_id="agent-1"),
        )
        assert len(trace.steps) == 1
        assert trace.steps[0].step_type == "tool_call"
        assert trace.steps[0].name == "search"
        assert trace.steps[0].args == {"query": "test"}
        assert trace.steps[0].result == {"count": 5}
        assert trace.steps[0].duration_ms == 120
        assert trace.steps[0].started_at is not None


class TestTraceWithOutcome:
    """Trace with outcome information."""

    def test_trace_with_outcome(self) -> None:
        outcome = Outcome(
            status=OutcomeStatus.SUCCESS,
            metrics={"latency_ms": 200},
            summary="Completed successfully",
        )
        trace = Trace(
            source=TraceSource.WORKFLOW,
            intent="run pipeline",
            outcome=outcome,
            context=TraceContext(domain="engineering"),
        )
        assert trace.outcome is not None
        assert trace.outcome.status == OutcomeStatus.SUCCESS
        assert trace.outcome.metrics == {"latency_ms": 200}
        assert trace.outcome.summary == "Completed successfully"

    def test_outcome_defaults_to_unknown(self) -> None:
        outcome = Outcome()
        assert outcome.status == OutcomeStatus.UNKNOWN
        assert outcome.metrics == {}
        assert outcome.summary is None


class TestTraceWithEvidenceAndArtifacts:
    """Trace with evidence and artifact references."""

    def test_trace_with_evidence_and_artifacts(self) -> None:
        ev = EvidenceRef(evidence_id="ev-001", role="context")
        art = ArtifactRef(artifact_id="art-001", artifact_type="report")
        trace = Trace(
            source=TraceSource.SYSTEM,
            intent="generate report",
            evidence_used=[ev],
            artifacts_produced=[art],
            context=TraceContext(),
        )
        assert len(trace.evidence_used) == 1
        assert trace.evidence_used[0].evidence_id == "ev-001"
        assert trace.evidence_used[0].role == "context"
        assert len(trace.artifacts_produced) == 1
        assert trace.artifacts_produced[0].artifact_id == "art-001"
        assert trace.artifacts_produced[0].artifact_type == "report"

    def test_evidence_ref_default_role(self) -> None:
        ev = EvidenceRef(evidence_id="ev-002")
        assert ev.role == "input"


class TestTraceForbidsExtras:
    """Trace rejects unknown fields."""

    def test_trace_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            Trace(
                source=TraceSource.AGENT,
                intent="test",
                context=TraceContext(),
                not_a_field="oops",  # type: ignore[call-arg]
            )

    def test_trace_step_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            TraceStep(
                step_type="x",
                name="y",
                bogus=True,  # type: ignore[call-arg]
            )


class TestFeedback:
    """Feedback model tests."""

    def test_feedback_defaults(self) -> None:
        fb = Feedback()
        assert len(fb.feedback_id) == 26
        assert fb.rating is None
        assert fb.given_by == "unknown"
        assert fb.given_at is not None
