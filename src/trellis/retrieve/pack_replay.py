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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.learning.pack_observations import join_pack_feedback
from trellis.retrieve.disclosure import POINTER_LABEL_MAX_CHARS
from trellis.retrieve.excerpts import EXCERPT_MAX_CHARS
from trellis.retrieve.pack_value import MIN_ATTRIBUTED_PACKS, collect_pack_verdicts

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_DEFAULT_EVENT_LIMIT = 5000

#: Fixed characters a pointer spends beyond its label — the ellipsis, the
#: withheld-size note and the fetch hint, at the widest plausible size
#: rendering. Kept as a constant rather than re-deriving
#: :func:`~trellis.retrieve.disclosure.pointer_excerpt` because the replay
#: has no excerpt text to derive a label from (limit 1 above).
_POINTER_SCAFFOLD_CHARS = len(" … [+999.9k chars — fetch id for full text]")


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
    served_tokens: int
    included: bool
    verdict: str
    title: str | None


def _candidates(
    payload: Mapping[str, Any], verdicts: tuple[set[str], set[str]]
) -> list[_Candidate] | None:
    """Every candidate that reached the token walk, in walk order.

    ``None`` when the pack carries no ``budget_trace`` — the walk cannot be
    re-run from an event that did not record it.
    """
    trace = payload.get("budget_trace")
    if not isinstance(trace, list) or not trace:
        return None
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
        raw_tokens = step.get("item_tokens")
        tokens = int(raw_tokens) if isinstance(raw_tokens, int | float) else 0
        title = meta.get(item_id, {}).get("title")
        out.append(
            _Candidate(
                item_id=item_id,
                served_tokens=tokens,
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
        chars = _chars_from_tokens(candidate.served_tokens)
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
        useful_token_fraction=(
            round(helpful / injected, 4) if injected else None
        ),
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
        if candidates is None:
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
        raw_budget = payload.get("budget_max_tokens")
        max_tokens = int(raw_budget) if isinstance(raw_budget, int | float) else 0
        served, ungraded = _price(candidates, max_tokens=max_tokens, policy=policy)
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

    withheld = len(
        (helpful_servings & counter_served_servings) - counter_body_servings
    )
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
            round(
                counterfactual.injected_tokens / baseline.injected_tokens - 1.0, 4
            )
            if baseline.injected_tokens
            else None
        ),
        fraction_delta=(
            round(
                counterfactual.useful_token_fraction
                / baseline.useful_token_fraction
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
        "Counterfactual over the SAME packs and the SAME citations: only "
        "the serving policy differs. Excerpt lengths are inverted from "
        "estimated_tokens (~4 chars/token), so per-item costs are accurate "
        "to about one token.",
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
            f"{no_trace} attributed pack(s) carry no budget_trace and could "
            "not be re-priced; they are excluded from both arms."
        )
    return notes


__all__ = [
    "EXCERPT_MAX_CHARS",
    "ReplayArm",
    "ReplayPolicy",
    "ReplayReport",
    "replay_pack_value",
]
