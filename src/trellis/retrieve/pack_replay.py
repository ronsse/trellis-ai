"""Counterfactual replay of pack policy over a window that already happened.

``trellis analyze value`` (#353) measures what the served packs delivered.
This module answers the next question — *what would a different serving
policy have delivered on the same packs?* — and it exists because the
obvious way to answer it is wrong.

**The trap.** A trimming change only affects future packs, so a naive
before/after compares two different populations of packs, graded by
different callers on different work, and attributes the whole difference
to the change. Everything about a window can move except the policy.

**The replay.** Take the same ``PACK_ASSEMBLED`` events, the same
``FEEDBACK_RECORDED`` citations, and re-run the budget arithmetic under a
different policy. Same packs, same verdicts, different rules. The delta is
then attributable, because nothing else varied.

What makes this possible is that ``PACK_ASSEMBLED`` already carries the
whole walk: ``budget_trace[]`` lists every candidate that reached the token
budget *including the ones it rejected*, with the tokens each was charged.
So the counterfactual is not a model of the walk, it is the walk, re-run.

Four limits, none of them hideable:

1. **Excerpt text is not in the event.** Only ``estimated_tokens``, which
   is ``len(excerpt) // 4 + 1`` — invertible to within four characters.
   Every character count here is that inversion, so a replayed saving is
   accurate to ±1 token per item and not to the character.
2. **A newly-admitted item has no verdict.** When a policy makes items
   cheaper and the greedy refills, it admits items the original pack never
   served, which therefore nobody graded. They enter the denominator as
   unjudged and can only push the fraction down.
   :attr:`ReplayReport.admitted_ungraded_items` reports how many, so a
   fraction that fell because of them is not read as a fraction that fell
   because of the policy.
3. **A pointer is not a body.** When graduated disclosure demotes an item
   the caller cited helpful, this counts *zero* helpful tokens for it —
   the id was served, the substance was not. That makes the replayed
   fraction a lower bound on the policy, and it makes
   :attr:`ReplayReport.helpful_bodies_withheld` the number to read against
   the saving: it is the recall the policy costs.
4. **Rank order is held fixed.** A width policy changes what the content
   floor measures and could in principle reorder a pack. The replay does
   not model that; it re-prices and re-admits, it does not re-rank.

The verdict join is :func:`~trellis.retrieve.pack_value.collect_pack_verdicts`
and the event join is
:func:`~trellis.learning.pack_observations.join_pack_feedback` — the same
two functions ``analyze value`` uses, so the baseline this module prints
cannot drift from the one that command prints.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.learning.pack_observations import join_pack_feedback
from trellis.retrieve.class_prior import (
    DEFAULT_SCOPE,
    PARENTS_AND_CHUNKS_SCOPE,
    ClassPriorConfig,
)
from trellis.retrieve.concentration import parent_id_of
from trellis.retrieve.disclosure import POINTER_LABEL_MAX_CHARS
from trellis.retrieve.excerpts import EXCERPT_MAX_CHARS
from trellis.retrieve.pack_value import MIN_ATTRIBUTED_PACKS, collect_pack_verdicts
from trellis.retrieve.strategies import GRAPH_STRATEGY_NAME
from trellis.schemas.document_metadata import document_form_of

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_DEFAULT_EVENT_LIMIT = 5000

#: Fixed characters a pointer spends beyond its label — the ellipsis, the
#: withheld-size note and the fetch hint, at the widest plausible size
#: rendering. Kept as a constant rather than re-deriving
#: :func:`~trellis.retrieve.disclosure.pointer_excerpt` because the replay
#: has no excerpt text to derive a label from (limit 1 above).
_POINTER_SCAFFOLD_CHARS = len(
    " … [+999.9k chars withheld — get_items fetches the source]"
)


def _chars_from_tokens(tokens: int) -> int:
    """Invert ``estimate_tokens``: ``len // 4 + 1`` back to a length.

    Returns the midpoint of the four-character bucket a token count came
    from, so the round trip is exact for the cap-bound items that dominate
    the corpus and off by at most two characters for the rest.
    """
    return max(0, tokens - 1) * 4 + 2


def _tokens_from_chars(chars: int) -> int:
    return chars // 4 + 1


@dataclass(frozen=True)
class ReplayPolicy:
    """A serving policy to price the window against.

    ``excerpt_max_chars``
        Uniform excerpt cap, the width lever. ``None`` leaves widths as
        served.
    ``body_items``
        Graduated-disclosure cut — items past this rank are priced as
        pointers. ``None`` disables graduation.
    ``max_items``
        Hard item ceiling. Items past it are *dropped*, not demoted, which
        is why :attr:`ReplayReport.helpful_items_dropped` is reported
        separately from ``helpful_bodies_withheld``.
    ``refill``
        Whether the greedy walk re-fills budget the policy freed. ``True``
        is what the shipped walk does and is therefore the honest default:
        a policy that makes items cheaper without an item ceiling does not
        produce a cheaper pack, it produces a wider one. Set ``False`` only
        to isolate the pricing effect from the admission effect.
    """

    excerpt_max_chars: int | None = None
    body_items: int | None = None
    max_items: int | None = None
    refill: bool = True

    def __post_init__(self) -> None:
        if self.excerpt_max_chars is not None and self.excerpt_max_chars < 1:
            msg = f"excerpt_max_chars must be >= 1; got {self.excerpt_max_chars!r}"
            raise ValueError(msg)
        if self.body_items is not None and self.body_items < 1:
            msg = f"body_items must be >= 1; got {self.body_items!r}"
            raise ValueError(msg)
        if self.max_items is not None and self.max_items < 1:
            msg = f"max_items must be >= 1; got {self.max_items!r}"
            raise ValueError(msg)

    def describe(self) -> str:
        """One-line rendering, for the report and the CLI header."""
        parts: list[str] = []
        if self.excerpt_max_chars is not None:
            parts.append(f"excerpt_max_chars={self.excerpt_max_chars}")
        if self.body_items is not None:
            parts.append(f"body_items={self.body_items}")
        if self.max_items is not None:
            parts.append(f"max_items={self.max_items}")
        parts.append(f"refill={'on' if self.refill else 'off'}")
        return ", ".join(parts)


class ReplayArm(TrellisModel):
    """One priced arm of the replay — the window as served, or as it would be."""

    label: str = ""
    items: int = 0
    injected_tokens: int = 0
    helpful_tokens: int = 0
    unhelpful_tokens: int = 0
    unjudged_tokens: int = 0
    useful_token_fraction: float | None = None
    #: Items served with a full excerpt (equals ``items`` on the baseline
    #: arm and whenever graduation is off).
    body_items_served: int = 0
    #: Items served as a one-line pointer instead of an excerpt.
    pointer_items_served: int = 0


class ReplayReport(TrellisModel):
    """Baseline and counterfactual for one window, plus what the change costs.

    Read :attr:`counterfactual` only next to :attr:`helpful_bodies_withheld`
    and :attr:`helpful_items_dropped`. A policy can always raise the
    fraction by serving less; those two say what serving less lost.
    """

    window_days: int
    policy: str = ""
    #: **The ``n``.** Distinct flat packs that both joined a feedback event
    #: and were cited. Every ratio below is computed over exactly these.
    attributed_packs: int = 0
    min_attributed_packs: int = MIN_ATTRIBUTED_PACKS
    suppressed: bool = False
    suppressed_reason: str = ""

    baseline: ReplayArm = Field(default_factory=ReplayArm)
    counterfactual: ReplayArm = Field(default_factory=ReplayArm)

    #: ``counterfactual.injected_tokens / baseline.injected_tokens - 1``.
    token_delta: float | None = None
    #: Relative change in ``useful_token_fraction``. ``None`` when either
    #: arm is suppressed.
    fraction_delta: float | None = None

    # -- what the change cost ------------------------------------------
    #: Cited-helpful *servings* whose body the policy replaced with a
    #: pointer. The id stayed addressable; the substance did not arrive
    #: unfetched.
    helpful_bodies_withheld: int = 0
    #: Cited-helpful servings the policy removed from their pack entirely.
    helpful_items_dropped: int = 0
    #: Cited-helpful ``(pack, item)`` servings in the window — the
    #: denominator for both counts above.
    #:
    #: **Servings, not distinct items**, deliberately. One memory can be
    #: cited helpful in two packs and sit at rank 4 in one and rank 16 in
    #: the other; counting distinct ids would let the pack that served its
    #: body mask the pack that withheld it, and the cost column would read
    #: zero for a policy that had really withheld a body from a caller who
    #: wanted it. The question this answers is per delivery: in the pack
    #: that served it, did the caller get the substance?
    helpful_items_total: int = 0
    #: Items the counterfactual admitted that the original pack never
    #: served, and which therefore carry no verdict. They enter the
    #: denominator as unjudged and can only lower the fraction.
    admitted_ungraded_items: int = 0

    #: Packs the replay could not price — no ``budget_trace``, so the walk
    #: cannot be re-run. Excluded from both arms.
    packs_without_budget_trace: int = 0

    notes: list[str] = Field(default_factory=list)

    estimator: str = "estimate_4_chars_per_token"


def _pointer_chars(title: str | None) -> int:
    """Characters a pointer for an item with this title would occupy.

    An item with no recorded ``title`` falls back to the first clause of
    its excerpt, which fills the label budget for any excerpt longer than
    it — the overwhelming majority (the corpus median excerpt is 432
    characters). Assuming a full label there is therefore accurate rather
    than merely conservative, and where it is not, it over-states the
    pointer's cost and under-states the saving.
    """
    label = (
        min(len(title.strip()), POINTER_LABEL_MAX_CHARS)
        if isinstance(title, str) and title.strip()
        else POINTER_LABEL_MAX_CHARS
    )
    return label + _POINTER_SCAFFOLD_CHARS


@dataclass(frozen=True)
class _Candidate:
    item_id: str
    #: What the pack paid for the item: ``budget_trace[].item_tokens``, which
    #: graduated disclosure re-prices to the pointer after the walk.
    served_tokens: int
    #: What the walk charged it (``budget_trace[].walk_tokens`` when the
    #: builder recorded one, else ``item_tokens``). Every counterfactual that
    #: re-runs the walk sums this, because a ``max_items``-cut candidate is
    #: priced at the walk's charge too and the two must not be mixed.
    walk_tokens: int
    included: bool
    verdict: str
    title: str | None


def _candidates(
    payload: Mapping[str, Any], verdicts: tuple[set[str], set[str]]
) -> list[_Candidate] | str:
    """Every candidate that reached the token walk, in walk order.

    The ``_UNMODELLED_REASONS`` tag instead when the walk cannot be re-run
    from the event: ``no_budget_trace`` when it recorded none, or
    ``trace_unreadable`` when a step's token charge is missing, fractional,
    ``nan`` or ``inf``. A corrupt charge refuses the pack rather than
    aborting the replay (``int()`` of ``inf`` raises) or being replayed as a
    different number.
    """
    trace = payload.get("budget_trace")
    if not isinstance(trace, list) or not trace:
        return "no_budget_trace"
    helpful, unhelpful = verdicts
    meta = {
        raw["item_id"]: raw
        for raw in (payload.get("injected_items") or [])
        if isinstance(raw, Mapping) and raw.get("item_id")
    }
    out: list[_Candidate] = []
    for step in trace:
        if not isinstance(step, Mapping):
            continue
        item_id = step.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        tokens = _int_or_none(step.get("item_tokens"))
        # ``walk_tokens`` is absent unless the builder re-priced the step, and
        # then the walk charged ``item_tokens``; present-but-unreadable is
        # refused, not read as absent.
        raw_walk = step.get("walk_tokens")
        walk = tokens if raw_walk is None else _int_or_none(raw_walk)
        if tokens is None or walk is None:
            return "trace_unreadable"
        title = meta.get(item_id, {}).get("title")
        out.append(
            _Candidate(
                item_id=item_id,
                served_tokens=tokens,
                walk_tokens=walk,
                included=bool(step.get("included")),
                verdict=(
                    "helpful"
                    if item_id in helpful
                    else "unhelpful"
                    if item_id in unhelpful
                    else "unjudged"
                ),
                title=title if isinstance(title, str) else None,
            )
        )
    return out


def _price(
    candidates: list[_Candidate],
    *,
    max_tokens: int,
    policy: ReplayPolicy,
) -> tuple[list[tuple[_Candidate, int, bool]], int]:
    """Re-run the walk under ``policy``.

    Returns ``(served, admitted_ungraded)`` where each served entry is
    ``(candidate, tokens_charged, is_pointer)``.
    """
    pool = candidates
    if policy.max_items is not None:
        pool = pool[: policy.max_items]

    def body_tokens(candidate: _Candidate) -> int:
        chars = _chars_from_tokens(candidate.walk_tokens)
        if policy.excerpt_max_chars is not None:
            chars = min(chars, policy.excerpt_max_chars)
        return _tokens_from_chars(chars)

    # Admission. With refill on, the greedy re-runs against the new prices
    # and takes whatever fits; with it off, the pack keeps exactly the
    # items it originally served, so the pricing effect is isolated.
    admitted: list[_Candidate] = []
    if policy.refill:
        total = 0
        for candidate in pool:
            tokens = body_tokens(candidate)
            if total + tokens > max_tokens:
                break
            total += tokens
            admitted.append(candidate)
    else:
        admitted = [candidate for candidate in pool if candidate.included]

    # Graduation is priced after admission, exactly as the builder applies
    # it after the walk — see :mod:`trellis.retrieve.disclosure`.
    served: list[tuple[_Candidate, int, bool]] = []
    for index, candidate in enumerate(admitted):
        tokens = body_tokens(candidate)
        if policy.body_items is not None and index >= policy.body_items:
            pointer = _tokens_from_chars(_pointer_chars(candidate.title))
            if pointer < tokens:
                served.append((candidate, pointer, True))
                continue
        served.append((candidate, tokens, False))

    admitted_ungraded = sum(1 for c, _, _ in served if not c.included)
    return served, admitted_ungraded


def _arm(label: str, served: list[tuple[_Candidate, int, bool]]) -> ReplayArm:
    injected = sum(tokens for _, tokens, _ in served)
    # A pointer delivers an id, not substance: a cited-helpful item served
    # as a pointer contributes nothing to the numerator. That makes the
    # arm a lower bound on the policy rather than a flattering estimate.
    helpful = sum(
        tokens
        for candidate, tokens, is_pointer in served
        if candidate.verdict == "helpful" and not is_pointer
    )
    unhelpful = sum(
        tokens
        for candidate, tokens, is_pointer in served
        if candidate.verdict == "unhelpful" and not is_pointer
    )
    return ReplayArm(
        label=label,
        items=len(served),
        injected_tokens=injected,
        helpful_tokens=helpful,
        unhelpful_tokens=unhelpful,
        unjudged_tokens=injected - helpful - unhelpful,
        useful_token_fraction=(round(helpful / injected, 4) if injected else None),
        body_items_served=sum(1 for _, _, is_pointer in served if not is_pointer),
        pointer_items_served=sum(1 for _, _, is_pointer in served if is_pointer),
    )


def replay_pack_value(
    event_log: EventLog,
    *,
    policy: ReplayPolicy,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> ReplayReport:
    """Price one window as served and as ``policy`` would have served it.

    Args:
        event_log: Operational log holding ``PACK_ASSEMBLED`` and
            ``FEEDBACK_RECORDED``.
        policy: The counterfactual serving policy.
        days: Look-back window, matching ``trellis analyze value``.
        limit: Per-event-type scan limit.

    Returns:
        A :class:`ReplayReport`. Ratios are ``None`` — never ``0.0`` — when
        the attributed sample is below
        :data:`~trellis.retrieve.pack_value.MIN_ATTRIBUTED_PACKS`, so a
        refused ratio cannot be read as a measured zero.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)
    feedback_events, pack_payloads, _ = join_pack_feedback(
        event_log, since=since, limit=limit
    )
    flat_packs = {
        pack_id: payload
        for pack_id, payload in pack_payloads.items()
        if payload.get("injected_items")
    }
    verdicts = collect_pack_verdicts(feedback_events, flat_packs)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    attributed = sorted(set(helpful_by_pack) | set(unhelpful_by_pack))

    baseline_served: list[tuple[_Candidate, int, bool]] = []
    counter_served: list[tuple[_Candidate, int, bool]] = []
    admitted_ungraded = 0
    no_trace = 0
    # Keyed by ``(pack_id, item_id)`` — see ``helpful_items_total``.
    helpful_servings: set[tuple[str, str]] = set()
    counter_body_servings: set[tuple[str, str]] = set()
    counter_served_servings: set[tuple[str, str]] = set()

    for pack_id in attributed:
        payload = flat_packs[pack_id]
        pack_verdicts = (
            helpful_by_pack.get(pack_id, set()),
            unhelpful_by_pack.get(pack_id, set()),
        )
        candidates = _candidates(payload, pack_verdicts)
        if isinstance(candidates, str):
            no_trace += 1
            continue
        # A refilling policy walks the recorded budget, so a pack whose budget
        # is absent or unreadable is refused like one with no trace. Walking 0
        # instead would empty the pack and read as a policy that serves
        # nothing. Without refill the budget is never consulted.
        max_tokens = _walk_budget(payload)
        if max_tokens is None and policy.refill:
            no_trace += 1
            continue
        helpful_servings |= {
            (pack_id, c.item_id)
            for c in candidates
            if c.included and c.verdict == "helpful"
        }
        baseline_served.extend(
            (c, c.served_tokens, False) for c in candidates if c.included
        )
        served, ungraded = _price(candidates, max_tokens=max_tokens or 0, policy=policy)
        counter_served.extend(served)
        admitted_ungraded += ungraded
        counter_served_servings |= {(pack_id, c.item_id) for c, _, _ in served}
        counter_body_servings |= {
            (pack_id, c.item_id) for c, _, is_pointer in served if not is_pointer
        }

    attributed_packs = len(attributed) - no_trace
    thin = attributed_packs < MIN_ATTRIBUTED_PACKS
    baseline = _arm("as served", baseline_served)
    counterfactual = _arm(policy.describe(), counter_served)
    if thin:
        baseline.useful_token_fraction = None
        counterfactual.useful_token_fraction = None

    withheld = len((helpful_servings & counter_served_servings) - counter_body_servings)
    dropped = len(helpful_servings - counter_served_servings)

    report = ReplayReport(
        window_days=days,
        policy=policy.describe(),
        attributed_packs=attributed_packs,
        suppressed=thin,
        suppressed_reason="below_min_attributed_packs" if thin else "",
        baseline=baseline,
        counterfactual=counterfactual,
        token_delta=(
            round(counterfactual.injected_tokens / baseline.injected_tokens - 1.0, 4)
            if baseline.injected_tokens
            else None
        ),
        fraction_delta=(
            round(
                counterfactual.useful_token_fraction / baseline.useful_token_fraction
                - 1.0,
                4,
            )
            if baseline.useful_token_fraction and counterfactual.useful_token_fraction
            else None
        ),
        helpful_bodies_withheld=withheld,
        helpful_items_dropped=dropped,
        helpful_items_total=len(helpful_servings),
        admitted_ungraded_items=admitted_ungraded,
        packs_without_budget_trace=no_trace,
        notes=_notes(
            thin=thin,
            attributed_packs=attributed_packs,
            admitted_ungraded=admitted_ungraded,
            withheld=withheld,
            dropped=dropped,
            no_trace=no_trace,
        ),
    )
    logger.debug(
        "pack_value_replayed",
        window_days=days,
        policy=policy.describe(),
        attributed_packs=attributed_packs,
        token_delta=report.token_delta,
        fraction_delta=report.fraction_delta,
    )
    return report


def _notes(
    *,
    thin: bool,
    attributed_packs: int,
    admitted_ungraded: int,
    withheld: int,
    dropped: int,
    no_trace: int,
) -> list[str]:
    """Caveats that travel with the numbers, in both output formats."""
    notes = [
        (
            "Counterfactual over the SAME packs and the SAME citations: only "
            "the serving policy differs. Excerpt lengths are inverted from "
            "estimated_tokens (~4 chars/token), so per-item costs are accurate "
            "to about one token."
        ),
    ]
    if thin:
        notes.append(
            f"Ratios suppressed: {attributed_packs} attributed pack(s) is "
            f"below the {MIN_ATTRIBUTED_PACKS}-pack minimum."
        )
    if admitted_ungraded:
        notes.append(
            f"{admitted_ungraded} item(s) the counterfactual admitted were "
            "never served originally, so nobody graded them; they enter as "
            "unjudged and can only lower the counterfactual fraction."
        )
    if withheld:
        notes.append(
            f"{withheld} cited-helpful serving(s) were a pointer "
            "rather than a body. Their id stayed addressable via get_items; "
            "their substance did not arrive unfetched, and they contribute "
            "zero helpful tokens here."
        )
    if dropped:
        notes.append(
            f"{dropped} cited-helpful serving(s) were removed from the pack "
            "entirely by the item ceiling — not demoted, dropped."
        )
    if no_trace:
        notes.append(
            f"{no_trace} attributed pack(s) carry no usable budget_trace or "
            "token budget and could not be re-priced; they are excluded from "
            "both arms."
        )
    return notes


class ClassPriorReplayReport(TrellisModel):
    """What :mod:`trellis.retrieve.class_prior` would have cost or saved,
    replayed over packs already served.

    Unlike :class:`ReplayReport`, this never re-runs the token-budget walk.
    ``PACK_ASSEMBLED.budget_trace[]`` records a tokens-and-``included``
    triple for *every* candidate the walk saw, served or not, but only a
    served item's ``injected_items[]`` row carries the ``document_form``
    and ``strategy_source`` this rule matches on — a candidate the walk
    rejected on tokens alone leaves no trace of either. So the population
    here is servings the original pack actually served, counted per
    ``(pack_id, item_id)``, the same convention
    :attr:`ReplayReport.helpful_items_total` uses; see ``notes`` for what
    that forecloses.
    """

    window_days: int
    #: ``mode=... forms=... axes=...``, from the
    #: :class:`~trellis.retrieve.class_prior.ClassPriorConfig` replayed.
    applied_config: str = ""
    attributed_packs: int = 0
    #: Servings whose ``document_form`` and axis (``strategy_source``) both
    #: matched ``config`` — the population every count below is drawn from,
    #: including the ``still_served_by_other_axis`` subset that withhold
    #: would not actually remove.
    matched_servings: int = 0
    #: Matched servings cited helpful whose id carries no dedup copy on an
    #: axis outside ``config.axes`` — these would no longer be served.
    #: Excludes ``still_served_by_other_axis``.
    helpful_lost: int = 0
    #: Matched servings cited unhelpful under the same exclusion — the
    #: removals the rule is for. Excludes ``still_served_by_other_axis``.
    unhelpful_removed: int = 0
    #: The gross token cost of every matched serving counted in
    #: ``helpful_lost`` or ``unhelpful_removed``, summed. Not a repriced
    #: pack — see ``notes``. A serving in ``still_served_by_other_axis``
    #: contributes nothing here: withhold frees none of its tokens.
    tokens_freed: int = 0
    #: Matched servings for which the off pack's own ``rejected_items`` has
    #: a dedup row for a copy of the same id on an axis outside
    #: ``config.axes`` (the shape :mod:`trellis.retrieve.withholding` calls
    #: a non-absence). The gate is axis-scoped: withholding the
    #: configured-axis copy lets that other copy win dedup and may still
    #: serve the id, so none of these count as removed, as a token saving,
    #: or as a helpful/unhelpful loss. Under budget pressure that other
    #: copy can still miss the cut, so this count is an upper bound on
    #: survivals, not a tally of them.
    still_served_by_other_axis: int = 0
    #: How many ``matched_servings`` matched only through their *parent's*
    #: form (``scope="parents_and_chunks"``): the chunk's own row carried no
    #: ``document_form``. Zero under the default scope.
    matched_via_parent: int = 0
    #: Servings on a configured axis with no ``document_form`` of their own
    #: whose id is a chunk id, looked up in the document store to find a
    #: parent form — every serving the chunk scope had to resolve. A
    #: non-chunk id is never looked up (serving does not resolve one through
    #: a parent either). Zero under the default scope, which never looks
    #: anything up.
    parent_lookups: int = 0
    #: Lookups that reached a parent carrying a ``document_form`` (any form,
    #: whether or not it is in ``config.forms``).
    parent_form_resolved: int = 0
    #: Lookups that could not be answered: the served item's row, or its
    #: parent's, is no longer in the store, the store raised, or no store was
    #: supplied. Not matched; ``matched_servings`` is a floor by this much.
    #: The remainder of ``parent_lookups`` reached a parent with no form.
    parent_form_unresolved: int = 0

    notes: list[str] = Field(default_factory=list)


def _is_chunk_id(item_id: str) -> bool:
    """Whether ``item_id`` is a ``<parent>#chunk-N`` row id.

    Deferred import, as in
    :func:`trellis.retrieve.concentration.parent_id_of`: ``trellis.ingest_corpus``
    imports back into ``trellis.retrieve``.
    """
    from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

    return is_chunk_doc_id(item_id)


class _ParentFormLookup:
    """Resolve a served chunk's parent form through the document store.

    ``PACK_ASSEMBLED.injected_items[]`` rows carry the item's own
    ``document_form`` but never its parent's, so a historical chunk serving
    has to be looked up: the stored row's parent id, then that parent row's
    ``document_form``. The parent id is read as serving reads it
    (:func:`trellis.retrieve.concentration.parent_id_of`, behind
    :func:`trellis.retrieve.parent_verdicts.parent_document_form`): the row's
    ``parent_doc_id``, else the chunk id's own prefix. The caller looks up
    only chunk ids, the same guard serving's cache applies. One ``get`` per
    distinct item and one per distinct parent, memoized, so a chunk served in
    forty packs costs the same as one served once. A store that raises is
    caught whole, as the serve-time cache does: one unreadable row must not
    abort a measurement, and the miss is counted as unresolved rather than
    hidden.
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    NO_FORM = "no_form"

    def __init__(self, document_store: DocumentStore | None) -> None:
        self._store = document_store
        self._by_item: dict[str, tuple[str, str | None]] = {}
        self._parent_form: dict[str, str | None] = {}

    def lookup(self, item_id: str) -> tuple[str, str | None]:
        """``(outcome, parent_form)`` for one served chunk id."""
        if self._store is None:
            return self.UNRESOLVED, None
        if item_id not in self._by_item:
            try:
                self._by_item[item_id] = self._resolve(self._store, item_id)
            except Exception:
                logger.warning("class_prior_replay_parent_lookup_failed")
                self._by_item[item_id] = (self.UNRESOLVED, None)
        return self._by_item[item_id]

    def _resolve(self, store: DocumentStore, item_id: str) -> tuple[str, str | None]:
        doc = store.get(item_id)
        if doc is None:
            return self.UNRESOLVED, None
        metadata = doc.get("metadata")
        parent_id = parent_id_of(
            item_id, metadata if isinstance(metadata, dict) else None
        )
        if parent_id == item_id:
            return self.NO_FORM, None
        if parent_id not in self._parent_form:
            parent = store.get(parent_id)
            if parent is None:
                return self.UNRESOLVED, None
            parent_meta = parent.get("metadata")
            self._parent_form[parent_id] = document_form_of(
                parent_meta if isinstance(parent_meta, dict) else None
            )
        form = self._parent_form[parent_id]
        if form is None:
            return self.NO_FORM, None
        return self.RESOLVED, form


def _class_prior_notes(
    *,
    matched_servings: int,
    mode_is_off: bool,
    saw_document_form: bool,
    still_served_by_other_axis: int,
    chunk_scope: bool = False,
    has_document_store: bool = True,
    matched_via_parent: int = 0,
    parent_lookups: int = 0,
    parent_form_resolved: int = 0,
    parent_form_unresolved: int = 0,
) -> list[str]:
    """Caveats that travel with the class-prior counts."""
    if mode_is_off:
        return [
            (
                'mode is "off": the counterfactual is identical to the '
                "baseline by construction, so every count above is zero."
            ),
        ]
    notes = [
        (
            "Counterfactual over the SAME packs and the SAME citations. "
            "This arm classifies and counts already-served items; it does "
            "not re-run the token-budget walk, so tokens_freed is the "
            "gross cost of the matched servings, not a repriced pack. It "
            "cannot say which item the freed budget would have admitted "
            "instead, and it cannot grade one, because that item was "
            "never served and nobody rated it."
        ),
    ]
    if matched_servings == 0:
        if not saw_document_form:
            notes.append(
                "No served item in the window carries a document_form at "
                "all, so every count above is zero because there is no "
                "history yet for this rule to grade, not because the rule "
                "found nothing to do."
            )
        else:
            notes.append(
                "No served item in the window matched config.forms on an "
                "axis in config.axes, so every count above is zero because "
                "nothing was eligible, not because the rule found nothing "
                "to do."
            )
    if still_served_by_other_axis:
        notes.append(
            f"{still_served_by_other_axis} matched serving(s) are not "
            "counted as removed, as freed tokens, or as a helpful/unhelpful "
            "loss: the gate is axis-scoped, and the off pack's own "
            "rejected_items has a dedup row for a copy of the same id on "
            "an axis outside config.axes, which withhold may still serve. "
            "Under budget pressure that copy can miss the cut, so this "
            "count is an upper bound on survivals, not a tally of them. "
            "See still_served_by_other_axis."
        )
    if chunk_scope:
        if not has_document_store:
            notes.append(
                'scope is "parents_and_chunks" but no document store was '
                "supplied, so no chunk's parent could be looked up: "
                f"{parent_form_unresolved} chunk serving(s) with no form of "
                "their own were left unresolved and counted as not matched. "
                "The counts above are parents only."
            )
        else:
            notes.append(
                'scope is "parents_and_chunks". Historical PACK_ASSEMBLED '
                "rows carry no chunk form, so each chunk-id serving on a "
                "configured axis with no document_form of its own was "
                "resolved through the document store: the stored row's "
                "parent_doc_id (else the chunk id's own prefix, as serving "
                "reads it), then that parent's document_form. This reads the "
                "store as it is now, not as it was at serving time. "
                f"{parent_lookups} serving(s) were looked up; "
                f"{parent_form_resolved} reached a parent with a form and "
                f"{parent_form_unresolved} could not be resolved (row or "
                "parent no longer stored, or a store error), counted as not "
                f"matched; {matched_via_parent} of the matched servings "
                "matched only through their parent. The rest of the lookups "
                "reached a parent with no form."
            )
    return notes


def _dedup_copy_outside_axes(
    item_id: str,
    rejected_rows: list[Mapping[str, Any]],
    axes: frozenset[str] | set[str],
) -> bool:
    """``True`` when the pack's own dedup shows another axis's copy of
    ``item_id`` losing to the copy on one of ``axes``.

    Withholding the ``axes`` copy removes it from dedup's contest, so that
    losing copy — on an axis outside ``axes`` — would win instead and the id
    would still be served. A gate scoped to some axes therefore cannot count
    such an id as removed (see ``trellis.retrieve.withholding``'s
    ``NON_ABSENCE_REASONS`` treatment of the same ``dedup`` row): only a
    ``dedup`` rejection on an axis the gate does not touch means the id
    survives; any other reason, or a loser inside ``axes``, says nothing
    about what withhold would do.
    """
    return any(
        r.get("item_id") == item_id
        and r.get("reason") == "dedup"
        and isinstance(r.get("strategy_source"), str)
        and r["strategy_source"] not in axes
        for r in rejected_rows
    )


@dataclass
class _ClassPriorTally:
    """Running counts for one :func:`replay_class_prior` pass."""

    matched_servings: int = 0
    helpful_lost: int = 0
    unhelpful_removed: int = 0
    tokens_freed: int = 0
    still_served_by_other_axis: int = 0
    matched_via_parent: int = 0
    parent_lookups: int = 0
    parent_form_resolved: int = 0
    parent_form_unresolved: int = 0
    saw_document_form: bool = False


def _tally_class_prior_pack(
    rows: list[Mapping[str, Any]],
    *,
    rejected_rows: list[Mapping[str, Any]],
    helpful: set[str],
    unhelpful: set[str],
    config: ClassPriorConfig,
    lookup: _ParentFormLookup,
    tally: _ClassPriorTally,
) -> None:
    """Fold one attributed pack's served rows into ``tally``.

    A row is judged under its own ``document_form``; only when it has none,
    ``config.scope`` is ``"parents_and_chunks"``, the row is on a configured
    axis and its id is a chunk id is its parent's form looked up (see
    :class:`_ParentFormLookup`). The default scope never reaches ``lookup``.
    """
    chunk_scope = config.scope == PARENTS_AND_CHUNKS_SCOPE
    for row in rows:
        item_id = row["item_id"]
        form = row.get("document_form")
        axis = row.get("strategy_source")
        on_axis = isinstance(axis, str) and axis in config.axes
        via_parent = False
        if isinstance(form, str):
            tally.saw_document_form = True
        elif chunk_scope and on_axis and _is_chunk_id(item_id):
            # Only a chunk id resolves through a parent, whatever axis or
            # item_type served it (the semantic axis writes "vector" rows):
            # serving's rule, so replay prices what serving would withhold.
            tally.parent_lookups += 1
            outcome, parent_form = lookup.lookup(item_id)
            if outcome == _ParentFormLookup.RESOLVED:
                tally.parent_form_resolved += 1
                tally.saw_document_form = True
                form, via_parent = parent_form, True
            elif outcome == _ParentFormLookup.UNRESOLVED:
                tally.parent_form_unresolved += 1
        if not (on_axis and isinstance(form, str) and form in config.forms):
            continue
        tally.matched_servings += 1
        if via_parent:
            tally.matched_via_parent += 1
        if _dedup_copy_outside_axes(item_id, rejected_rows, config.axes):
            tally.still_served_by_other_axis += 1
            continue
        raw_tokens = row.get("estimated_tokens")
        if isinstance(raw_tokens, int | float):
            tally.tokens_freed += int(raw_tokens)
        if item_id in helpful:
            tally.helpful_lost += 1
        elif item_id in unhelpful:
            tally.unhelpful_removed += 1


def replay_class_prior(
    event_log: EventLog,
    *,
    config: ClassPriorConfig,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
    document_store: DocumentStore | None = None,
) -> ClassPriorReplayReport:
    """Price :mod:`trellis.retrieve.class_prior` over a window already served.

    Args:
        event_log: Operational log holding ``PACK_ASSEMBLED`` and
            ``FEEDBACK_RECORDED``.
        config: The :class:`~trellis.retrieve.class_prior.ClassPriorConfig`
            to replay — typically
            :func:`~trellis.retrieve.class_prior.resolve_class_prior_config`'s
            result, so the arm matches whatever the gate would actually do.
        days: Look-back window, matching ``trellis analyze value``.
        limit: Per-event-type scan limit.
        document_store: Read only under ``config.scope ==
            "parents_and_chunks"``, to find the parent form of a served
            item whose row carries none (historical events record no chunk
            form). Without one, those servings are counted as unresolved.

    Returns:
        A :class:`ClassPriorReplayReport`. See its docstring and ``notes``
        for what this arm cannot measure.
    """
    mode_is_off = config.mode == "off" or not config.forms or not config.axes
    chunk_scope = config.scope == PARENTS_AND_CHUNKS_SCOPE
    applied_config = (
        f"mode={config.mode} forms={sorted(config.forms)} axes={sorted(config.axes)}"
    )
    if config.scope != DEFAULT_SCOPE:
        applied_config += f" scope={config.scope}"
    parent_lookup = _ParentFormLookup(document_store)
    since = datetime.now(tz=UTC) - timedelta(days=days)
    feedback_events, pack_payloads, _ = join_pack_feedback(
        event_log, since=since, limit=limit
    )
    flat_packs = {
        pack_id: payload
        for pack_id, payload in pack_payloads.items()
        if payload.get("injected_items")
    }
    verdicts = collect_pack_verdicts(feedback_events, flat_packs)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    attributed = sorted(set(helpful_by_pack) | set(unhelpful_by_pack))

    tally = _ClassPriorTally()
    if not mode_is_off:
        for pack_id in attributed:
            payload = flat_packs[pack_id]
            rows = [
                row
                for row in (payload.get("injected_items") or [])
                if isinstance(row, Mapping) and isinstance(row.get("item_id"), str)
            ]
            if not rows:
                continue
            _tally_class_prior_pack(
                rows,
                rejected_rows=[
                    r
                    for r in (payload.get("rejected_items") or [])
                    if isinstance(r, Mapping)
                ],
                helpful=helpful_by_pack.get(pack_id, set()),
                unhelpful=unhelpful_by_pack.get(pack_id, set()),
                config=config,
                lookup=parent_lookup,
                tally=tally,
            )

    report = ClassPriorReplayReport(
        window_days=days,
        applied_config=applied_config,
        attributed_packs=len(attributed),
        matched_servings=tally.matched_servings,
        helpful_lost=tally.helpful_lost,
        unhelpful_removed=tally.unhelpful_removed,
        tokens_freed=tally.tokens_freed,
        still_served_by_other_axis=tally.still_served_by_other_axis,
        matched_via_parent=tally.matched_via_parent,
        parent_lookups=tally.parent_lookups,
        parent_form_resolved=tally.parent_form_resolved,
        parent_form_unresolved=tally.parent_form_unresolved,
        notes=_class_prior_notes(
            matched_servings=tally.matched_servings,
            mode_is_off=mode_is_off,
            still_served_by_other_axis=tally.still_served_by_other_axis,
            saw_document_form=tally.saw_document_form,
            chunk_scope=chunk_scope,
            has_document_store=document_store is not None,
            matched_via_parent=tally.matched_via_parent,
            parent_lookups=tally.parent_lookups,
            parent_form_resolved=tally.parent_form_resolved,
            parent_form_unresolved=tally.parent_form_unresolved,
        ),
    )
    logger.debug(
        "pack_value_class_prior_replayed",
        window_days=days,
        applied_config=applied_config,
        attributed_packs=report.attributed_packs,
        matched_servings=tally.matched_servings,
    )
    return report


class GraphSelectionReplayReport(TrellisModel):
    """What withholding one ``graph_selection`` value would have cost, and
    what the freed budget would have served instead (#961).

    Same population and unit as :class:`ClassPriorReplayReport`: servings
    the original pack actually served, counted per ``(pack_id, item_id)``,
    over attributed packs. It adds what that arm says it cannot: a pack's
    ``budget_trace[]`` lists the candidates the token walk turned away, so
    the walk is re-run without the withheld servings and whatever it now
    admits is the freed budget's replacement. Those replacements were never
    served, so nobody graded them; they are counted and attributed to an
    axis, never scored.
    """

    window_days: int
    #: The ``graph_selection`` value withheld.
    graph_selection: str
    attributed_packs: int = 0
    #: Packs holding at least one matched serving.
    affected_packs: int = 0
    #: Servings stamped with the value, including the
    #: ``still_served_by_other_axis`` subset withhold would not remove.
    matched_servings: int = 0
    #: Withheld servings cited helpful: the recall this arm costs.
    helpful_lost: int = 0
    #: Withheld servings cited unhelpful: what the arm is for.
    unhelpful_removed: int = 0
    #: Withheld servings no grader named. ``helpful_lost +
    #: unhelpful_removed + unjudged_removed + still_served_by_other_axis ==
    #: matched_servings``.
    unjudged_removed: int = 0
    #: Gross ``estimated_tokens`` of the withheld servings, as
    #: :attr:`ClassPriorReplayReport.tokens_freed` counts it.
    tokens_freed: int = 0
    #: Matched servings whose id the pack's own ``rejected_items`` shows a
    #: dedup copy of on a non-graph axis, which withhold would let win; as
    #: in :class:`ClassPriorReplayReport`, none counts as removed or freed.
    still_served_by_other_axis: int = 0

    # -- the freed budget ------------------------------------------------
    #: Affected packs whose token walk was re-run. The re-run reproduced the
    #: recorded decisions before the servings were removed, so it is the
    #: walk and not a model of it.
    refilled_packs: int = 0
    #: Affected packs that could not be re-run: no ``budget_trace`` or token
    #: budget, the re-run did not reproduce what was recorded (index mode,
    #: graduated disclosure), or the pack lost candidates at the ``max_items``
    #: cut whose cost the event does not record. ``notes`` says which. Their
    #: withheld servings are still counted above; their freed budget is not.
    unmodelled_packs: int = 0
    #: Candidates the original build rejected, on ``max_items`` or on tokens,
    #: that the freed slots and tokens would admit. Never served, so ungraded.
    replacement_servings: int = 0
    replacement_tokens: int = 0
    #: ``replacement_servings`` by the axis that collected each, from the
    #: pack's ``rejected_items``; ``(unknown)`` when it names none.
    replacement_by_strategy: dict[str, int] = Field(default_factory=dict)
    #: Graph-axis candidates the walk rejected whose selection no served row
    #: of that pack settles; left out of the re-run rather than guessed at.
    unclassified_graph_candidates: int = 0

    notes: list[str] = Field(default_factory=list)


def _walk(
    candidates: list[_Candidate], max_tokens: int, max_items: int | None = None
) -> set[str]:
    """The builder's two-stage budget: the item cut, then the token walk.

    Mirrors ``PackBuilder.build``: only the first ``max_items`` candidates
    reach ``PackBuilder._apply_token_budget_tracked``, whose
    ``budget_exceeded`` latch means a small candidate behind an overflow is
    never admitted. ``max_items=None`` leaves the item cut off.
    """
    admitted: set[str] = set()
    total = 0
    for candidate in candidates[:max_items]:
        if total + candidate.walk_tokens > max_tokens:
            break
        total += candidate.walk_tokens
        admitted.add(candidate.item_id)
    return admitted


#: Why an affected pack's walk could not be re-run, keyed by the tag
#: :func:`_refill_pack` records and worded for the note that reports it.
_UNMODELLED_REASONS = {
    "no_budget_trace": "recorded no budget_trace",
    "no_token_budget": "recorded no token budget",
    "trace_incomplete": ("had an empty budget_trace or one missing a withheld item"),
    "walk_not_reproduced": "did not reproduce the recorded walk",
    "trace_unreadable": "recorded a budget_trace with an unreadable token charge",
    "disclosure_repriced": (
        "were served under graduated disclosure, which re-priced the trace "
        "after the walk, and were written before the event kept the walk's "
        "charge (walk_tokens)"
    ),
    "max_items_unbounded": (
        "lost candidates at the max_items cut but recorded no budget_max_items"
    ),
    "max_items_unpriced": (
        "lost candidates at the max_items cut whose token cost the event "
        "does not record (written before the builder priced the cut)"
    ),
}


def _graph_selection_notes(
    *,
    graph_selection: str,
    attributed_packs: int,
    matched_servings: int,
    saw_stamp: bool,
    still_served_by_other_axis: int,
    unmodelled_reasons: Mapping[str, int],
    unclassified: int,
) -> list[str]:
    """Caveats that travel with the graph-selection counts."""
    notes = [
        (
            "Counterfactual over the SAME packs and the SAME citations. The "
            "freed budget is re-spent by re-running the pack's two-stage "
            "budget without the withheld servings: the max_items cut, then "
            "the token walk against the effective budget. What it admits was "
            "never served, so nobody graded it and it is counted, not "
            "scored. Rank order is held fixed, and a withheld item's absence "
            "is not re-ranked."
        ),
    ]
    if attributed_packs < MIN_ATTRIBUTED_PACKS:
        notes.append(
            f"{attributed_packs} attributed pack(s) is below the "
            f"{MIN_ATTRIBUTED_PACKS}-pack minimum every other ratio in "
            "`analyze value` is held to; read these counts as raw tallies, "
            "not as a measurement."
        )
    if matched_servings == 0:
        if saw_stamp:
            notes.append(
                f"No serving in the window carries graph_selection="
                f"{graph_selection!r}, so every count above is zero because "
                "nothing was eligible, not because the arm found nothing to do."
            )
        else:
            notes.append(
                "No served item in the window carries a graph_selection stamp "
                "at all, so every count above is zero because there is no "
                "history yet for this arm to grade."
            )
    if still_served_by_other_axis:
        notes.append(
            f"{still_served_by_other_axis} matched serving(s) are not counted "
            "as removed or freed: the pack's own rejected_items has a dedup "
            "row for a copy of the same id on a non-graph axis, which "
            "withhold would let win. Under budget pressure that copy can miss "
            "the cut, so this is an upper bound on survivals."
        )
    if unmodelled_reasons:
        why = "; ".join(
            f"{count} {_UNMODELLED_REASONS.get(reason, reason)}"
            for reason, count in sorted(unmodelled_reasons.items())
        )
        notes.append(
            f"{sum(unmodelled_reasons.values())} affected pack(s) could not "
            f"have their two-stage budget (item cut, then token walk) re-run "
            f"({why}). Their withheld servings are counted; their freed "
            "budget is not."
        )
    if unclassified:
        notes.append(
            f"{unclassified} rejected graph-axis candidate(s) were left out "
            "of the re-run: only served items record graph_selection, and the "
            "pack's served graph rows do not agree on one value, so whether "
            "withhold would also have dropped them is unknown."
        )
    return notes


@dataclass
class _GraphSelectionTally:
    """Running counts for :func:`replay_graph_selection`."""

    matched: int = 0
    helpful: int = 0
    unhelpful: int = 0
    unjudged: int = 0
    tokens_freed: int = 0
    other_axis: int = 0
    affected: int = 0
    refilled: int = 0
    unmodelled: int = 0
    unmodelled_reasons: dict[str, int] = field(default_factory=dict)
    replacement_tokens: int = 0
    unclassified: int = 0
    saw_stamp: bool = False
    replacement_by_strategy: dict[str, int] = field(default_factory=dict)


def _stamp_of(row: Mapping[str, Any]) -> str:
    """The row's ``graph_selection``, or ``""`` when it carries none."""
    stamp = row.get("graph_selection")
    return stamp.strip() if isinstance(stamp, str) else ""


def _withhold_pack(
    rows: list[Mapping[str, Any]],
    rejected_rows: list[Mapping[str, Any]],
    value: str,
    *,
    helpful: set[str],
    unhelpful: set[str],
    tally: _GraphSelectionTally,
) -> set[str]:
    """Count one pack's servings stamped ``value``; return the ids withhold removes."""
    withheld: set[str] = set()
    seen: set[str] = set()
    matched = 0
    for row in rows:
        stamp = _stamp_of(row)
        tally.saw_stamp = tally.saw_stamp or bool(stamp)
        item_id = row["item_id"]
        # One serving per ``(pack, item)``; a pack dedups by id, so a repeat
        # is not expected, and skipping it keeps the unit true.
        if item_id in seen:
            continue
        seen.add(item_id)
        if stamp != value:
            continue
        matched += 1
        if _dedup_copy_outside_axes(
            item_id, rejected_rows, frozenset({GRAPH_STRATEGY_NAME})
        ):
            tally.other_axis += 1
            continue
        withheld.add(item_id)
        raw_tokens = row.get("estimated_tokens")
        if isinstance(raw_tokens, int | float):
            tally.tokens_freed += int(raw_tokens)
        if item_id in helpful:
            tally.helpful += 1
        elif item_id in unhelpful:
            tally.unhelpful += 1
        else:
            tally.unjudged += 1
    tally.matched += matched
    tally.affected += 1 if matched else 0
    return withheld


def _unmodelled(tally: _GraphSelectionTally, reason: str) -> None:
    """Count one affected pack whose walk could not be re-run, and say why."""
    tally.unmodelled += 1
    tally.unmodelled_reasons[reason] = tally.unmodelled_reasons.get(reason, 0) + 1


def _int_or_none(raw: object) -> int | None:
    """``raw`` as an ``int`` when it is a whole number, else ``None``.

    A float is accepted only when it is integral (``8000.0``, which some JSON
    writers emit for an int). A fractional one is refused rather than
    truncated: no token count or budget the builder writes is fractional, so
    ``7999.5`` is a corrupted field, and quietly replaying it as ``7999``
    would model a budget the pack never had. ``nan`` and ``inf`` are refused
    too (``int()`` of either raises, which would abort the whole replay).
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if isinstance(raw, float) and not (math.isfinite(raw) and raw.is_integer()):
        return None
    return int(raw)


def _walk_budget(payload: Mapping[str, Any]) -> int | None:
    """The token budget the pack's walk ran against, or ``None`` if unrecorded.

    The builder walks ``_effective_token_budget`` (the safety margin applied),
    which it records as ``token_budget_effective`` beside the nominal
    ``budget_max_tokens``. Events from before that key fall back to the
    nominal budget, but only when the key is *absent*: an effective budget
    that is present and unreadable is refused, because the nominal one is the
    larger whenever a margin applied and would walk a budget the pack never
    had. **Every replay arm reads its budget here**, so two arms of one
    replay cannot disagree about what a pack was allowed to spend.
    """
    raw = payload.get("token_budget_effective")
    if raw is None:
        raw = payload.get("budget_max_tokens")
    return _int_or_none(raw)


def _disclosure_repriced(payload: Mapping[str, Any]) -> bool:
    """True when the trace's served prices are pointers the walk never charged.

    Graduated disclosure runs after the token walk and the builder then
    re-prices the demoted steps in ``budget_trace`` to the pointer, while a
    ``max_items``-cut row stays priced at the walk's charge. Re-spending freed
    budget over that mix frees more than the pack ever paid, so the refill
    needs the walk's own charge, which the builder keeps as ``walk_tokens``
    on every included step of such a pack. An event that names demoted ids in
    ``disclosure.pointer_item_ids`` and lacks it on one of their steps was
    written before that, and its walk cannot be reconstructed. A ``disclosure``
    block whose ``pointer_item_ids`` is not a list is refused too: this
    cannot tell it from the case above.
    """
    disclosure = payload.get("disclosure")
    if not disclosure:
        # Absent or ``{}``: graduation never ran, so nothing was re-priced.
        return False
    pointers = (
        disclosure.get("pointer_item_ids") if isinstance(disclosure, Mapping) else None
    )
    if not isinstance(pointers, list):
        return True
    demoted = {p for p in pointers if isinstance(p, str)}
    return any(
        isinstance(step, Mapping)
        and step.get("included")
        and isinstance(step.get("item_id"), str)
        and step["item_id"] in demoted
        and step.get("walk_tokens") is None
        for step in payload.get("budget_trace") or []
    )


def _max_items_cut(
    rejected_rows: list[Mapping[str, Any]], max_items: int | None
) -> list[_Candidate] | str:
    """The candidates the ``max_items`` cut turned away, in rank order.

    ``[]`` when it turned none away; otherwise the ``_UNMODELLED_REASONS``
    tag when the cut cannot be re-run: no ``budget_max_items`` to re-apply, or
    a row with no ``estimated_tokens``. The cut runs before the token walk, so
    ``budget_trace`` never lists these items and the row's own
    ``estimated_tokens`` (written by the builder since #970) is the only
    record of their cost; an older event has none and stays unpriced.
    """
    cut_rows = [r for r in rejected_rows if r.get("reason") == "max_items"]
    if not cut_rows:
        return []
    if max_items is None:
        return "max_items_unbounded"
    cut: list[_Candidate] = []
    for row in cut_rows:
        item_id = row.get("item_id")
        tokens = _int_or_none(row.get("estimated_tokens"))
        if not isinstance(item_id, str) or not item_id or tokens is None or tokens < 0:
            return "max_items_unpriced"
        cut.append(
            _Candidate(
                item_id=item_id,
                served_tokens=tokens,
                walk_tokens=tokens,
                included=False,
                verdict="unjudged",
                title=None,
            )
        )
    return cut


@dataclass(frozen=True)
class _RefillPlan:
    """What a pack's two-stage budget is re-run over."""

    #: The walk's candidates, in walk order, as ``budget_trace`` records them.
    candidates: list[_Candidate]
    #: The candidates the ``max_items`` cut turned away, in rank order.
    cut: list[_Candidate]
    #: The budget the builder walked against, safety margin applied.
    budget: int
    max_items: int | None


def _unreproducible(
    payload: Mapping[str, Any],
    candidates: list[_Candidate],
    withheld: set[str],
    budget: int,
    max_items: int | None,
) -> str | None:
    """The ``_UNMODELLED_REASONS`` tag if the recorded walk cannot be re-run."""
    if not candidates or not withheld <= {c.item_id for c in candidates}:
        # A re-run that cannot even see what it removes proves nothing.
        return "trace_incomplete"
    if _disclosure_repriced(payload):
        return "disclosure_repriced"
    if _walk(candidates, budget, max_items) != {
        c.item_id for c in candidates if c.included
    }:
        return "walk_not_reproduced"
    return None


def _plan_refill(
    payload: Mapping[str, Any],
    rejected_rows: list[Mapping[str, Any]],
    withheld: set[str],
    verdicts: tuple[set[str], set[str]],
) -> _RefillPlan | str:
    """The inputs of the re-run, or the ``_UNMODELLED_REASONS`` tag saying why not.

    The re-run is trusted only if, before anything is removed, it reproduces
    the walk the pack recorded.
    """
    candidates = _candidates(payload, verdicts)
    if isinstance(candidates, str):
        return candidates
    budget = _walk_budget(payload)
    if budget is None:
        return "no_token_budget"
    max_items = _int_or_none(payload.get("budget_max_items"))
    if max_items is not None and max_items < 1:
        max_items = None
    flaw = _unreproducible(payload, candidates, withheld, budget, max_items)
    if flaw:
        return flaw
    cut = _max_items_cut(rejected_rows, max_items)
    if isinstance(cut, str):
        return cut
    return _RefillPlan(candidates, cut, budget, max_items)


def _refill_pack(
    payload: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    rejected_rows: list[Mapping[str, Any]],
    value: str,
    *,
    withheld: set[str],
    verdicts: tuple[set[str], set[str]],
    tally: _GraphSelectionTally,
) -> None:
    """Re-run one pack's two-stage budget without ``withheld``; tally what it admits.

    The builder cuts to ``max_items`` first and walks tokens second, so a
    withheld serving frees an item slot as well as tokens: the candidates the
    cut turned away join the pool behind the trace, in rank order, and the
    walk runs against the effective budget the builder used. A pack that
    :func:`_plan_refill` cannot vouch for counts as unmodelled.
    """
    plan = _plan_refill(payload, rejected_rows, withheld, verdicts)
    if isinstance(plan, str):
        _unmodelled(tally, plan)
        return
    budget, max_items = plan.budget, plan.max_items
    strategy_by_rejected = {
        r["item_id"]: r.get("strategy_source")
        for r in rejected_rows
        if r.get("reason") in ("token_budget", "max_items")
        and isinstance(r.get("item_id"), str)
    }
    # Only served rows record ``graph_selection``, but one graph search stamps
    # every item it returns alike, so a pack whose served graph rows agree on
    # one value settles the candidates the budget turned away.
    served_stamps = {
        _stamp_of(row)
        for row in rows
        if row.get("strategy_source") == GRAPH_STRATEGY_NAME and _stamp_of(row)
    }
    pack_selection = next(iter(served_stamps)) if len(served_stamps) == 1 else None

    pool: list[_Candidate] = []
    for candidate in [*plan.candidates, *plan.cut]:
        if candidate.item_id in withheld:
            continue
        if (
            not candidate.included
            and strategy_by_rejected.get(candidate.item_id) == GRAPH_STRATEGY_NAME
        ):
            if pack_selection is None:
                tally.unclassified += 1
                continue
            if pack_selection == value:
                # Withhold would drop it too; it was never served.
                continue
        pool.append(candidate)

    tally.refilled += 1
    admitted = _walk(pool, budget, max_items)
    for candidate in pool:
        if candidate.included or candidate.item_id not in admitted:
            continue
        tally.replacement_tokens += candidate.walk_tokens
        axis = strategy_by_rejected.get(candidate.item_id)
        label = axis if isinstance(axis, str) and axis else "(unknown)"
        tally.replacement_by_strategy[label] = (
            tally.replacement_by_strategy.get(label, 0) + 1
        )


def replay_graph_selection(
    event_log: EventLog,
    *,
    graph_selection: str,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> GraphSelectionReplayReport:
    """Price withholding every serving whose ``graph_selection`` equals a value.

    Args:
        event_log: Operational log holding ``PACK_ASSEMBLED`` and
            ``FEEDBACK_RECORDED``. Read only; holdout packs and the
            feedback naming them are dropped by the join.
        graph_selection: The ``injected_items[].graph_selection`` value to
            withhold, e.g. ``"recency_window"``.
        days: Look-back window, matching ``trellis analyze value``.
        limit: Per-event-type scan limit.

    Returns:
        A :class:`GraphSelectionReplayReport`. See its docstring and
        ``notes`` for what this arm cannot measure.

    Raises:
        ValueError: ``graph_selection`` is empty.
    """
    value = graph_selection.strip()
    if not value:
        msg = "graph_selection must be a non-empty string"
        raise ValueError(msg)

    since = datetime.now(tz=UTC) - timedelta(days=days)
    feedback_events, pack_payloads, _ = join_pack_feedback(
        event_log, since=since, limit=limit
    )
    flat_packs = {
        pack_id: payload
        for pack_id, payload in pack_payloads.items()
        if payload.get("injected_items")
    }
    verdicts = collect_pack_verdicts(feedback_events, flat_packs)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    attributed = sorted(set(helpful_by_pack) | set(unhelpful_by_pack))

    tally = _GraphSelectionTally()
    for pack_id in attributed:
        payload = flat_packs[pack_id]
        helpful = helpful_by_pack.get(pack_id, set())
        unhelpful = unhelpful_by_pack.get(pack_id, set())
        rows = [
            row
            for row in (payload.get("injected_items") or [])
            if isinstance(row, Mapping) and isinstance(row.get("item_id"), str)
        ]
        rejected_rows = [
            r for r in (payload.get("rejected_items") or []) if isinstance(r, Mapping)
        ]
        withheld = _withhold_pack(
            rows,
            rejected_rows,
            value,
            helpful=helpful,
            unhelpful=unhelpful,
            tally=tally,
        )
        if withheld:
            _refill_pack(
                payload,
                rows,
                rejected_rows,
                value,
                withheld=withheld,
                verdicts=(helpful, unhelpful),
                tally=tally,
            )

    report = GraphSelectionReplayReport(
        window_days=days,
        graph_selection=value,
        attributed_packs=len(attributed),
        affected_packs=tally.affected,
        matched_servings=tally.matched,
        helpful_lost=tally.helpful,
        unhelpful_removed=tally.unhelpful,
        unjudged_removed=tally.unjudged,
        tokens_freed=tally.tokens_freed,
        still_served_by_other_axis=tally.other_axis,
        refilled_packs=tally.refilled,
        unmodelled_packs=tally.unmodelled,
        replacement_servings=sum(tally.replacement_by_strategy.values()),
        replacement_tokens=tally.replacement_tokens,
        replacement_by_strategy=dict(sorted(tally.replacement_by_strategy.items())),
        unclassified_graph_candidates=tally.unclassified,
        notes=_graph_selection_notes(
            graph_selection=value,
            attributed_packs=len(attributed),
            matched_servings=tally.matched,
            saw_stamp=tally.saw_stamp,
            still_served_by_other_axis=tally.other_axis,
            unmodelled_reasons=tally.unmodelled_reasons,
            unclassified=tally.unclassified,
        ),
    )
    logger.debug(
        "pack_value_graph_selection_replayed",
        window_days=days,
        graph_selection=value,
        attributed_packs=report.attributed_packs,
        matched_servings=report.matched_servings,
    )
    return report


__all__ = [
    "EXCERPT_MAX_CHARS",
    "ClassPriorReplayReport",
    "GraphSelectionReplayReport",
    "ReplayArm",
    "ReplayPolicy",
    "ReplayReport",
    "replay_class_prior",
    "replay_graph_selection",
    "replay_pack_value",
]
