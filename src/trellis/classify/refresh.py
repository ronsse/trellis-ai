"""Classification refresh — reclassify items with stale tags.

Closes Gap 1.1 (tag drift): ingestion-time tags accumulate staleness as
the graph grows, new keyword vocab is added, or neighborhood signals
shift. Nothing refreshes them. This module provides programmatic and
batch entry points to re-run the :class:`ClassifierPipeline` over
already-ingested items, stamping :attr:`ContentTags.classified_at` so
retrieval can reason about freshness.

Design notes:

* **Deterministic-first sequencing is preserved.** The caller chooses
  which pipeline to run. Calling with an ingestion-mode pipeline keeps
  the refresh deterministic; enrichment-mode adds LLM fallback.
* **Never deletes tags.** If a refresh produces an empty classification
  (no classifier matched), the previous tags are retained — we only
  write if we have fresh signal. Prevents a transient pipeline regression
  from erasing good prior classifications.
* **Audit via :class:`EventType.TAGS_REFRESHED`.** Each refresh emits an
  event carrying the before/after diff so operators can trace why a
  classification changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.importance import compute_importance
from trellis.classify.pipeline import ClassifierPipeline
from trellis.classify.protocol import ClassificationContext
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Documents fetched per ``list_documents`` round-trip in
#: :func:`reclassify_stale`. Mirrors ``trellis admin reindex-vectors`` —
#: the other operator-driven backfill over the same store.
DEFAULT_PAGE_SIZE = 100


@dataclass
class RefreshOutcome:
    """Result of a single-item reclassification."""

    item_id: str
    refreshed: bool
    reason: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


@dataclass
class BatchRefreshResult:
    """Result of a batch reclassification pass."""

    scanned: int = 0
    refreshed: int = 0
    skipped_missing_content: int = 0
    skipped_fresh: int = 0
    skipped_no_signal: int = 0
    item_ids_refreshed: list[str] = field(default_factory=list)


def reclassify_item(
    item_id: str,
    *,
    pipeline: ClassifierPipeline,
    document_store: DocumentStore,
    event_log: EventLog | None = None,
    context_builder: Any | None = None,
    include_domain: bool = False,
    dry_run: bool = False,
) -> RefreshOutcome:
    """Re-run the classifier pipeline against a single item and persist
    updated tags.

    Args:
        item_id: ID of the document to reclassify.
        pipeline: Pipeline to run. Callers choose deterministic-only or
            enrichment-mode by how they constructed it.
        document_store: Where the document lives. Tags are written back
            to ``metadata.content_tags``.
        event_log: Optional — when provided, a :class:`EventType.TAGS_REFRESHED`
            event is emitted with the before/after diff. Failure to emit
            is fail-soft (logged, never raised).
        context_builder: Optional callable ``(doc) -> ClassificationContext``.
            Defaults to a builder that populates ``existing_tags`` /
            ``title`` / ``source_system`` from doc metadata. Pass a custom
            builder to inject graph-neighborhood context or other signals
            the base metadata doesn't carry.
        include_domain: When ``False`` (the default, and the safe choice for a
            deterministic backfill), the freshly-derived ``domain`` facet is
            discarded and the item's prior ``domain`` is carried forward
            unchanged — mirroring classify-on-write
            (:mod:`trellis.classify.ingest`). ``domain`` is the only facet that
            *hard-excludes* a document from a domain-scoped query on mismatch,
            so letting the deterministic keyword / source-system classifiers
            (re)assign it during a refresh could silently hide a document.
            Set ``True`` only for a deliberate enrichment-mode refresh whose
            LLM classifier is trusted to (re)compute ``domain``.
        dry_run: When ``True`` the fresh tags are computed and returned in
            ``after`` but nothing is persisted and no event is emitted.
            ``refreshed`` still reports whether a real run *would* have
            written, so a dry-run and a live run agree on the counts.

    Returns:
        :class:`RefreshOutcome` with the before/after tag diffs and a
        ``refreshed`` flag.
    """
    doc = document_store.get(item_id)
    if doc is None:
        logger.debug("reclassify_item_not_found", item_id=item_id)
        return RefreshOutcome(
            item_id=item_id,
            refreshed=False,
            reason="document not found",
        )

    content = doc.get("content", "")
    metadata: dict[str, Any] = dict(doc.get("metadata") or {})
    before_tags = dict(metadata.get("content_tags") or {})

    builder = context_builder or _default_context_builder
    context = builder(doc)

    merged = pipeline.classify(content, context=context)
    if not merged.tags:
        return RefreshOutcome(
            item_id=item_id,
            refreshed=False,
            reason="pipeline produced no tags — keeping prior",
            before=before_tags,
            after=before_tags,
        )

    # Hook B (adr-importance-score-freshness §3.3): re-derive importance
    # against the freshly-merged tags so the stored score ages on the
    # same cadence as the tags it depends on. The base_importance preserves
    # the LLM contribution (frozen prior) while re-applying tag-derived
    # boosts on top.
    fresh_tags_obj = merged.to_content_tags()

    # Domain-facet safety (mirrors classify-on-write, trellis.classify.ingest):
    # `domain` is the only facet that HARD-EXCLUDES a document from a
    # domain-scoped query on mismatch. The deterministic keyword / source-system
    # classifiers will confidently (re)assign a code-flavoured domain to
    # personal content, so a refresh must never let that heuristic *introduce*
    # or overwrite a domain and silently hide the document. Carry the prior
    # domain forward unchanged — preserving any operator- or enrichment-set
    # value, and leaving a never-domained document domain-less — unless the
    # caller explicitly opts in. Applied before the tags-unchanged early-out so
    # a spurious fresh domain never counts as a change. `compute_importance`
    # ignores `domain`, so the ordering relative to the score is immaterial.
    if not include_domain:
        fresh_tags_obj = fresh_tags_obj.model_copy(
            update={"domain": list(before_tags.get("domain") or [])}
        )

    prior_importance = float(metadata.get("auto_importance", 0.0))
    new_importance = compute_importance(
        fresh_tags_obj,
        base_importance=prior_importance,
    )
    fresh_tags_obj = fresh_tags_obj.model_copy(
        update={"importance_scored_at": datetime.now(UTC)}
    )
    fresh_tags = fresh_tags_obj.model_dump(mode="json")

    # Tags-unchanged early-out: skip when neither the tag set nor the
    # importance score would change. We compare against ``before_tags``
    # ignoring the freshness stamp itself (the stamp varies on every call;
    # using it as a tiebreaker would defeat the early-out). Importance is
    # checked against the existing metadata value.
    before_tags_no_stamp = {
        k: v for k, v in before_tags.items() if k != "importance_scored_at"
    }
    fresh_tags_no_stamp = {
        k: v for k, v in fresh_tags.items() if k != "importance_scored_at"
    }
    if (
        fresh_tags_no_stamp == before_tags_no_stamp
        and new_importance == prior_importance
    ):
        return RefreshOutcome(
            item_id=item_id,
            refreshed=False,
            reason="tags unchanged",
            before=before_tags,
            after=before_tags,
        )

    if dry_run:
        return RefreshOutcome(
            item_id=item_id,
            refreshed=True,
            reason="tags would be updated (dry-run)",
            before=before_tags,
            after=fresh_tags,
        )

    metadata["content_tags"] = fresh_tags
    metadata["auto_importance"] = new_importance
    document_store.put(item_id, content, metadata)
    logger.info(
        "tags_refreshed",
        item_id=item_id,
        classifier_count=len(merged.classified_by),
    )

    if event_log is not None:
        _emit_tags_refreshed(event_log, item_id, before_tags, fresh_tags)

    return RefreshOutcome(
        item_id=item_id,
        refreshed=True,
        reason="tags updated",
        before=before_tags,
        after=fresh_tags,
    )


def reclassify_stale(
    *,
    pipeline: ClassifierPipeline,
    document_store: DocumentStore,
    event_log: EventLog | None = None,
    max_age_days: int = 30,
    limit: int = 100,
    page_size: int = DEFAULT_PAGE_SIZE,
    context_builder: Any | None = None,
    include_domain: bool = False,
    dry_run: bool = False,
) -> BatchRefreshResult:
    """Scan the document store for items with stale or missing
    ``classified_at`` and reclassify them.

    An item is considered stale when:

    * ``content_tags.classified_at`` is missing (legacy or hand-edited), or
    * ``classified_at`` is older than ``max_age_days``.

    Items that have no ``content_tags`` at all are also refreshed — they
    likely bypassed the ingestion pipeline and have never been tagged.

    The scan pages through ``list_documents`` in ``page_size`` chunks so a
    store larger than one page is covered in a single call without holding
    every document in memory. Paging by offset is stable across the writes
    this function performs: both shipped backends order by ``created_at``,
    which ``DocumentStore.put`` never rewrites on an existing row.

    Args:
        pipeline: Pipeline to run.
        document_store: Where to scan + write.
        event_log: Optional audit sink.
        max_age_days: Freshness threshold. Items tagged more recently
            than this are skipped.
        limit: Max number of documents to scan. ``0`` (or negative) means
            "every document in the store", paging until exhausted.
        page_size: Documents fetched per ``list_documents`` round-trip.
        context_builder: Optional ``(doc) -> ClassificationContext``.
        include_domain: Forwarded to :func:`reclassify_item`. Defaults to
            ``False`` so a batch backfill never lets the deterministic pipeline
            introduce a hard-excluding ``domain`` — see that function's docs.
        dry_run: Forwarded to :func:`reclassify_item`. Nothing is persisted
            and no event is emitted; the counts report what a live run would
            have done.

    Returns:
        :class:`BatchRefreshResult` with counts and the list of refreshed
        item IDs.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    result = BatchRefreshResult()
    offset = 0

    while True:
        fetch = page_size if limit <= 0 else min(page_size, limit - result.scanned)
        if fetch <= 0:
            break
        page = document_store.list_documents(limit=fetch, offset=offset)
        if not page:
            break
        offset += len(page)
        result.scanned += len(page)

        for doc in page:
            item_id = doc.get("doc_id")
            if not item_id:
                continue
            if not doc.get("content"):
                result.skipped_missing_content += 1
                continue

            tags = (doc.get("metadata") or {}).get("content_tags") or {}
            if not _is_stale(tags, cutoff):
                result.skipped_fresh += 1
                continue

            outcome = reclassify_item(
                item_id,
                pipeline=pipeline,
                document_store=document_store,
                event_log=event_log,
                context_builder=context_builder,
                include_domain=include_domain,
                dry_run=dry_run,
            )
            if outcome.refreshed:
                result.refreshed += 1
                result.item_ids_refreshed.append(item_id)
            elif outcome.reason.startswith("pipeline produced no tags"):
                result.skipped_no_signal += 1

    logger.info(
        "reclassify_stale_completed",
        scanned=result.scanned,
        refreshed=result.refreshed,
        skipped_fresh=result.skipped_fresh,
        skipped_no_signal=result.skipped_no_signal,
        dry_run=dry_run,
    )
    return result


def _is_stale(tags: dict[str, Any], cutoff: datetime) -> bool:
    """``True`` when an item's tags are older than ``cutoff`` — or unproven.

    Option A: a missing or unparseable ``classified_at`` is treated as
    *always stale*. Legacy or hand-edited rows that never carried a stamp
    must be reclassified — there's no other freshness signal, and silently
    skipping them would let drift accumulate forever.
    """
    classified_at = _parse_classified_at(tags.get("classified_at"))
    return classified_at is None or classified_at < cutoff


def _default_context_builder(doc: dict[str, Any]) -> ClassificationContext:
    """Build a ClassificationContext from a document's metadata.

    Extracts the signals already sitting in ``metadata``: source system,
    title, existing tag set (so the :class:`GraphNeighborClassifier` can
    reason against current state), and the whole metadata dict as a
    free-form carrier. Callers who want neighbor-graph signals should
    pass a custom builder that fetches from the graph store.
    """
    metadata: dict[str, Any] = doc.get("metadata") or {}
    existing_tags_raw = metadata.get("content_tags")

    existing_tags = None
    if isinstance(existing_tags_raw, dict):
        # Import lazily to avoid a hard schema dependency in this layer.
        from pydantic import ValidationError  # noqa: PLC0415

        from trellis.schemas.classification import ContentTags  # noqa: PLC0415

        try:
            existing_tags = ContentTags.model_validate(existing_tags_raw)
        # GRACEFUL-DEGRADATION: refresh tolerates malformed pre-1.1
        # tags by re-classifying from scratch; warn so corrupt rows remain
        # observable to operators.
        # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
        except ValidationError:
            logger.warning(
                "existing_tags_malformed",
                item_id=doc.get("doc_id"),
                exc_info=True,
            )

    return ClassificationContext(
        title=str(metadata.get("title") or ""),
        source_system=str(metadata.get("source_system") or ""),
        file_path=str(metadata.get("file_path") or ""),
        entity_type=str(metadata.get("entity_type") or ""),
        node_id=str(doc.get("doc_id") or ""),
        existing_tags=existing_tags,
        existing_metadata=metadata,
    )


def _parse_classified_at(raw: Any) -> datetime | None:
    """Parse a stored classified_at value (ISO-8601 string) to datetime.

    Callers in :func:`reclassify_stale` treat ``None`` as "missing =>
    always stale", so the empty-return is a first-class signal (not a
    silent swallow). The fall-through cases below intentionally return
    ``None`` to drive that behavior. Corrupt non-empty strings still
    log a warning so operators see when stored stamps are malformed.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    # GRACEFUL-DEGRADATION: caller treats None as "always stale" so
    # the empty return is the intended signal; log on malformed input so
    # corrupt stamps remain observable.
    # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
    except (TypeError, ValueError):
        logger.warning(
            "classified_at_parse_failed",
            raw=raw,
            exc_info=True,
        )
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _emit_tags_refreshed(
    event_log: EventLog,
    item_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Fail-soft TAGS_REFRESHED emission with before/after diff."""
    try:
        event_log.emit(
            EventType.TAGS_REFRESHED,
            source="classify.refresh",
            entity_id=item_id,
            entity_type="document",
            payload={
                "item_id": item_id,
                "before": before,
                "after": after,
                "classified_by": after.get("classified_by", []),
            },
        )
    # GRACEFUL-DEGRADATION: tags already persisted to document_store;
    # this event emit is post-success telemetry and must not roll back the write.
    # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
    except Exception:
        logger.exception(
            "tags_refreshed_emit_failed",
            item_id=item_id,
        )
