"""Shadow-mode tagging — record LLM verdicts without letting them drive retrieval.

The ``DETERMINISTIC > LOCAL > FRONTIER`` north star (``docs/PRD.md`` §6) says
judgment-avoidance should *dissolve* as evidence accumulates: the LLM
bootstraps a vocabulary, the deterministic layer inherits it, and the LLM
switches off for whatever has been learned. For the tagging layer that loop
cannot even start, because the evidence does not exist — measured 2026-08-22,
every classified document in production carries ``classified_mode:
"ingestion"``. Zero LLM classifications have ever been persisted.

This module is the precondition (#321 Phase 1): run the LLM over stored
documents and persist what it says under :data:`SHADOW_TAGS_KEY`, a key no
retrieval path reads.

Why a separate key rather than the live ``content_tags``:

* **Retrieval does not move.** No pack ranking changes while the corpus
  accrues, so the pass is safe to run over a production store. That takes two
  separate guarantees, not one: the shadow key is never *read* (unaddressable
  by tag filters, stripped at the serving boundary by
  :mod:`trellis.retrieve.servable`), and the shadow *write* does not perturb
  the row — it passes ``preserve_updated_at=True``, because ``updated_at``
  drives recency decay and a whole-corpus pass would otherwise re-rank
  everything to "brand new".
* **It sidesteps a vocabulary collision that would otherwise destroy data.**
  The deterministic path emits :data:`~trellis.schemas.classification.ContentType`
  values (``error-resolution`` / ``procedure`` / ``code``); the enrichment path
  emits :data:`~trellis_workers.enrichment.service.DEFAULT_CLASSIFICATIONS`
  (``reference`` / ``research`` / ``notes`` / …). The two vocabularies overlap
  in exactly one value. Writing LLM output into ``content_tags`` would
  *replace* one taxonomy with another rather than refine it — and would in
  fact raise ``ValidationError``, because ``ContentTags.content_type`` is a
  closed ``Literal`` that rejects nine of the ten enrichment values. The
  disagreement is not noise to be coerced away; it is the measurement Phase 2
  mines. So it is recorded verbatim, as
  :class:`~trellis.schemas.classification.ShadowTags`.

**Where the tags live, and why not in the event.** Each judged classification
also emits a leak-safe
:attr:`~trellis.stores.base.event_log.EventType.MEMORY_OP_JUDGED` event (#264's
substrate — this module is its classify-layer instance, not a parallel
channel). The event carries a digest, a short ``content_type`` verdict label,
confidence, and a subject pointer — never the open-vocabulary ``domain`` tags,
because a value like ``yellowstone-national-park`` reveals the subject matter
of a document the event log may not be scoped to reveal. Content-revealing
tags stay on the document, behind the same access path as the content they
describe; the event log gets the label-only training pair.

**Not an ingest-time hook, deliberately.** Classify-on-write
(:mod:`trellis.classify.ingest`) is deterministic and inline for a reason —
microseconds, no network, no cost. A shadow classification is ~1.6 s against a
local model. Putting that in the write path would trade the write path's
latency budget for a measurement, so shadowing is a batch pass only. Documents
written after the last pass are covered by re-running it: an item with no
shadow record is exactly what :func:`shadow_classify_stale` scans for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.ingest import CLASSIFY_METADATA_KEYS
from trellis.classify.protocol import ClassificationContext
from trellis.classify.refresh import (
    DEFAULT_PAGE_SIZE,
    default_context_builder,
    parse_classified_at,
)
from trellis.core.hashing import content_hash
from trellis.schemas.classification import (
    CONTENT_TYPE_VALUES,
    LIST_FACETS,
    SHADOW_TAGS_KEY,
    ShadowTags,
    facet_values,
    shadow_verdict,
)
from trellis.schemas.memory_op import (
    REF_TYPE_DOCUMENT,
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.classify.protocol import Classifier
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

# ``SHADOW_TAGS_KEY`` is re-exported from :mod:`trellis.schemas.classification`,
# which owns the name beside the model it names. Deliberately *not* a facet
# inside ``content_tags``: every tag filter the document stores build is rooted
# at the JSON path ``$.content_tags.<facet>`` (see
# ``trellis.stores.sqlite.document._build_tag_conditions`` and its Postgres
# twin), so a sibling top-level key is unreachable from tag filtering by
# construction rather than by convention. That keeps shadow tags out of every
# *filter*; keeping them out of every *served payload* is a separate guarantee,
# enforced at the serving boundary by :mod:`trellis.retrieve.servable`.
# Both are pinned by ``tests/unit/classify/test_shadow.py``.

#: Metadata keys the shadow pass must never write — the keys retrieval reads.
#: Aliases :data:`~trellis.classify.ingest.CLASSIFY_METADATA_KEYS` rather than
#: restating it: that constant is pinned by
#: ``test_classify_metadata_keys_matches_return_shape`` to the classify path's
#: actual return shape, so a facet added there cannot silently fall out of this
#: guarantee. Its own docstring asks callers to key off it for exactly this.
PROTECTED_LIVE_KEYS = CLASSIFY_METADATA_KEYS

#: ``decision`` value recorded on the ``MEMORY_OP_JUDGED`` event when the
#: classifier produced no classification label at all — see
#: :attr:`~trellis.schemas.classification.ShadowTags.verdict`, which resolves
#: that label across both tag vocabularies. A verdict of "produced nothing" is
#: itself a training signal — coverage is the headline number the shadow pass
#: exists to measure — so it is logged as a label rather than skipped. It must
#: mean *the model said nothing*, never *this reader looked in one place*.
DECISION_UNCLASSIFIED = "unclassified"

#: :attr:`ShadowOutcome.reason` values. Constants, not free text, so the batch
#: pass buckets outcomes by identity instead of sniffing prose — the same
#: discipline :mod:`trellis.classify.refresh` uses.
REASON_NOT_FOUND = "document not found"
REASON_NO_SIGNAL = "classifier produced no tags"
REASON_DRY_RUN = "shadow record would be written (dry-run)"
REASON_WRITTEN = "shadow record written"


@dataclass
class ShadowOutcome:
    """Result of shadow-classifying a single item."""

    item_id: str
    written: bool
    reason: str
    #: The freshly-computed shadow record, JSON-mode. ``None`` when the
    #: classifier produced nothing or the document was missing.
    shadow: dict[str, Any] | None = None
    #: The item's *live* ``content_tags``, unchanged — carried so a caller can
    #: diff without a second read.
    live: dict[str, Any] | None = None


@dataclass
class BatchShadowResult:
    """Counts from a batch shadow pass.

    Every scanned document lands in exactly one bucket.
    """

    scanned: int = 0
    written: int = 0
    skipped_missing_content: int = 0
    skipped_fresh: int = 0
    skipped_no_signal: int = 0
    errors: int = 0
    item_ids_written: list[str] = field(default_factory=list)


def shadow_classify_item(
    item_id: str,
    *,
    classifier: Classifier,
    document_store: DocumentStore,
    event_log: EventLog | None = None,
    context_builder: Callable[[dict[str, Any]], ClassificationContext] | None = None,
    model_id: str = "",
    dry_run: bool = False,
) -> ShadowOutcome:
    """Classify one stored document and persist the verdict as a shadow record.

    The live ``content_tags`` are read (and returned for comparison) but never
    written — see :data:`PROTECTED_LIVE_KEYS`.

    Args:
        item_id: Document to classify.
        classifier: Any :class:`~trellis.classify.protocol.Classifier`. In
            production this is
            :class:`~trellis.classify.classifiers.llm.LLMFacetClassifier`; the
            protocol (rather than a concrete LLM type) is the parameter so the
            pass is testable without a model and so a *local* model, a frontier
            model, and a future distilled classifier are all the same call.
        document_store: Where the document lives and where the shadow record
            is written.
        event_log: Optional. When provided, one leak-safe
            ``MEMORY_OP_JUDGED`` event is emitted per judged document.
            Emission is fail-soft — the shadow record is already durable and
            telemetry must not roll it back.
        context_builder: Optional ``(doc) -> ClassificationContext``. Defaults
            to the same builder the live refresh path uses, so a classifier
            sees identical context in both modes and an agreement number is
            not confounded by a context difference.
        model_id: Label recorded on the shadow record and the event (e.g.
            ``"hermes3:8b"``). Defaults to the classifier's ``name``.
        dry_run: Compute and return the record without persisting or emitting.

    Returns:
        :class:`ShadowOutcome`.
    """
    doc = document_store.get(item_id)
    if doc is None:
        logger.debug("shadow_classify_item_not_found", item_id=item_id)
        return ShadowOutcome(item_id=item_id, written=False, reason=REASON_NOT_FOUND)

    content = doc.get("content", "")
    metadata: dict[str, Any] = dict(doc.get("metadata") or {})
    live_tags = metadata.get("content_tags")
    live = dict(live_tags) if isinstance(live_tags, dict) else {}

    builder = context_builder or default_context_builder
    result = classifier.classify(content, context=builder(doc))
    classifier_name = result.classifier_name or str(classifier.name)
    resolved_model_id = model_id or classifier_name

    shadow = _to_shadow_tags(
        result.tags,
        classifier_name=classifier_name,
        confidence=result.confidence,
        model_id=resolved_model_id,
    )

    # The no-signal test is on the *record*, not on the raw tag map. A raw map
    # can be non-empty and still carry no tags:
    # :class:`~trellis.classify.classifiers.llm.LLMFacetClassifier` adds
    # ``_auto_importance`` / ``_auto_summary`` independently of ``domain`` and
    # ``content_type``, so a model that returns a summary but classifies
    # nothing yields a truthy map that ``_to_shadow_tags`` reduces to nothing.
    # Testing the map would persist that empty record, count it as written, and
    # — because ``_needs_shadow`` only asks whether a record exists — mark the
    # document judged forever. The coverage number this pass exists to produce
    # would then overcount its own successes.
    if not shadow.has_tags:
        # Still a judged operation: "the model looked and produced nothing" is
        # the coverage signal, so the event fires even though nothing is
        # written.
        if event_log is not None and not dry_run:
            _emit_judged(
                event_log,
                item_id=item_id,
                content=content,
                decision=DECISION_UNCLASSIFIED,
                confidence=result.confidence,
                model_id=resolved_model_id,
            )
        return ShadowOutcome(
            item_id=item_id,
            written=False,
            reason=REASON_NO_SIGNAL,
            live=live,
        )

    shadow_json = shadow.model_dump(mode="json")

    if dry_run:
        return ShadowOutcome(
            item_id=item_id,
            written=True,
            reason=REASON_DRY_RUN,
            shadow=shadow_json,
            live=live,
        )

    _write_shadow(
        document_store,
        item_id=item_id,
        content=content,
        metadata=metadata,
        shadow_json=shadow_json,
    )
    logger.info(
        "shadow_tags_written",
        item_id=item_id,
        classified_by=shadow.classified_by,
        model_id=shadow.model_id,
    )

    if event_log is not None:
        _emit_judged(
            event_log,
            item_id=item_id,
            content=content,
            decision=shadow.verdict or DECISION_UNCLASSIFIED,
            confidence=result.confidence,
            model_id=shadow.model_id,
        )

    return ShadowOutcome(
        item_id=item_id,
        written=True,
        reason=REASON_WRITTEN,
        shadow=shadow_json,
        live=live,
    )


def shadow_classify_stale(
    *,
    classifier: Classifier,
    document_store: DocumentStore,
    event_log: EventLog | None = None,
    max_age_days: int | None = None,
    limit: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    context_builder: Callable[[dict[str, Any]], ClassificationContext] | None = None,
    model_id: str = "",
    dry_run: bool = False,
) -> BatchShadowResult:
    """Shadow-classify every document that lacks a fresh shadow record.

    Args:
        classifier: See :func:`shadow_classify_item`.
        document_store: Store to scan and write.
        event_log: Optional audit sink.
        max_age_days: Freshness window. ``None`` (the default) means *only*
            documents with no shadow record at all — the cost-conscious
            default, because unlike the deterministic refresh each item here
            costs a model call, and re-judging an unchanged document with an
            unchanged model buys nothing. An integer re-judges records older
            than that many days (``0`` re-judges everything).
        limit: Stop after scanning this many documents. ``0`` means every
            document, paging until exhausted.
        page_size: Documents per ``list_documents`` round-trip.
        context_builder: See :func:`shadow_classify_item`.
        model_id: See :func:`shadow_classify_item`.
        dry_run: Nothing persisted, nothing emitted; counts report what a live
            run would have done.

    Returns:
        :class:`BatchShadowResult`. A failure on one document is counted in
        ``errors`` and skipped — a whole-store pass must not abort halfway and
        leave the operator with committed writes and no counts.
    """
    cutoff = (
        None
        if max_age_days is None
        else datetime.now(UTC) - timedelta(days=max_age_days)
    )
    result = BatchShadowResult()
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

            try:
                if not _needs_shadow(
                    (doc.get("metadata") or {}).get(SHADOW_TAGS_KEY), cutoff
                ):
                    result.skipped_fresh += 1
                    continue
                outcome = shadow_classify_item(
                    item_id,
                    classifier=classifier,
                    document_store=document_store,
                    event_log=event_log,
                    context_builder=context_builder,
                    model_id=model_id,
                    dry_run=dry_run,
                )
            # GRACEFUL-DEGRADATION: one unreachable model call or one
            # malformed row must not abort a whole-store pass. Counted in
            # `errors` and surfaced in the CLI summary; logged with a
            # traceback so the offending row is identifiable.
            except Exception:
                result.errors += 1
                logger.exception("shadow_classify_item_failed", item_id=item_id)
                continue

            _tally(result, item_id, outcome)

    logger.info(
        "shadow_classify_completed",
        scanned=result.scanned,
        written=result.written,
        skipped_fresh=result.skipped_fresh,
        skipped_no_signal=result.skipped_no_signal,
        errors=result.errors,
        dry_run=dry_run,
    )
    return result


# ---------------------------------------------------------------------------
# Comparison — shadow vs. live, per document
# ---------------------------------------------------------------------------


#: Facets compared between the shadow record and the live tags. ``domain`` is
#: a list facet (compared as a set); the rest are scalars.
COMPARED_FACETS = ("domain", "content_type", "scope", "signal_quality")


@dataclass(frozen=True)
class FacetAgreement:
    """How shadow and live tags line up on one facet across the corpus."""

    facet: str
    #: Both sides produced a value and the values match (set equality for
    #: ``domain``).
    agreed: int = 0
    #: Both sides produced a value and they differ.
    disagreed: int = 0
    #: Shadow produced a value, live did not — the *coverage gain* from the
    #: LLM, and the headline number for this facet.
    live_missing: int = 0
    #: Live produced a value, shadow did not.
    shadow_missing: int = 0
    #: Neither side produced a value.
    both_missing: int = 0

    @property
    def comparable(self) -> int:
        """Documents where both sides produced a value."""
        return self.agreed + self.disagreed

    @property
    def agreement_rate(self) -> float | None:
        """``agreed / comparable``, or ``None`` when nothing is comparable.

        ``None`` rather than ``0.0`` or ``1.0``: a rate over an empty
        denominator is not a measurement, and reporting one as a number is how
        a metric ends up wired to a constant.
        """
        return self.agreed / self.comparable if self.comparable else None


@dataclass(frozen=True)
class ShadowComparison:
    """Per-document shadow-vs-live diff."""

    item_id: str
    live: dict[str, Any]
    shadow: dict[str, Any]
    #: Facet -> ``True`` agreed / ``False`` disagreed / ``None`` not comparable
    #: (at least one side had no value).
    agreements: dict[str, bool | None]


@dataclass
class ShadowAgreementReport:
    """Corpus-level shadow-vs-live comparison."""

    scanned: int = 0
    with_shadow: int = 0
    per_facet: dict[str, FacetAgreement] = field(default_factory=dict)
    #: Shadow ``content_type`` values that are not in the live
    #: :data:`~trellis.schemas.classification.CONTENT_TYPE_VALUES` vocabulary,
    #: with document counts. This is the vocabulary collision made countable:
    #: a non-empty map means promoting shadow ``content_type`` wholesale would
    #: mean *adopting a different taxonomy*, not refining the current one.
    out_of_vocabulary_content_types: dict[str, int] = field(default_factory=dict)
    comparisons: list[ShadowComparison] = field(default_factory=list)


def compare_shadow_to_live(
    *,
    document_store: DocumentStore,
    limit: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    collect_comparisons: bool = False,
) -> ShadowAgreementReport:
    """Compare every shadowed document's LLM tags against its live tags.

    Read-only. This is the query the Phase 1 acceptance criterion asks for —
    "a query can compare shadow vs live tags per document" — and the input a
    human uses to decide whether the shadow corpus is worth promoting from.

    Args:
        document_store: Store to scan.
        limit: Stop after this many documents (``0`` = all).
        page_size: Documents per round-trip.
        collect_comparisons: When ``True``, a per-document row is retained for
            every shadowed item. Off by default: over a whole store that is an
            O(corpus) retention (~42 MB at 50k documents) a caller asking for
            aggregate counts never wanted.

    Returns:
        :class:`ShadowAgreementReport`.
    """
    report = ShadowAgreementReport()
    tallies: dict[str, dict[str, int]] = {
        facet: {
            "agreed": 0,
            "disagreed": 0,
            "live_missing": 0,
            "shadow_missing": 0,
            "both_missing": 0,
        }
        for facet in COMPARED_FACETS
    }
    offset = 0

    while True:
        fetch = page_size if limit <= 0 else min(page_size, limit - report.scanned)
        if fetch <= 0:
            break
        page = document_store.list_documents(limit=fetch, offset=offset)
        if not page:
            break
        offset += len(page)
        report.scanned += len(page)

        for doc in page:
            metadata = doc.get("metadata") or {}
            shadow = metadata.get(SHADOW_TAGS_KEY)
            if not isinstance(shadow, dict):
                continue
            report.with_shadow += 1
            live_raw = metadata.get("content_tags")
            live = live_raw if isinstance(live_raw, dict) else {}

            agreements: dict[str, bool | None] = {}
            for facet in COMPARED_FACETS:
                bucket, verdict = _compare_facet(
                    facet, live.get(facet), shadow.get(facet)
                )
                tallies[facet][bucket] += 1
                agreements[facet] = verdict

            # The *verdict*, not the raw facet: an LLM record files its label
            # under ``document_form``, so reading ``content_type`` here counted
            # zero collisions on a corpus that is 93% collision (#325's defect,
            # in the one measurement that justifies ShadowTags existing).
            shadow_ct = shadow_verdict(shadow)
            if shadow_ct is not None and shadow_ct not in CONTENT_TYPE_VALUES:
                report.out_of_vocabulary_content_types[shadow_ct] = (
                    report.out_of_vocabulary_content_types.get(shadow_ct, 0) + 1
                )

            if collect_comparisons:
                report.comparisons.append(
                    ShadowComparison(
                        item_id=str(doc.get("doc_id") or ""),
                        live=dict(live),
                        shadow=dict(shadow),
                        agreements=agreements,
                    )
                )

    report.per_facet = {
        facet: FacetAgreement(facet=facet, **counts)
        for facet, counts in tallies.items()
    }
    return report


def _compare_facet(
    facet: str, live_value: Any, shadow_value: Any
) -> tuple[str, bool | None]:
    """Bucket one facet comparison. Returns ``(bucket_name, agreed_or_None)``.

    An empty list facet counts as *absent*, not as a value: every document the
    classify-on-write path tags stores ``domain: []`` deliberately (see
    :mod:`trellis.classify.ingest`), and counting that as "live produced a
    domain" would report near-total disagreement on the one facet where the
    live side has, by design, said nothing at all.
    """
    if facet in LIST_FACETS:
        live: Any = set(facet_values(live_value))
        shadow: Any = set(facet_values(shadow_value))
        live_present, shadow_present = bool(live), bool(shadow)
    else:
        live, shadow = live_value, shadow_value
        live_present = live_value is not None and live_value != ""
        shadow_present = shadow_value is not None and shadow_value != ""

    if not live_present and not shadow_present:
        return "both_missing", None
    if not live_present:
        return "live_missing", None
    if not shadow_present:
        return "shadow_missing", None

    agreed = bool(live == shadow)
    return ("agreed" if agreed else "disagreed"), agreed


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _to_shadow_tags(
    tags: dict[str, list[str]],
    *,
    classifier_name: str,
    confidence: float,
    model_id: str,
) -> ShadowTags:
    """Build a :class:`ShadowTags` from a classifier's raw facet map.

    Unlike :meth:`MergedClassification.to_content_tags` this coerces nothing
    into a controlled vocabulary — that is the entire point of the shadow
    record. Facet keys the classifier emits that are not first-class facets
    (``_auto_importance`` / ``_auto_summary``, which
    :class:`~trellis.classify.classifiers.llm.LLMFacetClassifier` uses as
    out-of-band channels) are dropped rather than smuggled into ``custom``:
    they are scores and prose, not tags, and the shadow record is meant to hold
    what the model said *about classification*.
    """
    scalar = _first_or_none

    return ShadowTags(
        domain=[str(v) for v in tags.get("domain", [])],
        content_type=scalar(tags.get("content_type")),
        scope=scalar(tags.get("scope")),
        signal_quality=scalar(tags.get("signal_quality")),
        retrieval_affinity=[str(v) for v in tags.get("retrieval_affinity", [])],
        custom={
            key: [str(v) for v in values]
            for key, values in tags.items()
            if not key.startswith("_")
            and key
            not in {
                "domain",
                "content_type",
                "scope",
                "signal_quality",
                "retrieval_affinity",
            }
        },
        classified_by=[classifier_name] if classifier_name else [],
        classified_at=datetime.now(UTC),
        model_id=model_id,
        confidence=max(0.0, min(1.0, float(confidence))),
    )


def _first_or_none(values: Any) -> str | None:
    """First element of a single-label facet's list form, or ``None``."""
    if isinstance(values, str):
        return values or None
    if isinstance(values, list) and values:
        return str(values[0])
    return None


def _write_shadow(
    document_store: DocumentStore,
    *,
    item_id: str,
    content: str,
    metadata: dict[str, Any],
    shadow_json: dict[str, Any],
) -> None:
    """Persist the shadow record without disturbing anything retrieval reads.

    Two distinct things have to hold, and only the first is obvious.

    **The live tag keys must not change.** The write is a whole-metadata
    ``put`` (the store API offers no partial update), so the live values are
    captured before the shadow key is set and restored after — even a caller
    that handed us a mutated mapping cannot move them.

    **The row must not look modified.** ``put`` normally stamps
    ``updated_at`` with the current time, and ``updated_at`` is what
    :class:`~trellis.retrieve.strategies.KeywordSearch` feeds to its recency
    decay. A whole-corpus shadow pass would therefore reset every document to
    "brand new" and flatten recency ordering across the entire store — the
    precise opposite of this module's guarantee, and invisible to a test that
    only checks the shadow *values* stay out of a pack. ``preserve_updated_at``
    is why the guarantee actually holds; see
    ``DocumentStoreContractTests.test_put_preserve_updated_at_keeps_prior_stamp``.
    """
    # `metadata` is this function's own copy and ``SHADOW_TAGS_KEY`` is not in
    # :data:`PROTECTED_LIVE_KEYS`, so the splat cannot move a live key — the
    # guarantee is structural, not a restore step.
    document_store.put(
        item_id,
        content,
        {**metadata, SHADOW_TAGS_KEY: shadow_json},
        preserve_updated_at=True,
    )


def _needs_shadow(raw: Any, cutoff: datetime | None) -> bool:
    """``True`` when an item has no usable shadow record, or a stale one.

    A record that is not a mapping (hand-edited, legacy) is unproven and
    treated as absent — same Option-A reading
    :func:`trellis.classify.refresh._is_stale` applies to ``content_tags``.
    """
    if not isinstance(raw, dict):
        return True
    if cutoff is None:
        # Default mode: a record exists, so it is not re-judged.
        return False
    classified_at = parse_classified_at(raw.get("classified_at"))
    return classified_at is None or classified_at < cutoff


def _tally(result: BatchShadowResult, item_id: str, outcome: ShadowOutcome) -> None:
    """Route one :class:`ShadowOutcome` into its counter bucket."""
    if outcome.written:
        result.written += 1
        result.item_ids_written.append(item_id)
    elif outcome.reason == REASON_NO_SIGNAL:
        result.skipped_no_signal += 1
    else:
        # REASON_NOT_FOUND: the store listed the row then could not read it
        # back (a concurrent delete). A document we were asked to judge and
        # did not is an error, not a skip.
        result.errors += 1


def _emit_judged(
    event_log: EventLog,
    *,
    item_id: str,
    content: str,
    decision: str,
    confidence: float,
    model_id: str,
) -> None:
    """Emit one leak-safe ``MEMORY_OP_JUDGED`` classification training pair.

    Carries the ``(input, decision)`` half only; the outcome *label* arrives
    later from the same feedback-attribution join
    :mod:`trellis.learning.pack_observations` already runs. Digest, verdict
    label, confidence and pointers only — never the document text and never
    the open-vocabulary ``domain`` tags (see the module docstring).
    """
    payload = MemoryOpJudgedPayload(
        op_type=JudgedOpType.CLASSIFICATION,
        model_id=model_id,
        input_digest=InputDigest(
            hash=content_hash(content),
            length=len(content),
            source_refs=[item_id],
        ),
        decision=decision,
        confidence=max(0.0, min(1.0, float(confidence))),
        subject_ref=SubjectRef(ref_type=REF_TYPE_DOCUMENT, ref_id=item_id),
    )
    try:
        event_log.emit(
            EventType.MEMORY_OP_JUDGED,
            source="classify.shadow",
            entity_id=item_id,
            entity_type="document",
            payload=payload.model_dump(mode="json"),
        )
    # GRACEFUL-DEGRADATION: the shadow record is already durable; this event
    # is post-success telemetry and must not roll the write back.
    except Exception:
        logger.exception("memory_op_judged_emit_failed", item_id=item_id)


__all__ = [
    "COMPARED_FACETS",
    "PROTECTED_LIVE_KEYS",
    "REASON_NO_SIGNAL",
    "SHADOW_TAGS_KEY",
    "BatchShadowResult",
    "FacetAgreement",
    "ShadowAgreementReport",
    "ShadowComparison",
    "ShadowOutcome",
    "compare_shadow_to_live",
    "shadow_classify_item",
    "shadow_classify_stale",
]
