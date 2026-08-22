"""Tag-keyword promotion loop — mining a deterministic vocabulary from LLM tags.

The second half of the ``DETERMINISTIC > LOCAL > FRONTIER`` ladder (#321). Phase
1 (:mod:`trellis.classify.shadow`) accrues a corpus of what an LLM says about
stored documents. This module reads that corpus and asks the only question that
lets the LLM be switched off for a tag: *is there a keyword rule the
deterministic classifier could own instead?* — "documents containing ``todoist``
were tagged ``task-management`` in 40 of 41 cases; propose that keyword".

Modelled directly on :mod:`trellis.learning.schema_evolution`, which already
solves this shape for open-string node/edge types, and sharing its four
constraints:

* **Read-only against the corpus.** The analyzer proposes; it never rewrites a
  tag, a document, or a config file. The only write is a
  :attr:`~trellis.stores.base.event_log.EventType.TAG_KEYWORD_CANDIDATE` event.
* **Thresholds from :class:`~trellis.ops.ParameterRegistry`, missing key
  raises.** No silent defaults — misconfiguration surfaces at the earliest
  possible point.
* **Idempotent across runs.** A candidate resurfaces only on material growth or
  an elapsed cooldown (:mod:`trellis.learning.cooldown`).
* **Filters its own writes.** A keyword already in the effective domain-keyword
  map is excluded, so a promoted keyword can neither be re-proposed nor inflate
  its own support on the next run. Without this the loop bootstraps off its own
  output and every promotion looks like it is getting stronger.

The write target already exists
-------------------------------

:class:`~trellis.classify.classifiers.keyword.KeywordDomainClassifier` merges
``config.yaml``'s ``classify.domain_keywords`` over its built-in defaults, so a
promoted vocabulary is a *config emission, not a code change*. That is what
makes "switch the LLM off for what has been learned" mechanically possible
rather than aspirational. :func:`apply_promotion` and :func:`revoke_promotion`
are the pure map transforms over that block; a promotion is revocable because
revoking is just the inverse transform.

Two properties of that classifier shape the safety story, and both are
load-bearing rather than incidental:

* It matches keywords as **substrings** of lowercased content, while this
  analyzer mines whole **tokens**. Token presence implies substring presence,
  so a mined keyword always fires — the mining is strictly more conservative
  than the matching.
* A domain needs :data:`~trellis.classify.classifiers.keyword._MIN_HITS` (2)
  distinct keyword hits before it is assigned. So promoting a *single* keyword
  cannot by itself make the classifier assign a domain. That is a feature: one
  bad promotion cannot hide a document on its own.

Why ``domain`` is surfaced and never auto-applied
-------------------------------------------------

``domain`` is the one facet that *hard-excludes* a document from a
domain-scoped query on mismatch. A wrong promoted keyword therefore **hides**
content rather than merely mis-ranking it — precisely the #282 failure, and the
reason ``include_domain=False`` is the default on both the ingest and refresh
paths and why ``trellis classify backfill --include-domain`` is flagged
DANGEROUS in its own help text. So for ``domain`` the loop ends at a proposal a
human approves.

Facets that only shape ranking (``content_type``, ``scope``,
``signal_quality``) carry no such risk — a wrong value degrades ordering at
worst — and are safe to auto-promote under a statistical gate. They are *not*
auto-promoted here, for a boring reason rather than a principled one: no config
write target exists for them (``classify.domain_keywords`` is domain-only), so
there is nothing to write a promotion into. :data:`FACETS_WITH_WRITE_TARGET`
records which facets have one. Candidates for the others are still surfaced —
that is where the vocabulary gap between the two classification paths shows up
— they simply cannot be applied yet.

What this loop does *not* measure
---------------------------------

Promotion here is gated on **agreement with the LLM**, not on retrieval
outcome. Agreement means the deterministic rule imitates the model; it does not
show that either was useful. Closing that requires joining promoted tags back
to pack feedback — the same attribution machinery
:mod:`trellis.learning.pack_observations` runs, which is currently thin. Until
that join exists this is a distillation step with a human gate, not a closed
learning loop, and the ``notes`` on a candidate say so rather than letting a
reviewer infer otherwise.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.learning.cooldown import (
    PriorCandidate,
    cooldown_blocks_emission,
    load_prior_candidates,
)
from trellis.schemas.classification import SHADOW_TAGS_KEY, _reserved_name_for
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from trellis.ops import ParameterRegistry
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Threshold keys + scope
# ---------------------------------------------------------------------------

#: Parameter-registry component id used for threshold resolution.
PARAM_COMPONENT_ID: str = "learning.tag_evolution"

#: Documents that must contain the keyword *and* carry the tag before a
#: candidate is eligible. The sample-size floor.
PARAM_MIN_SUPPORT: str = "tag_keyword_min_support"

#: Fraction of keyword-bearing documents that must carry the tag —
#: ``support / keyword_documents``. How reliably the keyword predicts the tag.
PARAM_MIN_PRECISION: str = "tag_keyword_min_precision"

#: Minimum ``lift - 1``: how much better the keyword predicts the tag than the
#: tag's own base rate in the corpus. Precision alone is not evidence — in a
#: corpus where 90% of documents carry one tag, *every* keyword has 0.9
#: precision for it. This is the effect-size gate that keeps the measurement
#: from being satisfiable by a constant.
PARAM_MIN_LIFT: str = "tag_keyword_min_lift"

#: Shadow-corpus size floor. Below this many shadowed documents the analyzer
#: emits nothing at all — mining rules from a handful of documents produces
#: confident nonsense.
PARAM_MIN_CORPUS: str = "tag_keyword_min_corpus"

#: Days between re-emissions of the same ``candidate_id`` when support has not
#: grown materially.
PARAM_COOLDOWN_DAYS: str = "tag_keyword_cooldown_days"


REQUIRED_PARAM_KEYS: tuple[str, ...] = (
    PARAM_MIN_SUPPORT,
    PARAM_MIN_PRECISION,
    PARAM_MIN_LIFT,
    PARAM_MIN_CORPUS,
    PARAM_COOLDOWN_DAYS,
)

#: Recommended seed values for a fresh ParameterRegistry, documented here so an
#: operator can hand-seed without consulting the issue.
#:
#: ``min_support`` and ``min_lift`` follow the precedent already set by
#: :mod:`trellis.learning.tuners.auto_promote`
#: (``DEFAULT_AUTO_MIN_SAMPLE_SIZE = 30``, ``DEFAULT_AUTO_MIN_EFFECT_SIZE =
#: 0.25``) rather than inventing a second statistical convention;
#: ``cooldown_days`` matches :mod:`trellis.learning.schema_evolution`.
RECOMMENDED_SEED_VALUES: dict[str, float | int | str | bool] = {
    PARAM_MIN_SUPPORT: 30,
    PARAM_MIN_PRECISION: 0.75,
    PARAM_MIN_LIFT: 0.25,
    PARAM_MIN_CORPUS: 30,
    PARAM_COOLDOWN_DAYS: 7,
}


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

#: Facets whose promotion has somewhere to be written. Only ``domain`` does:
#: ``config.yaml``'s ``classify.domain_keywords`` is a domain -> keywords map
#: and there is no equivalent for the ranking facets. Candidates for other
#: facets are surfaced for review but cannot be applied — see the module
#: docstring.
FACETS_WITH_WRITE_TARGET: frozenset[str] = frozenset({"domain"})

#: Facets stored as a list on :class:`~trellis.schemas.classification.ShadowTags`.
_LIST_FACETS: frozenset[str] = frozenset({"domain", "retrieval_affinity"})

#: Note attached to every candidate for a facet with no write target.
NOTE_NO_WRITE_TARGET = (
    "no config write target for this facet — surfaced for review only"
)

#: Note attached to every candidate, on every facet. States the limit of what
#: the measurement supports so a reviewer does not have to infer it.
NOTE_AGREEMENT_NOT_OUTCOME = (
    "gated on agreement with the LLM, not on retrieval outcome — "
    "this shows the rule imitates the model, not that either helped"
)

#: Note attached to ``domain`` candidates.
NOTE_DOMAIN_HARD_EXCLUDES = (
    "domain hard-excludes on mismatch: a wrong keyword hides documents from "
    "domain-scoped queries rather than re-ranking them — human approval required"
)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

#: Token splitter. Keeps ``-`` and ``_`` inside tokens so ``task-management``
#: and ``source_system`` survive as single keywords — the multi-word tags an
#: LLM produces are exactly the interesting candidates.
_TOKEN_RE = re.compile(r"[^a-z0-9_-]+")

#: Shortest token that can become a keyword. Two-character tokens match far too
#: much under the classifier's substring semantics (``"ml"`` is inside
#: ``"html"``).
_MIN_TOKEN_LENGTH = 3

#: Tokens that never become keywords. Deliberately small — a real stopword list
#: is a tuning surface, and the support/precision/lift gates already reject a
#: word that appears everywhere (its lift collapses to ~1). This list only
#: removes the tokens common enough to waste a slot in the counting pass.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "not",
        "but",
        "you",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "its",
        "it's",
        "they",
        "them",
        "their",
        "there",
        "then",
        "than",
        "when",
        "what",
        "which",
        "who",
        "how",
        "all",
        "any",
        "some",
        "one",
        "two",
        "our",
        "your",
        "his",
        "her",
        "out",
        "get",
        "got",
        "use",
        "used",
        "using",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "just",
        "also",
        "more",
        "most",
        "such",
        "only",
        "very",
        "much",
        "each",
        "other",
        "same",
        "these",
        "those",
        "been",
        "being",
        "does",
        "did",
        "doing",
        "here",
        "because",
        "while",
        "after",
        "before",
        "between",
    }
)


def extract_keywords(content: str) -> set[str]:
    """Deterministic token set for one document.

    Returns a *set*: presence, not frequency. The classifier this feeds asks
    "does this keyword appear at all", so counting repeats would let one
    keyword-heavy document look like many.
    """
    tokens = _TOKEN_RE.split(content.lower())
    return {
        token
        for token in tokens
        if len(token) >= _MIN_TOKEN_LENGTH
        and token not in _STOPWORDS
        and not token.isdigit()
        and token.strip("-_") != ""
    }


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TagKeywordCandidate:
    """A keyword that predicts an LLM-assigned tag strongly enough to surface.

    Frozen / slotted so it is hashable and dedupable across runs by
    ``candidate_id``. Advisory only: the human reviewing the proposal decides
    whether to accept, narrow, or reject it.
    """

    facet: str
    keyword: str
    tag: str
    #: Documents containing the keyword *and* carrying the tag.
    support: int
    #: Documents containing the keyword (the precision denominator).
    keyword_documents: int
    #: Documents carrying the tag (the recall denominator).
    tag_documents: int
    #: Shadowed documents scanned.
    corpus_documents: int
    #: ``support / keyword_documents`` — how reliably the keyword predicts.
    precision: float
    #: ``support / tag_documents`` — how much of the tag this keyword covers.
    #: Not gated on: a narrow-but-reliable keyword is still worth promoting.
    recall: float
    #: ``precision / base_rate(tag)`` — how much better than chance.
    lift: float
    candidate_id: str
    #: A few item ids a reviewer can open to judge the rule. Bounded.
    example_item_ids: tuple[str, ...] = ()
    cooldown_until: datetime | None = None
    recurrence_count: int = 0
    #: Findings recorded but not blocking — read by the reviewer.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_write_target(self) -> bool:
        """Whether a promotion of this candidate has anywhere to be written."""
        return self.facet in FACETS_WITH_WRITE_TARGET

    def to_event_payload(self) -> dict[str, Any]:
        """Render as a ``TAG_KEYWORD_CANDIDATE`` payload.

        Stable wire contract — downstream analyzers read this directly. Lists
        are plain ``list[str]`` so JSON round-trips across every backend.
        """
        return {
            "candidate_id": self.candidate_id,
            "facet": self.facet,
            "keyword": self.keyword,
            "tag": self.tag,
            "support": self.support,
            "keyword_documents": self.keyword_documents,
            "tag_documents": self.tag_documents,
            "corpus_documents": self.corpus_documents,
            "precision": self.precision,
            "recall": self.recall,
            "lift": self.lift,
            "has_write_target": self.has_write_target,
            "example_item_ids": list(self.example_item_ids),
            "recurrence_count": self.recurrence_count,
            "notes": list(self.notes),
        }


def compute_candidate_id(facet: str, keyword: str, tag: str) -> str:
    """Stable hash of ``(facet, keyword, tag)`` for cooldown bookkeeping."""
    raw = f"{facet}\x1f{keyword}\x1f{tag}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Threshold resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Thresholds:
    """Resolved threshold bundle. Constructed once per analyzer call."""

    min_support: int
    min_precision: float
    min_lift: float
    min_corpus: int
    cooldown_days: int


def _resolve_thresholds(registry: ParameterRegistry) -> _Thresholds:
    """Resolve every required threshold. A missing key raises :class:`KeyError`.

    No fallback to module-level constants: a silent default is exactly the
    misconfiguration the POC directive forbids, and here it would mean
    proposing vocabulary changes under thresholds nobody chose.
    """
    from trellis.schemas.parameters import ParameterScope  # noqa: PLC0415

    values = registry.get_values(ParameterScope(component_id=PARAM_COMPONENT_ID))
    missing = [key for key in REQUIRED_PARAM_KEYS if key not in values]
    if missing:
        seed_hint = ", ".join(f"{k}={RECOMMENDED_SEED_VALUES[k]!r}" for k in missing)
        msg = (
            f"ParameterRegistry is missing required tag-evolution thresholds: "
            f"{sorted(missing)!r}. Seed defaults are: {seed_hint}. See "
            f"trellis.learning.tag_evolution.RECOMMENDED_SEED_VALUES."
        )
        raise KeyError(msg)

    min_precision = float(values[PARAM_MIN_PRECISION])
    if not 0.0 < min_precision <= 1.0:
        msg = f"{PARAM_MIN_PRECISION} must be in (0.0, 1.0], got {min_precision!r}"
        raise ValueError(msg)
    min_lift = float(values[PARAM_MIN_LIFT])
    if min_lift < 0.0:
        msg = f"{PARAM_MIN_LIFT} must be >= 0.0, got {min_lift!r}"
        raise ValueError(msg)

    return _Thresholds(
        min_support=int(values[PARAM_MIN_SUPPORT]),
        min_precision=min_precision,
        min_lift=min_lift,
        min_corpus=int(values[PARAM_MIN_CORPUS]),
        cooldown_days=int(values[PARAM_COOLDOWN_DAYS]),
    )


# ---------------------------------------------------------------------------
# Corpus scan
# ---------------------------------------------------------------------------

#: Documents per ``list_documents`` round-trip.
DEFAULT_PAGE_SIZE = 100

#: Cap on documents scanned per run.
DEFAULT_SCAN_LIMIT = 50_000

#: Example ids carried on each candidate for human review.
_MAX_EXAMPLES = 5


@dataclass
class _Corpus:
    """Counts accumulated over the shadow corpus in a single pass."""

    documents: int = 0
    keyword_documents: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tag_documents: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    pair_documents: dict[tuple[str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    pair_examples: dict[tuple[str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )


def _scan_corpus(
    document_store: DocumentStore,
    *,
    facet: str,
    excluded_keywords: frozenset[str],
    scan_limit: int,
    page_size: int,
) -> _Corpus:
    """Single pass over shadowed documents accumulating co-occurrence counts.

    ``excluded_keywords`` is how the loop *filters its own writes*: a keyword
    already in the effective classifier vocabulary is dropped before counting,
    so a promoted keyword neither reappears as a candidate nor accrues support
    the loop could read as its own growing evidence.
    """
    corpus = _Corpus()
    offset = 0
    scanned = 0

    while scanned < scan_limit:
        fetch = min(page_size, scan_limit - scanned)
        page = document_store.list_documents(limit=fetch, offset=offset)
        if not page:
            break
        offset += len(page)
        scanned += len(page)

        for doc in page:
            metadata = doc.get("metadata") or {}
            shadow = metadata.get(SHADOW_TAGS_KEY)
            if not isinstance(shadow, dict):
                continue
            tags = _facet_values(facet, shadow.get(facet))
            if not tags:
                continue
            content = doc.get("content") or ""
            if not content.strip():
                continue

            corpus.documents += 1
            keywords = extract_keywords(content) - excluded_keywords
            item_id = str(doc.get("doc_id") or "")

            for tag in tags:
                corpus.tag_documents[tag] += 1
            for keyword in keywords:
                corpus.keyword_documents[keyword] += 1
                for tag in tags:
                    pair = (keyword, tag)
                    corpus.pair_documents[pair] += 1
                    if len(corpus.pair_examples[pair]) < _MAX_EXAMPLES and item_id:
                        corpus.pair_examples[pair].append(item_id)

    return corpus


def _facet_values(facet: str, raw: Any) -> list[str]:
    """Normalise a stored facet value to the list of tags it carries."""
    if raw is None:
        return []
    if facet in _LIST_FACETS:
        if isinstance(raw, str):
            return [raw] if raw else []
        if isinstance(raw, list):
            return [str(v) for v in raw if v]
        return []
    return [str(raw)] if raw else []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_tag_keyword_candidates(
    *,
    document_store: DocumentStore,
    event_log: EventLog,
    registry: ParameterRegistry,
    facet: str = "domain",
    known_keywords: Iterable[str] | None = None,
    emit_events: bool = True,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    page_size: int = DEFAULT_PAGE_SIZE,
    now: datetime | None = None,
) -> list[TagKeywordCandidate]:
    """Mine the shadow corpus for keyword rules worth teaching the classifier.

    Read-only against the document store. The only write is one
    :attr:`~trellis.stores.base.event_log.EventType.TAG_KEYWORD_CANDIDATE`
    event per surfaced candidate, and only when ``emit_events`` is ``True``.

    Args:
        document_store: Source of shadowed documents.
        event_log: Source of prior candidate events (for cooldown) and
            destination for emissions.
        registry: Threshold resolver. Must carry every key in
            :data:`REQUIRED_PARAM_KEYS`; a missing key raises
            :class:`KeyError` rather than falling back to a default.
        facet: Which shadow facet to mine. ``"domain"`` is the only facet with
            a config write target (:data:`FACETS_WITH_WRITE_TARGET`); others
            are surfaced for review only.
        known_keywords: Keywords the deterministic classifier already owns —
            pass
            :func:`~trellis.classify.classifiers.keyword.build_domain_keyword_map`'s
            values so the loop cannot re-propose or bootstrap off its own
            prior promotions. ``None`` means "the classifier owns nothing",
            which is only right in tests.
        emit_events: ``False`` runs the analyzer as a dry run.
        scan_limit: Cap on documents read.
        page_size: Documents per store round-trip.
        now: Test seam for the cooldown clock.

    Returns:
        Surfaced candidates, sorted by descending support then keyword.

    Raises:
        KeyError: When the registry lacks a required threshold key.
        ValueError: When a threshold is out of range.
    """
    thresholds = _resolve_thresholds(registry)
    eval_now = now if now is not None else datetime.now(tz=UTC)
    excluded = frozenset(k.lower() for k in (known_keywords or ()))

    corpus = _scan_corpus(
        document_store,
        facet=facet,
        excluded_keywords=excluded,
        scan_limit=scan_limit,
        page_size=page_size,
    )

    if corpus.documents < thresholds.min_corpus:
        # Loud, not silent: "nothing surfaced" and "not enough corpus to look"
        # are different answers and an operator must be able to tell them
        # apart — the shadow pass may simply not have been run yet.
        logger.info(
            "tag_evolution.corpus_below_floor",
            facet=facet,
            shadowed_documents=corpus.documents,
            min_corpus=thresholds.min_corpus,
        )
        return []

    prior_candidates = load_prior_candidates(
        event_log,
        event_type=EventType.TAG_KEYWORD_CANDIDATE,
        count_key="support",
    )

    surfaced = _surface_candidates(
        corpus,
        facet=facet,
        thresholds=thresholds,
        prior_candidates=prior_candidates,
        eval_now=eval_now,
    )
    surfaced.sort(key=lambda c: (-c.support, c.keyword, c.tag))

    if emit_events:
        for candidate in surfaced:
            event_log.emit(
                EventType.TAG_KEYWORD_CANDIDATE,
                source="learning.tag_evolution",
                entity_id=candidate.candidate_id,
                entity_type="tag_keyword",
                payload=candidate.to_event_payload(),
            )

    logger.info(
        "tag_evolution.completed",
        facet=facet,
        shadowed_documents=corpus.documents,
        surfaced=len(surfaced),
        emitted=len(surfaced) if emit_events else 0,
    )
    return surfaced


def _surface_candidates(
    corpus: _Corpus,
    *,
    facet: str,
    thresholds: _Thresholds,
    prior_candidates: dict[str, PriorCandidate],
    eval_now: datetime,
) -> list[TagKeywordCandidate]:
    """Apply the gates to every (keyword, tag) pair and build candidates."""
    surfaced: list[TagKeywordCandidate] = []

    for (keyword, tag), support in corpus.pair_documents.items():
        if support < thresholds.min_support:
            continue
        # A reserved policy namespace is never proposable as a tag value —
        # ContentTags rejects it outright. The shadow record deliberately does
        # not reject it (recording what a model proposed is the point), so the
        # refusal lives here, at the gate where it matters.
        if _reserved_name_for(tag) is not None:
            logger.info("tag_evolution.reserved_tag_skipped", facet=facet, tag=tag)
            continue

        keyword_documents = corpus.keyword_documents[keyword]
        tag_documents = corpus.tag_documents[tag]
        precision = support / keyword_documents if keyword_documents else 0.0
        if precision < thresholds.min_precision:
            continue

        base_rate = tag_documents / corpus.documents if corpus.documents else 0.0
        # A tag present on every shadowed document has a base rate of 1.0 and
        # therefore no achievable lift — correctly so. "Everything is tagged X"
        # teaches a keyword rule nothing.
        lift = precision / base_rate if base_rate > 0 else 0.0
        if lift - 1.0 < thresholds.min_lift:
            continue

        candidate_id = compute_candidate_id(facet, keyword, tag)
        blocked, cooldown_until, recurrence = cooldown_blocks_emission(
            candidate_id=candidate_id,
            current_count=support,
            prior=prior_candidates.get(candidate_id),
            cooldown_days=thresholds.cooldown_days,
            now=eval_now,
            log_event="tag_evolution.candidate_suppressed_cooldown",
        )
        if blocked:
            continue

        notes = [NOTE_AGREEMENT_NOT_OUTCOME]
        if facet == "domain":
            notes.append(NOTE_DOMAIN_HARD_EXCLUDES)
        if facet not in FACETS_WITH_WRITE_TARGET:
            notes.append(NOTE_NO_WRITE_TARGET)

        surfaced.append(
            TagKeywordCandidate(
                facet=facet,
                keyword=keyword,
                tag=tag,
                support=support,
                keyword_documents=keyword_documents,
                tag_documents=tag_documents,
                corpus_documents=corpus.documents,
                precision=precision,
                recall=support / tag_documents if tag_documents else 0.0,
                lift=lift,
                candidate_id=candidate_id,
                example_item_ids=tuple(corpus.pair_examples[(keyword, tag)]),
                cooldown_until=cooldown_until,
                recurrence_count=recurrence,
                notes=tuple(notes),
            )
        )

    return surfaced


# ---------------------------------------------------------------------------
# Promotion — pure transforms over the config block
# ---------------------------------------------------------------------------


def apply_promotion(
    config_domains: Mapping[str, list[str]] | None,
    candidates: Iterable[TagKeywordCandidate],
) -> dict[str, list[str]]:
    """Return ``config_domains`` with every promotable candidate merged in.

    A **pure transform** over the ``classify.domain_keywords`` mapping — it
    reads no file and writes none. The operator owns ``config.yaml``; this
    produces the block to put in it. That is what keeps the ``domain`` facet
    surface-only in practice and not just in intent.

    Candidates for a facet with no write target
    (:data:`FACETS_WITH_WRITE_TARGET`) are skipped rather than silently coerced
    into the domain map, which would file a ``content_type`` rule under a
    domain name.

    Keywords are appended (not replaced) and deduplicated, preserving the
    operator's existing ordering — a promotion adds vocabulary, it never
    removes any.
    """
    promoted = {k: list(v) for k, v in (config_domains or {}).items()}
    for candidate in candidates:
        if not candidate.has_write_target:
            logger.info(
                "tag_evolution.promotion_skipped_no_write_target",
                facet=candidate.facet,
                keyword=candidate.keyword,
                tag=candidate.tag,
            )
            continue
        existing = promoted.setdefault(candidate.tag, [])
        if candidate.keyword not in existing:
            existing.append(candidate.keyword)
    return promoted


def revoke_promotion(
    config_domains: Mapping[str, list[str]] | None,
    candidates: Iterable[TagKeywordCandidate],
) -> dict[str, list[str]]:
    """Return ``config_domains`` with every candidate's keyword removed.

    The exact inverse of :func:`apply_promotion` — applying then revoking the
    same candidates returns the original mapping, including dropping a domain
    key the promotion created. A promoted keyword that turns out to hide
    documents has to be removable without an archaeology session, so
    revocation is a first-class operation rather than a manual edit.

    Revoking a keyword that is not present is a no-op, so a revoke is safe to
    re-run.
    """
    revoked = {k: list(v) for k, v in (config_domains or {}).items()}
    for candidate in candidates:
        keywords = revoked.get(candidate.tag)
        if keywords is None:
            continue
        revoked[candidate.tag] = [k for k in keywords if k != candidate.keyword]
        if not revoked[candidate.tag]:
            # An empty keyword list is a domain that can never match; drop the
            # key so revoking restores the mapping exactly.
            del revoked[candidate.tag]
    return revoked


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SCAN_LIMIT",
    "FACETS_WITH_WRITE_TARGET",
    "NOTE_AGREEMENT_NOT_OUTCOME",
    "NOTE_DOMAIN_HARD_EXCLUDES",
    "NOTE_NO_WRITE_TARGET",
    "PARAM_COMPONENT_ID",
    "PARAM_COOLDOWN_DAYS",
    "PARAM_MIN_CORPUS",
    "PARAM_MIN_LIFT",
    "PARAM_MIN_PRECISION",
    "PARAM_MIN_SUPPORT",
    "RECOMMENDED_SEED_VALUES",
    "REQUIRED_PARAM_KEYS",
    "TagKeywordCandidate",
    "analyze_tag_keyword_candidates",
    "apply_promotion",
    "compute_candidate_id",
    "extract_keywords",
    "revoke_promotion",
]
