"""Enrichment worker — auto-tags, classification, importance scoring."""

from trellis_workers.enrichment.service import (
    EnrichmentResult,
    EnrichmentService,
    normalize_tag,
)

__all__ = [
    "EnrichmentResult",
    "EnrichmentService",
    "normalize_tag",
]
