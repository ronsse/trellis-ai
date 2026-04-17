"""Feedback loop: apply noise tags from effectiveness analysis."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)


def apply_noise_tags(
    noise_candidates: list[str],
    document_store: DocumentStore,
) -> int:
    """Update signal_quality to ``"noise"`` for items flagged by effectiveness analysis.

    Returns the number of items updated.
    """
    if not noise_candidates:
        return 0

    updated = 0
    for item_id in noise_candidates:
        doc = document_store.get(item_id)
        if doc is None:
            logger.debug("noise_candidate_not_found", item_id=item_id)
            continue

        metadata: dict[str, Any] = doc.get("metadata", {})
        content_tags = metadata.setdefault("content_tags", {})
        content_tags["signal_quality"] = "noise"

        document_store.put(item_id, doc["content"], metadata)
        updated += 1
        logger.info("noise_tag_applied", item_id=item_id)

    return updated
