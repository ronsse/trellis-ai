"""API request and response models — backward-compatibility shim.

All wire DTOs live in :mod:`trellis_wire` (zero-dependency-on-core).
This module re-exports them under their original names so existing
imports (``from trellis_api.models import BulkIngestRequest``) keep
working through the refactor.

New code should prefer::

    from trellis_wire import BulkIngestRequest

directly — routes will be migrated to that form incrementally.  The
shim stays indefinitely; it adds no cost beyond the import itself.

See [TODO.md — Client Boundary Phase 1, Step 2](../../TODO.md) for
the rationale behind the wire/core split.
"""

from trellis_wire import (
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

__all__ = [
    "BatchCommandItem",
    "BatchCommandRequest",
    "BatchCommandResponse",
    "BulkAliasItem",
    "BulkEdgeItem",
    "BulkEntityItem",
    "BulkGroupResult",
    "BulkIngestRequest",
    "BulkIngestResponse",
    "BulkItemResult",
    "CommandResponse",
    "DeprecationNotice",
    "EntityCreateRequest",
    "ErrorResponse",
    "FeedbackRequest",
    "HealthResponse",
    "IngestResponse",
    "LinkRequest",
    "PackRequest",
    "PackResponse",
    "PromoteRequest",
    "SearchRequest",
    "StatsResponse",
    "StatusResponse",
    "VersionResponse",
]
