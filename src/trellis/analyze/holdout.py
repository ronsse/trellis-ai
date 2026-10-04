"""The pack holdout experiment's pre-registered analysis (effect work item 5).

#701 added ``TRELLIS_PACK_HOLDOUT_RATE``. Above zero, each built pack is
withheld with that probability, drawn from a hash of its pack id, and the
agent receives an empty pack in the normal shape. Every ``PACK_ASSEMBLED``
row since #701 records ``holdout`` and ``holdout_rate``; a withheld row keeps
its would-be pack under ``holdout_items`` (flat) or ``holdout_sections``
(sectioned). This module reads that experiment and writes nothing.

**Units.** One unit per sub-agent task: a captured transcript whose latest
``CAPTURE_SESSION_PACKS`` join names a parent session. The task's first pack
is the first pack id its join parsed, and its arm is that pack's
``holdout`` (intention to treat), so a task whose later calls fell in the
other arm keeps its first arm and is counted. A task is eligible when its
first pack

* was written inside the window by a build that records ``holdout``
  (older rows are counted and left out: they are not experiment rows),
* was written at the analysed rate,
* is non-empty in its own arm's terms: a withheld row's would-be items
  count, so eligibility is defined identically in both arms,

and the task has finished: a completed, non-dry-run capture sweep ran at
least ``settle_hours`` after its latest join. A changed transcript gets a
new join, so that sweep saw nothing new (the pre-registration's "no new
record for 3 h").

**Exclusions.** Usage-limit cut-offs (``outcome.ended_on_error``) are
post-treatment and excluded unless ``itt``. The pre-treatment exclusion, a
first retrieval made after a commit or a stop-hook nudge, needs the first
retrieval's position in the transcript, which capture does not record; it
is reported as not applied, and the post-hoc share as not measurable.

**Primary statistic.** The stratum-weighted difference in mean outcome,
served minus withheld, with weights ``n1*n0/(n1+n0)`` over strata of parent
session x ISO week of the first pack. A stratum holding one arm has weight
zero, contributes nothing and is counted. The p-value is two-sided, from
within-stratum permutations of the arm labels, so every permutation keeps
each stratum's arm counts; the confidence interval is a percentile
interval from a stratified bootstrap that resamples tasks within each
stratum, arm labels attached, and recomputes the weights per replicate.

**Power** re-runs the prestudy's simulation at the realised N: stratified
bootstraps of the analysed outcomes, with any estimated effect removed,
random arms at the analysed rate (the planned 50/50 when no rate in (0, 1)
ran), and a shift added to the served arm, each tested with
:data:`POWER_PERMUTATIONS` within-stratum permutations. The planned figures
are parameters, defaulting to the pre-registration's (MDE 0.33 log at
N about 114).

**The [R] re-measure.** The descriptive block reprints each figure the
pre-registration took from the prestudy and marks [R], computed the
prestudy's way: the analytic MDE ``(t(0.975) + t(0.80)) * SD * sqrt(4 / N)``
with t at ``N - 2`` degrees of freedom (:func:`analytic_mde`), at 30, 60 and
90 days of analysed tasks at the measured rate; the N a 10% effect needs;
the largest parent session's share of eligible tasks; and the same counts
and MDE for main sessions with their sub-agent tasks rolled up. A figure the
rows cannot give is ``None``, and ``not_measurable`` says what is missing.

**Output** is counts and statistics only. No pack id, session id, intent or
item text leaves this module, and a non-significant result reads
:data:`NO_EFFECT_VERDICT`, never "no effect".
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import NormalDist
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.core.pack_holdout import (
    HOLDOUT_ITEMS_KEY,
    HOLDOUT_KEY,
    HOLDOUT_RATE_KEY,
    HOLDOUT_SECTIONS_KEY,
    is_holdout,
)
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event, EventLog

#: Two-sided significance level, fixed by the pre-registration.
ALPHA = 0.05
#: The power the pre-registration sized the experiment for.
TARGET_POWER = 0.80
#: The pre-registration pauses the experiment when the arm ratio fails a
#: binomial test at this level.
ARM_RATIO_PAUSE_P = 0.001
#: Within-stratum permutations per simulated experiment (the prestudy's 499).
POWER_PERMUTATIONS = 499
#: The pre-registration's planned figures, and the prestudy's seed.
DEFAULT_PLANNED_MDE = 0.33
DEFAULT_PLANNED_N = 114
DEFAULT_SEED = 20261004
#: The pre-registration's "finished": no new record for this many hours.
DEFAULT_SETTLE_HOURS = 3.0
DEFAULT_OUTCOME = "log1p_turns"

DETECTED_VERDICT = f"difference detected at alpha {ALPHA:g}"
NO_EFFECT_VERDICT = "no effect larger than the MDE detected"
NO_TASKS = "no tasks"
NO_WITHHELD_ARM = "no withheld arm"
NO_SERVED_ARM = "no served arm"
NO_BOTH_ARM_STRATUM = "no stratum with both arms"

_DAY_SECONDS = 86_400
_RATE_TOLERANCE = 1e-9
#: Cap on one permutation block's cells, so memory stays flat at any N.
_BLOCK_CELLS = 2_000_000
#: Shifts tried when searching for the MDE at the target power.
_POWER_GRID_POINTS = 241
#: z(0.975) + z(0.80): the normal-approximation MDE multiplier.
_MDE_MULTIPLIER = 2.8016
_SOURCE_SYSTEM_DEFAULT = "claude-code"
#: A spread, or a stratum that can hold both arms, needs two tasks.
_PAIR = 2
#: The pre-registration's 50/50 split, simulated when no rate in (0, 1) ran.
_PLANNED_SHARE = 0.5
#: Days of evidence the pre-registration's MDE table reads.
HORIZON_DAYS = (30, 60, 90)
#: The analytic MDE needs ``N - 2 >= 4`` degrees of freedom; the prestudy
#: leaves it undefined below.
MIN_FORMULA_N = 6
#: The pre-registration's "10% effect".
_TEN_PERCENT = 0.1
_EMPTY_WINDOW = "the evidence window is empty"
_NO_ELIGIBLE = "no eligible task"

_PRE_TREATMENT_NOTE = (
    "Not applied: capture records neither the first retrieval's position in "
    "the transcript nor a commit or stop-hook nudge before it, so a first "
    "retrieval made after the work cannot be told apart."
)
_POST_HOC_MISSING = (
    "the post-hoc rule needs the first ask's position against stop-hook "
    "nudges, commits and the last 10% of turns, and capture records none of "
    "them"
)
_POST_HOC_NOTE = f"Not measurable from capture rows: {_POST_HOC_MISSING}."
_NON_EPHEMERAL_NOTE = (
    "Upstream: session capture writes no join for a session in an ephemeral "
    "project, so none reaches this analysis or its counts."
)
_COVARIATE_NOTE = (
    "Covariate-residualised permutation (secondary analysis): not applied, "
    "because capture records no brief-length field."
)


class HoldoutAnalysisError(ValueError):
    """The analysis cannot run as asked; the message says what to change."""


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


class HoldoutRows(TrellisModel):
    """``PACK_ASSEMBLED`` rows inside the evidence window."""

    scanned: int = 0
    without_holdout_key: int = 0
    by_rate: dict[str, int] = Field(default_factory=dict)
    at_rate: int = 0
    withheld_at_rate: int = 0


class HoldoutFunnel(TrellisModel):
    """Every captured session, from scanned join to analysed unit.

    Each stage counts the sessions it removes, in order, so the counts
    from ``main_sessions`` to ``unfinished`` plus ``eligible`` sum to
    ``sessions``, and ``eligible`` minus ``cut_offs_excluded`` and
    ``outcome_missing`` is ``analysed``. ``eligible_with_unparsed_pack_ids``
    is not a stage: it counts the eligible tasks whose join reports a pack
    id the capture could not parse, so their first parsed pack may not be
    their first call.
    """

    joins_scanned: int = 0
    sessions: int = 0
    main_sessions: int = 0
    no_outcome: int = 0
    no_retrieval: int = 0
    first_pack_not_found: int = 0
    outside_window: int = 0
    first_pack_old_build: int = 0
    other_rate: int = 0
    empty_first_pack: int = 0
    unfinished: int = 0
    eligible: int = 0
    cut_offs_excluded: int = 0
    outcome_missing: int = 0
    analysed: int = 0
    eligible_with_unparsed_pack_ids: int = 0


class HoldoutExclusion(TrellisModel):
    """One pre-registered population rule or exclusion, and what it removed.

    ``excluded`` is ``None`` when this analysis cannot count what the rule
    removed: it was not applied, or it was applied before the rows it reads.
    """

    name: str
    timing: str
    applied: bool
    excluded: int | None
    note: str


class ArmCounts(TrellisModel):
    served: int = 0
    withheld: int = 0


class ArmShares(TrellisModel):
    served: float | None = None
    withheld: float | None = None


class ArmRatioCheck(TrellisModel):
    """An exact two-sided binomial test of the withheld share against the rate."""

    population: str
    n: int
    withheld: int
    expected_share: float
    p_value: float


class HoldoutHorizon(TrellisModel):
    """The analytic MDE after ``days`` of evidence at the measured rate.

    ``n`` is the measured count per 30 days scaled to ``days``. ``ratio`` is
    ``exp(mde)``, the multiplicative change a log1p outcome's MDE stands
    for; ``share_of_mean`` is ``mde`` over the mean it is compared with, for
    an outcome on its own scale. ``not_measurable`` says why ``mde`` is
    ``None``.
    """

    days: int
    n: float | None
    mde: float | None
    ratio: float | None
    share_of_mean: float | None
    not_measurable: str | None


class HoldoutSessions(TrellisModel):
    """Main sessions with their sub-agent tasks rolled up (the prestudy's unit).

    A main session is its own join plus every join naming it as parent,
    dated by the latest of those joins; one dated outside the window is not
    counted. One whose main transcript never joined, or with a join no
    completed sweep has settled, is counted and left out. Of the rest,
    ``reaching_retrieval`` made a retrieval call (a parsed pack id or a
    retrieval result), ``eligible`` got a non-empty pack in its own arm's
    terms, and ``analysed`` is eligible with an outcome on every join,
    summed field by field. Cut-offs are kept, as in the prestudy.
    ``outcome_sd`` is the plain SD of the analysed sessions' outcomes.
    """

    rolled_up: int
    not_finished: int
    without_main_join: int
    reaching_retrieval: int
    reaching_retrieval_per_30d: float | None
    eligible: int
    eligible_per_30d: float | None
    analysed: int
    analysed_per_30d: float | None
    outcome_sd: float | None
    mde_by_horizon: list[HoldoutHorizon]
    not_measurable: dict[str, str] = Field(default_factory=dict)


class HoldoutDescriptive(TrellisModel):
    """The pre-registration's [R] re-measure, reported in every flag state.

    ``between_parent_share`` is bias-adjusted (epsilon-squared: one minus
    the within-parent variance over the total variance), floored at 0, as
    in the prestudy. ``pr_base_rate_served`` counts served-arm tasks only;
    ``prs_created_mean`` is over both arms. ``top_parent_share`` is the
    largest parent session's share of eligible tasks, both arms and
    cut-offs included. ``mde_by_horizon`` uses the within-parent SD and N
    from analysed tasks per 30 days; ``n_for_10pct_effect`` is the N that
    MDE formula needs for x0.9 on a log1p outcome, or for 10% of the
    served-arm mean otherwise. ``not_measurable`` maps each ``None``
    figure to what the rows lack.
    """

    eligible_tasks: int
    eligible_per_30d: float | None
    analysed_tasks: int
    analysed_per_30d: float | None
    outcome_sd_within_parent: float | None
    outcome_sd_total: float | None
    between_parent_share: float | None
    parents: int
    packs_per_task: float | None
    tasks_with_several_packs: int
    post_hoc_share: float | None
    post_hoc_note: str
    cut_off_share: float | None
    cut_off_share_by_arm: ArmShares
    pr_base_rate_served: float | None
    prs_created_mean: float | None
    eligible_by_arm: ArmCounts
    cut_offs_by_arm: ArmCounts
    analysed_by_arm: ArmCounts
    tasks_with_both_arms: int
    arm_ratio: list[ArmRatioCheck]
    top_parent_share: float | None
    mde_by_horizon: list[HoldoutHorizon]
    n_for_10pct_effect: int | None
    sessions: HoldoutSessions
    not_measurable: dict[str, str] = Field(default_factory=dict)


class HoldoutInference(TrellisModel):
    """The primary analysis, or the reason it cannot run."""

    status: str
    verdict: str
    mean_served: float | None
    mean_withheld: float | None
    difference: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    alpha: float
    permutations: int
    bootstraps: int
    strata: int
    strata_both_arms: int
    strata_one_arm: int
    tasks_in_one_arm_strata: int


class HoldoutPower(TrellisModel):
    """Achieved power at the realised N against the planned figures."""

    status: str
    planned_mde: float
    planned_n: int
    target_power: float
    withheld_share: float
    realised_n: int
    realised_sd: float | None
    power_at_planned_mde: float | None
    mde_at_target_power: float | None
    sims: int
    sim_permutations: int


class HoldoutReport(TrellisModel):
    status: str = "ok"
    window_days: int
    effective_window_days: float
    since: str
    until: str
    rate: float | None
    outcome: str
    itt: bool
    seed: int
    settle_hours: float
    rows: HoldoutRows
    funnel: HoldoutFunnel
    exclusions: list[HoldoutExclusion]
    descriptive: HoldoutDescriptive
    inference: HoldoutInference
    power: HoldoutPower
    scan: ScanCoverage
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _count(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _log1p_count(value: object) -> float | None:
    number = _count(value)
    return None if number is None else math.log1p(number)


def _log1p_tokens(outcome: Mapping[str, Any]) -> float | None:
    counts = [_count(outcome.get(name)) for name in _TOKEN_FIELDS]
    present = [count for count in counts if count is not None]
    return math.log1p(sum(present)) if present else None


def _any_commit(outcome: Mapping[str, Any]) -> float | None:
    commits = _count(outcome.get("commits"))
    return None if commits is None else float(commits > 0)


#: Outcomes read from ``SessionOutcome``; ``None`` means not captured. The
#: first four are the task's choices; ``log1p_commits`` and ``any_commit``
#: are the pre-registration's commit guardrails.
OUTCOMES: dict[str, Callable[[Mapping[str, Any]], float | None]] = {
    "log1p_turns": lambda outcome: _log1p_count(outcome.get("assistant_turns")),
    "prs_created": lambda outcome: _count(outcome.get("prs_created")),
    "log1p_tokens": _log1p_tokens,
    "commits": lambda outcome: _count(outcome.get("commits")),
    "log1p_commits": lambda outcome: _log1p_count(outcome.get("commits")),
    "any_commit": _any_commit,
}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _codes(strata: npt.ArrayLike) -> npt.NDArray[np.intp]:
    """Stratum labels as dense integer codes ``0..G-1``."""
    _, inverse = np.unique(np.asarray(strata), return_inverse=True)
    return np.asarray(inverse, dtype=np.intp).reshape(-1)


@dataclass(frozen=True)
class _Design:
    """Per-unit centred outcomes and the total weight of one assignment."""

    codes: npt.NDArray[np.intp]
    centred: npt.NDArray[np.float64]
    total_weight: float


def _design(
    y: npt.NDArray[np.float64],
    served: npt.NDArray[np.bool_],
    codes: npt.NDArray[np.intp],
) -> _Design:
    groups = int(codes.max()) + 1 if codes.size else 0
    n = np.bincount(codes, minlength=groups).astype(float)
    n1 = np.bincount(codes, weights=served.astype(float), minlength=groups)
    present = n > 0
    weights = np.zeros(groups)
    weights[present] = n1[present] * (n[present] - n1[present]) / n[present]
    means = np.zeros(groups)
    means[present] = (
        np.bincount(codes, weights=y, minlength=groups)[present] / n[present]
    )
    return _Design(codes, y - means[codes], float(weights.sum()))


def weighted_difference(
    y: npt.ArrayLike, served: npt.ArrayLike, strata: npt.ArrayLike
) -> float | None:
    """Served minus withheld, stratum by stratum, weighted by ``n1*n0/n``.

    Equal to ``sum(y_i - mean_s(i) for served i) / sum(n1*n0/n)``, the form
    the permutation test uses. ``None`` when no stratum holds both arms.
    """
    values = np.asarray(y, dtype=float)
    arms = np.asarray(served, dtype=bool)
    design = _design(values, arms, _codes(strata))
    if design.total_weight <= 0:
        return None
    return float(arms.astype(float) @ design.centred / design.total_weight)


def permute_within_strata(
    served: npt.ArrayLike,
    strata: npt.ArrayLike,
    rng: np.random.Generator,
    n: int,
) -> npt.NDArray[np.bool_]:
    """``n`` relabellings of ``served``, each shuffled inside every stratum.

    Row ``r`` keeps every stratum's number of served units: sorting
    ``code + U(0, 1)`` groups positions by stratum in a random order within
    each, and the stratum-sorted labels are written back onto them.
    """
    arms = np.asarray(served, dtype=bool)
    codes = _codes(strata)
    order = np.argsort(codes[None, :] + rng.random((n, codes.size)), axis=1)
    template = arms[np.argsort(codes, kind="stable")]
    perms = np.empty((n, codes.size), dtype=bool)
    np.put_along_axis(perms, order, np.broadcast_to(template, perms.shape), axis=1)
    return perms


def _exceed_tolerance(magnitude: float | npt.NDArray[np.float64]) -> Any:
    return 1e-9 * np.maximum(1.0, magnitude)


def permutation_p_value(
    y: npt.ArrayLike,
    served: npt.ArrayLike,
    strata: npt.ArrayLike,
    rng: np.random.Generator,
    permutations: int,
) -> float | None:
    """Two-sided p-value of :func:`weighted_difference` under the sharp null.

    ``(1 + b) / (permutations + 1)``, where ``b`` counts within-stratum
    permutations whose statistic is at least as far from zero as the
    observed one. ``None`` when no stratum holds both arms.
    """
    values = np.asarray(y, dtype=float)
    arms = np.asarray(served, dtype=bool)
    design = _design(values, arms, _codes(strata))
    if design.total_weight <= 0:
        return None
    observed = abs(float(arms.astype(float) @ design.centred / design.total_weight))
    threshold = observed - _exceed_tolerance(observed)
    block = max(1, min(1_000, _BLOCK_CELLS // max(1, values.size)))
    beyond = 0
    done = 0
    while done < permutations:
        size = min(block, permutations - done)
        perms = permute_within_strata(arms, design.codes, rng, size)
        stats = perms.astype(float) @ design.centred / design.total_weight
        beyond += int(np.count_nonzero(np.abs(stats) >= threshold))
        done += size
    return (1 + beyond) / (permutations + 1)


def bootstrap_ci(
    y: npt.ArrayLike,
    served: npt.ArrayLike,
    strata: npt.ArrayLike,
    rng: np.random.Generator,
    bootstraps: int,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """Percentile interval of :func:`weighted_difference` over task resamples.

    Each replicate draws every stratum's tasks with replacement from that
    stratum, arm labels attached, and recomputes the ``n1*n0/n`` weights, so
    a stratum's arm split varies as it would in a fresh draw of tasks. A
    replicate in which no stratum holds both arms is dropped. ``None`` when
    no stratum holds both arms, or no replicate kept one.
    """
    values = np.asarray(y, dtype=float)
    arms = np.asarray(served, dtype=bool)
    codes = _codes(strata)
    if _design(values, arms, codes).total_weight <= 0:
        return None
    order = np.argsort(codes, kind="stable")
    sorted_codes = codes[order]
    sorted_y = values[order]
    sorted_served = arms[order].astype(float)
    sizes = np.bincount(sorted_codes)
    starts = np.concatenate(([0], np.cumsum(sizes)[:-1]))
    column_start = starts[sorted_codes]
    column_size = sizes[sorted_codes]
    block = max(1, min(bootstraps, _BLOCK_CELLS // max(1, values.size)))
    kept: list[npt.NDArray[np.float64]] = []
    done = 0
    while done < bootstraps:
        rows = min(block, bootstraps - done)
        picks = column_start + rng.integers(0, column_size, size=(rows, values.size))
        drawn_y = sorted_y[picks]
        drawn_served = sorted_served[picks]
        n1 = np.add.reduceat(drawn_served, starts, axis=1)
        n0 = sizes - n1
        sum1 = np.add.reduceat(drawn_y * drawn_served, starts, axis=1)
        sum0 = np.add.reduceat(drawn_y, starts, axis=1) - sum1
        both = (n1 > 0) & (n0 > 0)
        weights = np.where(both, n1 * n0 / sizes, 0.0)
        gaps = np.where(both, sum1 / np.maximum(n1, 1) - sum0 / np.maximum(n0, 1), 0.0)
        total = weights.sum(axis=1)
        good = total > 0
        kept.append((weights * gaps).sum(axis=1)[good] / total[good])
        done += rows
    stats = np.concatenate(kept)
    if stats.size == 0:
        return None
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(stats, [tail, 1.0 - tail])
    return float(low), float(high)


def simulate_power(
    pool: npt.ArrayLike,
    strata: npt.ArrayLike,
    rng: np.random.Generator,
    deltas: Sequence[float],
    *,
    sims: int,
    permutations: int = POWER_PERMUTATIONS,
    withheld_share: float = 0.5,
) -> list[float]:
    """Share of simulated experiments the permutation test rejects, per shift.

    Each simulation draws a stratified bootstrap of ``pool``, withholds
    each task with probability ``withheld_share``, adds ``delta`` to the
    served arm and runs the within-stratum permutation test at
    :data:`ALPHA`. A shift moves every relabelled statistic linearly, so
    one set of permutations serves every ``delta``.
    """
    outcomes = np.asarray(pool, dtype=float)
    codes = _codes(strata)
    groups = [np.flatnonzero(codes == code) for code in range(int(codes.max()) + 1)]
    sizes = np.bincount(codes).astype(float)
    shifts = np.asarray(deltas, dtype=float)
    rejections = np.zeros(shifts.size)
    for _ in range(sims):
        y = np.empty_like(outcomes)
        for members in groups:
            y[members] = outcomes[rng.choice(members, size=members.size)]
        served = rng.random(outcomes.size) >= withheld_share
        design = _design(y, served, codes)
        if design.total_weight <= 0:
            continue
        share_served = np.bincount(codes, weights=served.astype(float)) / sizes
        offsets = served.astype(float) - share_served[codes]
        perms = permute_within_strata(served, codes, rng, permutations).astype(float)
        base = perms @ design.centred
        slope = perms @ offsets
        observed = served.astype(float) @ design.centred / design.total_weight
        observed_stats = np.abs(observed + shifts)
        stats = np.abs(
            (base[None, :] + shifts[:, None] * slope[None, :]) / design.total_weight
        )
        threshold = observed_stats - _exceed_tolerance(observed_stats)
        beyond = np.count_nonzero(stats >= threshold[:, None], axis=1)
        rejections += (1 + beyond) / (permutations + 1) < ALPHA
    return [float(value) for value in rejections / sims]


def binomial_two_sided(k: int, n: int, p: float) -> float:
    """Exact two-sided binomial p-value: the mass of outcomes no likelier than ``k``."""
    if n <= 0:
        return 1.0
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n = math.lgamma(n + 1)
    log_pmf = [
        log_n
        - math.lgamma(i + 1)
        - math.lgamma(n - i + 1)
        + i * log_p
        + (n - i) * log_q
        for i in range(n + 1)
    ]
    bound = log_pmf[k] + 1e-7
    return min(1.0, math.fsum(math.exp(value) for value in log_pmf if value <= bound))


def _t_quantile(p: float, df: float) -> float:
    """Student's t quantile by the Cornish-Fisher expansion, without SciPy.

    Four terms in ``1/df`` around the normal quantile: within 0.04% of the
    exact quantile at 4 degrees of freedom, the fewest :func:`analytic_mde`
    uses, and closer as ``df`` grows.
    """
    z = NormalDist().inv_cdf(p)
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    g4 = (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160
    return z + g1 / df + g2 / df**2 + g3 / df**3 + g4 / df**4


def analytic_mde(sd: float, n: float) -> float | None:
    """The prestudy's MDE: ``(t(0.975) + t(0.80)) * sd * sqrt(4 / n)``.

    The two-sample formula for ``n`` units split 50/50, two-sided at
    :data:`ALPHA` with :data:`TARGET_POWER`, its t quantiles at ``n - 2``
    degrees of freedom. ``None`` below :data:`MIN_FORMULA_N` units, where
    the prestudy leaves it undefined.
    """
    if n < MIN_FORMULA_N:
        return None
    df = n - _PAIR
    multiplier = _t_quantile(1 - ALPHA / 2, df) + _t_quantile(TARGET_POWER, df)
    return multiplier * sd * math.sqrt(4 / n)


def n_for_mde(sd: float, effect: float) -> int:
    """The smallest N whose :func:`analytic_mde` at ``sd`` is at most ``effect``.

    Raises:
        ValueError: ``effect`` is not positive.
    """
    if effect <= 0:
        message = f"effect must be positive, got {effect:g}"
        raise ValueError(message)

    def reaches(n: int) -> bool:
        mde = analytic_mde(sd, n)
        return mde is not None and mde <= effect

    low, high = MIN_FORMULA_N - 1, MIN_FORMULA_N
    while not reaches(high):
        low, high = high, 2 * high
    while high - low > 1:
        middle = (low + high) // 2
        if reaches(middle):
            high = middle
        else:
            low = middle
    return high


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


@dataclass
class _Unit:
    parent: str
    stratum: str
    withheld: bool
    packs: int
    both_arms: bool
    cut_off: bool
    outcome: Mapping[str, Any]


@dataclass
class _Rows:
    window: list[Event] = field(default_factory=list)

    def report(self, rate: float | None) -> HoldoutRows:
        keyed = [e for e in self.window if HOLDOUT_KEY in (e.payload or {})]
        at_rate = [e for e in keyed if _matches_rate(e.payload or {}, rate)]
        return HoldoutRows(
            scanned=len(self.window),
            without_holdout_key=len(self.window) - len(keyed),
            by_rate=dict(
                sorted(Counter(_rate_label(e.payload) for e in keyed).items())
            ),
            at_rate=len(at_rate),
            withheld_at_rate=sum(is_holdout(e.payload) for e in at_rate),
        )


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _rate_value(payload: Mapping[str, Any] | None) -> float | None:
    value = (payload or {}).get(HOLDOUT_RATE_KEY)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _rate_label(payload: Mapping[str, Any] | None) -> str:
    value = _rate_value(payload)
    return "unknown" if value is None else f"{value:g}"


def _matches_rate(payload: Mapping[str, Any], rate: float | None) -> bool:
    value = _rate_value(payload)
    return (
        rate is not None and value is not None and abs(value - rate) <= _RATE_TOLERANCE
    )


def _section_items(sections: object) -> int:
    if not isinstance(sections, list):
        return 0
    total = 0
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        count = section.get("items_count")
        if isinstance(count, int) and not isinstance(count, bool):
            total += count
        elif isinstance(section.get("item_ids"), list):
            total += len(section["item_ids"])
    return total


def _pack_size(payload: Mapping[str, Any]) -> int:
    """Items the pack held, or would have held when it was withheld."""
    if is_holdout(payload):
        items = payload.get(HOLDOUT_ITEMS_KEY)
        if isinstance(items, list):
            return len(items)
        return _section_items(payload.get(HOLDOUT_SECTIONS_KEY))
    for key in ("items_count", "total_items"):
        count = payload.get(key)
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    ids = payload.get("injected_item_ids")
    if isinstance(ids, list):
        return len(ids)
    return _section_items(payload.get("sections"))


def _iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _latest_sweeps(events: Sequence[Event]) -> dict[str, datetime]:
    """The latest completed, non-dry-run capture sweep per source system."""
    latest: dict[str, datetime] = {}
    for event in events:
        payload = event.payload or {}
        if payload.get("dry_run") is True:
            continue
        source = payload.get("source_system")
        if not isinstance(source, str) or not source:
            if not event.entity_id:
                continue
            source = event.entity_id.removeprefix("capture:")
        at = _as_utc(event.occurred_at)
        if source not in latest or at > latest[source]:
            latest[source] = at
    return latest


def _pack_ids(payload: Mapping[str, Any]) -> list[str]:
    """The pack ids a join parsed, in call order."""
    return [p for p in payload.get("pack_ids") or [] if isinstance(p, str) and p]


def _finished(join: Event, sweeps: Mapping[str, datetime], settle: timedelta) -> bool:
    """A completed sweep for the join's source system ran ``settle`` after it."""
    source = (join.payload or {}).get("source_system")
    swept = sweeps.get(
        source if isinstance(source, str) and source else _SOURCE_SYSTEM_DEFAULT
    )
    return swept is not None and swept >= _as_utc(join.occurred_at) + settle


def _find_pack(
    event_log: EventLog, packs: dict[str, Event], pack_id: str
) -> Event | None:
    """The pack's row from the scan, or by id when it predates the scan."""
    if pack_id in packs:
        return packs[pack_id]
    found = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=1
    )
    return found[0] if found else None


def _resolve_rate(rows: _Rows, rate: float | None) -> tuple[float | None, list[str]]:
    """The rate to analyse, refusing to guess between several."""
    keyed = [e.payload or {} for e in rows.window if HOLDOUT_KEY in (e.payload or {})]
    present = Counter(
        value for value in (_rate_value(p) for p in keyed) if value is not None
    )
    if rate is not None:
        if any(abs(value - rate) <= _RATE_TOLERANCE for value in present):
            return rate, []
        return rate, [f"No row in the window was written at rate {rate:g}."]
    if not present:
        return None, []
    if len(present) > 1:
        listing = ", ".join(
            f"{value:g} ({count} rows)" for value, count in sorted(present.items())
        )
        message = (
            f"Several holdout rates are present in the window: {listing}. "
            "Name the one to analyse with --rate."
        )
        raise HoldoutAnalysisError(message)
    return next(iter(present)), []


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def _validate(
    *,
    days: int,
    rate: float | None,
    outcome: str,
    permutations: int,
    bootstraps: int,
    power_sims: int,
    planned_mde: float,
    planned_n: int,
    settle_hours: float,
    seed: int,
    limit: int,
) -> None:
    """Refuse out-of-range options, naming each one as the CLI spells it."""
    problems: list[str] = []
    if outcome not in OUTCOMES:
        problems.append(f"--outcome {outcome!r} is not one of: {', '.join(OUTCOMES)}")
    counts = {
        "--days": days,
        "--permutations": permutations,
        "--bootstraps": bootstraps,
        "--power-sims": power_sims,
        "--planned-n": planned_n,
        "--limit": limit,
    }
    problems.extend(
        f"{name} must be at least 1, got {value}"
        for name, value in counts.items()
        if value < 1
    )
    if rate is not None and not 0.0 <= rate <= 1.0:
        problems.append(f"--rate must lie in [0, 1], got {rate:g}")
    if planned_mde <= 0:
        problems.append(f"--planned-mde must be positive, got {planned_mde:g}")
    if settle_hours < 0:
        problems.append(f"--settle-hours must not be negative, got {settle_hours:g}")
    if seed < 0:
        problems.append(f"--seed must not be negative, got {seed}")
    if problems:
        message = "; ".join(problems) + "."
        raise HoldoutAnalysisError(message)


def analyze_holdout(
    event_log: EventLog,
    *,
    days: int = 60,
    until: datetime | None = None,
    rate: float | None = None,
    outcome: str = DEFAULT_OUTCOME,
    itt: bool = False,
    permutations: int = 10_000,
    bootstraps: int = 4_000,
    power_sims: int = 600,
    planned_mde: float = DEFAULT_PLANNED_MDE,
    planned_n: int = DEFAULT_PLANNED_N,
    settle_hours: float = DEFAULT_SETTLE_HOURS,
    seed: int = DEFAULT_SEED,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> HoldoutReport:
    """Run the pre-registered holdout analysis over ``days`` before ``until``.

    Args:
        event_log: Operational event log holding ``PACK_ASSEMBLED``,
            ``CAPTURE_SESSION_PACKS`` and ``CAPTURE_SWEEP_COMPLETED``.
        days: Window length; a task belongs to it by its first pack's time.
        until: Window end (default: now). Joins and sweeps after it are
            read, because a task's outcome is captured after it starts.
        rate: The ``holdout_rate`` to analyse. Default: the single rate
            present in the window.
        outcome: A key of :data:`OUTCOMES`.
        itt: Keep usage-limit cut-offs (the first sensitivity analysis).
        permutations: Within-stratum permutations for the p-value.
        bootstraps: Stratified bootstrap replicates for the 95% CI.
        power_sims: Simulated experiments per power estimate.
        planned_mde: The planned minimum detectable effect, in outcome units.
        planned_n: The planned number of analysed tasks.
        settle_hours: Hours a completed sweep must follow a task's latest
            join before the task counts as finished.
        seed: Seed for every random draw.
        limit: Per-event-type scan limit.

    Returns:
        A :class:`HoldoutReport` of counts and statistics only.

    Raises:
        HoldoutAnalysisError: An option is out of range, or several rates
            are present and ``rate`` names none of them.
    """
    _validate(
        days=days,
        rate=rate,
        outcome=outcome,
        permutations=permutations,
        bootstraps=bootstraps,
        power_sims=power_sims,
        planned_mde=planned_mde,
        planned_n=planned_n,
        settle_hours=settle_hours,
        seed=seed,
        limit=limit,
    )
    end = _as_utc(until) if until is not None else datetime.now(tz=UTC)
    since = end - timedelta(days=days)
    pack_scan = scan_events(
        event_log, event_type=EventType.PACK_ASSEMBLED, since=since, limit=limit
    )
    join_scan = scan_events(
        event_log, event_type=EventType.CAPTURE_SESSION_PACKS, since=since, limit=limit
    )
    sweep_scan = scan_events(
        event_log,
        event_type=EventType.CAPTURE_SWEEP_COMPLETED,
        since=since,
        limit=limit,
    )
    scan = merge_coverage(pack_scan.coverage, join_scan.coverage, sweep_scan.coverage)
    evidence_start = since
    if scan.truncated and scan.covered_since:
        evidence_start = max(since, _as_utc(datetime.fromisoformat(scan.covered_since)))
    effective_days = max((end - evidence_start).total_seconds(), 0.0) / _DAY_SECONDS

    packs: dict[str, Event] = {}
    rows = _Rows()
    for event in pack_scan.events:
        if event.entity_id:
            packs.setdefault(event.entity_id, event)
        if evidence_start <= _as_utc(event.occurred_at) < end:
            rows.window.append(event)
    rate, notes = _resolve_rate(rows, rate)

    funnel = HoldoutFunnel(joins_scanned=len(join_scan.events))
    latest_joins: dict[str, Event] = {}
    for event in join_scan.events:
        if event.entity_id:
            latest_joins[event.entity_id] = event
    sweeps = _latest_sweeps(sweep_scan.events)
    settle = timedelta(hours=settle_hours)
    units: list[_Unit] = []
    for join in latest_joins.values():
        funnel.sessions += 1
        unit = _unit(
            event_log, join, packs, sweeps, settle, rate, evidence_start, end, funnel
        )
        if unit is not None:
            units.append(unit)
    sessions = _sessions(
        event_log,
        latest_joins,
        packs,
        sweeps,
        settle,
        evidence_start,
        end,
        effective_days,
        outcome,
    )

    return _report(
        units=units,
        rows=rows,
        funnel=funnel,
        scan=scan,
        notes=notes,
        sweeps=sweeps,
        sessions=sessions,
        options={
            "days": days,
            "since": since,
            "end": end,
            "effective_days": effective_days,
            "rate": rate,
            "outcome": outcome,
            "itt": itt,
            "permutations": permutations,
            "bootstraps": bootstraps,
            "power_sims": power_sims,
            "planned_mde": planned_mde,
            "planned_n": planned_n,
            "settle_hours": settle_hours,
            "seed": seed,
        },
    )


def _unit(  # noqa: PLR0911 - one return per funnel stage
    event_log: EventLog,
    join: Event,
    packs: dict[str, Event],
    sweeps: Mapping[str, datetime],
    settle: timedelta,
    rate: float | None,
    start: datetime,
    end: datetime,
    funnel: HoldoutFunnel,
) -> _Unit | None:
    """The join's eligible unit, or ``None`` after counting why it is not one."""
    payload = join.payload or {}
    parent = payload.get("parent_session_id")
    if not isinstance(parent, str) or not parent:
        funnel.main_sessions += 1
        return None
    outcome = payload.get("outcome")
    if not isinstance(outcome, Mapping):
        funnel.no_outcome += 1
        return None
    pack_ids = _pack_ids(payload)
    if not pack_ids:
        funnel.no_retrieval += 1
        return None
    first = _find_pack(event_log, packs, pack_ids[0])
    if first is None:
        funnel.first_pack_not_found += 1
        return None
    first_at = _as_utc(first.occurred_at)
    if not start <= first_at < end:
        funnel.outside_window += 1
        return None
    first_payload = first.payload or {}
    if HOLDOUT_KEY not in first_payload:
        funnel.first_pack_old_build += 1
        return None
    if not _matches_rate(first_payload, rate):
        funnel.other_rate += 1
        return None
    if _pack_size(first_payload) <= 0:
        funnel.empty_first_pack += 1
        return None
    if not _finished(join, sweeps, settle):
        funnel.unfinished += 1
        return None
    funnel.eligible += 1
    if (_count(payload.get("pack_ids_unparsed")) or 0) > 0:
        funnel.eligible_with_unparsed_pack_ids += 1
    arms = {
        is_holdout(packs[p].payload)
        for p in pack_ids
        if p in packs and HOLDOUT_KEY in (packs[p].payload or {})
    }
    return _Unit(
        parent=parent,
        stratum=f"{parent}|{_iso_week(first_at)}",
        withheld=is_holdout(first_payload),
        packs=len(pack_ids),
        both_arms=len(arms) == _PAIR,
        cut_off=outcome.get("ended_on_error") is True,
        outcome=outcome,
    )


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _per_30d(count: int, window_days: float) -> float | None:
    return count * 30 / window_days if window_days > 0 else None


def _arm_counts(units: Sequence[_Unit]) -> ArmCounts:
    withheld = sum(unit.withheld for unit in units)
    return ArmCounts(served=len(units) - withheld, withheld=withheld)


def _parent_spread(
    values: npt.NDArray[np.float64], parents: Sequence[str]
) -> tuple[float | None, float | None, float | None]:
    """SD within parent, SD overall and the between-parent share of variance.

    The share is bias-adjusted as in the prestudy: epsilon-squared, one
    minus the within-parent variance (on ``n - groups`` degrees of freedom)
    over the total variance (on ``n - 1``), floored at 0.
    """
    n = values.size
    if n < _PAIR:
        return None, None, None
    codes = _codes(np.asarray(parents))
    groups = int(codes.max()) + 1
    means = np.bincount(codes, weights=values) / np.bincount(codes)
    ss_within = float(((values - means[codes]) ** 2).sum())
    ss_total = float(((values - values.mean()) ** 2).sum())
    sd_within = math.sqrt(ss_within / (n - groups)) if n > groups else None
    sd_total = math.sqrt(ss_total / (n - 1))
    share = (
        max(0.0, 1 - sd_within**2 / sd_total**2)
        if sd_within is not None and sd_total > 0
        else None
    )
    return sd_within, sd_total, share


def _log_scale(outcome: str) -> bool:
    """Whether the outcome is a log1p count, whose MDE reads as a ratio."""
    return outcome.startswith("log1p_")


def _horizons(
    per_30d: float | None,
    sd: float | None,
    mean: float | None,
    *,
    log_scale: bool,
    sd_reason: str,
) -> list[HoldoutHorizon]:
    """The analytic MDE after each of :data:`HORIZON_DAYS` days of evidence."""
    horizons: list[HoldoutHorizon] = []
    for days in HORIZON_DAYS:
        n = None if per_30d is None else per_30d * days / 30
        mde = None if sd is None or n is None else analytic_mde(sd, n)
        reason = None
        if sd is None:
            reason = sd_reason
        elif n is None:
            reason = _EMPTY_WINDOW
        elif mde is None:
            reason = f"N {n:.3g} is below {MIN_FORMULA_N}, the formula's minimum"
        horizons.append(
            HoldoutHorizon(
                days=days,
                n=n,
                mde=mde,
                ratio=math.exp(mde) if mde is not None and log_scale else None,
                share_of_mean=(
                    mde / mean if mde is not None and not log_scale and mean else None
                ),
                not_measurable=reason,
            )
        )
    return horizons


def _n_for_ten_percent(
    sd: float | None, mean: float | None, *, log_scale: bool, sd_reason: str
) -> tuple[int | None, str]:
    """N for a 10% effect: x0.9 on a log1p outcome, else 10% of ``mean``."""
    if sd is None:
        return None, sd_reason
    if log_scale:
        return n_for_mde(sd, -math.log(1 - _TEN_PERCENT)), ""
    if mean is None:
        return None, "no analysed task in the served arm"
    if mean <= 0:
        return None, "the served mean is 0, so 10% of it is zero"
    return n_for_mde(sd, _TEN_PERCENT * mean), ""


def _reasons(figures: Mapping[str, tuple[object, str]]) -> dict[str, str]:
    """Each ``None`` figure's reason, keyed by the figure's name."""
    return {name: why for name, (value, why) in figures.items() if value is None}


def _rolled_outcome(
    payloads: Sequence[Mapping[str, Any]],
    read: Callable[[Mapping[str, Any]], float | None],
) -> float | None:
    """The joins' outcomes summed field by field, then read.

    ``None`` when a join carries no outcome. A field that any join lacks,
    or holds as something other than a count, is ``None`` in the sum.
    """
    outcomes = [payload.get("outcome") for payload in payloads]
    if not all(isinstance(outcome, Mapping) for outcome in outcomes):
        return None
    rolled: dict[str, float | None] = {}
    for key in {key for outcome in outcomes for key in outcome}:
        counts = [_count(outcome.get(key)) for outcome in outcomes]
        known = [count for count in counts if count is not None]
        rolled[key] = math.fsum(known) if len(known) == len(counts) else None
    return read(rolled)


def _non_empty_pack(
    event_log: EventLog, packs: dict[str, Event], payload: Mapping[str, Any]
) -> bool:
    """Whether a pack the join parsed held items, or would have if withheld."""
    for pack_id in _pack_ids(payload):
        pack = _find_pack(event_log, packs, pack_id)
        if pack is not None and _pack_size(pack.payload or {}) > 0:
            return True
    return False


def _sessions(
    event_log: EventLog,
    joins: Mapping[str, Event],
    packs: dict[str, Event],
    sweeps: Mapping[str, datetime],
    settle: timedelta,
    start: datetime,
    end: datetime,
    window_days: float,
    outcome: str,
) -> HoldoutSessions:
    """Main sessions with their sub-agent tasks rolled up (:class:`HoldoutSessions`)."""
    groups: dict[str, list[Event]] = {}
    for session_id, join in joins.items():
        parent = (join.payload or {}).get("parent_session_id")
        key = parent if isinstance(parent, str) and parent else session_id
        groups.setdefault(key, []).append(join)
    rolled_up = not_finished = without_main_join = reaching = eligible = 0
    values: list[float] = []
    for key, members in groups.items():
        if not start <= max(_as_utc(m.occurred_at) for m in members) < end:
            continue
        if key not in {m.entity_id for m in members}:
            without_main_join += 1
            continue
        if not all(_finished(m, sweeps, settle) for m in members):
            not_finished += 1
            continue
        rolled_up += 1
        payloads = [m.payload or {} for m in members]
        if any(
            _pack_ids(p) or (_count(p.get("retrieval_results")) or 0) > 0
            for p in payloads
        ):
            reaching += 1
        if not any(_non_empty_pack(event_log, packs, p) for p in payloads):
            continue
        eligible += 1
        value = _rolled_outcome(payloads, OUTCOMES[outcome])
        if value is not None:
            values.append(value)
    sd = float(np.std(values, ddof=1)) if len(values) >= _PAIR else None
    sd_reason = (
        "needs 2 main sessions with a non-empty pack whose joins all carry the "
        f"outcome, found {len(values)}"
    )
    reaching_per_30d = _per_30d(reaching, window_days)
    eligible_per_30d = _per_30d(eligible, window_days)
    analysed_per_30d = _per_30d(len(values), window_days)
    return HoldoutSessions(
        rolled_up=rolled_up,
        not_finished=not_finished,
        without_main_join=without_main_join,
        reaching_retrieval=reaching,
        reaching_retrieval_per_30d=reaching_per_30d,
        eligible=eligible,
        eligible_per_30d=eligible_per_30d,
        analysed=len(values),
        analysed_per_30d=analysed_per_30d,
        outcome_sd=sd,
        mde_by_horizon=_horizons(
            analysed_per_30d,
            sd,
            float(np.mean(values)) if values else None,
            log_scale=_log_scale(outcome),
            sd_reason=sd_reason,
        ),
        not_measurable=_reasons(
            {
                "reaching_retrieval_per_30d": (reaching_per_30d, _EMPTY_WINDOW),
                "eligible_per_30d": (eligible_per_30d, _EMPTY_WINDOW),
                "analysed_per_30d": (analysed_per_30d, _EMPTY_WINDOW),
                "outcome_sd": (sd, sd_reason),
            }
        ),
    )


def _descriptive(
    *,
    units: Sequence[_Unit],
    analysed: Sequence[_Unit],
    y: npt.NDArray[np.float64],
    served: npt.NDArray[np.bool_],
    arm_ratio: list[ArmRatioCheck],
    sessions: HoldoutSessions,
    options: Mapping[str, Any],
) -> HoldoutDescriptive:
    """The [R] re-measure over the eligible and the analysed tasks."""
    window_days: float = options["effective_days"]
    parents = [unit.parent for unit in analysed]
    sd_within, sd_total, between = _parent_spread(y, parents)
    spread_reason = (
        "needs more analysed tasks than parent sessions, found "
        f"{len(analysed)} across {len(set(parents))}"
    )
    cut_offs = [unit for unit in units if unit.cut_off]
    eligible_by_arm = _arm_counts(units)
    cut_offs_by_arm = _arm_counts(cut_offs)
    shares = ArmShares(
        served=_share(cut_offs_by_arm.served, eligible_by_arm.served),
        withheld=_share(cut_offs_by_arm.withheld, eligible_by_arm.withheld),
    )
    prs = [(unit, _count(unit.outcome.get("prs_created"))) for unit in analysed]
    prs_known = [value for _, value in prs if value is not None]
    prs_served = [v for unit, v in prs if v is not None and not unit.withheld]
    pr_base_rate = _share(sum(v >= 1 for v in prs_served), len(prs_served))
    prs_mean = sum(prs_known) / len(prs_known) if prs_known else None
    by_parent = Counter(unit.parent for unit in units)
    top_share = _share(max(by_parent.values()), len(units)) if units else None
    packs_per_task = sum(unit.packs for unit in units) / len(units) if units else None
    cut_off_share = _share(len(cut_offs), len(units))
    eligible_per_30d = _per_30d(len(units), window_days)
    analysed_per_30d = _per_30d(len(analysed), window_days)
    log_scale = _log_scale(options["outcome"])
    served_mean = float(y[served].mean()) if served.any() else None
    n_10, n_10_reason = _n_for_ten_percent(
        sd_within, served_mean, log_scale=log_scale, sd_reason=spread_reason
    )
    figures: dict[str, tuple[object, str]] = {
        "eligible_per_30d": (eligible_per_30d, _EMPTY_WINDOW),
        "analysed_per_30d": (analysed_per_30d, _EMPTY_WINDOW),
        "outcome_sd_within_parent": (sd_within, spread_reason),
        "outcome_sd_total": (sd_total, f"needs 2 analysed tasks, found {y.size}"),
        "between_parent_share": (
            between,
            spread_reason
            if sd_within is None
            else "every analysed outcome is equal, so there is no variance to share",
        ),
        "top_parent_share": (top_share, _NO_ELIGIBLE),
        "packs_per_task": (packs_per_task, _NO_ELIGIBLE),
        "post_hoc_share": (None, _POST_HOC_MISSING),
        "cut_off_share": (cut_off_share, _NO_ELIGIBLE),
        "cut_off_share_by_arm.served": (
            shares.served,
            "no eligible task in the served arm",
        ),
        "cut_off_share_by_arm.withheld": (
            shares.withheld,
            "no eligible task in the withheld arm",
        ),
        "pr_base_rate_served": (
            pr_base_rate,
            "no analysed served-arm task records prs_created",
        ),
        "prs_created_mean": (prs_mean, "no analysed task records prs_created"),
        "n_for_10pct_effect": (n_10, n_10_reason),
    }
    return HoldoutDescriptive(
        eligible_tasks=len(units),
        eligible_per_30d=eligible_per_30d,
        analysed_tasks=len(analysed),
        analysed_per_30d=analysed_per_30d,
        outcome_sd_within_parent=sd_within,
        outcome_sd_total=sd_total,
        between_parent_share=between,
        parents=len(set(parents)),
        packs_per_task=packs_per_task,
        tasks_with_several_packs=sum(unit.packs > 1 for unit in units),
        post_hoc_share=None,
        post_hoc_note=_POST_HOC_NOTE,
        cut_off_share=cut_off_share,
        cut_off_share_by_arm=shares,
        pr_base_rate_served=pr_base_rate,
        prs_created_mean=prs_mean,
        eligible_by_arm=eligible_by_arm,
        cut_offs_by_arm=cut_offs_by_arm,
        analysed_by_arm=_arm_counts(analysed),
        tasks_with_both_arms=sum(unit.both_arms for unit in units),
        arm_ratio=arm_ratio,
        top_parent_share=top_share,
        mde_by_horizon=_horizons(
            analysed_per_30d,
            sd_within,
            served_mean,
            log_scale=log_scale,
            sd_reason=spread_reason,
        ),
        n_for_10pct_effect=n_10,
        sessions=sessions,
        not_measurable=_reasons(figures),
    )


def _arm_ratio(
    rows: _Rows, eligible: Sequence[_Unit], rate: float | None
) -> list[ArmRatioCheck]:
    if rate is None:
        return []
    at_rate = [
        e
        for e in rows.window
        if HOLDOUT_KEY in (e.payload or {}) and _matches_rate(e.payload or {}, rate)
    ]
    checks = [_ratio_check("rows", at_rate, rate)]
    tasks_withheld = sum(unit.withheld for unit in eligible)
    checks.append(
        ArmRatioCheck(
            population="tasks",
            n=len(eligible),
            withheld=tasks_withheld,
            expected_share=rate,
            p_value=binomial_two_sided(tasks_withheld, len(eligible), rate),
        )
    )
    weeks: dict[str, list[Event]] = {}
    for event in at_rate:
        weeks.setdefault(_iso_week(_as_utc(event.occurred_at)), []).append(event)
    checks.extend(
        _ratio_check(f"rows {week}", events, rate)
        for week, events in sorted(weeks.items())
    )
    return checks


def _ratio_check(
    population: str, events: Sequence[Event], rate: float
) -> ArmRatioCheck:
    withheld = sum(is_holdout(e.payload) for e in events)
    return ArmRatioCheck(
        population=population,
        n=len(events),
        withheld=withheld,
        expected_share=rate,
        p_value=binomial_two_sided(withheld, len(events), rate),
    )


def _report(
    *,
    units: list[_Unit],
    rows: _Rows,
    funnel: HoldoutFunnel,
    scan: ScanCoverage,
    notes: list[str],
    sweeps: Mapping[str, datetime],
    sessions: HoldoutSessions,
    options: dict[str, Any],
) -> HoldoutReport:
    rate: float | None = options["rate"]
    itt: bool = options["itt"]
    read = OUTCOMES[options["outcome"]]
    cut_offs = [unit for unit in units if unit.cut_off]
    kept = units if itt else [unit for unit in units if not unit.cut_off]
    funnel.cut_offs_excluded = 0 if itt else len(cut_offs)
    measured = [(unit, read(unit.outcome)) for unit in kept]
    analysed = [unit for unit, value in measured if value is not None]
    funnel.outcome_missing = len(kept) - len(analysed)
    funnel.analysed = len(analysed)
    y = np.array([value for _, value in measured if value is not None], dtype=float)
    served = np.array([not unit.withheld for unit in analysed], dtype=bool)
    strata = np.array([unit.stratum for unit in analysed], dtype=str)

    effective_days: float = options["effective_days"]
    arm_ratio = _arm_ratio(rows, units, rate)
    descriptive = _descriptive(
        units=units,
        analysed=analysed,
        y=y,
        served=served,
        arm_ratio=arm_ratio,
        sessions=sessions,
        options=options,
    )

    rngs = [
        np.random.default_rng(s)
        for s in np.random.SeedSequence(options["seed"]).spawn(3)
    ]
    inference = _inference(y, served, strata, rngs[0], rngs[1], options)
    power = _power(
        y,
        served,
        strata,
        rngs[2],
        inference.difference,
        descriptive.outcome_sd_within_parent,
        rate,
        options,
    )
    notes = notes + _notes(
        rows,
        funnel,
        rate,
        sweeps,
        arm_ratio,
        power,
        scan,
        options["settle_hours"],
        sessions,
    )
    return HoldoutReport(
        window_days=options["days"],
        effective_window_days=round(effective_days, 2),
        since=options["since"].isoformat(),
        until=options["end"].isoformat(),
        rate=rate,
        outcome=options["outcome"],
        itt=itt,
        seed=options["seed"],
        settle_hours=options["settle_hours"],
        rows=rows.report(rate),
        funnel=funnel,
        exclusions=[
            HoldoutExclusion(
                name="non_ephemeral",
                timing="population",
                applied=True,
                excluded=None,
                note=_NON_EPHEMERAL_NOTE,
            ),
            HoldoutExclusion(
                name="pre_treatment",
                timing="pre-treatment",
                applied=False,
                excluded=None,
                note=_PRE_TREATMENT_NOTE,
            ),
            HoldoutExclusion(
                name="cut_offs",
                timing="post-treatment",
                applied=not itt,
                excluded=funnel.cut_offs_excluded,
                note=(
                    "Usage-limit cut-offs, read as outcome.ended_on_error; "
                    + ("kept (ITT sensitivity analysis)." if itt else "excluded.")
                ),
            ),
        ],
        descriptive=descriptive,
        inference=inference,
        power=power,
        scan=scan,
        notes=notes,
    )


def _inference(
    y: npt.NDArray[np.float64],
    served: npt.NDArray[np.bool_],
    strata: npt.NDArray[np.str_],
    permutation_rng: np.random.Generator,
    bootstrap_rng: np.random.Generator,
    options: Mapping[str, Any],
) -> HoldoutInference:
    both = one = in_one = 0
    if y.size:
        codes = _codes(strata)
        n = np.bincount(codes)
        n1 = np.bincount(codes, weights=served.astype(float))
        two_arm = (n1 > 0) & (n1 < n)
        both, one = int(two_arm.sum()), int((~two_arm).sum())
        in_one = int(n[~two_arm].sum())
    status = "ok"
    if not y.size:
        status = NO_TASKS
    elif served.all():
        status = NO_WITHHELD_ARM
    elif not served.any():
        status = NO_SERVED_ARM
    elif not both:
        status = NO_BOTH_ARM_STRATUM
    difference = p_value = None
    interval = None
    if status == "ok":
        difference = weighted_difference(y, served, strata)
        p_value = permutation_p_value(
            y, served, strata, permutation_rng, options["permutations"]
        )
        interval = bootstrap_ci(y, served, strata, bootstrap_rng, options["bootstraps"])
    if status != "ok" or p_value is None:
        verdict = status
    elif p_value < ALPHA:
        verdict = DETECTED_VERDICT
    else:
        verdict = NO_EFFECT_VERDICT
    return HoldoutInference(
        status=status,
        verdict=verdict,
        mean_served=float(y[served].mean()) if served.any() else None,
        mean_withheld=float(y[~served].mean()) if (~served).any() else None,
        difference=difference,
        ci_low=None if interval is None else interval[0],
        ci_high=None if interval is None else interval[1],
        p_value=p_value,
        alpha=ALPHA,
        permutations=options["permutations"],
        bootstraps=options["bootstraps"],
        strata=both + one,
        strata_both_arms=both,
        strata_one_arm=one,
        tasks_in_one_arm_strata=in_one,
    )


def _power(
    y: npt.NDArray[np.float64],
    served: npt.NDArray[np.bool_],
    strata: npt.NDArray[np.str_],
    rng: np.random.Generator,
    estimate: float | None,
    sd: float | None,
    rate: float | None,
    options: Mapping[str, Any],
) -> HoldoutPower:
    planned_mde: float = options["planned_mde"]
    share = rate if rate is not None and 0.0 < rate < 1.0 else _PLANNED_SHARE
    result = HoldoutPower(
        status="ok",
        planned_mde=planned_mde,
        planned_n=options["planned_n"],
        target_power=TARGET_POWER,
        withheld_share=share,
        realised_n=int(y.size),
        realised_sd=sd,
        power_at_planned_mde=None,
        mde_at_target_power=None,
        sims=options["power_sims"],
        sim_permutations=POWER_PERMUTATIONS,
    )
    if y.size < _PAIR or np.bincount(_codes(strata)).max() < _PAIR:
        result.status = "no stratum with two or more tasks"
        return result
    pool = y - estimate * served if estimate is not None else y
    scale = _MDE_MULTIPLIER * (sd or float(np.std(pool))) * 2 / math.sqrt(y.size)
    top = max(3 * scale, 2 * planned_mde)
    grid = [float(v) for v in np.linspace(0.0, top, _POWER_GRID_POINTS)]
    power = simulate_power(
        pool,
        strata,
        rng,
        [planned_mde, *grid],
        sims=options["power_sims"],
        withheld_share=share,
    )
    result.power_at_planned_mde = power[0]
    reached = [
        delta
        for delta, value in zip(grid, power[1:], strict=True)
        if value >= TARGET_POWER
    ]
    result.mde_at_target_power = reached[0] if reached else None
    return result


def _notes(
    rows: _Rows,
    funnel: HoldoutFunnel,
    rate: float | None,
    sweeps: Mapping[str, datetime],
    arm_ratio: Sequence[ArmRatioCheck],
    power: HoldoutPower,
    scan: ScanCoverage,
    settle_hours: float,
    sessions: HoldoutSessions,
) -> list[str]:
    notes: list[str] = []
    keyed = sum(HOLDOUT_KEY in (e.payload or {}) for e in rows.window)
    if rows.window and not keyed:
        notes.append(
            "No PACK_ASSEMBLED row in the window records a holdout: the build "
            "that wrote them predates #701, so nothing here is an experiment row."
        )
    elif len(rows.window) > keyed:
        notes.append(
            f"{len(rows.window) - keyed} PACK_ASSEMBLED rows record no holdout "
            "(builds before #701); they are not experiment rows and are left out."
        )
    if rate == 0.0:
        notes.append(
            "At rate 0 an empty pack is returned without a pack id, so a task "
            "whose first retrieval was empty joins from its first non-empty "
            "pack: eligibility can over-count such tasks."
        )
    if funnel.first_pack_not_found:
        notes.append(
            f"{funnel.first_pack_not_found} tasks name a first pack with no "
            "PACK_ASSEMBLED row; every eligible task should join to its row."
        )
    if not sweeps:
        notes.append(
            "No completed, non-dry-run capture sweep was found, so no task "
            "counts as finished."
        )
    elif funnel.unfinished:
        notes.append(
            f"{funnel.unfinished} tasks are not finished yet: no completed, "
            "non-dry-run capture sweep for their source system ran at least "
            f"{settle_hours:g} h after their latest join (completed sweeps found "
            f"for: {', '.join(sorted(sweeps))}). They are left out."
        )
    if funnel.eligible_with_unparsed_pack_ids:
        notes.append(
            f"{funnel.eligible_with_unparsed_pack_ids} eligible tasks reported a "
            "pack id the capture could not parse, so the first parsed pack, "
            "which sets the arm, may not be the task's first call."
        )
    if sessions.not_finished or sessions.without_main_join:
        notes.append(
            "Main sessions left out of the session figures: "
            f"{sessions.not_finished} not finished yet, "
            f"{sessions.without_main_join} with no join for the main transcript."
        )
    failing = [
        check.population for check in arm_ratio if check.p_value < ARM_RATIO_PAUSE_P
    ]
    if failing:
        notes.append(
            "Arm ratio off the rate (binomial p < 0.001) for: "
            f"{', '.join(failing)}. The pre-registration says pause and fix."
        )
    if power.status == "ok" and power.mde_at_target_power is None:
        notes.append("Simulated power stays below the target across the shifts tried.")
    if scan.truncated and scan.note:
        notes.append(scan.note)
    notes.append(_COVARIATE_NOTE)
    return notes


__all__ = [
    "ALPHA",
    "ARM_RATIO_PAUSE_P",
    "DEFAULT_OUTCOME",
    "DETECTED_VERDICT",
    "HORIZON_DAYS",
    "MIN_FORMULA_N",
    "NO_BOTH_ARM_STRATUM",
    "NO_EFFECT_VERDICT",
    "NO_SERVED_ARM",
    "NO_TASKS",
    "NO_WITHHELD_ARM",
    "OUTCOMES",
    "POWER_PERMUTATIONS",
    "TARGET_POWER",
    "ArmCounts",
    "ArmRatioCheck",
    "ArmShares",
    "HoldoutAnalysisError",
    "HoldoutDescriptive",
    "HoldoutExclusion",
    "HoldoutFunnel",
    "HoldoutHorizon",
    "HoldoutInference",
    "HoldoutPower",
    "HoldoutReport",
    "HoldoutRows",
    "HoldoutSessions",
    "analytic_mde",
    "analyze_holdout",
    "binomial_two_sided",
    "bootstrap_ci",
    "n_for_mde",
    "permutation_p_value",
    "permute_within_strata",
    "simulate_power",
    "weighted_difference",
]
