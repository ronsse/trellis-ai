"""EventLog — abstract interface, Event model, and EventType enum."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from trellis.core.base import VersionedModel, utc_now
from trellis.core.ids import generate_ulid

#: Sort order for ``EventLog.get_events``. ``"asc"`` returns the oldest
#: events first (chronological), ``"desc"`` returns the most recent
#: events first. The default is ``"asc"`` so existing analytics callers
#: that consume events in chronological order keep working without a
#: change. Callers that short-circuit on the first match (duplicate
#: checks, "find latest" lookups) should pass ``order="desc"`` so the
#: ``limit`` cap doesn't truncate the recent end of the log.
EventOrder = Literal["asc", "desc"]


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
    #: Optional assembly-time quality score emitted by
    #: :class:`~trellis.retrieve.pack_builder.PackBuilder` when an evaluator
    #: hook returns a :class:`~trellis.retrieve.evaluate.QualityReport`. Joins
    #: to :attr:`FEEDBACK_RECORDED` via ``pack_id`` so downstream analysis can
    #: correlate per-dimension scores with task success. Never fires when no
    #: evaluator is configured — zero noise for consumers who don't opt in.
    PACK_QUALITY_SCORED = "pack.quality_scored"

    # Graph
    LINK_CREATED = "link.created"
    LINK_REMOVED = "link.removed"
    LABEL_ADDED = "label.added"
    LABEL_REMOVED = "label.removed"
    #: Emitted by :meth:`GraphStore.compact_versions` — records the cutoff,
    #: per-table drop counts, and the ``valid_to`` range of the compacted
    #: rows. Closes Gap 4.2 by giving operators an audit trail for SCD2
    #: retention runs without preserving the rows themselves.
    GRAPH_VERSIONS_COMPACTED = "graph.versions_compacted"
    #: Emitted by :meth:`BlobStore.sweep_expired` — records the cutoff
    #: and per-bucket counts of deleted / skipped / errored blobs.
    #: Closes Gap 4.4 by giving operators an audit trail for blob TTL
    #: retention runs. Dry runs emit the event with ``dry_run=True``.
    BLOB_GC_SWEPT = "blob.gc_swept"

    # Feedback
    FEEDBACK_RECORDED = "feedback.recorded"

    # Advisory lifecycle (soft-suppression + restore — see Gap 2.1)
    ADVISORY_SUPPRESSED = "advisory.suppressed"
    ADVISORY_RESTORED = "advisory.restored"
    # Advisory fitness drift — regime shift vs. gradual update (Gap 2.4).
    # Smoothed confidence updates mask fast shifts; this event surfaces
    # them so operators can review before the smoothing absorbs them.
    ADVISORY_DRIFT_DETECTED = "advisory.drift_detected"

    # Classification refresh (stale-tag reclassification — see Gap 1.1)
    TAGS_REFRESHED = "tags.refreshed"

    # Memory (save_memory MCP tool / unstructured observation ingestion)
    MEMORY_STORED = "memory.stored"

    # Extraction (tiered extraction pipeline — raw input -> entity/edge drafts)
    EXTRACTION_DISPATCHED = "extraction.dispatched"
    #: Emitted when the :class:`~trellis.extract.dispatcher.ExtractionDispatcher`
    #: selects an extractor below the natural priority order (``prefer_tier``
    #: override) or when the chosen extractor produces an empty draft set
    #: ("silent failure"). Closes Gap 4.3 by giving graduation tracking
    #: (LLM → Hybrid → Deterministic as domains stabilize) an observable
    #: substrate — without this event, patterns like "rules always return
    #: empty for this source; LLM always runs" are invisible.
    EXTRACTOR_FALLBACK = "extractor.fallback"

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
    #: Emitted when post-promotion monitoring detects a significant drop in
    #: success rate for a recently-promoted ``params_version`` vs. the
    #: baseline it replaced. Signal-only by default — auto-demotion is a
    #: separate opt-in (see :class:`PostPromotionPolicy.auto_demote`) so
    #: noisy outcomes can't silently unwind deliberate promotions.
    #: Closes Gap 2.2.
    PARAMETERS_DEGRADED = "parameters.degraded"


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
        order: EventOrder = "asc",
        payload_filters: dict[str, str] | None = None,
    ) -> list[Event]:
        """Query events with filters.

        ``order`` controls truncation semantics when ``limit`` is hit:
        ``"asc"`` (default) preserves chronological consumption for
        analytics aggregators; ``"desc"`` returns the most recent events
        first so duplicate-check / latest-N lookups can short-circuit
        without missing recent rows.

        ``payload_filters`` maps payload-key to expected string value;
        predicates are AND-ed and pushed into the backend SQL so the
        ``limit`` cap applies *after* the filter. This is the SQL-side
        equivalent of post-fetch ``e.payload.get(K) == V`` and matters
        when the unfiltered window would pull megabytes of JSON only to
        keep a few rows. Backends compare against the textual JSON value
        (``payload->>K`` on Postgres, ``json_extract(payload, '$.K')`` on
        SQLite), so callers comparing against ints / bools must coerce
        to ``str`` at the call site.
        """

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
