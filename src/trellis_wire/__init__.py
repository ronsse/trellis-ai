"""Trellis wire contract — frozen DTOs shared by the REST API and SDK.

This package is the boundary between the Trellis runtime and its
clients.  It has **zero dependencies on ``trellis.*`` core** so that:

* Client packages (``trellis_sdk``, third-party extractors like
  ``trellis_unity_catalog``) can depend on wire DTOs without pulling
  in stores, executors, or any runtime infrastructure.
* Core schemas can evolve independently — as long as translators in
  :mod:`trellis.wire.translate` keep the mapping intact, wire
  contracts stay stable.

Wire DTOs differ from core Pydantic models on two axes:

* ``extra="forbid"`` — non-negotiable; unknown fields are rejected.
  This is the primary contract guarantee.
* ``frozen=True`` — applied to **request** DTOs (constructed once by
  clients) but not to responses (which routes currently populate
  incrementally).  Fully freezing responses is tracked as a follow-up
  in [TODO.md — Client Boundary Phase 1, Step 2](../../TODO.md).

Wire-level enum values are identical to their core counterparts and
guarded by parity tests (see ``tests/unit/wire/test_parity.py``).
"""

from trellis_wire.base import WireModel, WireRequestModel
from trellis_wire.dtos import (
    BatchCommandItem,
    BatchCommandRequest,
    BatchCommandResponse,
    BulkAliasItem,
    BulkEdgeItem,
    BulkEntityItem,
    BulkGroupResult,
    BulkIngestRequest,
    BulkIngestResponse,
    BulkItemResult,
    CommandResponse,
    DeprecationNotice,
    EntityCreateRequest,
    ErrorResponse,
    FeedbackRequest,
    HealthResponse,
    IngestResponse,
    LinkRequest,
    PackRequest,
    PackResponse,
    PromoteRequest,
    SearchRequest,
    StatsResponse,
    StatusResponse,
    VersionResponse,
)
from trellis_wire.enums import BatchStrategy, NodeRole
from trellis_wire.extract import (
    DraftSubmissionRequest,
    DraftSubmissionResult,
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
)

__all__ = [
    # Base
    "WireModel",
    "WireRequestModel",
    # Enums
    "BatchStrategy",
    "NodeRole",
    # Generic
    "StatusResponse",
    "ErrorResponse",
    # Version handshake
    "DeprecationNotice",
    "VersionResponse",
    # Ingest
    "IngestResponse",
    # Retrieve
    "SearchRequest",
    "PackRequest",
    "PackResponse",
    # Curate
    "PromoteRequest",
    "LinkRequest",
    "EntityCreateRequest",
    "FeedbackRequest",
    "CommandResponse",
    # Batch mutations
    "BatchCommandItem",
    "BatchCommandRequest",
    "BatchCommandResponse",
    # Bulk ingest
    "BulkEntityItem",
    "BulkEdgeItem",
    "BulkAliasItem",
    "BulkIngestRequest",
    "BulkItemResult",
    "BulkGroupResult",
    "BulkIngestResponse",
    # Admin
    "HealthResponse",
    "StatsResponse",
    # Extract (client-side extractor contract)
    "DraftSubmissionRequest",
    "DraftSubmissionResult",
    "EdgeDraft",
    "EntityDraft",
    "ExtractionBatch",
    "ExtractorTier",
]
