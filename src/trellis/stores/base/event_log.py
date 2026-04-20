"""EventLog — abstract interface, Event model, and EventType enum."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from trellis.core.base import VersionedModel, utc_now
from trellis.core.ids import generate_ulid


class EventType(StrEnum):
    """Event types for the experience graph domain."""

    # Trace lifecycle
    TRACE_INGESTED = "trace.ingested"
    TRACE_UPDATED = "trace.updated"
    TRACE_OUTCOME_RECORDED = "trace.outcome_recorded"

    # Entity lifecycle
    ENTITY_CREATED = "entity.created"
    ENTITY_UPDATED = "entity.updated"
    ENTITY_MERGED = "entity.merged"
    ENTITY_DELETED = "entity.deleted"

    # Evidence lifecycle
    EVIDENCE_INGESTED = "evidence.ingested"
    EVIDENCE_ATTACHED = "evidence.attached"

    # Precedent lifecycle
    PRECEDENT_PROMOTED = "precedent.promoted"
    PRECEDENT_UPDATED = "precedent.updated"

    # Policy
    POLICY_CREATED = "policy.created"
    POLICY_VIOLATED = "policy.violated"

    # Pack
    PACK_ASSEMBLED = "pack.assembled"

    # Graph
    LINK_CREATED = "link.created"
    LINK_REMOVED = "link.removed"
    LABEL_ADDED = "label.added"
    LABEL_REMOVED = "label.removed"

    # Feedback
    FEEDBACK_RECORDED = "feedback.recorded"

    # Memory (save_memory MCP tool / unstructured observation ingestion)
    MEMORY_STORED = "memory.stored"

    # Extraction (tiered extraction pipeline — raw input -> entity/edge drafts)
    EXTRACTION_DISPATCHED = "extraction.dispatched"

    # System
    SYSTEM_INITIALIZED = "system.initialized"
    MUTATION_EXECUTED = "mutation.executed"
    MUTATION_REJECTED = "mutation.rejected"

    # Token tracking
    TOKEN_TRACKED = "token.tracked"

    # Feedback-driven parameter tuning — audit trail of governance
    # decisions on ParameterStore snapshots (not raw OutcomeEvents;
    # those live in the Operational-Plane OutcomeStore).
    PARAMS_UPDATED = "parameters.updated"
    TUNER_PROPOSAL_CREATED = "tuner.proposal_created"
    TUNER_PROPOSAL_REJECTED = "tuner.proposal_rejected"


class Event(VersionedModel):
    """An immutable event record."""

    event_id: str = Field(default_factory=generate_ulid)
    event_type: EventType
    source: str  # component that emitted the event
    entity_id: str | None = None
    entity_type: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    recorded_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventLog(ABC):
    """Abstract interface for an append-only event log."""

    @abstractmethod
    def append(self, event: Event) -> None:
        """Append event (immutable, no updates)."""

    @abstractmethod
    def get_events(
        self,
        *,
        event_type: EventType | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Query events with filters."""

    @abstractmethod
    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        """Count events with optional filters."""

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""

    def has_idempotency_key(self, key: str) -> bool:
        """Check if an idempotency key exists in the event log.

        Default implementation queries events. Backends should override
        with a targeted index query for performance.
        """
        events = self.get_events(
            event_type=EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            limit=100,
        )
        return any(e.payload.get("idempotency_key") == key for e in events)

    def emit(
        self,
        event_type: EventType,
        source: str,
        *,
        entity_id: str | None = None,
        entity_type: str | None = None,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Event:
        """Convenience: create and append an event. Returns the event."""
        event = Event(
            event_type=event_type,
            source=source,
            entity_id=entity_id,
            entity_type=entity_type,
            payload=payload or {},
            metadata=metadata or {},
        )
        self.append(event)
        return event
