"""Trace schema for Trellis."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from trellis.core.base import TimestampedModel, VersionedModel, utc_now
from trellis.core.ids import generate_ulid
from trellis.schemas.enums import OutcomeStatus, TraceSource


class EvidenceRef(VersionedModel):
    """Reference to a piece of evidence used or produced by a trace."""

    evidence_id: str
    role: str = "input"


class ArtifactRef(VersionedModel):
    """Reference to an artifact produced by a trace."""

    artifact_id: str
    artifact_type: str


class TraceStep(VersionedModel):
    """A single step within a trace."""

    step_type: str
    name: str
    args: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    error: str | None = None
    duration_ms: int | None = None
    started_at: datetime = Field(default_factory=utc_now)


class Outcome(VersionedModel):
    """Outcome of a trace execution."""

    status: OutcomeStatus = OutcomeStatus.UNKNOWN
    metrics: dict = Field(default_factory=dict)
    summary: str | None = None


class Feedback(VersionedModel):
    """Feedback on a trace."""

    feedback_id: str = Field(default_factory=generate_ulid)
    rating: float | None = None
    label: str | None = None
    comment: str | None = None
    given_by: str = "unknown"
    given_at: datetime = Field(default_factory=utc_now)


class TraceContext(VersionedModel):
    """Context in which a trace was executed."""

    agent_id: str | None = None
    team: str | None = None
    domain: str | None = None
    workflow_id: str | None = None
    parent_trace_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None


class Trace(TimestampedModel, VersionedModel):
    """A complete trace record capturing an agent or workflow execution."""

    trace_id: str = Field(default_factory=generate_ulid)
    source: TraceSource
    intent: str
    steps: list[TraceStep] = Field(default_factory=list)
    evidence_used: list[EvidenceRef] = Field(default_factory=list)
    artifacts_produced: list[ArtifactRef] = Field(default_factory=list)
    outcome: Outcome | None = None
    feedback: list[Feedback] = Field(default_factory=list)
    context: TraceContext
    metadata: dict = Field(default_factory=dict)

    def to_summary_dict(self) -> dict:
        """Return a compact summary dict suitable for list endpoints."""
        return {
            "trace_id": self.trace_id,
            "source": self.source.value,
            "intent": self.intent,
            "outcome": self.outcome.status.value if self.outcome else None,
            "domain": self.context.domain if self.context else None,
            "agent_id": self.context.agent_id if self.context else None,
            "created_at": self.created_at.isoformat(),
        }
