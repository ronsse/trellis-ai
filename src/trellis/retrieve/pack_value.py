"""Value density of served context — what share of injected tokens got cited.

The thesis of this system is that injected memory returns more than it
costs. Nothing computed that ratio, so the thesis was an assumption. This
module makes it a number, from telemetry that already existed on both
sides of the join:

* **Cost per item** — what the pack actually charged for it. That is
  ``PACK_ASSEMBLED.budget_trace[].item_tokens`` where the event records
  one, falling back to ``injected_items[].estimated_tokens``. The two are
  byte-identical for an ordinary pack (both are the excerpt's token
  count), and they diverge exactly where an item was served as something
  other than its excerpt: an index-mode pack (#305) charges an index line
  while ``estimated_tokens`` deliberately keeps carrying the excerpt
  *read* cost, and a graduated pack
  (:mod:`trellis.retrieve.disclosure`) serves a pointer. Reading the
  read cost there would price a body the prompt never carried.
* **Citation per item** — ``FEEDBACK_RECORDED.helpful_item_ids`` /
  ``unhelpful_item_ids``, the caller's verdict on what it used.

The headline is :attr:`PackValueReport.useful_token_fraction`: of the
tokens a pack injected, the share carried by items the caller later named
helpful.

**What this is not.** It measures the *precision of what was served* — a
value-density proxy. It is **not benefit**. Benefit is counterfactual
(does an agent with memory outperform the same agent without it?) and
answering it needs a withhold arm this system does not have. A low
fraction says the pack was wide, not that memory was worthless; a high
one says the pack was tight, not that it caused the outcome. Nothing in
this module, its CLI surface, or its output may describe the number as
benefit, value delivered, or ROI.

Three honesty rules, each earned by a failure this repo has already had:

1. **Every ratio carries its ``n``.** ``attributed_packs`` is the sample
   the fraction is computed over, and it is small — 16 packs all-time on
   the reference deployment at time of writing. A ratio published without
   its sample size is how a two-pack accident becomes a roadmap input.
2. **A ratio from too thin a sample is refused, not rounded.** Below
   :data:`MIN_ATTRIBUTED_PACKS` the fraction is ``None`` and
   ``suppressed_reason`` names why. The raw token counts are still
   reported — the operator sees the evidence, just not a number dressed
   as a finding. Per-axis cells are held to the same bar independently:
   on the reference corpus this suppresses 6 of 7 intent families, which
   is the correct answer, not a defect.
3. **Uncited is not unhelpful.** An item nobody mentioned got no verdict.
   ``unjudged_tokens`` is reported as its own bucket rather than folded
   into the denominator's "not helpful" residue, because the two license
   different conclusions. ``useful_token_fraction`` is therefore a
   *lower bound* on usefulness, and its docstring says so wherever it is
   rendered.
4. **A bound is stated as an interval, not as a number with a caveat**
   (#364). On the reference deployment 42.0% of injected tokens got no
   verdict at all, so the headline 0.0884 described 58% of what it was
   named after and a footnote was carrying the other 42%. Three numbers
   now travel together and the reader picks the reading:

   * ``useful_token_fraction`` — ``helpful / injected``. **Unchanged**,
     denominator and all. Every ungraded token counted as not-useful:
     the pessimistic end.
   * ``useful_token_fraction_upper_bound`` — ``(helpful + unjudged) /
     injected``. Every ungraded token counted as useful: the optimistic
     end. Its only job is to make the interval visible, and the interval
     is the honest epistemic state. **Its width is exactly
     ``unjudged_token_fraction``** — an identity, pinned by test — so
     "how much of this is guesswork" is readable off the JSON without
     arithmetic.
   * ``useful_token_fraction_judged`` — ``helpful / judged``, over the
     tokens that actually got a verdict, with ``judged_tokens`` and
     ``judged_token_coverage`` beside it. A *conditional* reading, not a
     third estimate of the whole: extending it to the ungraded tokens
     assumes they are missing at random, and #364 argues at length that
     they are not (graders cite what helped, not what they ignored).

   What is deliberately **not** done: widen ``useful_token_fraction``'s
   denominator to the judged tokens. That would triple the headline
   (0.0884 → 0.1524) by discarding the population that makes it
   uncertain, which is the ``attribution_rate`` failure mode
   (``write_health.ServeAttributionReport``) run backwards. The new
   numbers are additive; the old one is load-bearing and untouched.

The unjudged share is **not uniform across axes**, which is why every
per-axis cell carries it too. Measured over the 30 days to 2026-08-28
(n=17 attributed packs), ``unjudged_token_fraction`` by strategy runs
graph 0.548 / semantic 0.431 / keyword 0.362 — so the axis with the best
citation rate is also the least graded, and reads 0.174 pessimistic
against 0.386 conditional. The *ranking* of the three axes happens to be
identical under both readings on this corpus; the *levels* differ by more
than 2x. A trimming decision that only needs the ranking survives the
ambiguity, and one that needs the level does not.

Two structural exclusions, both stated rather than silent:

* **Sectioned packs.** ``PackBuilder.build_sectioned`` emits ``sections``
  and no ``injected_items[]``, so a sectioned pack contributes zero
  per-item rows however carefully an agent cites it. They are counted in
  :attr:`PackValueReport.sectioned_packs_excluded` and excluded from
  every ratio.
* **Pack-targeted feedback that cites nothing.** Zero cited items is an
  absence of signal, not evidence of a useless pack. Folding it in would
  drive the fraction toward zero on grader laziness alone. Counted as
  :attr:`PackValueReport.pack_targeted_uncited`.

The join itself is delegated to
:func:`trellis.learning.pack_observations.join_pack_feedback` rather than
re-implemented, so this analyzer and the learning loop cannot drift on
what "joins to a pack" means.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.feedback.attribution import payload_pack_id
from trellis.learning.pack_observations import join_pack_feedback_with_coverage
from trellis.retrieve.token_pricing import estimate_dollars, resolve_pricing
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Minimum attributed packs before a ratio is stated rather than
#: suppressed. Matches ``write_health._MIN_ATTEMPTS_FOR_RATE`` — the
#: house precedent for "below this a rate is not a measurement" — so the
#: two coverage surfaces apply one standard.
MIN_ATTRIBUTED_PACKS = 5

#: Reason slug attached when a cell is below :data:`MIN_ATTRIBUTED_PACKS`.
SUPPRESSED_THIN_SAMPLE = "below_min_attributed_packs"

#: Reason slug for a conditional fraction whose denominator is empty —
#: nothing in the cell got a verdict at all, so ``helpful / judged`` is
#: undefined rather than zero.
SUPPRESSED_NO_JUDGED_TOKENS = "no_judged_tokens"

_DEFAULT_EVENT_LIMIT = DEFAULT_SCAN_LIMIT


#: Fallback key for an id that carries no namespace prefix — a bare ULID
#: written by ``save_memory`` / document ingest. Spelled the same as the
#: other axes' unknown bucket so a reader meets one convention.
NO_NAMESPACE = "(none)"

#: A leading ``<namespace>:`` on a pack item id. Anchored lowercase so an
#: uppercase Crockford ULID (``01KZDAAG...``) cannot match, and bounded so
#: a pathological id cannot mint a 200-character axis key. Only the FIRST
#: segment is taken, which is what makes ``artifact:https://example/x`` and
#: ``conversation:claude-ai:abc#chunk-0`` land in ``artifact`` and
#: ``conversation`` rather than in a bucket of one.
_NAMESPACE_RE = re.compile(r"^([a-z][a-z0-9_-]{0,31}):")


def item_namespace(item_id: str) -> str:
    """The namespace prefix an item id carries, or :data:`NO_NAMESPACE`.

    **Why this axis exists.** ``by_item_type`` reads
    ``PackItem.item_type``, and every row the graph strategy produces
    carries the same one — ``"entity"``. So that axis cannot separate a
    name-only stub minted from a trace (``artifact:src/foo.py``, whose
    excerpt *is* the path) from a real curated entity, which is precisely
    the distinction issue #298 is about. Measured on the reference
    deployment those three populations differ by more than an order of
    magnitude in citation rate while sharing one ``item_type``, so the
    existing axis reported their average and nothing else.

    The namespace is read off the id rather than off any stored field
    because it is the one discriminator that is already present on every
    item, in the event log, retroactively — no backfill, no new write
    path, and it prices windows that closed before this function existed.

    It is a *description of the id*, not a classification of the content:
    an id with no prefix is reported as :data:`NO_NAMESPACE`, never
    guessed at.
    """
    match = _NAMESPACE_RE.match(item_id)
    return match.group(1) if match else NO_NAMESPACE


class ValueBreakdown(TrellisModel):
    """One axis cell — a strategy, item type, or intent family.

    Suppression is per-cell and independent of the headline: a corpus can
    have enough attributed packs overall to state a global fraction while
    every intent family in it is too thin to state its own.
    """

    key: str
    #: Attributed packs contributing at least one item to this cell. The
    #: ``n`` for this cell's fraction, and the value the suppression
    #: threshold is applied to.
    attributed_packs: int = 0
    injected_tokens: int = 0
    helpful_tokens: int = 0
    unhelpful_tokens: int = 0
    unjudged_tokens: int = 0
    #: ``helpful_tokens + unhelpful_tokens`` — the tokens that got a
    #: verdict, and the denominator of the conditional reading.
    judged_tokens: int = 0
    #: ``helpful_tokens / injected_tokens``, or ``None`` when suppressed.
    #: The pessimistic end: every ungraded token counted as not-useful.
    useful_token_fraction: float | None = None
    #: ``(helpful_tokens + unjudged_tokens) / injected_tokens`` — the
    #: optimistic end. ``useful_token_fraction`` and this bracket the
    #: truth, and the gap between them is :attr:`unjudged_token_fraction`.
    useful_token_fraction_upper_bound: float | None = None
    #: ``helpful_tokens / judged_tokens``. ``None`` when suppressed **or**
    #: when nothing in this cell was graded — an undefined ratio, not a
    #: zero. Judgement coverage varies by more than 1.5x across strategies
    #: on the reference corpus, which is what this per-cell field exists
    #: to expose.
    useful_token_fraction_judged: float | None = None
    #: ``unjudged_tokens / injected_tokens`` — this cell's share of the
    #: bucket nobody graded.
    unjudged_token_fraction: float | None = None
    #: ``judged_tokens / injected_tokens`` — how much of this cell the
    #: conditional reading rests on.
    judged_token_coverage: float | None = None
    suppressed: bool = False
    suppressed_reason: str = ""


class PackValueReport(TrellisModel):
    """Value density of served context over a window, with its coverage.

    Read :attr:`useful_token_fraction` only together with
    :attr:`attributed_packs`. The fraction is a lower bound on usefulness
    (see :attr:`unjudged_tokens`) and a measure of serving precision, not
    of benefit.
    """

    window_days: int

    # -- Coverage: what the ratio was computed over --------------------
    #: All ``PACK_ASSEMBLED`` events in the window.
    packs: int = 0
    #: Packs emitting ``sections`` and no ``injected_items[]``. Excluded
    #: from every ratio — they carry no per-item rows to attribute.
    sectioned_packs_excluded: int = 0
    #: Packs carrying ``injected_items[]`` — the attributable population.
    flat_packs: int = 0
    feedback_events: int = 0
    #: Feedback naming a ``pack_id`` — where citation is possible at all.
    pack_targeted_feedback: int = 0
    #: Of those, events citing at least one item.
    pack_targeted_attributed: int = 0
    #: Pack-targeted feedback citing nothing. Absence of signal, not
    #: evidence of a useless pack; excluded from the ratios.
    pack_targeted_uncited: int = 0
    #: Pack-targeted feedback whose ``pack_id`` matched no pack in the
    #: window — usually a pack assembled before it starts.
    pack_targeted_unjoined: int = 0
    #: **The ``n`` for every ratio below.** Distinct flat packs that both
    #: joined and were cited.
    attributed_packs: int = 0
    #: The threshold below which ratios are refused, stated so a reader
    #: never has to guess what "too few" meant.
    min_attributed_packs: int = MIN_ATTRIBUTED_PACKS

    # -- Item-level: the headline --------------------------------------
    injected_tokens: int = 0
    helpful_tokens: int = 0
    unhelpful_tokens: int = 0
    #: Tokens on items the caller never mentioned. Not "unhelpful" —
    #: no verdict was given. This bucket is why the headline is a lower
    #: bound rather than a point estimate.
    unjudged_tokens: int = 0
    #: ``helpful_tokens + unhelpful_tokens`` — tokens that got a verdict.
    judged_tokens: int = 0
    #: ``helpful_tokens / injected_tokens``. ``None`` when suppressed.
    #: **The headline, and deliberately unchanged by #364**: the
    #: pessimistic end of the interval, with every ungraded token counted
    #: as not-useful. Read it together with
    #: :attr:`useful_token_fraction_upper_bound`.
    useful_token_fraction: float | None = None
    unhelpful_token_fraction: float | None = None
    unjudged_token_fraction: float | None = None
    #: ``(helpful_tokens + unjudged_tokens) / injected_tokens``. The
    #: optimistic end: every ungraded token counted as useful. Nothing
    #: this analyzer can see distinguishes the two ends, so the pair is
    #: the measurement and either one alone is an assertion.
    useful_token_fraction_upper_bound: float | None = None
    #: ``helpful_tokens / judged_tokens`` — the conditional reading, over
    #: the tokens that actually got a verdict. ``None`` when suppressed or
    #: when ``judged_tokens`` is zero. **Not a substitute for the
    #: headline**: applying it to the ungraded tokens assumes they are
    #: missing at random, and they are not (#364).
    useful_token_fraction_judged: float | None = None
    #: ``judged_tokens / injected_tokens`` — the share of the population
    #: :attr:`useful_token_fraction_judged` actually rests on. Publish the
    #: conditional without this and a 3%-coverage ratio reads like a
    #: 90%-coverage one.
    judged_token_coverage: float | None = None
    #: Why :attr:`useful_token_fraction_judged` is ``None``, when it is:
    #: :data:`SUPPRESSED_THIN_SAMPLE` or
    #: :data:`SUPPRESSED_NO_JUDGED_TOKENS`.
    judged_suppressed_reason: str = ""
    suppressed: bool = False
    suppressed_reason: str = ""

    # -- Scan coverage: what the event-log cap did to this report -------
    #: Whether the underlying EventLog reads hit their cap, and what that
    #: excluded (#374). A truncated report is computed over a shorter
    #: window than :attr:`window_days` claims; ``scan.covered_since`` names
    #: where its evidence actually begins.
    scan: ScanCoverage = Field(default_factory=ScanCoverage)

    # -- Citation hygiene ----------------------------------------------
    #: Ids a caller cited that the pack did not serve. These silently
    #: deflate the numerator, so they are surfaced rather than dropped:
    #: on the reference corpus every instance was an agent prefixing an
    #: item type onto the id (``entity:trace:X`` for a served ``trace:X``).
    cited_ids_not_served: int = 0

    # -- Dollars --------------------------------------------------------
    model: str = ""
    price_per_mtok: float = 0.0
    price_source: str = ""
    #: Priced :attr:`injected_tokens` across attributed packs.
    injected_dollars: float = 0.0
    #: Distinct items cited helpful across attributed packs.
    distinct_helpful_items: int = 0
    #: ``injected_dollars / distinct_helpful_items``: what one useful item
    #: cost, counting everything served alongside it. ``None`` when
    #: suppressed or when nothing was cited helpful.
    dollars_per_cited_item: float | None = None

    # -- Call-level: response cost, joined on the new ``pack_id`` -------
    #: ``TOKEN_TRACKED`` events in the window.
    response_events: int = 0
    #: Of those, events carrying a ``pack_id``. Zero for any event
    #: written before that field existed, which is why the coverage is
    #: reported rather than assumed.
    response_events_with_pack_id: int = 0
    response_pack_id_coverage: float = 0.0
    #: Attributed packs for which a response-token measurement exists.
    attributed_packs_with_response_tokens: int = 0
    #: Rendered response tokens across those packs — the true injected
    #: cost, including markdown scaffolding ``estimated_tokens`` omits.
    response_tokens_attributed: int = 0
    response_dollars_attributed: float = 0.0
    #: ``response_dollars_attributed / distinct_helpful_items``, over the
    #: covered subset only. ``None`` until response coverage exists.
    response_dollars_per_cited_item: float | None = None

    # -- Axes ------------------------------------------------------------
    by_strategy: list[ValueBreakdown] = Field(default_factory=list)
    by_item_type: list[ValueBreakdown] = Field(default_factory=list)
    #: Keyed by :func:`item_namespace` — the ``<namespace>:`` prefix on the
    #: item id. Separates the populations :attr:`by_item_type` collapses:
    #: every graph-strategy row is ``item_type="entity"``, so a name-only
    #: ``artifact:`` stub and a curated entity share a cell there and are
    #: reported as their average (#298).
    by_item_namespace: list[ValueBreakdown] = Field(default_factory=list)
    by_intent_family: list[ValueBreakdown] = Field(default_factory=list)

    #: Machine-readable caveats a renderer must not drop.
    notes: list[str] = Field(default_factory=list)

    #: What produced the token counts, for auditability — the same ~4
    #: chars/token heuristic ``trellis analyze cost`` prices.
    estimator: str = "estimate_4_chars_per_token"


def _fraction(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _finalize(cell: ValueBreakdown) -> ValueBreakdown:
    """Apply the suppression rule and both bound readings to one axis cell."""
    cell.judged_tokens = cell.helpful_tokens + cell.unhelpful_tokens
    if cell.attributed_packs < MIN_ATTRIBUTED_PACKS:
        cell.suppressed = True
        cell.suppressed_reason = SUPPRESSED_THIN_SAMPLE
        cell.useful_token_fraction = None
        cell.useful_token_fraction_upper_bound = None
        cell.useful_token_fraction_judged = None
        cell.unjudged_token_fraction = None
        cell.judged_token_coverage = None
        return cell
    cell.useful_token_fraction = _fraction(cell.helpful_tokens, cell.injected_tokens)
    cell.useful_token_fraction_upper_bound = _fraction(
        cell.helpful_tokens + cell.unjudged_tokens, cell.injected_tokens
    )
    # ``_fraction`` already returns None on a zero denominator, which is
    # exactly the "nothing here was graded" case: undefined, not zero.
    cell.useful_token_fraction_judged = _fraction(
        cell.helpful_tokens, cell.judged_tokens
    )
    cell.unjudged_token_fraction = _fraction(cell.unjudged_tokens, cell.injected_tokens)
    cell.judged_token_coverage = _fraction(cell.judged_tokens, cell.injected_tokens)
    return cell


def _sorted_cells(cells: dict[str, ValueBreakdown]) -> list[ValueBreakdown]:
    """Largest denominator first — the axis worth trimming leads."""
    return [
        _finalize(cell)
        for cell in sorted(cells.values(), key=lambda c: -c.injected_tokens)
    ]


def summarize_pack_value(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
    model: str | None = None,
    price_per_mtok: float | None = None,
) -> PackValueReport:
    """Compute serving precision and cost-per-cited-item over a window.

    Args:
        event_log: Operational event log holding ``PACK_ASSEMBLED``,
            ``FEEDBACK_RECORDED`` and ``TOKEN_TRACKED``.
        days: Look-back window. Defaults to 30 — attributed packs are
            rare enough that a 7-day window is almost always suppressed.
        limit: Per-event-type scan limit.
        model: Consuming model for pricing (else ``TRELLIS_COST_MODEL``).
        price_per_mtok: Explicit input price override, USD/Mtok.

    Returns:
        A :class:`PackValueReport`. Ratios are ``None`` — never ``0.0``
        — when the sample is too thin to state one, so a reader cannot
        mistake "refused" for "measured zero".
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)
    feedback_events, pack_payloads, pack_event_count, join_coverage = (
        join_pack_feedback_with_coverage(event_log, since=since, limit=limit)
    )

    # Sectioned packs carry no per-item rows; separate them before any
    # ratio touches them, and say how many were set aside.
    flat_packs = {
        pack_id: payload
        for pack_id, payload in pack_payloads.items()
        if payload.get("injected_items")
    }
    sectioned = len(pack_payloads) - len(flat_packs)

    verdicts = collect_pack_verdicts(feedback_events, flat_packs)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    families_by_pack = verdicts["families"]
    pack_targeted = verdicts["targeted"]
    pack_targeted_attributed = verdicts["targeted_attributed"]
    pack_targeted_unjoined = verdicts["targeted_unjoined"]

    attributed_ids = sorted(set(helpful_by_pack) | set(unhelpful_by_pack))

    tally = _accumulate(
        attributed_ids,
        flat_packs=flat_packs,
        helpful_by_pack=helpful_by_pack,
        unhelpful_by_pack=unhelpful_by_pack,
        families_by_pack=families_by_pack,
    )
    totals = tally["totals"]
    by_strategy = tally["by_strategy"]
    by_item_type = tally["by_item_type"]
    by_namespace = tally["by_item_namespace"]
    by_family = tally["by_family"]
    distinct_helpful = tally["distinct_helpful"]
    cited_not_served = tally["cited_not_served"]

    attributed_packs = len(attributed_ids)
    injected = totals["injected"]
    judged = totals["helpful"] + totals["unhelpful"]
    unjudged = injected - judged
    thin = attributed_packs < MIN_ATTRIBUTED_PACKS
    judged_reason = ""
    if thin:
        judged_reason = SUPPRESSED_THIN_SAMPLE
    elif not judged:
        judged_reason = SUPPRESSED_NO_JUDGED_TOKENS

    resolved_model, price, price_source = resolve_pricing(model, price_per_mtok)
    injected_dollars = estimate_dollars(injected, price)

    response = _response_token_join(
        event_log, since=since, limit=limit, attributed_ids=set(attributed_ids)
    )
    response_dollars = estimate_dollars(response["tokens"], price)
    scan = merge_coverage(join_coverage, response["coverage_scan"])

    report = PackValueReport(
        window_days=days,
        packs=pack_event_count,
        sectioned_packs_excluded=sectioned,
        flat_packs=len(flat_packs),
        feedback_events=len(feedback_events),
        pack_targeted_feedback=pack_targeted,
        pack_targeted_attributed=pack_targeted_attributed,
        pack_targeted_uncited=pack_targeted - pack_targeted_attributed,
        pack_targeted_unjoined=pack_targeted_unjoined,
        attributed_packs=attributed_packs,
        injected_tokens=injected,
        helpful_tokens=totals["helpful"],
        unhelpful_tokens=totals["unhelpful"],
        unjudged_tokens=unjudged,
        judged_tokens=judged,
        useful_token_fraction=(
            None if thin else _fraction(totals["helpful"], injected)
        ),
        unhelpful_token_fraction=(
            None if thin else _fraction(totals["unhelpful"], injected)
        ),
        unjudged_token_fraction=None if thin else _fraction(unjudged, injected),
        useful_token_fraction_upper_bound=(
            None if thin else _fraction(totals["helpful"] + unjudged, injected)
        ),
        useful_token_fraction_judged=(
            None if judged_reason else _fraction(totals["helpful"], judged)
        ),
        judged_token_coverage=None if thin else _fraction(judged, injected),
        judged_suppressed_reason=judged_reason,
        suppressed=thin,
        suppressed_reason=SUPPRESSED_THIN_SAMPLE if thin else "",
        scan=scan,
        cited_ids_not_served=cited_not_served,
        model=resolved_model,
        price_per_mtok=price,
        price_source=price_source,
        injected_dollars=round(injected_dollars, 6),
        distinct_helpful_items=len(distinct_helpful),
        dollars_per_cited_item=(
            None
            if thin or not distinct_helpful
            else round(injected_dollars / len(distinct_helpful), 6)
        ),
        response_events=response["events"],
        response_events_with_pack_id=response["with_pack_id"],
        response_pack_id_coverage=response["coverage"],
        attributed_packs_with_response_tokens=response["packs_covered"],
        response_tokens_attributed=response["tokens"],
        response_dollars_attributed=round(response_dollars, 6),
        response_dollars_per_cited_item=(
            None
            if thin or not distinct_helpful or not response["packs_covered"]
            else round(response_dollars / len(distinct_helpful), 6)
        ),
        by_strategy=_sorted_cells(by_strategy),
        by_item_type=_sorted_cells(by_item_type),
        by_item_namespace=_sorted_cells(by_namespace),
        by_intent_family=_sorted_cells(by_family),
        notes=_build_notes(
            attributed_packs=attributed_packs,
            sectioned=sectioned,
            cited_not_served=cited_not_served,
            unjudged=unjudged,
            injected=injected,
            judged=judged,
            helpful=totals["helpful"],
            scan=scan,
            response=response,
        ),
    )
    logger.debug(
        "pack_value_summarized",
        window_days=days,
        attributed_packs=attributed_packs,
        useful_token_fraction=report.useful_token_fraction,
        suppressed=thin,
    )
    return report


def collect_pack_verdicts(
    feedback_events: list[Any],
    flat_packs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Fold every pack-targeted feedback event into per-pack verdict sets.

    Verdicts are unioned per pack: two graders on one pack are two
    witnesses to the same delivery, not two deliveries, so the pack's
    tokens must not be counted twice.
    """
    helpful_by_pack: dict[str, set[str]] = defaultdict(set)
    unhelpful_by_pack: dict[str, set[str]] = defaultdict(set)
    families_by_pack: dict[str, str] = {}
    targeted = targeted_attributed = targeted_unjoined = 0

    for event in feedback_events:
        payload = event.payload or {}
        pack_id = payload_pack_id(payload)
        if not pack_id:
            continue
        targeted += 1
        helpful = _clean_ids(payload.get("helpful_item_ids"))
        unhelpful = _clean_ids(payload.get("unhelpful_item_ids"))
        if helpful or unhelpful:
            targeted_attributed += 1
        if pack_id not in flat_packs:
            # Either unknown in this window, or sectioned. Both are
            # unattributable; neither is the grader's fault.
            targeted_unjoined += 1
            continue
        if not (helpful or unhelpful):
            continue
        helpful_by_pack[pack_id] |= helpful
        unhelpful_by_pack[pack_id] |= unhelpful
        family = (
            payload.get("intent_family")
            or flat_packs[pack_id].get("intent_family")
            or "(none)"
        )
        families_by_pack.setdefault(pack_id, str(family))

    return {
        "helpful": helpful_by_pack,
        "unhelpful": unhelpful_by_pack,
        "families": families_by_pack,
        "targeted": targeted,
        "targeted_attributed": targeted_attributed,
        "targeted_unjoined": targeted_unjoined,
    }


class _Axis:
    """One breakdown axis: its cells, and the packs that reached each cell.

    A cell's ``n`` is the number of attributed packs that contributed at
    least one item to it — not the window's pack count — so a namespace
    seen in one pack is suppressed while the headline states a ratio. The
    two are tracked together here because keeping them in parallel dicts
    is how they drift apart.
    """

    __slots__ = ("cells", "seen")

    def __init__(self) -> None:
        self.cells: dict[str, ValueBreakdown] = {}
        self.seen: dict[str, set[str]] = defaultdict(set)

    def cell(self, key: str, pack_id: str) -> ValueBreakdown:
        self.seen[key].add(pack_id)
        return self.cells.setdefault(key, ValueBreakdown(key=key))

    def finalize(self) -> dict[str, ValueBreakdown]:
        for key, packs_seen in self.seen.items():
            self.cells[key].attributed_packs = len(packs_seen)
        return self.cells


def _charge_cells(cells: tuple[ValueBreakdown, ...], tokens: int, verdict: str) -> None:
    """Add one item's tokens to every axis cell it belongs to."""
    for cell in cells:
        cell.injected_tokens += tokens
        if verdict == "helpful":
            cell.helpful_tokens += tokens
        elif verdict == "unhelpful":
            cell.unhelpful_tokens += tokens
        else:
            cell.unjudged_tokens += tokens


def _accumulate(
    attributed_ids: list[str],
    *,
    flat_packs: Mapping[str, Mapping[str, Any]],
    helpful_by_pack: Mapping[str, set[str]],
    unhelpful_by_pack: Mapping[str, set[str]],
    families_by_pack: Mapping[str, str],
) -> dict[str, Any]:
    """Sum injected tokens into helpful / unhelpful / unjudged, per axis."""
    totals = {"injected": 0, "helpful": 0, "unhelpful": 0}
    strategy_axis, type_axis, namespace_axis = _Axis(), _Axis(), _Axis()
    by_family: dict[str, ValueBreakdown] = {}
    distinct_helpful: set[str] = set()
    cited_not_served = 0

    for pack_id in attributed_ids:
        helpful = helpful_by_pack.get(pack_id, set())
        unhelpful = unhelpful_by_pack.get(pack_id, set())
        family = families_by_pack.get(pack_id, "(none)")
        family_cell = by_family.setdefault(family, ValueBreakdown(key=family))
        family_cell.attributed_packs += 1

        served: set[str] = set()
        charged = _charged_tokens(flat_packs[pack_id])
        for raw in flat_packs[pack_id].get("injected_items") or []:
            if not isinstance(raw, Mapping):
                continue
            item_id = raw.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                continue
            served.add(item_id)
            raw_tokens = raw.get("estimated_tokens")
            fallback = int(raw_tokens) if isinstance(raw_tokens, int | float) else 0
            tokens = charged.get(item_id, fallback)

            # An item cannot be both; helpful wins, so a contradictory
            # pair can never inflate the denominator's judged share.
            if item_id in helpful:
                verdict = "helpful"
            elif item_id in unhelpful:
                verdict = "unhelpful"
            else:
                verdict = "unjudged"

            _charge_cells(
                (
                    strategy_axis.cell(
                        str(raw.get("strategy_source") or "(none)"), pack_id
                    ),
                    type_axis.cell(str(raw.get("item_type") or "(none)"), pack_id),
                    namespace_axis.cell(item_namespace(item_id), pack_id),
                    family_cell,
                ),
                tokens,
                verdict,
            )

            totals["injected"] += tokens
            if verdict == "helpful":
                totals["helpful"] += tokens
                distinct_helpful.add(item_id)
            elif verdict == "unhelpful":
                totals["unhelpful"] += tokens

        cited_not_served += len((helpful | unhelpful) - served)

    return {
        "totals": totals,
        "by_strategy": strategy_axis.finalize(),
        "by_item_type": type_axis.finalize(),
        "by_item_namespace": namespace_axis.finalize(),
        "by_family": by_family,
        "distinct_helpful": distinct_helpful,
        "cited_not_served": cited_not_served,
    }


def _charged_tokens(payload: Mapping[str, Any]) -> dict[str, int]:
    """Per-item tokens the pack's budget walk actually charged.

    Built from ``budget_trace[]``, which records what was spent rather than
    what the item would have cost as an excerpt. Only *included* steps are
    read: an excluded step describes an item the pack never served.

    Returns an empty mapping for an event with no ``budget_trace`` — every
    caller then falls back to ``estimated_tokens``, which is the same
    number on any pack that served excerpts.
    """
    trace = payload.get("budget_trace")
    if not isinstance(trace, list):
        return {}
    out: dict[str, int] = {}
    for step in trace:
        if not isinstance(step, Mapping) or not step.get("included"):
            continue
        item_id = step.get("item_id")
        tokens = step.get("item_tokens")
        if isinstance(item_id, str) and item_id and isinstance(tokens, int | float):
            out[item_id] = int(tokens)
    return out


def _clean_ids(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {entry for entry in raw if isinstance(entry, str) and entry}


def _response_token_join(
    event_log: EventLog,
    *,
    since: datetime,
    limit: int,
    attributed_ids: set[str],
) -> dict[str, Any]:
    """Join ``TOKEN_TRACKED`` to attributed packs on ``pack_id``.

    The per-call half of the ratio. ``pack_id`` on ``TOKEN_TRACKED`` is
    new, so on any deployment with history this returns near-zero
    coverage for a while — which is reported, not smoothed over. A pack
    with no response measurement contributes nothing rather than falling
    back to its ``estimated_tokens`` sum: silently substituting a
    different quantity is how a metric ends up measuring something other
    than its name.
    """
    events = 0
    with_pack_id = 0
    tokens_by_pack: dict[str, int] = {}
    coverage = ScanCoverage()
    try:
        scan = scan_events(
            event_log, event_type=EventType.TOKEN_TRACKED, since=since, limit=limit
        )
        coverage = scan.coverage
        # ``scan.events`` is ascending, which is what "last write wins"
        # below means by "last". A descending feed would silently invert
        # that rule into "first write wins".
        for event in scan.events:
            events += 1
            payload = event.payload or {}
            pack_id = payload.get("pack_id")
            if not isinstance(pack_id, str) or not pack_id:
                continue
            with_pack_id += 1
            if pack_id not in attributed_ids:
                continue
            raw = payload.get("response_tokens")
            if isinstance(raw, int | float):
                # Last write wins per pack: a retried render is one
                # delivery, and summing would double-count its cost.
                tokens_by_pack[pack_id] = int(raw)
    except Exception:
        # GRACEFUL-DEGRADATION: the item-level half is the headline and
        # is already computed; a TOKEN_TRACKED read failure must degrade
        # to zero call-level coverage, not to no report at all.
        logger.exception("response_token_join_failed")

    return {
        "events": events,
        "with_pack_id": with_pack_id,
        "coverage": round(with_pack_id / events, 4) if events else 0.0,
        "packs_covered": len(tokens_by_pack),
        "tokens": sum(tokens_by_pack.values()),
        "coverage_scan": coverage,
    }


def _build_notes(
    *,
    attributed_packs: int,
    sectioned: int,
    cited_not_served: int,
    unjudged: int,
    injected: int,
    judged: int,
    helpful: int,
    scan: ScanCoverage,
    response: dict[str, Any],
) -> list[str]:
    """Caveats that travel with the numbers, in both output formats."""
    notes: list[str] = [
        (
            "useful_token_fraction measures the precision of what was served, "
            "not benefit: it cannot say whether memory improved the outcome, "
            "only what share of injected tokens the caller cited."
        ),
    ]
    if scan.truncated and scan.note:
        # First after the definitional note, and before every other
        # caveat: if the window is not the window, nothing below it means
        # what it says.
        notes.append(scan.note)
    if attributed_packs < MIN_ATTRIBUTED_PACKS:
        notes.append(
            f"Ratios suppressed: {attributed_packs} attributed pack(s) is "
            f"below the {MIN_ATTRIBUTED_PACKS}-pack minimum."
        )
    if unjudged and injected:
        lower = helpful / injected
        upper = (helpful + unjudged) / injected
        notes.append(
            f"{unjudged / injected:.0%} of injected tokens got no verdict "
            "(neither helpful nor unhelpful), so useful_token_fraction is a "
            f"lower bound: the true share lies in [{lower:.1%}, {upper:.1%}] "
            "(useful_token_fraction .. useful_token_fraction_upper_bound), "
            "and the width of that interval IS the unjudged share."
        )
        if judged:
            notes.append(
                f"Conditional on being graded, {helpful / judged:.1%} of the "
                f"{judged:,} judged tokens were cited helpful "
                "(useful_token_fraction_judged). This is NOT a corrected "
                "headline: extending it to the ungraded tokens assumes they "
                "are missing at random, and graders cite what helped rather "
                "than enumerating what they ignored (#364)."
            )
    if sectioned:
        notes.append(
            f"{sectioned} sectioned pack(s) excluded: build_sectioned emits "
            "no injected_items[], so they carry no per-item rows to attribute."
        )
    if cited_not_served:
        notes.append(
            f"{cited_not_served} cited id(s) were not served by the pack they "
            "cited; these deflate the numerator and usually mean a malformed "
            "item id."
        )
    if not response["with_pack_id"]:
        notes.append(
            "No TOKEN_TRACKED event carries a pack_id, so response-token cost "
            "is unattributed. That field is new — coverage grows from packs "
            "assembled after it shipped."
        )
    return notes


__all__ = [
    "MIN_ATTRIBUTED_PACKS",
    "NO_NAMESPACE",
    "collect_pack_verdicts",
    "SUPPRESSED_NO_JUDGED_TOKENS",
    "SUPPRESSED_THIN_SAMPLE",
    "PackValueReport",
    "ValueBreakdown",
    "item_namespace",
    "summarize_pack_value",
]
