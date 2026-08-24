"""Domain-vocabulary normalization — collapse an open tag set into a filterable one.

:mod:`trellis.learning.tag_evolution` mines the shadow corpus for keyword
rules that predict a ``domain`` tag. It assumes the vocabulary it mines is
coherent. Running the first full shadow pass over a real 999-document corpus
showed it is not:

* **1160 distinct ``domain`` tags over 998 documents.**
* **59% of the vocabulary appears on exactly one document.** Only 29 tags
  (2%) reach 20 documents.
* One subject fragments across fourteen spellings — ``hunting`` (69),
  ``deer-hunting`` (6), ``deer`` (5), ``tribal-hunts`` (3),
  ``whitetail-deer``, ``budget-hunting``, ``hunting-opportunities``,
  ``hunting-options``, ``hunting-events``, ``game-draw-system``,
  ``deer-draw``, ``hunts`` (1 each) — because an open-vocabulary model
  invents a fresh near-synonym per document.

That matters more than it sounds. ``domain`` is the one facet that
**hard-excludes**: a query scoped to ``hunting`` does not merely rank
``tribal-hunts`` documents lower, it cannot see them. A dimension whose
values are 59% singletons cannot function as a filter, so promoting keyword
rules into it is polishing a lever that is not connected to anything.

**What this module proposes, and what it refuses to do.** It surfaces
``alias -> canonical`` merges for human approval and produces a config
fragment. It never applies one. The rule that keeps ``domain`` surface-only
applies here with *more* force than it does to a single keyword: a keyword
rule mis-tags the documents that contain one word, whereas an alias map
redirects an entire tag's worth of documents at once. Wrong in bulk is worse
than wrong once.

**Two generators, because one is not enough.**

*Lexical containment* — the alias contains a canonical as a whole token
(``budget-hunting`` ⊃ ``hunting``). High precision for the compound-noun
shape the model actually produces, and token-exact on purpose:
``scavenger-hunt`` contains ``hunt``, not ``hunting``, and is a genuinely
different subject. Stemming would merge it; token equality does not.

*Co-occurrence* — the alias's documents also carry the canonical, often
enough that they are evidently labels for the same thing. This catches
synonyms with no shared spelling (``whitetail-deer``), which lexical matching
cannot reach.

Neither is trusted alone. Every candidate reports **all** its evidence,
including the counter-evidence: a lexical match with zero co-occurrence and
no shared tag-neighbourhood is exactly the ``scavenger-hunt`` shape, and it
is surfaced *with that fact attached* rather than silently dropped, because a
reviewer who can see why a merge is weak makes a better decision than one
handed a shorter list.

Shares :mod:`trellis.learning.tag_evolution`'s four constraints: read-only
against the document store, :class:`ParameterRegistry` thresholds that raise
on a missing key rather than defaulting, idempotent across runs via the
shared :mod:`trellis.learning.cooldown`, and it filters its own writes — an
alias already in the operator's map is never re-proposed.

See ``#321``.
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
    cooldown_blocks_emission,
    load_prior_candidates,
)
from trellis.schemas.classification import (
    SHADOW_TAGS_KEY,
    _reserved_name_for,
    facet_values,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from trellis.ops import ParameterRegistry
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Documents read per store round-trip.
DEFAULT_PAGE_SIZE = 100

#: Cap on documents scanned in one analyzer run.
DEFAULT_SCAN_LIMIT = 50_000

#: ``ParameterScope.component_id`` these thresholds live under.
PARAM_COMPONENT_ID: str = "learning.domain_normalization"

#: Documents a tag must carry to be a *merge destination*. Canonical tags are
#: the small stable set the facet is being collapsed toward; a low-support tag
#: is not a destination, it is a candidate for becoming one's alias.
PARAM_MIN_CANONICAL_SUPPORT: str = "domain_min_canonical_support"

#: Documents an alias must carry to be proposed at all. Defaults to 1 — the
#: singletons *are* the problem, so excluding them would exclude 59% of the
#: vocabulary and most of the value.
PARAM_MIN_ALIAS_SUPPORT: str = "domain_min_alias_support"

#: Fraction of an alias's documents that must also carry the canonical for
#: co-occurrence alone to surface a merge.
PARAM_MIN_COOCCURRENCE: str = "domain_min_cooccurrence"

#: Jaccard overlap of the two tags' co-occurring neighbours, required when a
#: lexical match has no direct co-occurrence to corroborate it. Two tags that
#: never appear together but keep the same company are usually the same
#: subject; two that share neither are usually not.
PARAM_MIN_NEIGHBOR_OVERLAP: str = "domain_min_neighbor_overlap"

#: Days a surfaced candidate stays suppressed before it may be re-emitted.
PARAM_COOLDOWN_DAYS: str = "domain_alias_cooldown_days"

#: Every key :func:`_resolve_thresholds` requires. A missing key raises.
REQUIRED_PARAM_KEYS: tuple[str, ...] = (
    PARAM_MIN_CANONICAL_SUPPORT,
    PARAM_MIN_ALIAS_SUPPORT,
    PARAM_MIN_COOCCURRENCE,
    PARAM_MIN_NEIGHBOR_OVERLAP,
    PARAM_COOLDOWN_DAYS,
)

#: Defaults the CLI seeds when a deployment has none, loudly. The analyzer
#: itself never defaults — see :func:`_resolve_thresholds`.
RECOMMENDED_SEED_VALUES: dict[str, float | int | str | bool] = {
    PARAM_MIN_CANONICAL_SUPPORT: 20,
    PARAM_MIN_ALIAS_SUPPORT: 1,
    PARAM_MIN_COOCCURRENCE: 0.5,
    PARAM_MIN_NEIGHBOR_OVERLAP: 0.15,
    PARAM_COOLDOWN_DAYS: 7,
}

#: Signal labels recorded on :attr:`DomainAliasCandidate.signals`.
SIGNAL_LEXICAL = "lexical"
SIGNAL_COOCCURRENCE = "cooccurrence"

#: Splits a tag into comparable tokens. Tags are model-authored slugs
#: (``deer-hunting``, ``real_estate``, ``tax2026``), so hyphen / underscore /
#: case are noise and the token is the unit of meaning.
_TAG_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tag_tokens(tag: str) -> frozenset[str]:
    """Comparable token set for a tag slug."""
    return frozenset(_TAG_TOKEN_RE.findall(tag.lower()))


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainAliasCandidate:
    """A low-support tag proposed as a spelling of a high-support one.

    Advisory only. Frozen / slotted so it is hashable and dedupable across
    runs by ``candidate_id``.
    """

    alias: str
    canonical: str
    #: Documents carrying the alias.
    alias_documents: int
    #: Documents carrying the canonical.
    canonical_documents: int
    #: Shadowed documents scanned.
    corpus_documents: int
    #: Documents carrying both.
    cooccurrence_documents: int
    #: ``cooccurrence_documents / alias_documents`` — how often the two are
    #: already used together. ``0.0`` is the interesting case, not a failure:
    #: it means the merge is proposed on spelling alone.
    cooccurrence_rate: float
    #: Jaccard overlap of the tags each one co-occurs with, excluding each
    #: other. Corroborates a merge between tags that never appear together.
    neighbor_overlap: float
    #: Tokens the two slugs share. Empty for a pure co-occurrence match.
    shared_tokens: tuple[str, ...]
    #: Which generators fired — :data:`SIGNAL_LEXICAL`,
    #: :data:`SIGNAL_COOCCURRENCE`, or both.
    signals: tuple[str, ...]
    #: Other canonical tags named by the alias's remaining tokens. Non-empty
    #: means the alias sits across two subjects (``tax-planning`` is both
    #: ``tax`` and ``planning``), so merging it into either one hides it from
    #: the other.
    competing_canonicals: tuple[str, ...]
    #: Documents that carry the alias but **not** the canonical: what the
    #: merge would actually add to the canonical's reach. A merge whose
    #: co-occurrence is total is tidy but changes nothing a query can see.
    documents_gained: int
    candidate_id: str
    #: A few item ids a reviewer can open. Bounded, and deliberately **not**
    #: part of :meth:`to_event_payload` — same disclosure rule as
    #: :class:`~trellis.learning.tag_evolution.TagKeywordCandidate`.
    example_item_ids: tuple[str, ...] = ()
    cooldown_until: datetime | None = None
    recurrence_count: int = 0
    #: Findings recorded but not blocking — read by the reviewer.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_lexical_only(self) -> bool:
        """Proposed on spelling with no corpus evidence to back it.

        The ``scavenger-hunt`` shape. Not disqualifying — a genuinely rare
        synonym has nothing to co-occur with — but the single most useful
        thing a reviewer can know about a candidate.
        """
        return (
            SIGNAL_LEXICAL in self.signals
            and SIGNAL_COOCCURRENCE not in self.signals
            and self.cooccurrence_documents == 0
        )

    def to_event_payload(self) -> dict[str, Any]:
        """Render as a ``DOMAIN_ALIAS_CANDIDATE`` payload."""
        return {
            "candidate_id": self.candidate_id,
            "alias": self.alias,
            "canonical": self.canonical,
            "alias_documents": self.alias_documents,
            "canonical_documents": self.canonical_documents,
            "corpus_documents": self.corpus_documents,
            "cooccurrence_documents": self.cooccurrence_documents,
            "cooccurrence_rate": self.cooccurrence_rate,
            "neighbor_overlap": self.neighbor_overlap,
            "shared_tokens": list(self.shared_tokens),
            "signals": list(self.signals),
            "competing_canonicals": list(self.competing_canonicals),
            "documents_gained": self.documents_gained,
            "is_lexical_only": self.is_lexical_only,
            "example_count": len(self.example_item_ids),
            "recurrence_count": self.recurrence_count,
            "notes": list(self.notes),
        }


def compute_candidate_id(alias: str, canonical: str) -> str:
    """Stable hash of ``(alias, canonical)`` for cooldown bookkeeping."""
    raw = f"{alias}\x1f{canonical}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Threshold resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Thresholds:
    """Resolved threshold bundle. Constructed once per analyzer call."""

    min_canonical_support: int
    min_alias_support: int
    min_cooccurrence: float
    min_neighbor_overlap: float
    cooldown_days: int


def _resolve_thresholds(registry: ParameterRegistry) -> _Thresholds:
    """Read every threshold, raising on a missing or out-of-range key.

    No silent defaults, for the same reason
    :func:`~trellis.learning.tag_evolution._resolve_thresholds` has none: a
    threshold that quietly falls back is a gate nobody chose, and this gate
    decides which documents stay visible.
    """
    from trellis.schemas.parameters import ParameterScope  # noqa: PLC0415

    values = registry.get_values(ParameterScope(component_id=PARAM_COMPONENT_ID))
    missing = [k for k in REQUIRED_PARAM_KEYS if k not in values]
    if missing:
        seed_hint = ", ".join(f"{k}={RECOMMENDED_SEED_VALUES[k]!r}" for k in missing)
        msg = (
            f"ParameterRegistry is missing required domain-normalization "
            f"thresholds: {sorted(missing)!r}. Seed defaults are: {seed_hint}. "
            f"See trellis.learning.domain_normalization.RECOMMENDED_SEED_VALUES."
        )
        raise KeyError(msg)

    min_canonical_support = int(values[PARAM_MIN_CANONICAL_SUPPORT])
    min_alias_support = int(values[PARAM_MIN_ALIAS_SUPPORT])
    min_cooccurrence = float(values[PARAM_MIN_COOCCURRENCE])
    min_neighbor_overlap = float(values[PARAM_MIN_NEIGHBOR_OVERLAP])
    cooldown_days = int(values[PARAM_COOLDOWN_DAYS])

    if min_canonical_support < 1:
        msg = (
            f"{PARAM_MIN_CANONICAL_SUPPORT} must be >= 1, got {min_canonical_support!r}"
        )
        raise ValueError(msg)
    if min_alias_support < 1:
        msg = f"{PARAM_MIN_ALIAS_SUPPORT} must be >= 1, got {min_alias_support!r}"
        raise ValueError(msg)
    if not 0.0 <= min_cooccurrence <= 1.0:
        msg = (
            f"{PARAM_MIN_COOCCURRENCE} must be in [0.0, 1.0], got {min_cooccurrence!r}"
        )
        raise ValueError(msg)
    if not 0.0 <= min_neighbor_overlap <= 1.0:
        msg = (
            f"{PARAM_MIN_NEIGHBOR_OVERLAP} must be in [0.0, 1.0], "
            f"got {min_neighbor_overlap!r}"
        )
        raise ValueError(msg)
    if cooldown_days < 0:
        msg = f"{PARAM_COOLDOWN_DAYS} must be >= 0, got {cooldown_days!r}"
        raise ValueError(msg)

    return _Thresholds(
        min_canonical_support=min_canonical_support,
        min_alias_support=min_alias_support,
        min_cooccurrence=min_cooccurrence,
        min_neighbor_overlap=min_neighbor_overlap,
        cooldown_days=cooldown_days,
    )


# ---------------------------------------------------------------------------
# Corpus scan
# ---------------------------------------------------------------------------


@dataclass
class _Vocabulary:
    """Tag statistics accumulated over the shadow corpus in a single pass."""

    documents: int = 0
    tag_documents: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Unordered pair -> documents carrying both.
    pair_documents: dict[tuple[str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    #: Tag -> the tags it has ever appeared beside.
    neighbors: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: Tag -> a few item ids carrying it.
    examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def pair(self, a: str, b: str) -> int:
        """Documents carrying both tags, order-independent."""
        return self.pair_documents.get((a, b) if a <= b else (b, a), 0)


#: Example ids retained per tag. Enough to judge a merge, few enough that the
#: scan stays bounded on a corpus with a long singleton tail.
_MAX_EXAMPLES = 3


def _scan_vocabulary(
    document_store: DocumentStore,
    *,
    scan_limit: int,
    page_size: int,
) -> _Vocabulary:
    """Build tag / co-occurrence / neighbourhood statistics in one pass."""
    vocab = _Vocabulary()
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
            tags = sorted(set(facet_values(shadow.get("domain"))))
            if not tags:
                continue
            vocab.documents += 1
            item_id = str(doc.get("doc_id") or "")

            for tag in tags:
                vocab.tag_documents[tag] += 1
                if item_id and len(vocab.examples[tag]) < _MAX_EXAMPLES:
                    vocab.examples[tag].append(item_id)

            for i, a in enumerate(tags):
                for b in tags[i + 1 :]:
                    vocab.pair_documents[(a, b)] += 1
                    vocab.neighbors[a].add(b)
                    vocab.neighbors[b].add(a)

    return vocab


def _jaccard(a: set[str], b: set[str]) -> float:
    """Overlap of two neighbourhoods; ``0.0`` when either is empty."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


def analyze_domain_alias_candidates(
    *,
    document_store: DocumentStore,
    event_log: EventLog,
    registry: ParameterRegistry,
    known_aliases: Mapping[str, str] | None = None,
    aspect_tags: Iterable[str] | None = None,
    emit_events: bool = True,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    page_size: int = DEFAULT_PAGE_SIZE,
    now: datetime | None = None,
) -> list[DomainAliasCandidate]:
    """Propose ``alias -> canonical`` merges for the ``domain`` vocabulary.

    Read-only against the document store. The only write is one
    :attr:`~trellis.stores.base.event_log.EventType.DOMAIN_ALIAS_CANDIDATE`
    event per surfaced candidate, and only when ``emit_events`` is ``True``.

    Args:
        document_store: Source of shadowed documents.
        event_log: Source of prior candidate events (for cooldown) and
            destination for emissions.
        registry: Threshold resolver. Must carry every key in
            :data:`REQUIRED_PARAM_KEYS`; a missing key raises.
        aspect_tags: Tags that name an *aspect* of engagement rather than a
            subject (``planning``, ``research``). Excluded as merge
            *destinations*: collapsing ``estate-planning`` and
            ``trip-planning`` into ``planning`` keeps the mode and discards
            the subject, which is the half that identifies the document. An
            alias with no non-aspect destination is left unmerged rather than
            merged badly. Declared, never inferred — see
            :data:`~trellis.classify.factory.DOMAIN_ASPECTS_KEY`.
        known_aliases: The operator's existing ``alias -> canonical`` map.
            Aliases already mapped are not re-proposed — this is how the
            analyzer filters its own writes and cannot bootstrap off a merge
            it previously suggested.
        emit_events: ``False`` runs the analyzer as a dry run.
        scan_limit: Cap on documents read.
        page_size: Documents per store round-trip.
        now: Test seam for the cooldown clock.

    Returns:
        Candidates sorted by descending ``documents_gained``, then alias.

    Raises:
        KeyError: When the registry lacks a required threshold key.
        ValueError: When a threshold is out of range.
    """
    thresholds = _resolve_thresholds(registry)
    eval_now = now if now is not None else datetime.now(tz=UTC)
    already_mapped = {a.lower() for a in (known_aliases or {})}
    aspects = frozenset(aspect_tags or ())

    vocab = _scan_vocabulary(document_store, scan_limit=scan_limit, page_size=page_size)
    if not vocab.documents:
        logger.info("domain_normalization.empty_corpus")
        return []

    canonicals = sorted(
        tag
        for tag, count in vocab.tag_documents.items()
        if count >= thresholds.min_canonical_support
        and not _reserved_name_for(tag)
        and tag not in aspects
    )
    canonical_set = set(canonicals)
    canonical_tokens = {c: tag_tokens(c) for c in canonicals}
    # Single-token canonicals, indexed by that token, so an alias's leftover
    # tokens can be checked for naming a competing subject.
    canonical_token_index = {
        next(iter(tokens)): c
        for c, tokens in canonical_tokens.items()
        if len(tokens) == 1
    }

    logger.info(
        "domain_normalization.vocabulary_scanned",
        documents=vocab.documents,
        distinct_tags=len(vocab.tag_documents),
        canonicals=len(canonicals),
        aspects_excluded=len(aspects),
    )

    prior = load_prior_candidates(
        event_log,
        event_type=EventType.DOMAIN_ALIAS_CANDIDATE,
        count_key="alias_documents",
    )

    surfaced: list[DomainAliasCandidate] = []

    for alias, alias_count in sorted(vocab.tag_documents.items()):
        if alias in canonical_set:
            # Merging two established tags is a different, riskier decision
            # than absorbing a straggler, and not one this analyzer makes.
            continue
        if alias_count < thresholds.min_alias_support:
            continue
        if alias.lower() in already_mapped:
            continue
        if _reserved_name_for(alias):
            logger.info("domain_normalization.reserved_alias_skipped", alias=alias)
            continue

        alias_tokens = tag_tokens(alias)

        best: DomainAliasCandidate | None = None
        for canonical in canonicals:
            candidate = _evaluate_pair(
                alias=alias,
                alias_count=alias_count,
                alias_tokens=alias_tokens,
                canonical=canonical,
                canonical_tokens=canonical_tokens[canonical],
                canonical_token_index=canonical_token_index,
                vocab=vocab,
                thresholds=thresholds,
            )
            # One proposal per alias: the strongest destination wins, so a
            # reviewer is never asked to arbitrate between two merges of the
            # same tag.
            if candidate is not None and (
                best is None or _strength(candidate) > _strength(best)
            ):
                best = candidate

        if best is not None:
            surfaced.append(_annotate(best))

    surfaced = _apply_cooldown(
        surfaced,
        prior=prior,
        cooldown_days=thresholds.cooldown_days,
        now=eval_now,
    )
    surfaced.sort(key=lambda c: (-c.documents_gained, c.alias))

    if emit_events:
        for candidate in surfaced:
            event_log.emit(
                EventType.DOMAIN_ALIAS_CANDIDATE,
                source="learning.domain_normalization",
                entity_id=candidate.candidate_id,
                entity_type="domain_alias",
                payload=candidate.to_event_payload(),
            )

    logger.info(
        "domain_normalization.candidates_surfaced",
        count=len(surfaced),
        lexical_only=sum(1 for c in surfaced if c.is_lexical_only),
    )
    return surfaced


def _evaluate_pair(
    *,
    alias: str,
    alias_count: int,
    alias_tokens: frozenset[str],
    canonical: str,
    canonical_tokens: frozenset[str],
    canonical_token_index: Mapping[str, str],
    vocab: _Vocabulary,
    thresholds: _Thresholds,
) -> DomainAliasCandidate | None:
    """Judge one ``alias -> canonical`` pair, or ``None`` if neither signal fires."""
    # Token *containment*, not overlap: `hunting` is a spelling of the subject
    # `budget-hunting` names. A single shared token between two multi-token
    # slugs (`tax-planning` / `estate-planning`) is a shared modifier, not the
    # same subject.
    lexical = bool(canonical_tokens <= alias_tokens)
    if not lexical:
        # Co-occurrence cannot generate a merge on its own. Measured against a
        # real corpus it proposed `colorado -> hunting`, `technology ->
        # finance` and `playwright -> hunting`: in a corpus where one subject
        # clusters, everything appearing beside it looks like its alias.
        # Co-occurrence measures topical *association*; a merge needs
        # *synonymy*, and nothing in a co-occurrence table distinguishes a
        # synonym from a subtopic. So spelling generates, and co-occurrence
        # corroborates or contradicts. The cost is real and accepted: a
        # same-subject tag that shares no token (`whitetail-deer` ->
        # `hunting`) is never proposed. Missing a merge leaves the vocabulary
        # fragmented; a wrong merge hides documents.
        return None

    both = vocab.pair(alias, canonical)
    rate = both / alias_count if alias_count else 0.0
    cooccurs = both > 0 and rate >= thresholds.min_cooccurrence

    alias_neighbors = vocab.neighbors.get(alias, set())
    canonical_neighbors = vocab.neighbors.get(canonical, set())
    overlap = _jaccard(alias_neighbors - {canonical}, canonical_neighbors - {alias})

    # Uncorroborated spelling is **flagged, not dropped** — see
    # :attr:`DomainAliasCandidate.is_lexical_only`. Excluding it was measured
    # against a real corpus and removed exactly the candidates worth having:
    # the singletons (`budget-hunting`, `hunting-opportunities`) that do not
    # yet carry the canonical are the merges with something to gain, and being
    # singletons they have almost no neighbourhood to corroborate with. The
    # survivors were the redundant ones, where co-occurrence is trivially 1.0
    # because both tags are already on the document — a filter that admitted
    # only merges that change nothing.
    #
    # The `scavenger-hunt` case needs no gate: `{hunting}` is not a subset of
    # `{scavenger, hunt}`, so token-exact containment has already rejected it
    # above. Because containment is that selective, the surfaced set stays
    # small enough to actually review, and the honest move is to hand the
    # reviewer the weak ones labelled weak rather than decide for them.

    signals = tuple(
        name
        for name, fired in (
            (SIGNAL_LEXICAL, lexical),
            (SIGNAL_COOCCURRENCE, cooccurs),
        )
        if fired
    )
    # Tokens the alias carries beyond the canonical's. When one of them names
    # a *different* canonical, this is a cross-cutting subject rather than a
    # spelling, and either destination loses half of it.
    competing = tuple(
        sorted(
            {
                canonical_token_index[token]
                for token in alias_tokens - canonical_tokens
                if token in canonical_token_index
            }
        )
    )

    return DomainAliasCandidate(
        alias=alias,
        canonical=canonical,
        alias_documents=alias_count,
        canonical_documents=vocab.tag_documents[canonical],
        corpus_documents=vocab.documents,
        cooccurrence_documents=both,
        cooccurrence_rate=rate,
        neighbor_overlap=overlap,
        shared_tokens=tuple(sorted(alias_tokens & canonical_tokens)),
        signals=signals,
        competing_canonicals=competing,
        documents_gained=alias_count - both,
        candidate_id=compute_candidate_id(alias, canonical),
        example_item_ids=tuple(vocab.examples.get(alias, ())),
    )


def _strength(candidate: DomainAliasCandidate) -> tuple[int, float, float, int]:
    """Rank two destinations for the same alias. Corpus evidence outranks spelling."""
    return (
        len(candidate.signals),
        candidate.cooccurrence_rate,
        candidate.neighbor_overlap,
        candidate.canonical_documents,
    )


#: Attached when a merge rests on spelling with nothing in the corpus to back
#: it. Names the failure it resembles so a reviewer recognises the shape.
NOTE_LEXICAL_ONLY = (
    "spelling match only — these two tags never label the same document and "
    "keep different company. 'scavenger-hunt' vs 'hunting' is this shape: a "
    "shared token is not a shared subject"
)

NOTE_HARD_EXCLUDES = (
    "domain hard-excludes on mismatch: merging redirects every document "
    "carrying the alias, so a wrong merge hides them from queries scoped to "
    "either name — human approval required"
)

#: Attached when the alias names more than one canonical subject.
NOTE_COMPETING_CANONICAL = (
    "the alias also names another canonical subject, so it is cross-cutting "
    "rather than a spelling — merging it into one destination hides it from "
    "the other ('tax-planning' is both tax and planning). Consider leaving it "
    "unmerged, or splitting it into both tags"
)

NOTE_NO_GAIN = (
    "every document carrying the alias already carries the canonical — the "
    "merge tidies the vocabulary but changes nothing a query can see"
)


def _annotate(candidate: DomainAliasCandidate) -> DomainAliasCandidate:
    """Attach reviewer-facing findings. Never blocks."""
    notes = [NOTE_HARD_EXCLUDES]
    if candidate.competing_canonicals:
        notes.insert(0, NOTE_COMPETING_CANONICAL)
    if candidate.is_lexical_only:
        notes.insert(0, NOTE_LEXICAL_ONLY)
    if candidate.documents_gained == 0:
        notes.append(NOTE_NO_GAIN)
    return DomainAliasCandidate(
        **{
            **{
                f: getattr(candidate, f)
                for f in candidate.__slots__  # type: ignore[attr-defined]
                if f != "notes"
            },
            "notes": tuple(notes),
        }
    )


def _apply_cooldown(
    candidates: list[DomainAliasCandidate],
    *,
    prior: dict[str, Any],
    cooldown_days: int,
    now: datetime,
) -> list[DomainAliasCandidate]:
    """Drop candidates still inside their cooldown; stamp the survivors."""
    kept: list[DomainAliasCandidate] = []
    for candidate in candidates:
        blocked, until, recurrence = cooldown_blocks_emission(
            candidate_id=candidate.candidate_id,
            current_count=candidate.alias_documents,
            prior=prior.get(candidate.candidate_id),
            cooldown_days=cooldown_days,
            now=now,
            log_event="domain_normalization.candidate_suppressed_cooldown",
        )
        if blocked:
            continue
        kept.append(
            DomainAliasCandidate(
                **{
                    **{
                        f: getattr(candidate, f)
                        for f in candidate.__slots__  # type: ignore[attr-defined]
                        if f not in {"cooldown_until", "recurrence_count"}
                    },
                    "cooldown_until": until,
                    "recurrence_count": recurrence,
                }
            )
        )
    return kept


# ---------------------------------------------------------------------------
# Applying a normalization
# ---------------------------------------------------------------------------


def normalize_domain_tags(
    tags: Iterable[str], aliases: Mapping[str, str] | None
) -> list[str]:
    """Rewrite ``tags`` through an ``alias -> canonical`` map, deduplicated.

    Order-preserving on first appearance, so a caller comparing normalized
    output across runs gets a stable list. Unmapped tags pass through
    unchanged — the map is a merge list, never an allow-list, because
    dropping an unrecognised tag would silently delete vocabulary the map's
    author never considered.

    A one-step rewrite, deliberately: chained aliases (``a -> b -> c``) are
    not followed, so a map that accidentally contains a cycle terminates
    rather than hanging, and a reviewer's approval means what it says.
    """
    resolved = aliases or {}
    out: list[str] = []
    for tag in tags:
        canonical = resolved.get(tag, tag)
        if canonical not in out:
            out.append(canonical)
    return out


@dataclass(frozen=True)
class Normalization:
    """What a normalization changed, and how to undo exactly that.

    Carries the inserted pairs rather than re-deriving them, for the reason
    :class:`~trellis.learning.tag_evolution.Promotion` does: revoking by
    handing the candidates back would delete an operator's hand-written alias
    whenever a candidate happened to duplicate it.
    """

    #: The merged ``alias -> canonical`` map to write into ``config.yaml``.
    domain_aliases: dict[str, str]
    #: Aliases this normalization actually inserted.
    added: tuple[tuple[str, str], ...] = ()
    #: Aliases skipped — already mapped, to the same or a different canonical.
    skipped: tuple[tuple[str, str], ...] = ()


def apply_normalization(
    config_aliases: Mapping[str, str] | None,
    candidates: Iterable[DomainAliasCandidate],
) -> Normalization:
    """Merge approved candidates into an alias map.

    A **pure transform** — reads no file and writes none. The operator owns
    ``config.yaml``; this produces the block to put in it. That is what keeps
    the ``domain`` facet surface-only in practice and not merely in intent.

    An alias already present is never overwritten, even by a candidate
    proposing a different canonical: the operator's mapping is the authority,
    and silently redirecting it is precisely the bulk mis-tag this module
    exists to avoid.
    """
    merged = dict(config_aliases or {})
    added: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    for candidate in candidates:
        pair = (candidate.alias, candidate.canonical)
        if candidate.alias in merged:
            logger.info(
                "domain_normalization.alias_already_mapped",
                alias=candidate.alias,
                existing=merged[candidate.alias],
                proposed=candidate.canonical,
            )
            skipped.append(pair)
            continue
        merged[candidate.alias] = candidate.canonical
        added.append(pair)

    return Normalization(
        domain_aliases=merged, added=tuple(added), skipped=tuple(skipped)
    )


def revoke_normalization(
    config_aliases: Mapping[str, str] | None,
    normalization: Normalization,
) -> dict[str, str]:
    """Undo exactly the aliases ``normalization`` inserted.

    The exact inverse of :func:`apply_normalization`. A merge that turns out
    to hide documents has to be removable without an archaeology session, and
    it must never remove more than it added — the map it edits also holds
    hand-written operator vocabulary. Revoking an alias that is no longer
    present, or one an operator has since repointed, is a no-op.
    """
    revoked = dict(config_aliases or {})
    for alias, canonical in normalization.added:
        if revoked.get(alias) == canonical:
            del revoked[alias]
    return revoked


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SCAN_LIMIT",
    "NOTE_COMPETING_CANONICAL",
    "NOTE_HARD_EXCLUDES",
    "NOTE_LEXICAL_ONLY",
    "NOTE_NO_GAIN",
    "PARAM_COMPONENT_ID",
    "RECOMMENDED_SEED_VALUES",
    "REQUIRED_PARAM_KEYS",
    "SIGNAL_COOCCURRENCE",
    "SIGNAL_LEXICAL",
    "DomainAliasCandidate",
    "Normalization",
    "analyze_domain_alias_candidates",
    "apply_normalization",
    "compute_candidate_id",
    "normalize_domain_tags",
    "revoke_normalization",
    "tag_tokens",
]
