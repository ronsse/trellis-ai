"""Stage 2 of the two-stage precedent gate: a pure, report-only activation test.

The gate has two stages. Stage 1 is the mint: a candidate is recorded when it is
new (``trellis worker distill-precedents``, dedup only). Stage 2 is activation:
whether the citations a target collected are better than chance. This module is
stage 2 as a pure function. It reads nothing, writes nothing and serves nothing;
``trellis curate evaluate-precedent-gate`` feeds it and reports the counts.

A target (an item id) is activated when all of these hold, checked in this order:

1. ``helpful``: at least :attr:`ActivationThresholds.min_helpful` packs cited it
   helpful.
2. ``unhelpful``: fewer packs cited it unhelpful than helpful (``u < h``).
3. ``sessions``: the helpful packs span at least
   :attr:`ActivationThresholds.min_distinct_sessions` distinct sessions.
4. ``p_value``: a within-pack permutation test gives P below
   :attr:`ActivationThresholds.max_p_value`.
5. ``lift``: the target's helpful rate is at least
   :attr:`ActivationThresholds.min_lift` times its pack baseline.

Whether this criterion is good enough is an open experiment, and this module only
counts how many targets it would pass. Nothing here is wired to serving.

The unit is a pack. A pack the target was served in counts once, whatever the
number of graders: verdicts are unioned per pack and a helpful citation beats an
unhelpful one on the same item, as in :mod:`trellis.retrieve.pack_value`. A
sectioned pack counts like a flat one, its sections flattened and an id served in
two sections served once.

The permutation null keeps the structure of the data
----------------------------------------------------
A pack has a size ``n`` (items served) and a citation count ``k`` (items cited
helpful). Chance, for a pack, is the helpful labels landing on its served items
in any order: the target is helpful there with probability ``k / n``. So the null
shuffles the labels **within each pack**, leaving every pack's size and citation
count as they were, and counts how often the target comes out helpful at least as
often as it did. Pack ``p`` contributes one Bernoulli draw with probability
``k_p / n_p``; the null count is their sum. ``P = (1 + #{null >= observed}) /
(1 + R)`` over ``R`` resamples, so it is never zero and ``R`` bounds how small it
can be (see :class:`ActivationThresholds`).

A null that ignores the pack (one global helpful rate, or labels shuffled across
packs) credits the target for being served in small, heavily-cited packs, where
it is helpful by arithmetic and not by merit. ``tests/unit/learning/
test_precedent_activation.py`` pins the difference on packs that vary in size and
citation count.

The baseline and the lift use the same quantity: the expected number of helpful
labels the target gets under the null is ``sum(k_p / n_p)``, so the baseline rate
is that over the target's servings, and ``lift`` is the observed helpful rate over
it. A lift of 1 is the null's mean.

Determinism: each target draws from its own ``random.Random`` seeded from
``(seed, item_id)``, never the global generator, so a target's P does not depend
on which other targets were evaluated or in what order.

Not the same count as ``citation_evidence``. The learning layer counts a helpful
citation per feedback event and counts ungraded packs as servings; this counts
one per pack and only packs a grader named items in. The report layer
(:mod:`trellis.learning.precedent_gate_report`) states the difference.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import structlog

from trellis.errors import ConfigError

logger = structlog.get_logger(__name__)

#: The criteria, in the order they are checked. A target's ``first_failed`` is
#: the first one it does not meet, so the attrition counts add up to the targets
#: that failed.
CRITERION_HELPFUL: Final = "helpful"
CRITERION_UNHELPFUL: Final = "unhelpful"
CRITERION_SESSIONS: Final = "sessions"
CRITERION_P_VALUE: Final = "p_value"
CRITERION_LIFT: Final = "lift"
CRITERIA: Final = (
    CRITERION_HELPFUL,
    CRITERION_UNHELPFUL,
    CRITERION_SESSIONS,
    CRITERION_P_VALUE,
    CRITERION_LIFT,
)

DEFAULT_MIN_HELPFUL: Final = 3
DEFAULT_MIN_DISTINCT_SESSIONS: Final = 2
DEFAULT_MAX_P_VALUE: Final = 0.05
DEFAULT_MIN_LIFT: Final = 1.2
DEFAULT_RESAMPLES: Final = 2000
DEFAULT_SEED: Final = 0

_MAX_RESAMPLES: Final = 1_000_000
_SETTING_PREFIX: Final = "precedent_activation."


def _require(ok: bool, field: str, message: str) -> None:
    if not ok:
        raise ConfigError(message, setting=f"{_SETTING_PREFIX}{field}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class ActivationThresholds:
    """The stage-2 criteria. Frozen and validated; ``u < h`` is structural.

    There is no setting behind these: they are arguments, so a report run can
    price a different floor without touching what any store does.

    Raises :class:`~trellis.errors.ConfigError` (``setting`` names the field) on
    nonsense: a non-positive floor, a lift below 1, a P bound outside ``(0, 1]``,
    and a resample count that cannot reach the P bound (the smallest P a run can
    report is ``1 / (1 + resamples)``, so a bound below that is unreachable).
    """

    min_helpful: int = DEFAULT_MIN_HELPFUL
    min_distinct_sessions: int = DEFAULT_MIN_DISTINCT_SESSIONS
    max_p_value: float = DEFAULT_MAX_P_VALUE
    min_lift: float = DEFAULT_MIN_LIFT
    resamples: int = DEFAULT_RESAMPLES
    seed: int = DEFAULT_SEED

    def __post_init__(self) -> None:
        for name in ("min_helpful", "min_distinct_sessions"):
            value = getattr(self, name)
            _require(
                _is_int(value) and value >= 1,
                name,
                f"{name} must be an integer of at least 1",
            )
        _require(
            _is_int(self.resamples) and 1 <= self.resamples <= _MAX_RESAMPLES,
            "resamples",
            f"resamples must be an integer in 1..{_MAX_RESAMPLES}",
        )
        _require(_is_int(self.seed), "seed", "seed must be an integer")
        for name in ("max_p_value", "min_lift"):
            value = getattr(self, name)
            _require(
                not isinstance(value, bool)
                and isinstance(value, int | float)
                and math.isfinite(value),
                name,
                f"{name} must be a finite number",
            )
        _require(
            0.0 < self.max_p_value <= 1.0,
            "max_p_value",
            "max_p_value must be in (0, 1]",
        )
        _require(self.min_lift >= 1.0, "min_lift", "min_lift must be at least 1.0")
        _require(
            1.0 / (1 + self.resamples) < self.max_p_value,
            "resamples",
            "resamples is too small to ever report a P below max_p_value",
        )


@dataclass(frozen=True, slots=True)
class ServedItem:
    """One row a pack served. ``rank`` is section-local on a sectioned pack."""

    item_id: str
    rank: int | None = None
    section: str | None = None


@dataclass(frozen=True, slots=True)
class GradedPack:
    """A served pack that a grader named items in, and what was named.

    Only graded packs belong here: a pack nobody graded has no helpful count to
    shuffle, and would pull every baseline toward zero.
    """

    pack_id: str
    #: ``None`` when the pack was built without a session id: unknown, never its
    #: own one-pack session.
    session_id: str | None
    served: tuple[ServedItem, ...]
    helpful: frozenset[str]
    unhelpful: frozenset[str]
    intent_family: str = ""

    @property
    def served_ids(self) -> frozenset[str]:
        return frozenset(row.item_id for row in self.served if row.item_id)

    @property
    def size(self) -> int:
        """Items served, each id once (sectioned packs flattened)."""
        return len(self.served_ids)

    @property
    def helpful_served(self) -> int:
        """Items served that were cited helpful: the null's fixed ``k``."""
        return len(self.served_ids & self.helpful)

    def verdict(self, item_id: str) -> str:
        """``helpful``, ``unhelpful`` or ``unjudged``; helpful wins on a clash."""
        if item_id in self.helpful:
            return "helpful"
        if item_id in self.unhelpful:
            return "unhelpful"
        return "unjudged"


@dataclass(frozen=True, slots=True)
class TargetActivation:
    """One target's stage-2 numbers and verdict."""

    item_id: str
    #: Graded packs the target was served in.
    servings: int
    helpful: int
    unhelpful: int
    distinct_helpful_sessions: int
    #: Helpful packs with no session id; they never count toward sessions.
    sessionless_helpful: int
    target_rate: float | None
    #: The helpful rate chance predicts over the same packs: ``sum(k/n)`` over
    #: the target's servings.
    baseline_rate: float | None
    #: ``target_rate / baseline_rate``; ``None`` when the baseline is zero.
    lift: float | None
    #: ``None`` when the permutation was not reached (an earlier criterion failed).
    p_value: float | None
    passes: bool
    #: The first criterion not met, or ``None`` when all are.
    first_failed: str | None


def within_pack_permutation_p(
    observed: int,
    pools: Sequence[tuple[int, int]],
    *,
    resamples: int,
    rng: random.Random,
) -> float:
    """``P(null >= observed)`` with the helpful labels shuffled within each pack.

    ``pools`` is ``(n, k)`` per pack the target was served in: items served and
    items cited helpful. Shuffling a pack's labels among its items makes the
    target helpful with probability ``k / n``, which is drawn directly (a draw of
    ``randrange(n) < k`` is the label at the target's position of a uniform
    shuffle). The pack sizes and citation counts never change.
    """
    at_least = 0
    for _ in range(resamples):
        total = 0
        for size, cited in pools:
            if rng.randrange(size) < cited:
                total += 1
        if total >= observed:
            at_least += 1
    return (1 + at_least) / (1 + resamples)


def _rng(seed: int, item_id: str) -> random.Random:
    # A string seed is hashed with SHA-512, the same on every run and host, and
    # never touches the module-level generator.
    return random.Random(f"{seed}\x1f{item_id}")  # noqa: S311 - not security


#: A pack the target was served in, with the ``(n, k)`` the null holds fixed.
_Serving = tuple[GradedPack, int, int]


def _evaluate(
    item_id: str,
    servings_of_target: Sequence[_Serving],
    thresholds: ActivationThresholds,
) -> TargetActivation:
    helpful = unhelpful = sessionless = 0
    sessions: set[str] = set()
    pools: list[tuple[int, int]] = []
    for pack, size, cited in servings_of_target:
        pools.append((size, cited))
        verdict = pack.verdict(item_id)
        if verdict == "helpful":
            helpful += 1
            if pack.session_id:
                sessions.add(pack.session_id)
            else:
                sessionless += 1
        elif verdict == "unhelpful":
            unhelpful += 1

    servings = len(servings_of_target)
    target_rate = helpful / servings if servings else None
    expected = sum(cited / size for size, cited in pools)
    baseline_rate = expected / servings if servings else None
    lift = (
        target_rate / baseline_rate
        if target_rate is not None and baseline_rate
        else None
    )

    p_value: float | None = None
    failed: str | None = None
    if helpful < thresholds.min_helpful:
        failed = CRITERION_HELPFUL
    elif unhelpful >= helpful:
        failed = CRITERION_UNHELPFUL
    elif len(sessions) < thresholds.min_distinct_sessions:
        failed = CRITERION_SESSIONS
    else:
        p_value = within_pack_permutation_p(
            helpful,
            pools,
            resamples=thresholds.resamples,
            rng=_rng(thresholds.seed, item_id),
        )
        if p_value >= thresholds.max_p_value:
            failed = CRITERION_P_VALUE
        elif lift is None or lift < thresholds.min_lift:
            failed = CRITERION_LIFT

    return TargetActivation(
        item_id=item_id,
        servings=servings,
        helpful=helpful,
        unhelpful=unhelpful,
        distinct_helpful_sessions=len(sessions),
        sessionless_helpful=sessionless,
        target_rate=target_rate,
        baseline_rate=baseline_rate,
        lift=lift,
        p_value=p_value,
        passes=failed is None,
        first_failed=failed,
    )


def evaluate_targets(
    item_ids: Iterable[str],
    packs: Iterable[GradedPack],
    thresholds: ActivationThresholds | None = None,
) -> dict[str, TargetActivation]:
    """Evaluate each target over the graded packs that served it.

    ``packs`` should be one comparable population (the caller filters by intent
    family). A target served in none of them is evaluated over no servings and
    fails ``helpful``. A pack naming the same id twice counts it once.
    """
    limits = thresholds or ActivationThresholds()
    by_item: defaultdict[str, list[_Serving]] = defaultdict(list)
    for pack in packs:
        ids = pack.served_ids
        facts = (pack, len(ids), len(ids & pack.helpful))
        for item_id in ids:
            by_item[item_id].append(facts)
    results: dict[str, TargetActivation] = {}
    for item_id in item_ids:
        if item_id in results:
            continue
        results[item_id] = _evaluate(item_id, by_item.get(item_id, []), limits)
    logger.debug(
        "precedent_activation_evaluated",
        targets=len(results),
        passed=sum(1 for r in results.values() if r.passes),
    )
    return results


def evaluate_target(
    item_id: str,
    packs: Iterable[GradedPack],
    thresholds: ActivationThresholds | None = None,
) -> TargetActivation:
    """:func:`evaluate_targets` for one target."""
    return evaluate_targets([item_id], packs, thresholds)[item_id]


__all__ = [
    "CRITERIA",
    "CRITERION_HELPFUL",
    "CRITERION_LIFT",
    "CRITERION_P_VALUE",
    "CRITERION_SESSIONS",
    "CRITERION_UNHELPFUL",
    "DEFAULT_MAX_P_VALUE",
    "DEFAULT_MIN_DISTINCT_SESSIONS",
    "DEFAULT_MIN_HELPFUL",
    "DEFAULT_MIN_LIFT",
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "ActivationThresholds",
    "GradedPack",
    "ServedItem",
    "TargetActivation",
    "evaluate_target",
    "evaluate_targets",
    "within_pack_permutation_p",
]
