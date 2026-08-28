"""EventLog — abstract interface, Event model, and EventType enum.

Also home to :func:`scan_events`, the capped-read helper every analyzer
should use instead of calling :meth:`EventLog.get_events` with a bare
``limit``. See its docstring for why (#374).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel, VersionedModel, utc_now
from trellis.core.ids import generate_ulid

logger = structlog.get_logger(__name__)

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

    #: Emitted by :class:`~trellis.mutate.handlers.RetentionPruneHandler`
    #: for every ``retention.prune`` run, dry or not. Phase one is
    #: *archival*: the payload carries the resolved ``criteria``, per-kind
    #: counts, the operator's ``reason``, and a **capped** sample of item
    #: ids (``item_ids``, the ``_LINKED_SIGNAL_LIMIT`` convention — a
    #: follow-up pointer, not an exhaustive index; ``archived`` is the
    #: authoritative count). ``dry_run=True`` means nothing was written and
    #: the ids are a preview of what a real run would take.
    #: ``scan_truncated=True`` means the candidate set is a prefix of the
    #: real population because the scan cap bit first. ``command_id`` joins
    #: this semantic event to the executor's ``MUTATION_EXECUTED``.
    RETENTION_PRUNED = "retention.pruned"

    #: Emitted by :class:`~trellis.mutate.handlers.RetentionRestoreHandler`
    #: when archived items are returned to ``Lifecycle.state="current"``.
    #: A distinct verb from ``RETENTION_PRUNED`` so "what did we walk back,
    #: and why" is a single query rather than a filter over prune events.
    #: Payload carries the operator's ``reason``, ``restored`` / ``skipped``
    #: counts and the item ids — restore is corrective, so the ids are the
    #: point and are not sampled.
    RETENTION_RESTORED = "retention.restored"

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

    #: Emitted once per session-capture sweep
    #: (:func:`trellis_workers.session_capture.run_capture`) with the whole
    #: session funnel — seen / ephemeral / watermark-skipped / parsed /
    #: empty / sampled-out / triggered / judge-unavailable, and the count of
    #: distinct sessions that yielded a memory. Dry runs emit it too, flagged
    #: ``dry_run=True`` (the :attr:`CORPUS_SYNCED` convention).
    #:
    #: It exists because :attr:`CORPUS_SYNCED` cannot see the failure that
    #: matters: that event is emitted by the write seam and therefore only
    #: when the sweep wrote something, so a sweep that adjudicated forty
    #: sessions and produced nothing is indistinguishable from a sweep that
    #: never ran. That is precisely the #255 shape — capture shipped in July
    #: and did not write a memory until August while reporting success — and
    #: the denominator :mod:`trellis.ops.capture_coverage` needs lives
    #: nowhere else. Unconditional and fail-soft: a sweep that adjudicates
    #: nothing still says so.
    CAPTURE_SWEEP_COMPLETED = "capture.sweep_completed"

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

    #: Emitted by
    #: :func:`trellis.learning.domain_normalization.analyze_domain_alias_candidates`
    #: when a low-support ``domain`` tag looks like a spelling of a
    #: high-support one and is worth merging. Advisory, human-gated, and for
    #: the same reason its sibling above is: an alias map decides which
    #: documents a domain-scoped query can *see*, so a wrong merge hides
    #: content — in bulk, which is strictly worse than one bad keyword.
    #: Payload carries the evidence a reviewer needs to judge the merge
    #: (``cooccurrence_rate``, ``neighbor_overlap``, ``shared_tokens``,
    #: ``documents_gained``) rather than a bare verdict, and omits example
    #: item ids under the same disclosure rule.
    DOMAIN_ALIAS_CANDIDATE = "domain_alias.candidate"

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


# ---------------------------------------------------------------------------
# Capped reads — scan_events (#374)
# ---------------------------------------------------------------------------

#: Cap every analyzer applies to a single ``get_events`` read. Kept here
#: rather than re-declared per module so the three analyzers that share
#: it cannot drift, and so a reader of a truncated report can look up one
#: number.
DEFAULT_SCAN_LIMIT = 5000


class ScanCoverage(TrellisModel):
    """What a capped :func:`scan_events` read did — and did not — cover.

    A report that hit its cap has to say so. Before #374 the three health
    and value analyzers each passed ``limit=5000`` and took the default
    ``order="asc"``, so a window with more matches than the cap silently
    returned **the oldest** rows: the newest events — a write outage that
    started this morning — fell outside the answer, and nothing in the
    output distinguished that from a healthy window. Both failure modes
    point at "looks healthy", which is the expensive direction.

    Two things fix it, and both live in :func:`scan_events`: the read is
    issued newest-first so truncation drops the *oldest* rows, and the
    fact that it truncated travels with the numbers in this model.

    ``matched`` and ``dropped`` are ``None`` when the true total could not
    be established (the backend's ``count`` raised, or the scan carried an
    ``until`` bound ``count`` cannot express). ``None`` there means
    *unknown*, never zero — the same distinction ``useful_token_fraction``
    makes between a refused ratio and a measured one.
    """

    #: The cap that was applied to each underlying read.
    limit: int = 0
    #: Events actually returned and aggregated.
    scanned: int = 0
    #: Events matching the filters in the window, when knowable.
    matched: int | None = None
    #: ``matched - scanned``: events the cap excluded. ``None`` when
    #: ``matched`` is unknown.
    dropped: int | None = None
    #: True when at least one read came back full, i.e. the window holds
    #: at least ``limit`` matching events and the cap may have bitten.
    truncated: bool = False
    #: Which event types were capped, sorted. Empty when nothing was.
    truncated_event_types: list[str] = Field(default_factory=list)
    #: ISO-8601 UTC timestamp of the oldest event this scan could see, set
    #: only when truncated. **This, not the requested window, is where the
    #: report's evidence actually begins.** A string rather than a
    #: ``datetime`` so ``model_dump()`` stays JSON-encodable for every
    #: report that embeds it.
    covered_since: str = ""
    #: Human-readable rendering of the above, empty when not truncated.
    note: str = ""


@dataclass(frozen=True, slots=True)
class EventScan:
    """One capped read: the events, in chronological order, plus coverage.

    ``events`` is ascending by ``occurred_at`` — the order aggregators
    already consume — even though the underlying read is issued
    descending. The reversal is what lets the cap drop the oldest rows
    without asking any caller to revisit whether its aggregation assumes
    chronological arrival.
    """

    events: list[Event]
    coverage: ScanCoverage


def _as_utc(moment: datetime) -> datetime:
    """Normalise a stored timestamp to aware UTC.

    Backends differ: SQLite round-trips the UTC ISO string it was given
    (naive), Postgres hands back a ``timestamptz``. Comparing the two
    without normalising raises on the naive/aware mix.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def scan_events(
    event_log: EventLog,
    *,
    event_type: EventType | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> EventScan:
    """Read a capped window newest-first, and report whether the cap bit.

    Use this instead of ``event_log.get_events(..., limit=N)`` anywhere the
    result is aggregated into a reported number.

    Two guarantees, in the order they matter:

    1. **The newest events survive the cap.** The read is issued
       ``order="desc"`` and the result reversed, so a window holding more
       than ``limit`` matches yields the most recent ``limit`` of them, in
       ascending order. Reversing rather than propagating descending order
       is deliberate: it keeps the *arrival* order every existing
       aggregation was written against, so the change is confined to
       *which* events are dropped.
    2. **Truncation is stated, never silent.** ``coverage.truncated`` is
       set when the read came back full, and ``coverage.covered_since``
       names the oldest event the caller could see — the real start of its
       evidence, as opposed to the window it asked for.

    ``matched`` is resolved with :meth:`EventLog.count`, which accepts only
    ``event_type`` and ``since``; a scan bounded by ``until`` therefore
    reports ``matched=None`` rather than a count over a wider window. A
    ``count`` that raises degrades the same way — telemetry about a read
    must never turn that read into a failure.

    A non-positive ``limit`` is passed through untouched and never
    reported as truncated; backends treat it as their own no-op.
    """
    events = event_log.get_events(
        event_type=event_type,
        since=since,
        until=until,
        limit=limit,
        order="desc",
    )
    truncated = limit > 0 and len(events) >= limit
    scanned = len(events)
    matched: int | None = scanned
    covered_since = ""

    if truncated:
        matched = None
        if until is None:
            try:
                matched = event_log.count(event_type=event_type, since=since)
            except Exception:
                # GRACEFUL-DEGRADATION: an unknown total is reported as
                # unknown. Guessing one would be the silent-truncation
                # failure wearing a number.
                logger.warning(
                    "scan_events.count_failed",
                    event_type=str(event_type) if event_type else None,
                    exc_info=True,
                )
        oldest = min((_as_utc(event.occurred_at) for event in events), default=None)
        if oldest is not None:
            covered_since = oldest.isoformat()

    # Restore chronological consumption order (see guarantee 1).
    events.reverse()

    type_label = event_type.value if event_type is not None else "(any)"
    coverage = ScanCoverage(
        limit=limit,
        scanned=scanned,
        matched=matched,
        dropped=None if matched is None else max(matched - scanned, 0),
        truncated=truncated,
        truncated_event_types=[type_label] if truncated else [],
        covered_since=covered_since,
        note=_scan_note(type_label, limit, scanned, matched, covered_since)
        if truncated
        else "",
    )
    return EventScan(events=events, coverage=coverage)


def _scan_note(
    type_label: str,
    limit: int,
    scanned: int,
    matched: int | None,
    covered_since: str,
) -> str:
    total = f"{matched:,}" if matched is not None else "an unknown number of"
    tail = (
        f" This report's evidence begins at {covered_since}, not at the "
        "start of the requested window."
        if covered_since
        else ""
    )
    return (
        f"TRUNCATED: the {limit:,}-event cap was reached scanning "
        f"{type_label} ({scanned:,} of {total} matching events read). "
        f"The newest events were kept and the oldest dropped.{tail}"
    )


def merge_coverage(*coverages: ScanCoverage) -> ScanCoverage:
    """Combine the per-read coverages of one report into a single verdict.

    A report is truncated if any of its reads was. ``covered_since`` takes
    the **latest** of the truncated reads' starts, because the narrowest
    slice bounds what the whole report can claim — reporting the earliest
    would overstate coverage, which is the failure this model exists to
    prevent. ``matched`` is ``None`` as soon as any contributing read
    could not establish its own total.
    """
    present = [c for c in coverages if c is not None]
    if not present:
        return ScanCoverage()

    truncated = [c for c in present if c.truncated]
    scanned = sum(c.scanned for c in present)
    matched: int | None = None
    if all(c.matched is not None for c in present):
        matched = sum(c.matched or 0 for c in present)

    types = sorted({t for c in truncated for t in c.truncated_event_types})
    starts = sorted(c.covered_since for c in truncated if c.covered_since)
    covered_since = starts[-1] if starts else ""
    limit = max((c.limit for c in present), default=0)

    note = ""
    if truncated:
        note = " ".join(c.note for c in truncated if c.note)

    return ScanCoverage(
        limit=limit,
        scanned=scanned,
        matched=matched,
        dropped=None if matched is None else max(matched - scanned, 0),
        truncated=bool(truncated),
        truncated_event_types=types,
        covered_since=covered_since,
        note=note,
    )
