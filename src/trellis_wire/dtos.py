"""Wire DTOs for the Trellis REST API and SDK.

One-to-one port of the classes that formerly lived in
``trellis_api/models.py``.  All DTOs now inherit from
:class:`trellis_wire.base.WireModel` (response-shaped) or
:class:`trellis_wire.base.WireRequestModel` (frozen, for inputs
clients construct).

**Naming parity:** class names are unchanged from ``trellis_api.models``
so existing imports (``from trellis_api.models import BulkIngestRequest``)
keep working via a re-export shim in that module.

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


class DeprecationNotice(WireModel):
    """One deprecated route surfaced by ``GET /api/version``.

    Mirrors :class:`trellis_api.deprecation.DeprecationEntry` but with
    ISO-string dates for wire-safe serialization.  Clients can log a
    warning or schedule a migration off the deprecated path before
    they actually call it.
    """

    path: str
    deprecated_since: str  # ISO date
    sunset_on: str  # ISO date
    replacement: str | None = None
    reason: str | None = None


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
    deprecations: list[DeprecationNotice] = Field(default_factory=list)


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
    tag_filters: dict[str, list[str]] | None = None


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
