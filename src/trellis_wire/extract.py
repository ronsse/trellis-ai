"""Wire DTOs for the client-side extraction contract.

Client packages (``trellis_unity_catalog``, dbt syncs, custom domain
extractors) construct :class:`ExtractionBatch` objects locally and
submit them via :meth:`trellis_sdk.TrellisClient.submit_drafts` →
``POST /api/v1/extract/drafts``.

The shape mirrors :mod:`trellis.schemas.extraction` but lives here
so clients can depend on ``trellis_wire`` in isolation.  Translators
in :mod:`trellis.wire.translate` convert between the two sides; a
parity test (see ``tests/unit/wire/test_extract.py``) keeps them
honest.

**Namespaced types are the 80% extension path.**  Clients should use
``entity_type="unity_catalog.table"`` / ``edge_kind="unity_catalog.contains"``
rather than reusing well-known core values.  The server accepts any
string ([TODO.md — Sprint B](../../TODO.md#sprint-b--unblock-platform-integration))
so namespacing keeps domains from colliding without requiring core
changes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from trellis_wire.base import WireModel, WireRequestModel
from trellis_wire.dtos import CommandResponse
from trellis_wire.enums import BatchStrategy, NodeRole


class ExtractorTier(StrEnum):
    """Cost tier declared by an extractor when submitting drafts.

    Used for audit telemetry and effectiveness analysis.  The server
    trusts the client's self-reported tier — it's a honesty signal,
    not an enforcement mechanism.  Wire-level duplicate of
    :class:`trellis.extract.base.ExtractorTier`.
    """

    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    LLM = "llm"


class EntityDraft(WireRequestModel):
    """An entity candidate submitted by a client extractor.

    Mirrors :class:`trellis.schemas.extraction.EntityDraft` but
    without the ``generation_spec`` field (curated-node provenance
    goes through ``/api/v1/entities`` with an explicit curation
    flow; extraction drafts are machine-generated).

    ``properties`` is an escape hatch for domain-specific fields
    (Unity Catalog column schemas, dbt config blocks, etc.) —
    validated opaquely at the wire level.  A future schema registry
    (deferred; see [TODO.md](../../TODO.md)) may add per-namespace
    typed validation; for now, clients own the shape.
    """

    entity_type: str
    name: str
    entity_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    node_role: NodeRole = NodeRole.SEMANTIC
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class EdgeDraft(WireRequestModel):
    """An edge candidate submitted by a client extractor.

    ``source_id`` / ``target_id`` may reference either final entity
    IDs or draft-local IDs produced in the same batch — the server
    resolves draft-local references before issuing commands.
    """

    source_id: str
    target_id: str
    edge_kind: str
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ExtractionBatch(WireRequestModel):
    """One unit of submission from a client extractor.

    ``idempotency_key`` is the primary deduplication signal — the
    server records it on the audit trail and a repeat submission
    with the same key is a no-op.  Typical pattern: combine the
    extractor name with a source snapshot identifier, e.g.
    ``"unity_catalog-sync-2026-04-17T14:22:00Z"``.

    ``extractor_name`` / ``extractor_version`` feed the audit
    trail's ``requested_by`` field as ``f"{name}@{version}"`` so
    downstream effectiveness analysis can attribute drafts to a
    specific client release.
    """

    source: str  # namespaced: "unity_catalog", "dbt", "openlineage", ...
    extractor_name: str  # e.g. "trellis_unity_catalog.reader"
    extractor_version: str  # semver recommended, freeform accepted
    entities: list[EntityDraft] = Field(default_factory=list)
    edges: list[EdgeDraft] = Field(default_factory=list)
    tier: ExtractorTier = ExtractorTier.DETERMINISTIC
    idempotency_key: str | None = None


class DraftSubmissionRequest(WireRequestModel):
    """HTTP body wrapper for ``POST /api/v1/extract/drafts``.

    Carries the batch plus per-request knobs (strategy, override of
    ``requested_by``).  Most fields live on the batch itself; this
    wrapper only adds what's request-scoped.
    """

    batch: ExtractionBatch
    strategy: BatchStrategy = BatchStrategy.CONTINUE_ON_ERROR
    requested_by: str | None = None  # overrides batch default


class DraftSubmissionResult(WireModel):
    """Response for ``POST /api/v1/extract/drafts``.

    Mirrors the structure of
    :class:`trellis_wire.BatchCommandResponse` so callers can reuse
    existing batch-result handling, with the addition of batch-level
    metadata that identifies the submission end-to-end.
    """

    status: str = "ok"
    batch_id: str
    extractor: str  # f"{extractor_name}@{extractor_version}"
    strategy: str
    idempotency_key: str | None = None
    entities_submitted: int = 0
    edges_submitted: int = 0
    executed: int = 0
    succeeded: int = 0
    failed: int = 0
    rejected: int = 0
    duplicates: int = 0
    results: list[CommandResponse] = Field(default_factory=list)


__all__ = [
    "DraftSubmissionRequest",
    "DraftSubmissionResult",
    "EdgeDraft",
    "EntityDraft",
    "ExtractionBatch",
    "ExtractorTier",
]
