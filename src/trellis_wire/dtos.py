"""Wire DTOs for the Trellis REST API and SDK.

All DTOs inherit from :class:`trellis_wire.base.WireModel`
(response-shaped) or :class:`trellis_wire.base.WireRequestModel`
(frozen, for inputs clients construct).

**Frozen scope:** requests are frozen; responses are not, because the
bulk-ingest route currently builds responses by incrementing counters
on nested models.  Fully freezing responses is tracked as a follow-up
in [TODO.md — Step 2](../../TODO.md).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from trellis_wire.base import WireModel, WireRequestModel
from trellis_wire.enums import BatchStrategy, NodeRole

# -- Generic responses --


class StatusResponse(WireModel):
    """Generic success response."""

    status: str = "ok"
    message: str | None = None


class ErrorResponse(WireModel):
    """Generic error response."""

    status: str = "error"
    message: str
    code: str | None = None


# -- Version handshake --


class VersionResponse(WireModel):
    """Response body for ``GET /api/version``.

    Used by SDK clients on first call to enforce compatibility:

    * Reject when ``api_major`` differs from the client's expected major.
    * Warn when ``api_minor`` is older than the client's expected minor.
    * Reject when the client's own version is below ``sdk_min``.

    ``package_version`` is informational only — it's the actual
    installed ``trellis-ai`` package version, which may move without
    changing the API contract.
    """

    api_major: int
    api_minor: int
    api_version: str  # convenience: f"{api_major}.{api_minor}"
    wire_schema: str
    sdk_min: str
    package_version: str
    mcp_tools_version: int = 1


# -- Ingest --


class IngestResponse(WireModel):
    """Response after ingesting a trace or evidence."""

    status: str = "ok"
    trace_id: str | None = None
    evidence_id: str | None = None


# -- Retrieve --


class SearchRequest(WireRequestModel):
    """Full-text search request."""

    q: str
    domain: str | None = None
    limit: int = 20


class PackRequest(WireRequestModel):
    """Request to assemble a context pack."""

    intent: str
    domain: str | None = None
    agent_id: str | None = None
    max_items: int = 50
    max_tokens: int = 8000
    #: Per-facet operator dict: ``{"signal_quality": {"not_in": ["noise"]}}``.
    #: Operators are ``in`` / ``not_in`` / ``eq`` / ``ne``. See
    #: :func:`trellis.stores.base.tag_filters.normalize_facet_filter`.
    tag_filters: dict[str, dict[str, Any]] | None = None


class PackResponse(WireModel):
    """Response containing an assembled context pack."""

    status: str = "ok"
    pack_id: str
    intent: str
    domain: str | None = None
    agent_id: str | None = None
    count: int
    items: list[dict[str, Any]]
    advisories: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_report: dict[str, Any] | None = None


# -- Curate --


class PromoteRequest(WireRequestModel):
    """Request to promote a trace to a precedent."""

    trace_id: str
    title: str
    description: str
    requested_by: str = "api:promote"


class LinkRequest(WireRequestModel):
    """Request to create a graph edge."""

    source_id: str
    target_id: str
    edge_kind: str = "entity_related_to"
    properties: dict[str, Any] | None = None


class EntityCreateRequest(WireRequestModel):
    """Request to create an entity node."""

    entity_type: str
    name: str
    entity_id: str | None = None  # caller-supplied ID; ULID auto-generated if omitted
    properties: dict[str, Any] = Field(default_factory=dict)


class FeedbackRequest(WireRequestModel):
    """Request to record feedback on a target."""

    target_id: str
    rating: float
    comment: str | None = None
    pack_id: str | None = None  # Link feedback to a context pack


class PackFeedbackRequest(WireRequestModel):
    """Request to record element-level feedback on a context pack.

    Mirrors the MCP ``record_feedback`` tool's surface so the SDK,
    REST, and MCP feedback paths share one payload shape. Routes
    through :func:`trellis.feedback.recording.record_feedback`, which
    appends the durable ``pack_feedback.jsonl`` row and emits the
    authoritative ``FEEDBACK_RECORDED`` event.
    """

    success: bool
    helpful_item_ids: list[str] = Field(default_factory=list)
    unhelpful_item_ids: list[str] = Field(default_factory=list)
    followed_advisory_ids: list[str] = Field(default_factory=list)
    target_id: str | None = None  # trace/entity the pack supported, if any
    rating: float | None = None  # explicit 0.0-1.0 score; defaults from success
    comment: str | None = None  # free-text notes


class PackFeedbackResponse(WireModel):
    """Result of recording pack feedback.

    ``event_log_in_sync`` surfaces the
    :attr:`trellis.feedback.recording.FeedbackRecordResult.event_log_in_sync`
    semantics: ``True`` when the authoritative ``FEEDBACK_RECORDED``
    event reached the EventLog (emitted now, or already present from a
    prior call). ``False`` means only the durable JSONL row was written
    and a reconcile is owed — callers must not treat the signal as
    having driven behavior yet.
    """

    status: str = "ok"
    pack_id: str
    feedback_id: str
    feedback: str  # "positive" | "negative"
    event_log_in_sync: bool
    event_log_emitted: bool
    event_log_skipped_as_duplicate: bool


class CommandResponse(WireModel):
    """Response after executing a mutation command."""

    status: str
    command_id: str
    operation: str
    message: str
    created_id: str | None = None


# -- Batch mutations --


class BatchCommandItem(WireRequestModel):
    """A single command within a batch request."""

    operation: str
    target_id: str | None = None
    target_type: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BatchCommandRequest(WireRequestModel):
    """Request to execute a batch of mutation commands."""

    commands: list[BatchCommandItem]
    strategy: str = "stop_on_error"
    requested_by: str = "api:mutations"
    metadata: dict[str, Any] = Field(default_factory=dict)


class BatchCommandResponse(WireModel):
    """Response after executing a batch of mutation commands."""

    status: str = "ok"
    batch_id: str
    strategy: str
    total: int
    executed: int
    succeeded: int
    failed: int
    rejected: int
    duplicates: int
    results: list[CommandResponse]


# -- Bulk ingest (entities + edges + aliases in one request) --


# NOTE: BulkEntityItem / BulkEdgeItem previously inherited from
# EntityCreateRequest / LinkRequest.  Because EntityCreateRequest and
# LinkRequest are now frozen request DTOs, subclassing them carries
# ``frozen=True`` forward — which is exactly what we want for bulk
# request items too.  Inheritance is preserved so the wire shape is
# bit-identical to the pre-refactor classes.


class BulkEntityItem(EntityCreateRequest):
    """A single entity in a bulk ingest request.

    Inherits base entity fields from ``EntityCreateRequest`` and adds
    ``node_role``, ``generation_spec`` (for curated nodes), and an
    optional per-item ``idempotency_key``.
    """

    node_role: NodeRole = NodeRole.SEMANTIC
    generation_spec: dict[str, Any] | None = None
    idempotency_key: str | None = None


class BulkEdgeItem(LinkRequest):
    """A single edge in a bulk ingest request.

    Inherits base edge fields from ``LinkRequest`` and adds an optional
    per-item ``idempotency_key``. Overrides ``properties`` to default to
    an empty dict (``LinkRequest`` defaults to ``None``).
    """

    properties: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class BulkAliasItem(WireRequestModel):
    """A single alias in a bulk ingest request."""

    entity_id: str
    source_system: str
    raw_id: str
    raw_name: str | None = None
    match_confidence: float = 1.0
    is_primary: bool = False


class BulkIngestRequest(WireRequestModel):
    """Bulk ingest request carrying entities, edges, and aliases.

    Processed in order: entities → edges → aliases (edges and aliases
    reference entities, so entities must land first). Default strategy is
    ``continue_on_error`` — the common backfill case where partial success
    is acceptable and the caller wants a full per-item report.
    """

    entities: list[BulkEntityItem] = Field(default_factory=list)
    edges: list[BulkEdgeItem] = Field(default_factory=list)
    aliases: list[BulkAliasItem] = Field(default_factory=list)
    strategy: BatchStrategy = BatchStrategy.CONTINUE_ON_ERROR
    requested_by: str = "api:bulk-ingest"


class BulkItemResult(WireModel):
    """Per-item result within a bulk ingest group."""

    status: str  # "success", "failed", "rejected", "duplicate", "skipped"
    id: str | None = None  # node_id / edge_id / alias_id on success
    name: str | None = None  # echoes entity name / edge source→target / raw_id
    message: str = ""


class BulkGroupResult(WireModel):
    """Aggregated results for one group (entities, edges, or aliases)."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    rejected: int = 0
    duplicates: int = 0
    skipped: int = 0
    results: list[BulkItemResult] = Field(default_factory=list)


class BulkIngestResponse(WireModel):
    """Response for a bulk ingest request."""

    status: str = "ok"
    batch_id: str
    strategy: str
    entities: BulkGroupResult = Field(default_factory=BulkGroupResult)
    edges: BulkGroupResult = Field(default_factory=BulkGroupResult)
    aliases: BulkGroupResult = Field(default_factory=BulkGroupResult)


# -- Admin --


class HealthResponse(WireModel):
    """Health check response."""

    status: str = "ok"
    checks: dict[str, bool] = Field(default_factory=dict)


class StatsResponse(WireModel):
    """Store statistics response."""

    status: str = "ok"
    traces: int = 0
    documents: int = 0
    nodes: int = 0
    edges: int = 0
    events: int = 0


# -- Review queue (WP10) --
#
# DTOs backing the admin Review view (human-decision inbox). The four
# queue surfaces are: tuner proposals, learning-promotion candidates,
# schema-evolution candidates, and code-authoring proposals. Per
# ``docs/design/adr-autonomy-ladder.md`` the approve/reject/promote
# actions are human-gated; schema-evolution exposes *only* a draft-ADR
# action (no machine promote path).


class TunerProposalSummary(WireModel):
    """One pending tuner proposal as surfaced in the Review queue."""

    proposal_id: str
    tuner: str
    status: str
    component_id: str
    domain: str | None = None
    intent_family: str | None = None
    tool_name: str | None = None
    proposed_values: dict[str, Any] = Field(default_factory=dict)
    baseline_values: dict[str, Any] = Field(default_factory=dict)
    sample_size: int = 0
    effect_size: float | None = None


class TunerProposalListResponse(WireModel):
    """List of pending tuner proposals."""

    count: int = 0
    proposals: list[TunerProposalSummary] = Field(default_factory=list)


class ProposalPreviewResponse(WireModel):
    """Dry-run forecast for a single proposal promotion."""

    proposal_id: str
    predicted_status: str
    reason: str
    proposed_values: dict[str, Any] = Field(default_factory=dict)
    baseline_values: dict[str, Any] = Field(default_factory=dict)
    effect_size: float | None = None
    sample_size: int = 0


class ProposalDecisionResponse(WireModel):
    """Outcome of an approve / reject action on a proposal."""

    proposal_id: str
    status: str  # "promoted" | "rejected" | "skipped"
    reason: str
    params_version: str | None = None
    effect_size: float | None = None


class ProposalRejectRequest(WireRequestModel):
    """Body for ``POST /api/v1/proposals/{id}/reject`` — optional rationale."""

    reason: str = "rejected_by_reviewer"


class LearningCandidateListResponse(WireModel):
    """Most-recent learning-candidate artifact, or an empty hint."""

    status: str = "ok"
    generated_at_utc: str | None = None
    candidate_count: int = 0
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    hint: str | None = None


class LearningPromotionDecision(WireRequestModel):
    """One per-candidate decision in a learning-promotion submission."""

    candidate_id: str
    approved: bool = False
    promotion_name: str = ""
    rationale: str = ""


class LearningPromotionRequest(WireRequestModel):
    """Body for ``POST /api/v1/learning/promotions``.

    Carries per-candidate decisions. The server joins these against the
    most-recent candidate artifact, builds the entity/edge payloads via
    ``prepare_learning_promotions``, and submits each approved promotion
    through the governed :class:`MutationExecutor` pipeline.
    """

    decisions: list[LearningPromotionDecision] = Field(default_factory=list)


class LearningPromotionResultRow(WireModel):
    """Per-candidate outcome of a learning-promotion submission."""

    candidate_id: str
    status: str
    entity_id: str | None = None
    node_id: str | None = None
    message: str | None = None


class LearningPromotionResponse(WireModel):
    """Aggregate outcome of a learning-promotion submission."""

    status: str = "ok"
    approved_count: int = 0
    ready_count: int = 0
    promoted_count: int = 0
    results: list[LearningPromotionResultRow] = Field(default_factory=list)


class SchemaEvolutionCandidate(WireModel):
    """One WELL_KNOWN_CANDIDATE event as surfaced in the Review queue."""

    candidate_id: str
    candidate_kind: str | None = None
    open_string_value: str | None = None
    suggested_canonical_name: str | None = None
    count: int = 0
    distinct_extractors: list[str] = Field(default_factory=list)
    distinct_domains: list[str] = Field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None
    recorded_at: str | None = None


class SchemaEvolutionListResponse(WireModel):
    """List of schema-evolution candidates."""

    count: int = 0
    candidates: list[SchemaEvolutionCandidate] = Field(default_factory=list)


class DraftAdrResponse(WireModel):
    """Rendered promotion-ADR markdown for a schema-evolution candidate."""

    candidate_id: str
    markdown: str
    suggested_canonical_name: str | None = None


class CodeProposalSummary(WireModel):
    """One PROPOSAL_DRAFTED event as surfaced in the Review queue."""

    proposal_id: str
    cluster_signature: str = ""
    source_file: str | None = None
    source_event_count: int = 0
    markdown_preview: str = ""
    generated_at: str | None = None


class CodeProposalListResponse(WireModel):
    """List of code-authoring proposals (read-only)."""

    count: int = 0
    proposals: list[CodeProposalSummary] = Field(default_factory=list)


# -- Metrics dashboard (WP11) --
#
# DTOs backing ``GET /api/v1/metrics/timeseries`` — server-computed
# improvement-metric trends read from the EventLog (no new storage). The
# aggregation lives in ``trellis.retrieve.metrics_timeseries``; these
# DTOs are the wire projection of its result dataclasses.


class TimeseriesPointResponse(WireModel):
    """One bucket's value for one series.

    ``bucket_start`` is a UTC calendar-day key (``"YYYY-MM-DD"``).
    ``value`` is the metric value for that bucket; ``sample_count`` is
    the number of underlying observations (packs, items, or events —
    metric-dependent) so clients can dim low-confidence points.

    Buckets with no contributing events are **omitted** from a series
    rather than zero-filled — clients infer gaps from the missing
    ``bucket_start`` keys (an absent day means "no signal", not "zero").
    """

    bucket_start: str
    value: float
    sample_count: int = 0


class TimeseriesSeriesResponse(WireModel):
    """One group's ordered list of buckets.

    ``group_key`` is the resolved grouping value: a domain, an
    intent_family, ``"all"`` when ungrouped, or — for
    ``parameter_promotions`` — the governance event type. Points are
    sorted by ``bucket_start`` ascending and omit empty buckets (see
    :class:`TimeseriesPointResponse`).
    """

    group_key: str
    points: list[TimeseriesPointResponse] = Field(default_factory=list)


class MetricsTimeseriesResponse(WireModel):
    """Response for ``GET /api/v1/metrics/timeseries``.

    Echoes the request parameters (``metric`` / ``bucket`` / ``group_by``
    / ``days``) so the client can label the chart without re-deriving
    them, and carries one series per resolved group key. An empty store
    (or a metric with no events in the window) yields an empty
    ``series`` list.
    """

    metric: str
    bucket: str = "day"
    group_by: str = "none"
    days: int = 30
    series: list[TimeseriesSeriesResponse] = Field(default_factory=list)
