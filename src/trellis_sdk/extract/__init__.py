"""Client-side extractor contract.

Build a client package that extracts entities and edges from your
own domain (Unity Catalog, dbt manifests, custom metadata systems),
then submit the drafts to Trellis via
:meth:`trellis_sdk.TrellisClient.submit_drafts` — no server-side
code change required.

Typical shape::

    # trellis_unity_catalog/reader.py
    from trellis_sdk.extract import (
        EntityDraft,
        EdgeDraft,
        ExtractionBatch,
        ExtractorTier,
    )

    class UnityCatalogExtractor:
        name = "trellis_unity_catalog.reader"
        version = "0.3.1"
        tier = ExtractorTier.DETERMINISTIC

        def extract(self, uc_metadata) -> ExtractionBatch:
            entities = [
                EntityDraft(
                    entity_type="unity_catalog.table",
                    name=t.full_name,
                    properties={"columns": t.columns, "owner": t.owner},
                )
                for t in uc_metadata.tables
            ]
            return ExtractionBatch(
                source="unity_catalog",
                extractor_name=self.name,
                extractor_version=self.version,
                entities=entities,
                edges=[...],
                idempotency_key=f"uc-sync-{uc_metadata.snapshot_id}",
            )

Submission::

    from trellis_sdk import TrellisClient
    client = TrellisClient(base_url="https://trellis.prod")
    result = client.submit_drafts(UnityCatalogExtractor().extract(fetch_uc()))

**DTOs live in :mod:`trellis_wire`.**  This module re-exports them
for ergonomics; client packages can depend on ``trellis_sdk`` and
get the wire DTOs transitively without a separate dependency on
``trellis_wire``.

**Namespaced types are the extension pattern.**  Use
``entity_type="your_domain.resource"`` (not ``"table"``) so domains
don't collide.  Core accepts any string; keep well-known values
for agent-centric core use.
"""

from typing import Any, Protocol, runtime_checkable

from trellis_wire import (
    DraftSubmissionResult,
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
)


@runtime_checkable
class DraftExtractor(Protocol):
    """Protocol a client extractor class is expected to satisfy.

    Implementations are pure — they read from their source, emit
    drafts, and return.  No store access, no HTTP calls.  Submission
    happens separately via :meth:`TrellisClient.submit_drafts` so
    extraction and submission can be tested, cached, or retried
    independently.

    ``name`` should be the fully-qualified package+module path of
    the extractor (e.g. ``"trellis_unity_catalog.reader"``) so the
    audit trail can attribute drafts back to a specific package.

    ``version`` is freeform but semver is recommended.  The server
    records ``f"{name}@{version}"`` on every mutation event.
    """

    name: str
    version: str
    tier: ExtractorTier

    def extract(self, raw: Any) -> ExtractionBatch: ...


__all__ = [
    "DraftExtractor",
    "DraftSubmissionResult",
    "EdgeDraft",
    "EntityDraft",
    "ExtractionBatch",
    "ExtractorTier",
]
