"""Classify-on-write — inline deterministic tagging at ingest time.

The batch counterpart, :mod:`trellis.classify.refresh`, re-tags already-stored
documents; this module tags a document *as it is written* by ``sync_records``
(corpus, conversation-export, and session-capture ingest all funnel through
there), so retrieval-shaping tags exist from the first write rather than
waiting for a refresh pass that, in practice, is never scheduled. Deterministic
and inline (no LLM), fail-soft, and flag-gated — matching the sibling
ingest-time behaviours (embed / reconcile / memory-extraction).

Two deliberate scope limits:

* **Deterministic only.** Uses :func:`build_ingestion_pipeline` (structural +
  keyword-domain + source-system classifiers). The LLM enrichment path stays a
  separate, opt-in worker concern.
* **No auto-``domain``.** The classifier-derived ``domain`` facet is dropped
  before persisting. ``domain`` is the only facet that *hard-excludes* a
  document from a domain-scoped query on mismatch, and the deterministic
  keyword / source-system classifiers will confidently assign a code-flavoured
  domain to personal content (a career note, a health log). Operator-set domain
  (the scalar ``metadata['domain']`` / ``--domain``) is a separate key and is
  untouched. The facets we *do* persist (``signal_quality``, ``content_type``,
  ``scope``, ``retrieval_affinity``) only shape ranking / noise-exclusion /
  sectioning — they never exclude a document on mismatch, so a wrong value
  degrades ranking at worst, it never hides content.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.factory import build_ingestion_pipeline
from trellis.classify.importance import compute_importance
from trellis.classify.protocol import ClassificationContext

if TYPE_CHECKING:
    from trellis.classify.pipeline import ClassifierPipeline

logger = structlog.get_logger(__name__)

#: Truthy spellings that turn classify-on-ingest on (mirrors the embed hook).
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Feature flag — off by default, consistent with the sibling ingest-time
#: behaviours. Enable per-deployment; the tags it writes are always safe
#: (see module docstring) so flipping the default later is low-risk.
CLASSIFY_ON_INGEST_FLAG = "TRELLIS_ENABLE_CLASSIFY_ON_INGEST"


def classify_on_ingest_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_CLASSIFY_ON_INGEST`` is set truthy."""
    return os.environ.get(CLASSIFY_ON_INGEST_FLAG, "").strip().lower() in _TRUTHY


def build_ingest_classifier() -> ClassifierPipeline:
    """Build the deterministic ingestion pipeline for classify-on-write.

    Uses built-in defaults (no ``config.yaml`` domain-keyword seeding): the
    classifier-derived ``domain`` facet is dropped at persist time, so custom
    operator domain vocabulary would have no effect on the written tags anyway.
    """
    return build_ingestion_pipeline()


def classify_for_ingest(
    pipeline: ClassifierPipeline,
    content: str,
    *,
    source_system: str = "",
    title: str = "",
    doc_id: str = "",
    prior_importance: float = 0.0,
    include_domain: bool = False,
) -> dict[str, Any]:
    """Classify ``content`` and return metadata keys to merge into a document.

    Returns ``{"content_tags": {...}, "auto_importance": <float>}`` on success,
    or ``{}`` when classification produced nothing usable or raised — this is
    always fail-soft, because the document is (about to be) durably stored and
    inline tagging is a best-effort enhancement, never a gate on the write.

    Tag shape is identical to the refresh path
    (:func:`trellis.classify.refresh.reclassify_item`): ``content_tags`` is a
    JSON-mode :class:`~trellis.schemas.classification.ContentTags` dump carrying
    ``classified_at`` / ``importance_scored_at`` freshness stamps, so a
    document tagged at ingest is not re-touched by ``reclassify_stale`` until it
    genuinely ages out.
    """
    try:
        context = ClassificationContext(
            title=title,
            source_system=source_system,
            node_id=doc_id,
        )
        merged = pipeline.classify(content, context=context)
        tags_obj = merged.to_content_tags()
        if not include_domain:
            # Drop the only hard-excluding facet — see module docstring.
            tags_obj = tags_obj.model_copy(update={"domain": []})
        importance = compute_importance(tags_obj, base_importance=prior_importance)
        tags_obj = tags_obj.model_copy(
            update={"importance_scored_at": datetime.now(UTC)}
        )
        return {
            "content_tags": tags_obj.model_dump(mode="json"),
            "auto_importance": importance,
        }
    except Exception:
        # GRACEFUL-DEGRADATION: never let a classification error block ingest.
        logger.warning("classify_on_ingest_failed", doc_id=doc_id, exc_info=True)
        return {}


__all__ = [
    "CLASSIFY_ON_INGEST_FLAG",
    "build_ingest_classifier",
    "classify_for_ingest",
    "classify_on_ingest_enabled",
]
