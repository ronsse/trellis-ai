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
    #: Emitted by the MCP ``get_items`` batch fetch-by-id tool (#305) so a
    #: full-body fetch stays attributable to the pack whose index served
    #: the ids. Payload schema: ``{pack_id, requested_item_ids,
    #: served_item_ids, not_found_item_ids, omitted_item_ids,
    #: response_tokens, budget_tokens}``. ``pack_id`` is ``None`` for a
    #: fetch with no originating pack; ``omitted_item_ids`` are ids that
    #: resolved but did not fit the token budget (re-fetchable);
    #: ``entity_id`` carries the pack_id when present, so the event joins
    #: to ``PACK_ASSEMBLED`` / ``FEEDBACK_RECORDED`` the same way
    #: ``PACK_QUALITY_SCORED`` does.
    PACK_ITEMS_FETCHED = "pack.items_fetched"

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
    #: Emitted by ``RedactionApplyHandler`` (``redaction.apply``) after a
    #: graph entity is hard-purged: all SCD-2 version rows, every edge
    #: version touching the node (cascade), its alias rows, and its
    #: vector-store entry. The payload carries the audit justification,
    #: counts, and id pointers only — never the purged name/properties —
    #: so the append-only log records *that* a redaction happened and its
    #: shape without re-containing what was removed. Payload schema:
    #: ``{target_id, target_kind, reason, command_id, requested_by,
    #: node_versions_purged, edges_purged, aliases_purged, vector_deleted,
    #: document_ids, linked_observation_ids, linked_measurement_ids}``
    #: (the entity type rides the event's ``entity_type`` column).
    #: ``node_versions_purged`` is exact (history length); ``edges_purged``
    #: / ``aliases_purged`` count rows current at redaction time while the
    #: cascade removes all versions. ``document_ids`` is the union of the
    #: purged node's document links across all versions so a future
    #: document-level redaction can locate them;
    #: ``linked_observation_ids`` / ``linked_measurement_ids`` point at the
    #: surviving Observation/Measurement nodes about the subject (not
    #: cascaded — redact individually). ``command_id`` joins this semantic
    #: event to the executor's ``MUTATION_EXECUTED`` audit event.
    REDACTION_APPLIED = "redaction.applied"

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
    #: Emitted by a classifier when its upstream signal source fails and
    #: the classifier degrades to a sentinel result (rather than raising)
    #: so callers can keep flowing through the pipeline. First user:
    #: :class:`~trellis.classify.classifiers.llm.LLMFacetClassifier`,
    #: which emits this event when
    #: :class:`~trellis_workers.enrichment.service.EnrichmentService`
    #: returns ``result.success=False`` and the classifier returns
    #: ``ClassificationResult(needs_llm_review=True, tags={}, confidence=0.0)``.
    #: Payload schema (all keys required when ``event_log`` is wired):
    #: ``{classifier_id, upstream_failure_kind, subject_entity_id,
    #: degraded_to}``. ``classifier_id`` is the classifier's stable
    #: ``name`` property (e.g. ``"llm_facet"``). ``upstream_failure_kind``
    #: is a short slug describing why the upstream signal was unusable
    #: (e.g. ``"enrichment_failure"`` when the only signal is
    #: ``EnrichmentResult.success=False`` with no further structure).
    #: ``subject_entity_id`` identifies the item being classified — falls
    #: back to ``None`` when the caller did not supply one.
    #: ``degraded_to`` names the sentinel outcome the classifier chose
    #: (today: ``"needs_llm_review"``); analyzers can join on this value
    #: to count degradation modes per classifier. Joins to
    #: :attr:`EXTRACTION_FAILED` via ``subject_entity_id`` + timestamp
    #: when the upstream emitted its own failure event.
    CLASSIFICATION_DEGRADED = "classification.degraded"

    # Memory (save_memory MCP tool / unstructured observation ingestion)
    MEMORY_STORED = "memory.stored"

    #: Emitted once per ``trellis ingest corpus`` run
    #: (:func:`trellis.ingest_corpus.sync_corpus`) with the run counts —
    #: ingested / updated / moved / skipped / pruned / chunks_written /
    #: warnings. Dry runs emit it too, flagged ``dry_run=True`` (same
    #: convention as :attr:`BLOB_GC_SWEPT`). Per-document signals ride
    #: the existing :attr:`MEMORY_STORED` event; this is the run-level
    #: audit record. See ``docs/design/adr-corpus-ingestion.md`` §4.
    CORPUS_SYNCED = "corpus.synced"

    # Judged memory operation (north star — the memory system generates its
    # own training curriculum; plan-memory-lifecycle.md §0.1).
    #: Emitted once per **judged** memory-lifecycle operation — an
    #: extraction verdict, a reconciliation ADD/UPDATE/SUPERSEDE/NOOP
    #: call, a distillation summary, a curation verdict — to log the
    #: ``(input, decision)`` half of a training pair. The downstream
    #: *outcome label* that completes the pair is supplied later by the
    #: same feedback-attribution join that
    #: :mod:`trellis.learning.pack_observations` already runs for packs
    #: (join key: the payload's ``subject_ref``); this event does not
    #: invent a second join path. Payload is the typed, leak-safe
    #: :class:`~trellis.schemas.memory_op.MemoryOpJudgedPayload` — content
    #: digests, item refs, verdict labels, model identifiers only,
    #: **never** raw memory content or model prose (event logs have a
    #: different access/retention profile than the doc store). Opt-in and
    #: additive, the PACK_QUALITY_SCORED posture: nothing emits it until
    #: the judged-operation paths are wired (#263), so consumers who do
    #: not opt in see zero noise. See
    #: ``docs/design/plan-memory-lifecycle.md`` §0.1.
    MEMORY_OP_JUDGED = "memory_op.judged"

    # Empirical-observation ingestion — see adr-observation-entity-type.md
    # and Item 1 Phase 1 of plan-self-improvement-program.md. Emitted by
    # the ObservationHandler / MeasurementHandler when a new Observation
    # or Measurement node lands in the graph.
    OBSERVATION_RECORDED = "observation.recorded"
    MEASUREMENT_RECORDED = "measurement.recorded"

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
    #: Emitted by :class:`~trellis.extract.dispatcher.ExtractionDispatcher`
    #: when one or more :class:`~trellis.extract.validators.ExtractionValidator`
    #: instances flag a malformed extraction result. Enforcing — when this
    #: fires the dispatcher has already quarantined the original
    #: ``entities`` / ``edges`` into
    #: ``unparsed_residue["rejected_by_validators"]`` and returned an empty
    #: result, so no Commands flow downstream. Operators consume this for
    #: trend analysis via
    #: :func:`~trellis.extract.telemetry.analyze_extraction_validation`.
    #: Payload: ``{ source_hint, extractor_used, findings: [...] }``.
    #: Closes Logic Gap 1.3. See
    #: ``docs/design/adr-extraction-validation.md``.
    EXTRACTION_REJECTED = "extraction.rejected"
    #: Emitted by :func:`~trellis.extract.telemetry.emit_extraction_failure`
    #: at any extractor site that previously swallowed a parse/validation
    #: failure silently. Replaces the silent
    #: ``except json.JSONDecodeError: return []`` defect in
    #: :class:`~trellis.extract.llm.LLMExtractor` and
    #: ``trellis_workers.learning.miner.PrecedentMiner._parse_candidates``
    #: with an emit-then-raise contract; see
    #: ``docs/design/adr-extraction-failure-telemetry.md``. Payload schema:
    #: ``{extractor_id, extractor_tier, failure_kind, source_hint,
    #: prompt_hash, source_excerpt_hash, model, error_class,
    #: error_excerpt, correlation_id}``. ``error_excerpt`` is bounded at
    #: 200 chars and redacted of common PII patterns (email, UUID, SSN).
    EXTRACTION_FAILED = "extraction.failed"

    #: Emitted by the well-known promotion loop
    #: (:mod:`trellis.learning.schema_evolution`) when an open-string
    #: ``node_type`` or ``edge_kind`` value crosses the operator-configured
    #: promotion thresholds. Surface-only — the canonical
    #: :mod:`trellis.schemas.well_known` registry is never auto-mutated;
    #: this event is the signal that a human-authored ADR amendment may
    #: be warranted. Payload includes the stable ``candidate_id`` so
    #: cooldown / recurrence tracking can deduplicate. See
    #: ``docs/design/adr-well-known-promotion-loop.md``.
    WELL_KNOWN_CANDIDATE = "well_known.candidate"
    #: Emitted by the tag-keyword promotion loop
    #: (:mod:`trellis.learning.tag_evolution`) when a keyword predicts an
    #: LLM-assigned tag across the shadow corpus strongly enough to be worth
    #: teaching the deterministic classifier. Sibling of
    #: :attr:`WELL_KNOWN_CANDIDATE` and surface-only in the same way: the
    #: analyzer proposes, a human writes ``classify.domain_keywords`` in
    #: ``config.yaml``. Never auto-applied for the ``domain`` facet — a wrong
    #: domain keyword *hides* documents from domain-scoped queries rather than
    #: merely re-ranking them (the #282 failure mode). Payload includes the
    #: stable ``candidate_id`` for cooldown / recurrence tracking, plus
    #: ``support`` / ``precision`` / ``lift`` so a reviewer can see the
    #: strength of the association rather than trusting a bare verdict.
    #: Carries ``example_count`` but **not** the example item ids: pairing a
    #: mined keyword with specific documents would turn an aggregate over
    #: ``>= min_support`` documents back into a per-document disclosure, which
    #: is the same rule :attr:`MEMORY_OP_JUDGED` follows. See ``#321`` Phase 2.
    TAG_KEYWORD_CANDIDATE = "tag_keyword.candidate"

    # Proposal lifecycle (coding-agent self-improvement loop — Item 7).
    #: Emitted by
    #: :class:`trellis_workers.code_authoring.ProposalGenerator` when a
    #: new proposal is drafted for a signal cluster (e.g., a cluster of
    #: :attr:`EXTRACTION_FAILED` events crossing the count threshold, or
    #: a surfaced :attr:`WELL_KNOWN_CANDIDATE`). Payload schema:
    #: ``{proposal_id, cluster_signature, markdown_preview, source_event_count}``.
    #: ``proposal_id`` is the SHA-256 hash of the cluster signature so
    #: re-running the generator over the same window produces a stable
    #: ID for idempotency checks. See
    #: ``docs/design/adr-coding-agent-loop.md`` and
    #: ``docs/design/plan-coding-agent-loop.md`` Phase 0.
    PROPOSAL_DRAFTED = "proposal.drafted"
    #: Emitted by :class:`trellis_workers.code_authoring.ProposalGenerator`
    #: when a re-run of the generator surfaces the same ``proposal_id``
    #: that already has a :attr:`PROPOSAL_DRAFTED` event in the log.
    #: Currently fires whenever the same proposal would otherwise be
    #: re-drafted; Phase 2's growth-threshold logic ("cluster grew ≥
    #: 50%") will narrow this to the meaningful-change case. Payload is
    #: the same shape as :attr:`PROPOSAL_DRAFTED`.
    PROPOSAL_UPDATED = "proposal.updated"

    # System
    SYSTEM_INITIALIZED = "system.initialized"
    MUTATION_EXECUTED = "mutation.executed"
    MUTATION_REJECTED = "mutation.rejected"
    #: Emitted at an agent-facing write boundary (MCP tool, REST route)
    #: when a payload is rejected *before* a Command is constructed — the
    #: stage :attr:`MUTATION_REJECTED` cannot see because no Command ever
    #: existed. Without this event a schema-shaped rejection (an agent
    #: repeatedly putting ``artifacts`` under ``outcome``, an invalid
    #: ``source`` enum value) is visible only to the caller that made it;
    #: the 2026-08-07 recall-gap study found 13 such rejections across 12
    #: sessions, none observable from the backend. Payload schema:
    #: ``{tool, stage: "boundary", error_class, payload_chars,
    #: rejections: [{kind, loc, msg}], hints: [str]}``. ``kind`` is a
    #: closed taxonomy slug (see ``trellis.ops.write_health.RejectionKind``),
    #: ``loc`` the dotted field path, ``hints`` deterministic
    #: field-relocation guidance derived from the live schema. ``source``
    #: carries the same ``mcp:<tool>`` string the executor stores in
    #: ``requested_by``, so acceptance and rejection join per tool.
    #: Consumed by :func:`trellis.ops.write_health.summarize_write_health`.
    WRITE_REJECTED = "write.rejected"

    #: Emitted by the API Review-queue endpoints (WP10) whenever a human
    #: operator acts on a governance surface from the UI inbox — approving
    #: or rejecting a tuner proposal, promoting learning candidates, or
    #: drafting a schema-evolution ADR. This is an *audit-of-the-reviewer*
    #: record that complements (never replaces) the surface-specific event
    #: the underlying pipeline already emits (e.g. ``PARAMS_UPDATED``). The
    #: payload always carries the authenticated key identity so the audit
    #: trail attributes the decision to a credential. Payload schema:
    #: ``{surface: str, action: str, key_id: str | None, key_name: str |
    #: None, ...surface-specific detail}``. See
    #: ``docs/design/adr-autonomy-ladder.md`` for which surfaces are
    #: human-gated.
    REVIEW_DECISION_RECORDED = "review.decision_recorded"

    # Token tracking
    TOKEN_TRACKED = "token.tracked"
    #: Emitted by a real-LLM-bearing context (today: the
    #: ``program_convergence_real_llm`` eval scenario — Unit E3) to record
    #: the total token + dollar cost of a single bounded run against a
    #: real provider. Payload schema (all keys required):
    #: ``{tokens_consumed: int, dollars_estimated: float, provider: str,
    #: model: str}``. ``provider`` is a short slug (``"openai"``,
    #: ``"anthropic"``) identifying which vendor the cost is billed
    #: against; ``model`` is the specific model identifier (e.g.,
    #: ``"text-embedding-3-small"``). One event per ``run()`` invocation,
    #: emitted unconditionally — including when the run aborts mid-loop on
    #: a hard cost cap — so operators always see the bill.
    #:
    #: Cohort 2's coding-agent loop (see
    #: ``docs/design/plan-coding-agent-loop-cohort2.md`` §5) reserves a
    #: richer payload variant carrying ``run_id`` / ``proposal_id`` /
    #: ``loc_delta`` etc. When that path lands it joins this same enum
    #: value; the payload union is documented in that plan's §5 schema.
    #: Consumers MUST tolerate both shapes (key on ``source``: the eval
    #: scenario emits with ``source="eval.program_convergence_real_llm"``,
    #: the coding-agent loop emits with
    #: ``source="trellis_workers.code_authoring.budget"``).
    BUDGET_CONSUMED = "budget.consumed"

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
    #: Tier-1 autonomy events (see
    #: ``docs/design/adr-autonomy-ladder.md``). Distinct from the
    #: ``PARAMS_UPDATED`` emitted by a *manual* ``metrics promote --commit``
    #: so the audit trail makes the autonomous path self-identifying. A
    #: ``trellis worker tune`` run that auto-applies a qualifying proposal
    #: emits ``PARAMS_AUTO_PROMOTED`` *in addition to* the underlying
    #: ``PARAMS_UPDATED`` (same governance path, no new mutation route);
    #: when post-promotion monitoring later rolls that snapshot back, it
    #: emits ``PARAMS_AUTO_ROLLED_BACK`` alongside the rollback's own
    #: ``PARAMS_UPDATED``. The dedicated events are tier-1 invariant (c) —
    #: every autonomous action leaves a self-identifying audit record.
    PARAMS_AUTO_PROMOTED = "parameters.auto_promoted"
    PARAMS_AUTO_ROLLED_BACK = "parameters.auto_rolled_back"


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
        """Convenience: create and append an event. Returns the event.

        Stamps ``metadata["write_provenance"]`` with the emitting build
        and the write-behaviour flags in effect (see
        :mod:`trellis.core.write_provenance`) so a stored row can be
        attributed to the code that produced it. Additive: the stamp
        lives in the free-form ``metadata`` dict, rows written before it
        existed simply lack the key, and a caller that supplies its own
        stamp (replay / backfill on behalf of another build) keeps it.
        """
        from trellis.core.write_provenance import stamp_metadata  # noqa: PLC0415

        event = Event(
            event_type=event_type,
            source=source,
            entity_id=entity_id,
            entity_type=entity_type,
            payload=payload or {},
            metadata=stamp_metadata(metadata),
        )
        self.append(event)
        return event
