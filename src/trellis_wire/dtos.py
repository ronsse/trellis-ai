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

from typing import Any, Literal

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

    ``write_provenance`` is the write-provenance stamp this server puts
    on every event it emits (build identity + the write-behaviour
    environment it was launched with).  It exists so an operator can ask
    a *running container* what it is, rather than inferring it from
    whatever image was last built.  Free-form: it mirrors
    ``metadata.write_provenance`` on stored events, whose shape is owned
    by :mod:`trellis.core.write_provenance`, not by the wire contract.
    ``None`` means the server withheld it — it is ops detail, gated like
    the ``/readyz`` breakdown — not that the server cannot report it.

    ``write_behaviour_settings`` is gated identically (``None`` under the
    same withholding) but, unlike ``write_provenance``, is read fresh on
    every call rather than cached once per process: a settings-store
    override can change between two calls with no restart in between.
    Each row is one :meth:`trellis.core.write_config.WriteBehaviourConfig.describe`
    entry — ``name``, ``env_var``, ``value``, ``default``, ``overridden``,
    and ``overridden_by`` (``"env"`` / ``"settings"`` / ``"default"``).
    """

    api_major: int
    api_minor: int
    api_version: str  # convenience: f"{api_major}.{api_minor}"
    wire_schema: str
    sdk_min: str
    package_version: str
    mcp_tools_version: int = 1
    write_provenance: dict[str, Any] | None = None
    write_behaviour_settings: list[dict[str, Any]] | None = None


# -- Ingest --


class IngestResponse(WireModel):
    """Response after ingesting a trace or evidence."""

    status: str = "ok"
    trace_id: str | None = None
    evidence_id: str | None = None
    #: Set by ``POST /traces``: ``True`` when the store already held the
    #: ``trace_id``, so this call stored and extracted nothing (traces are
    #: immutable). ``None`` on ``POST /evidence``, and from a server that
    #: predates the field.
    already_ingested: bool | None = None


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
    #: Request-scoped attribution carried into ``PACK_ASSEMBLED`` telemetry
    #: so the learning loop can credit the run and bucket the intent. Both
    #: optional: ``run_id`` has no derivation, ``intent_family`` is derived
    #: from ``intent`` server-side when omitted.
    run_id: str | None = None
    intent_family: str | None = None
    max_items: int = 50
    max_tokens: int = 8000
    #: Per-facet operator dict: ``{"signal_quality": {"not_in": ["noise"]}}``.
    #: Operators are ``in`` / ``not_in`` / ``eq`` / ``ne``. See
    #: :func:`trellis.stores.base.tag_filters.normalize_facet_filter`.
    tag_filters: dict[str, dict[str, Any]] | None = None


class ClassPriorWithheldDetail(WireModel):
    """One (document form, retrieval axis) pair the class-prior gate withheld.

    Counts only, same posture as the rest of :class:`WithholdingResponse` —
    ``form``/``axis`` name the class and the axis the prior was scoped to,
    never an item id or excerpt.
    """

    form: str
    axis: str
    count: int


class WithholdingResponse(WireModel):
    """Pack withholding telemetry exposed without item content."""

    total: int
    by_reason: dict[str, int]
    withheld_item_ids: list[str]
    non_absence_reasons: list[str]
    section_filtered: int
    served_count: int
    #: Breakdown of the ``class_prior`` row in ``by_reason`` by document
    #: form and retrieval axis, e.g. ``{"form": "conversation", "axis":
    #: "keyword", "count": 2}``. Always present (possibly empty) on a
    #: server new enough to emit it; absent on an older event payload,
    #: which the ``default_factory`` here reads the same as empty.
    class_prior_detail: list[ClassPriorWithheldDetail] = Field(default_factory=list)


class AxisReportResponse(WireModel):
    """Which retrieval axes this pack has, ran, and did not.

    Mirrors :class:`trellis.retrieve.builder_factory.AxisReport`: axis names
    and states only, never an exception message. ``embedder_error_type`` and
    ``embedder_setting`` are non-``None`` only when ``semantic ==
    "embedder_failed"`` (Q5-A); default to ``None`` so a client built
    against an older server still parses this model.
    """

    available: list[str]
    ran: list[str]
    failed: list[str]
    semantic: str
    #: ``"ran"``, ``"failed"``, or ``"unseeded_fallback_off"``: the graph axis
    #: ran and served nothing because no seed resolved and the server's
    #: ``TRELLIS_GRAPH_RECENCY_FALLBACK`` is off. Defaults to ``"ran"`` so a
    #: client parsing an older server's block, which has no such state, reads
    #: what that server did.
    graph: str = "ran"
    embedder_error_type: str | None = None
    embedder_setting: str | None = None


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
    #: Verbatim ``Pack.metadata["withholding"]`` telemetry. ``None`` means
    #: the response came from a server older than API 1.2.
    withholding: WithholdingResponse | None = None
    #: Which axes were available, ran, and failed during this build — the
    #: :func:`~trellis.retrieve.builder_factory.describe_axes` mapping that
    #: ``trellis retrieve pack --format json`` also prints. ``None`` means a
    #: server that predates this field.
    axes: AxisReportResponse | None = None


class SectionedPackRequest(WireRequestModel):
    """Request to assemble a sectioned context pack.

    ``sections`` rows are ``SectionRequest``-shaped dicts (name,
    retrieval_affinities, content_types, scopes, entity_ids,
    max_tokens, max_items); kept as raw dicts here so the wire
    package stays free of trellis-core imports — the server
    validates them into ``SectionRequest`` models.
    """

    intent: str
    sections: list[dict[str, Any]]
    domain: str | None = None
    agent_id: str | None = None
    #: See :class:`PackRequest` — same request-scoped attribution.
    run_id: str | None = None
    intent_family: str | None = None


class SectionedPackResponse(WireModel):
    """Response containing an assembled sectioned pack."""

    status: str = "ok"
    pack_id: str
    intent: str
    domain: str | None = None
    agent_id: str | None = None
    sections: list[dict[str, Any]]
    advisories: list[dict[str, Any]] = Field(default_factory=list)
    #: Verbatim ``SectionedPack.metadata["withholding"]`` telemetry. ``None``
    #: means the response came from a server older than API 1.2.
    withholding: WithholdingResponse | None = None
    #: Which axes were available, ran, and failed during this build — the
    #: same :func:`~trellis.retrieve.builder_factory.describe_axes` mapping
    #: ``POST /api/v1/packs`` reports. ``None`` means a server that
    #: predates this field. The server refuses ``sections=[]`` (422), so
    #: every 200 has a section to read the "ran" axes off.
    axes: AxisReportResponse | None = None


# -- Curate --


class PromoteRequest(WireRequestModel):
    """Request to promote a trace to a precedent."""

    trace_id: str
    title: str
    description: str
    requested_by: str = "api:promote"


class PrecedentDemoteRequest(WireRequestModel):
    """Request to demote a precedent. ``target_id`` rides the URL path."""

    reason: str
    requested_by: str = "api:demote-precedent"


class PrecedentReinstateRequest(WireRequestModel):
    """Request to reinstate a demoted precedent. ``target_id`` rides the
    URL path.

    No ``already_demoted``-shaped response field exists for this verb: a
    target that is not currently ``"demoted"`` is a hard rejection (400),
    never a 200 idempotent no-op the way re-demoting an already-demoted
    precedent is — so the response is a plain ``CommandResponse``
    (``trellis_api.routes._results.command_response``), not a dedicated
    DTO the way :class:`PrecedentDemoteResponse` is.
    """

    reason: str
    requested_by: str = "api:reinstate-precedent"


class LinkRequest(WireRequestModel):
    """Request to create a graph edge.

    ``allow_dangling`` mirrors ``EdgeDraft.allow_dangling`` over the wire: it
    opts the edge out of the ``LinkCreateHandler`` pre-flight FK check.
    Default ``False`` keeps strict FK semantics.

    Backend caveat: the flag skips the *handler* check only. On the
    Bolt/openCypher backends (ArcadeDB, Neo4j) the store layer still requires
    both endpoint vertices — a graph relationship cannot exist without its
    endpoints — so an edge-before-node write fails there regardless of this
    flag ("has no current version"). A true dangling edge is representable
    only on the SQLite/Postgres backends, where edges are rows keyed by
    node-id strings. On Bolt substrates, materialise a stub endpoint node
    first (the pattern ``trellis.meta.recorder`` uses); see issues #211/#215.
    """

    source_id: str
    target_id: str
    edge_kind: str = "entity_related_to"
    properties: dict[str, Any] | None = None
    allow_dangling: bool = False


class EntityCreateRequest(WireRequestModel):
    """Request to create an entity node."""

    entity_type: str
    name: str
    entity_id: str | None = None  # caller-supplied ID; ULID auto-generated if omitted
    properties: dict[str, Any] = Field(default_factory=dict)


class FeedbackRequest(WireRequestModel):
    """Request to record feedback on a target."""

    target_id: str
    #: Explicit 0.0-1.0 score. Bounded here because it lands at
    #: ``payload["rating"]``, the key the fitness loops threshold against —
    #: the MCP surface enforces the same range.
    rating: float = Field(ge=0.0, le=1.0)
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

    #: Explicit outcome claim. Optional so a graded call can omit it:
    #: every consumer reads ``payload["success"]`` before falling back to
    #: ``rating``, so shipping a defaulted ``True`` alongside
    #: ``rating=0.3`` would have the loops read a mediocre pack as a
    #: plain win. ``None`` means "derive it from the rating" —
    #: :data:`trellis.feedback.models.SUCCESS_RATING_THRESHOLD`.
    success: bool | None = None
    helpful_item_ids: list[str] = Field(default_factory=list)
    unhelpful_item_ids: list[str] = Field(default_factory=list)
    followed_advisory_ids: list[str] = Field(default_factory=list)
    target_id: str | None = None  # trace/entity the pack supported, if any
    #: Explicit 0.0-1.0 score; defaults from ``success``. Bounded here
    #: because it lands at ``payload["rating"]``, the key the fitness
    #: loops threshold against — the MCP surface enforces the same range.
    rating: float | None = Field(default=None, ge=0.0, le=1.0)
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
    # Non-blocking policy verdicts (``Enforcement.WARN``, or an
    # ``action="warn"`` rule) carried forward from
    # ``CommandResult.warnings``. Always present, unlike the event
    # payload's conditional ``policy_warnings`` key -- an audit event
    # keeps that key absent so a zero-policy deployment emits a
    # byte-identical payload to the pre-gate world, but for a response a
    # caller reads, an always-present ``[]`` is what distinguishes "the
    # gate ran and nothing warned" from "this build predates the field".
    warnings: list[str] = Field(default_factory=list)


class PrecedentDemoteResponse(WireModel):
    """Response after ``POST /precedents/{target_id}/demote``.

    A dedicated shape rather than a second construction of
    :class:`CommandResponse` (``tests/unit/api/test_command_response_rule.py``
    pins that DTO to exactly one projection, shared by curate/extract/
    mutations and meaningless to add a demote-only field to) --
    mirroring that DTO's fields and adding ``already_demoted``: true when
    the command was a no-op because the target was already in the
    demoted state (``PrecedentDemoteHandler``'s idempotent re-demotion),
    false on every other outcome including a fresh demotion. Built only
    in ``trellis_api.routes.curate.demote``.
    """

    status: str
    command_id: str
    operation: str
    message: str
    created_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    already_demoted: bool = False


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


class BatchCommandBodyErrorItem(WireModel):
    """One pydantic field error, mirroring FastAPI's own validation-error shape.

    FastAPI's ``ValidationError`` items always carry ``input`` (the value
    that failed) and, for some error types (e.g. a wrong-type error),
    ``ctx`` (extra context such as the expected type). Both are declared
    here so a real 422 validates against this model -- ``extra="forbid"``
    on :class:`~trellis_wire.base.WireModel` otherwise rejects every real
    response, since pydantic always emits ``input`` and often emits
    ``ctx``.
    """

    loc: list[str | int]
    msg: str
    type: str
    input: Any = None
    ctx: dict[str, Any] | None = None


class BatchCommandBodyError(WireModel):
    """``/commands/batch`` 422: the request body failed pydantic validation.

    Same shape as every other route's default 422 (FastAPI's own
    ``HTTPValidationError``) -- declared separately here, rather than
    reusing that name, because ``/commands/batch`` also documents a
    second, differently-shaped 422 cause (:class:`BatchOperationNameError`)
    under the same status code, via ``anyOf``.
    """

    detail: list[BatchCommandBodyErrorItem]


class BatchOperationNameError(WireModel):
    """``/commands/batch`` 422: a command named an operation outside the
    :class:`~trellis.mutate.Operation` enum.

    ``_parse_operation`` catches ``Operation(value)``'s ``ValueError`` and
    raises a plain-string-``detail`` ``HTTPException`` naming the bad
    value and the valid vocabulary -- not the list-of-field-errors shape
    pydantic body validation produces, so it needs its own model rather
    than a ``$ref`` to ``HTTPValidationError``.
    """

    detail: str


class DefaultValidationError(WireModel):
    """FastAPI's own default 422 shape: ``detail`` is a list of field
    errors (``loc`` / ``msg`` / ``type`` / ...), generated automatically
    for any route with query/path/body params FastAPI itself validates.

    Structurally identical to :class:`BatchCommandBodyError` -- that
    model stays separately named and documented for ``/commands/batch``
    specifically; this one is the reusable, route-agnostic declaration
    for any other route whose 422 can be *either* that default shape
    *or* a hand-raised plain-string-``detail`` shape
    (:class:`StringDetailError`), so the two can be declared together
    via a ``model`` union without re-deriving the list shape per route.
    """

    detail: list[BatchCommandBodyErrorItem]


class StringDetailError(WireModel):
    """A 422 whose ``detail`` is a plain string, not FastAPI's default
    list-of-field-errors shape -- the shape of a hand-raised
    ``HTTPException(422, detail="...")`` guarding an input check FastAPI's
    own query/body validation cannot express (an unknown enum-like value,
    an empty list, an out-of-range computed field, ...). Reused across
    every route with this pattern instead of one model per route, since
    the shape itself carries no route-specific fields.
    """

    detail: str


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


class BackendHealth(WireModel):
    """One backend's probe outcome — mirrors ``/readyz``'s per-backend shape."""

    status: str
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(WireModel):
    """Health check response.

    ``checks`` is a flat bool-per-key view (``api`` plus one key per
    probed backend) kept for backward compatibility with existing
    callers; ``backends`` carries the same probe results with latency
    and error detail for a dashboard. Both are derived from the same
    probe run (``trellis_api.routes.health.probe_backends``) — never a
    hard-coded literal.
    """

    status: str = "ok"
    checks: dict[str, bool] = Field(default_factory=dict)
    backends: dict[str, BackendHealth] | None = None


class StatsResponse(WireModel):
    """Store statistics response.

    ``documents`` and ``document_rows`` describe two populations, and both
    are reported because each answers a question the other cannot (#412).

    **Breaking:** ``documents`` used to be the raw row count. A consumer
    that wants that number now reads ``document_rows``.
    """

    status: str = "ok"
    traces: int = 0
    documents: int = Field(
        default=0,
        description=(
            "Whole documents, excluding <parent>#chunk-N fragment rows."
            " Reconciles with GET /api/v1/documents' total."
        ),
    )
    document_rows: int = Field(
        default=0,
        description=(
            "Every stored document row, fragments included. Reconciles with"
            " GET /api/v1/documents?include_chunks=true."
        ),
    )
    nodes: int = 0
    edges: int = 0
    events: int = 0


class RuntimeResponse(WireModel):
    """Response body for ``GET /api/v1/runtime`` — the admin Runtime panel.

    A read-only snapshot of this process's effective configuration,
    sourced from the same resolvers the behaviour itself consults
    (``trellis_api.auth.resolve_auth_mode``,
    ``trellis_api.routes.health.resolve_ops_detail``,
    ``trellis_api.observability.resolve_metrics_public``,
    ``trellis_api.app.get_bind_info``) so this view can never drift from
    what a real request gets.

    Deliberately excludes anything secret-shaped: ``stores`` is class
    names only, ``embedder`` names only the configuration knob in play
    (never a key or URL), ``llm_routes`` carries tier names only (never a
    provider base URL, credential source, or API-key env name), and the
    two directory fields are plain filesystem paths with no embedded
    credentials. See ``tests/unit/api/test_runtime_route.py``'s
    secret-leakage test for the seeded-secret assertion backing that
    claim.
    """

    auth_mode: str
    auth_mode_permissive: bool = Field(
        description=(
            "True when a request presenting no credential is granted"
            " every scope (auth mode 'off' or 'optional') — the exact"
            " condition the UI's warning banner fires on."
        )
    )
    ops_detail: str
    metrics_public: bool
    bind_host: str
    bind_port: int
    stores: dict[str, str] = Field(
        default_factory=dict,
        description="Backend class name per store, keyed by store name.",
    )
    embedder: str | None = Field(
        default=None,
        description=(
            "Which knob registry.embedding_fn resolved from"
            " (TRELLIS_EMBEDDING_FN or embeddings.provider), the same"
            " resolver every retrieval surface calls; null when neither"
            " is set. 'unknown (...)' when the knob is set but failed to"
            " resolve -- never a key or URL."
        ),
    )
    llm_routes: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "Tier name each LLM consumer routes to, keyed by consumer"
            " name; null means the consumer is unrouted (falls back to"
            " the parent llm: block)."
        ),
    )
    llm_routes_error: str | None = Field(
        default=None,
        description="Set instead of llm_routes when llm.routes is malformed.",
    )
    learning_artifacts_dir: str | None = None
    host_jobs_dir: str | None = Field(
        default=None,
        description=(
            "TRELLIS_HOST_JOBS_DIR as seen by this process. Only the host"
            " job dispatcher (docs/ops/job-dispatcher.sh.example) resolves"
            " an executable path under it — this process never reads the"
            " directory itself, so a null here means the variable is"
            " unset in this process's environment, not that host-only jobs"
            " are unconfigured."
        ),
    )


# -- Settings (PR 3, governed settings write) --


class SettingUpdateRequest(WireRequestModel):
    """Body for ``PUT``/``POST /api/v1/settings/{name}``.

    ``value`` is required but nullable: an explicit ``null`` means "remove
    the override" (``Operation.SETTINGS_UPDATE``'s own contract — there is
    no separate reset verb), while an *absent* field is an ordinary 422,
    so a client cannot reset a setting by typo'ing the field name.
    """

    value: bool | int | float | str | None
    requested_by: str = "api:settings"


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
    #: Whether a snapshot written at this scope could ever be read back by
    #: the component it targets.  ``None`` means *not checked* — the
    #: component has no in-tree parameter reader to reason about — which is
    #: a different fact from ``True`` and calls for a different action.
    reachable: bool | None = None
    #: One sentence per reason the proposal is inert, empty when reachable.
    #: This is the approval surface, so the reason travels with the row an
    #: operator is judging rather than living only in a log line.
    unreachable_reasons: list[str] = Field(default_factory=list)


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
    """Most-recent learning-candidate artifact, or why none could be served.

    ``status`` is ``"ok"`` when the artifact was read (``candidates`` may
    still be empty) and ``"error"`` when there was none to read; then
    ``code`` says why (``learning_artifacts_dir_missing``,
    ``learning_candidates_missing``, ``learning_candidates_unreadable`` or
    ``stores_dir_unconfigured``) and ``hint`` names the path and how to
    produce the artifact. ``artifacts_dir`` names the directory looked in,
    ``None`` only when none could be resolved.
    """

    status: str = "ok"
    code: str | None = None
    artifacts_dir: str | None = None
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
    """Aggregate outcome of a learning-promotion submission.

    ``status`` is the run's verdict, the one ``trellis curate
    promote-learning`` states: ``"ok"`` when every approved row landed (or
    none was approved), ``"refused"`` when a row was declined before
    anything was written, or an approved row named no candidate
    (``approved_count`` above ``named_count``), and no write failed,
    ``"failed"`` when a write did not land. Rows that promoted still landed
    in a ``"refused"`` run. The HTTP status is 200 whatever the verdict.

    ``approved_count`` is the rows marked approved, blank ids included;
    ``named_count`` is the approved rows that name a candidate, the ones the
    run acted on and the right denominator for ``promoted_count``.
    ``promoted_count`` counts rows whose entity, edges and precedent event
    all landed; a row that wrote its entity but not the rest reads
    ``promoted_incomplete``.
    """

    status: Literal["ok", "refused", "failed"] = "ok"
    approved_count: int = 0
    named_count: int = 0
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


class ClassifyCandidate(WireModel):
    """One mined ``tag_keyword`` or ``domain_alias`` candidate, latest emission only.

    ``kind`` is ``"tag_keyword"`` (``key`` = keyword, ``target`` = domain
    tag) or ``"domain_alias"`` (``key`` = alias, ``target`` = canonical
    tag). ``status`` is ``"open"``, ``"applied"`` (an overlay row exists
    for this ``(kind, key)``) or ``"dismissed"`` (a
    ``CLASSIFY_CANDIDATE_DISMISSED`` event postdates this emission).

    ``metrics`` carries the mined candidate's own kind-specific fields
    verbatim from its ``to_event_payload()`` -- ``support``/``precision``/
    ``recall``/``lift``/``has_write_target`` for ``tag_keyword``;
    ``cooccurrence_rate``/``is_lexical_only``/``documents_gained``/etc. for
    ``domain_alias``. The two kinds' metric vocabularies differ enough
    that typing each field individually would force every row of one kind
    to carry nulls for the other's fields; a passthrough dict keeps the
    DTO's own fields (above) honest about what every row actually has.
    """

    candidate_id: str
    kind: str
    key: str
    target: str
    status: str
    recurrence_count: int = 0
    example_count: int = 0
    notes: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    emitted_at: str
    source_event_id: str


class ClassifyCandidateListResponse(WireModel):
    """List of mined classify candidates, deduped to their latest emission.

    ``store_degradation`` is the overrides store's own
    ``LoadDegradation.to_dict()`` (``None`` when the file loaded clean) --
    an applied-row-derived ``"applied"``/``"dismissed"`` status above is
    read from whatever of that file parsed, so a caller rendering this
    list needs the same "is what I'm showing the whole picture?" signal
    ``GET /settings`` and ``GET /policies`` already carry.
    """

    count: int = 0
    candidates: list[ClassifyCandidate] = Field(default_factory=list)
    store_degradation: dict[str, Any] | None = None


class ClassifyMineRequest(WireRequestModel):
    """Body for ``POST /api/v1/classify/candidates/mine``.

    ``use_recommended_defaults`` (default ``True``) mirrors ``trellis
    classify tag-candidates``/``domain-candidates``'s own
    ``_tag_evolution_registry``/``_domain_normalization_registry``: when
    the real, persistent ``ParameterRegistry`` is missing a required
    threshold for a component, mine with that component's shipped
    ``RECOMMENDED_SEED_VALUES`` instead of hard-failing -- never silent,
    since the response's ``used_recommended_defaults`` names exactly
    which component(s) this happened for and a ``structlog`` WARN is
    emitted for each. Pin it ``False`` to require every threshold
    pre-seeded (``trellis admin settings`` has no seeding command for
    these yet -- seed via a governed write directly) and get a 400
    naming the missing keys and their recommended values instead of a
    mined set of candidates run against unreviewed defaults.
    """

    kinds: list[str] = Field(default_factory=lambda: ["tag_keyword", "domain_alias"])
    scan_limit: int | None = None
    use_recommended_defaults: bool = True


class ClassifyMineResponse(WireModel):
    """Outcome of one mining run, plus the candidates it just emitted."""

    status: str = "ok"
    tag_keyword_count: int = 0
    domain_alias_count: int = 0
    used_recommended_defaults: list[str] = Field(default_factory=list)
    candidates: list[ClassifyCandidate] = Field(default_factory=list)


class ClassifyRuleRow(WireModel):
    """One effective tag/domain rule, with the source that wins it.

    ``source`` is ``"default"`` (built-in), ``"config"``
    (``config.yaml``) or ``"overlay"`` (applied through the governed
    write path) -- the precedence documented in
    ``trellis.classify.factory``. ``applied_at``/``applied_by``/
    ``source_event_id`` are set only for an ``"overlay"`` row.
    """

    kind: str
    key: str
    target: str
    source: str
    applied_at: str | None = None
    applied_by: str | None = None
    source_event_id: str | None = None


class ClassifyRuleListResponse(WireModel):
    """Effective tag/domain rules, defaults and config.yaml included.

    ``store_degradation`` is the overrides store's own
    ``LoadDegradation.to_dict()`` (``None`` when the file loaded clean) --
    a row that failed to parse there is simply missing from ``rules``
    with no other signal, same reasoning as ``ClassifyCandidateListResponse``.
    """

    count: int = 0
    rules: list[ClassifyRuleRow] = Field(default_factory=list)
    store_degradation: dict[str, Any] | None = None


class ClassifyRuleApplyRequest(WireRequestModel):
    """Body for ``POST /api/v1/classify/rules``.

    ``source_event_id`` must name the ``TAG_KEYWORD_CANDIDATE`` /
    ``DOMAIN_ALIAS_CANDIDATE`` event this rule was mined from, so the
    applied overlay row -- and its audit event -- stay traceable back to
    the evidence that justified it. ``support``/``precision``/``lift``
    are copied onto the ``CLASSIFY_RULE_APPLIED`` audit event verbatim;
    they are optional only because a caller applying a rule with no
    mined candidate behind it (hand-typed) has none to carry.

    Each metric's range mirrors what :mod:`trellis.learning.tag_evolution`
    computes it from -- ``support`` is a document count (``ge=0``),
    ``precision`` is ``support / keyword_documents`` (``ge=0, le=1``), and
    ``lift`` is ``precision / base_rate`` (``ge=0``, unbounded above).
    ``allow_inf_nan=False`` on the two floats refuses a raw ``NaN``/
    ``Infinity`` a scripted client can still send even though
    ``json.dumps`` never emits one by default -- without it, a float
    field happily accepts either and REST answers 400 from deeper in
    the pipeline instead of a clean 422 at the boundary.
    :class:`~trellis.mutate.handlers.ClassifyRuleApplyHandler` re-checks
    the same ranges itself before any write
    (``_validate_classify_rule_metrics``), so a caller that reaches the
    handler directly -- the CLI, a direct ``MutationExecutor.execute``
    -- is refused the same way even without this field; it buys REST
    callers a cheaper, more specific error, not the only backstop.
    """

    kind: str
    key: str
    target: str
    source_event_id: str
    support: int | None = Field(default=None, ge=0)
    precision: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    lift: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)


class ClassifyCandidateDismissResponse(WireModel):
    """Outcome of dismissing one open candidate."""

    status: str = "ok"
    candidate_id: str
    kind: str | None = None
    message: str | None = None


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

    ``scan_truncated`` / ``scanned_events`` flatten
    :class:`trellis.stores.base.event_log.ScanCoverage` (``truncated`` /
    ``scanned``) into plain scalars rather than nesting that core model
    directly: ``trellis_wire`` has zero dependency on ``trellis`` core by
    design (see ``trellis_wire.base``), so only the two fields the UI
    needs cross the boundary. ``scan_truncated`` is true when the
    underlying EventLog read(s) hit their cap, meaning buckets near the
    recent end of the window may be missing data rather than genuinely
    showing none (P1-8).
    """

    metric: str
    bucket: str = "day"
    group_by: str = "none"
    days: int = 30
    series: list[TimeseriesSeriesResponse] = Field(default_factory=list)
    scan_truncated: bool = False
    scanned_events: int | None = None


# -- Loop health --


class LoopHealthRowResponse(WireModel):
    """One loop's last-run snapshot, as served by ``GET /api/v1/loops``.

    ``last_run_at`` / ``last_status`` / ``counters`` are ``None``
    together exactly when the loop has produced no event the EventLog
    can see — "never run (or ran before this build)", never a bare
    ``0``. ``actuates`` says whether the loop can write without a human
    approving each change; ``what_it_changes`` is the one-line
    plain-English explanation either way.
    """

    name: str
    description: str
    actuates: bool
    what_it_changes: str
    #: The catalog job (``AdminJobSummary.name``) that drives this loop,
    #: or ``None`` when nothing schedules it. A UI links this to the
    #: Schedules tab row of the same name.
    job_name: str | None = None
    #: ISO-8601 UTC, not a ``datetime`` — see module note on DTOs below.
    last_run_at: str | None = None
    last_status: str | None = None
    counters: dict[str, Any] | None = None
    #: ``None`` when ``job_name`` is itself ``None`` (nothing schedules
    #: this loop). ``"not_scheduled"`` when the named job has no stored,
    #: enabled row with a cadence — disabled, manual-only, or never
    #: patched via ``PATCH /admin/jobs``. ``"scheduled"`` otherwise.
    schedule_status: str | None = None
    #: ``True`` only when ``schedule_status`` is ``"scheduled"`` AND a
    #: real last run (``last_run_at`` is not ``None``) is older than
    #: twice that job's own cadence tick interval plus slack. Always
    #: ``False`` for a loop with no job, a not-scheduled job, or one
    #: that has never run (that fact is already carried by
    #: ``last_run_at`` being ``None``).
    stale: bool = False


class LoopHealthResponse(WireModel):
    """Response for ``GET /api/v1/loops``: one row per loop."""

    generated_at: str
    loops: list[LoopHealthRowResponse] = Field(default_factory=list)


# -- Admin: scheduled jobs --
#
# DTOs backing ``GET``/``PATCH /api/v1/admin/jobs`` and
# ``POST /api/v1/admin/jobs/{name}/run``. The writable surface is
# deliberately thin: ``enabled``, ``cadence``, ``timeout_seconds`` and
# (via the run route) ``run_requested_at`` — the same four fields
# :class:`~trellis.schemas.schedule.ScheduledJob` exposes. There is no
# field here for a command, argv, path or environment variable, because
# the job's command always comes from
# :data:`trellis.schedule.catalog.JOB_CATALOG`, never from a request
# body (see that module's docstring for the full boundary argument).


class AdminJobLastRun(WireModel):
    """Most recent job-run record for one catalog job, from the EventLog.

    Derived from the latest ``JOB_RUN_STARTED`` / ``JOB_RUN_COMPLETED``
    pair (see ``trellis admin record-job-run``) — never a hardcoded
    literal. All fields ``None`` together means the job has no run
    record the EventLog can see (never run, or run before this build).
    ``started_at`` can be present with the rest ``None`` when a run
    started but the dispatcher never called ``record-job-run`` to close
    it out (crashed before reporting).
    """

    run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    status: str | None = None


class AdminJobSummary(WireModel):
    """One job: catalog identity joined with its schedule-registry row.

    ``name``, ``description``, ``host_only``, ``spends_llm`` and
    ``heavy`` are fixed catalog facts (source-reviewed, never writable
    over this API). ``enabled``, ``cadence``, ``timeout_seconds`` and
    ``run_requested_at`` are the mutable schedule-registry row —
    materialized with catalog defaults when no row has been written
    yet, so a job always appears here even before ``init-schedule`` or
    a first ``PATCH``. ``stored`` is ``False`` for exactly that
    materialized-default case (including a row that failed to parse
    out of a degraded store): the catalog default shown is never the
    operator's own choice, and ``next_due_at`` is always ``None`` for
    such a row — ``due-jobs`` only ever evaluates a *stored* row, so a
    catalog default could never actually fire at the time this field
    would otherwise claim. ``default_cadence`` is the catalog's own
    default, shown alongside the effective ``cadence`` so an operator
    can tell "customized" from "still the catalog default" without a
    second request. ``next_due_at`` is a display-only estimate
    (``None`` for a manual-only job, a job with no stored row, or when
    no tick could be found).
    """

    name: str
    description: str
    host_only: bool
    spends_llm: bool
    heavy: bool
    enabled: bool
    cadence: str | None = None
    default_cadence: str | None = None
    timeout_seconds: int
    run_requested_at: str | None = None
    last_run: AdminJobLastRun | None = None
    next_due_at: str | None = None
    stored: bool = True
    #: Loop names (``LoopHealthRowResponse.name``) this job drives, from
    #: ``trellis.ops.loop_health.JOB_LOOPS`` — the one mapping both this
    #: field and ``LoopHealthRowResponse.job_name`` are derived from.
    #: Empty for a job that drives no loop (most of the catalog).
    loops: list[str] = Field(default_factory=list)


class AdminJobListResponse(WireModel):
    """Response for ``GET /api/v1/admin/jobs``.

    ``dispatcher_last_seen_at`` is the most recent ``JOB_RUN_STARTED``
    across every catalog job — the only signal this server has that
    *some* dispatcher is polling ``due-jobs`` at all. ``dispatcher_seen``
    is the boolean a UI banner reads directly (recently seen within a
    server-chosen window) rather than re-deriving a staleness threshold
    client-side; ``False`` with ``dispatcher_last_seen_at is None`` means
    no dispatcher has ever reported. ``store_degradation`` is ``None``
    on a clean read and otherwise the same shape ``GET /api/v1/policies``
    and ``GET /api/v1/advisories`` already use (``LoadDegradation.
    to_dict()``, recovery command included) — a caller reading only
    ``jobs``/``count`` would otherwise see catalog defaults presented as
    the operator's own stored choices with no signal that the file
    behind them is damaged.
    """

    count: int = 0
    jobs: list[AdminJobSummary] = Field(default_factory=list)
    dispatcher_last_seen_at: str | None = None
    dispatcher_seen: bool = False
    store_degradation: dict[str, Any] | None = None


class AdminJobPatchRequest(WireRequestModel):
    """Body for ``PATCH /api/v1/admin/jobs/{name}``.

    Every field is optional with no sentinel beyond Pydantic's own
    ``model_fields_set``: the route reads which fields were actually
    present in the request body (vs omitted) so a ``PATCH`` changes only
    what the caller named — sending ``"cadence": null`` explicitly
    clears cadence (makes the job manual-only); omitting ``cadence``
    leaves it unchanged. ``extra="forbid"`` (:class:`WireRequestModel`)
    refuses any other field name outright, including a command, argv,
    path or environment variable — there is no such field here to
    forbid *selectively*, which is the point.
    """

    enabled: bool | None = None
    cadence: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)


class AdminJobPatchResponse(WireModel):
    """Response for a successful ``PATCH /api/v1/admin/jobs/{name}``."""

    status: str = "ok"
    job: AdminJobSummary


class AdminJobRunResponse(WireModel):
    """Response for ``POST /api/v1/admin/jobs/{name}/run``.

    Never claims synchronous execution: the write sets
    ``run_requested_at`` and nothing else runs inline.
    ``dispatcher_hint`` is the fixed, honest sentence a UI shows beside
    the confirm — a host dispatcher polling for due jobs on its own
    cadence is what actually executes the job, if one is installed.
    ``run_requested_at`` is exactly what the re-read row carries —
    ``None`` on the (expected-unreachable, since a non-success result
    raises before this is built) chance the handler's own write did not
    stick — never a timestamp this route or the CLI computed itself,
    which would report a value the handler never wrote.
    """

    status: str = "ok"
    name: str
    run_requested_at: str | None
    dispatcher_hint: str = (
        "Accepted. A host dispatcher, if one is installed, picks this "
        "up on its next poll, typically within ~5 minutes."
    )
