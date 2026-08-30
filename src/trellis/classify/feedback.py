"""Feedback loop: apply noise tags from effectiveness analysis."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.vector_metadata import sync_vector_metadata
from trellis.stores.base.document import DocumentStore

if TYPE_CHECKING:
    from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger(__name__)


def apply_noise_tags(
    noise_candidates: list[str],
    document_store: DocumentStore,
    vector_store: VectorStore | None = None,
) -> int:
    """Update signal_quality to ``"noise"`` for items flagged by effectiveness analysis.

    Also stamps ``classified_at`` so the refreshed tag set is visible to
    staleness-based retrieval logic (Gap 1.1) and ``importance_scored_at``
    because flipping ``signal_quality`` to ``"noise"`` shifts the
    ``compute_importance`` boost — the score effectively re-aged
    (adr-importance-score-freshness §3.3 close).

    **Pass ``vector_store``.** A vector row's metadata is a snapshot taken
    at embed time, so a demotion written only to the document store leaves
    the semantic axis serving the item with its pre-demotion tags — which is
    exactly what #338 measured in production: 45 noise-tagged documents, not
    one whose vector row agreed. When a store is supplied the new tags are
    mirrored onto the row by
    :func:`~trellis.core.vector_metadata.sync_vector_metadata`, a
    metadata-only re-upsert that re-embeds nothing.

    The parameter is optional only because a deployment may have no vector
    store configured at all; omitting it on one that does re-opens the
    divergence. Every in-tree caller supplies it, and the outcome is
    reported on the ``noise_tags_applied`` log line so a run that mirrored
    nothing says so.

    Returns the number of items updated **in the document store** — the
    authoritative count, unchanged by this parameter, since a document whose
    vector row is missing (never embedded) is still legitimately demoted.
    """
    if not noise_candidates:
        return 0

    updated = 0
    vector_rows_synced = 0
    stamp = datetime.now(UTC).isoformat()
    for item_id in noise_candidates:
        doc = document_store.get(item_id)
        if doc is None:
            logger.debug("noise_candidate_not_found", item_id=item_id)
            continue

        metadata: dict[str, Any] = doc.get("metadata", {})
        content_tags = metadata.setdefault("content_tags", {})
        content_tags["signal_quality"] = "noise"
        content_tags["classified_at"] = stamp
        content_tags["importance_scored_at"] = stamp

        # Metadata-only: ``signal_quality`` and the two stamps are derived
        # from the effectiveness analysis, not from the document, and the
        # content written back is the row's own. Its direct sibling
        # ``classify.refresh`` passes the flag for the same reason. Latent
        # while ``retrieve.noise.exclude_noise`` drops the item — but that
        # boundary is deliberately default-pass and invertible, and a later
        # refresh revising the facet returns the row to service still
        # carrying the falsified stamp (#406).
        document_store.put(
            item_id, doc["content"], metadata, preserve_updated_at=True
        )
        updated += 1
        # After the authoritative write, never before: the document row is
        # what a re-run repairs from, so it has to land first.
        if sync_vector_metadata(vector_store, item_id, metadata):
            vector_rows_synced += 1
        logger.info("noise_tag_applied", item_id=item_id)

    logger.info(
        "noise_tags_applied",
        updated=updated,
        vector_rows_synced=vector_rows_synced,
        vector_store_supplied=vector_store is not None,
    )
    return updated
