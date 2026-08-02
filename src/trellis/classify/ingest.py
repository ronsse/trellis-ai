"""Classify-on-write — inline deterministic tagging at ingest time.

The batch counterpart, :mod:`trellis.classify.refresh`, re-tags already-stored
documents; this module tags a document *as it is written* — by ``sync_records``
(corpus, conversation-export, and session-capture ingest all funnel through
there) and by the MCP / REST document write paths via
:func:`classify_metadata_on_write` — so retrieval-shaping tags exist from the
first write rather than waiting for a refresh pass that, in practice, is never
scheduled. Deterministic and inline (no LLM), fail-soft, and flag-gated —
matching the sibling ingest-time behaviours (embed / reconcile /
memory-extraction).

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
import threading
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

#: The metadata keys :func:`classify_for_ingest` writes. Callers that have to
#: move a classified document's tags somewhere else — corpus sync propagates
#: them from a parent document down to its chunks, which are the retrievable
#: unit — key off this instead of restating the return shape, so adding a facet
#: here cannot silently stop propagating. Pinned by
#: ``test_classify_metadata_keys_matches_return_shape``.
CLASSIFY_METADATA_KEYS = ("content_tags", "auto_importance")


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

    Returns ``{"content_tags": {...}, "auto_importance": <float>}`` (the keys
    in :data:`CLASSIFY_METADATA_KEYS`) on success, or
    ``{}`` when classification produced nothing usable or raised — this is
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


#: Process-wide pipeline for the single-document write paths. ``sync_records``
#: builds its own per-run instance (it already has a natural once-per-run
#: seam); the MCP server and the REST app write one document per call, so they
#: share this lazily built singleton instead of rebuilding the classifiers on
#: every write. The pipeline is pure and stateless, so sharing it is safe; the
#: lock only guards the one-time construction against concurrent http workers.
_classifier_lock = threading.Lock()
_ingest_classifier: ClassifierPipeline | None = None


def get_ingest_classifier() -> ClassifierPipeline:
    """Return the process-wide deterministic classify-on-write pipeline.

    A single instance per process is only sound because
    :func:`build_ingest_classifier` is config-free, which in turn is only
    sound because the ``domain`` facet is dropped at persist time
    (``include_domain=False``). If ``domain`` ever becomes persistable, both
    assumptions have to be revisited together.
    """
    global _ingest_classifier  # noqa: PLW0603
    # Double-checked: the lock exists solely to serialize the one-time build,
    # so once it is built every classified write reads the global lock-free
    # (attribute reads are atomic under the GIL).
    if _ingest_classifier is not None:
        return _ingest_classifier
    with _classifier_lock:
        if _ingest_classifier is None:
            _ingest_classifier = build_ingest_classifier()
        return _ingest_classifier


def classify_metadata_on_write(
    metadata: dict[str, Any],
    content: str,
    *,
    source_system: str = "",
    doc_id: str = "",
) -> dict[str, Any]:
    """Return ``metadata`` with deterministic tags merged in, if warranted.

    The single-document counterpart to the ``sync_records`` inline block: the
    same flag, the same ``classify_for_ingest`` call, so a document written
    through MCP ``save_memory`` / ``save_knowledge`` or the REST document /
    evidence routes carries the same tag shape as one written through corpus
    ingest, with the same four safety properties.

    * **Flag-gated** on ``TRELLIS_ENABLE_CLASSIFY_ON_INGEST`` — off returns the
      caller's mapping untouched, so behaviour is byte-identical to before.
    * **Fill-if-absent** — existing ``content_tags`` (a prior write, or the LLM
      enrichment pass) are never clobbered.
    * **Fail-soft** — *any* failure logs and returns the caller's mapping; a
      classification error must never block or fail a durable write. This is
      total: a ``metadata`` that is not a mapping at all degrades to a warning,
      it does not raise into the write path.
    * **No auto-``domain``** — inherited from :func:`classify_for_ingest`'s
      ``include_domain=False`` default (see the module docstring).

    Empty / whitespace-only content is a no-op too (uri-only evidence), the
    same skip the embed-on-ingest hook applies.

    Two deliberate deviations from ``sync_records``, which is why the two are
    not (yet) one function: this skips empty content, and it inspects only the
    caller's mapping — ``sync_records`` classifies against metadata already
    merged with the stored document's, and needs the tag dict separately to
    propagate onto the chunk rows.

    Args:
        metadata: The document metadata about to be persisted. Never mutated;
            a *new* dict is returned when tags are added, otherwise the same
            object comes back.
        content: The document content being written.
        source_system: Classification context. Defaults to
            ``metadata['source_system']`` when not given.
        doc_id: Document id, for logging / classification context. May be
            empty for store-assigned ids (the document is not written yet).

    Returns:
        The metadata to persist — tagged, or the caller's own mapping.
    """
    if not classify_on_ingest_enabled():
        return metadata

    try:
        if "content_tags" in metadata:
            return metadata
        if not content or not content.strip():
            # Nothing to classify (e.g. uri-only evidence) — tagging an empty
            # string would record structural verdicts about no content at all.
            return metadata
        classify_meta = classify_for_ingest(
            get_ingest_classifier(),
            content,
            source_system=source_system or str(metadata.get("source_system") or ""),
            title=str(metadata.get("title") or ""),
            doc_id=doc_id,
            prior_importance=float(metadata.get("auto_importance", 0.0) or 0.0),
        )
    except Exception:
        # GRACEFUL-DEGRADATION: classify_for_ingest already swallows classifier
        # errors; the guards live inside this try so the *whole* seam is total
        # — pipeline construction, a non-numeric auto_importance, and a
        # ``metadata`` that is not a mapping (an API caller can send
        # ``"metadata": null``) all degrade to an untagged write, never a 500.
        logger.warning("classify_on_write_failed", doc_id=doc_id, exc_info=True)
        return metadata

    return {**metadata, **classify_meta} if classify_meta else metadata


__all__ = [
    "CLASSIFY_METADATA_KEYS",
    "CLASSIFY_ON_INGEST_FLAG",
    "build_ingest_classifier",
    "classify_for_ingest",
    "classify_metadata_on_write",
    "classify_on_ingest_enabled",
    "get_ingest_classifier",
]
