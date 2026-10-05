"""Tests for the pack holdout analysis (``trellis.analyze.holdout``).

The fixtures write synthetic ``PACK_ASSEMBLED`` rows, capture joins and
sweeps through :mod:`tests.unit.analyze._holdout_fixture`; every id is
synthetic. Exact counts are asserted wherever a fixture fixes them, so a
mutant that moves one task between arms or funnel stages fails here.
"""

from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    INTENT_MARKER,
    MONDAY,
    UNTIL,
    HoldoutLog,
    outcome_payload,
    seed_experiment,
)
from trellis.analyze.holdout import (
    DETECTED_VERDICT,
    NO_EFFECT_VERDICT,
    NO_WITHHELD_ARM,
    HoldoutAnalysisError,
    HoldoutReport,
    analyze_holdout,
    binomial_two_sided,
    bootstrap_ci,
    permutation_p_value,
    permute_within_strata,
    simulate_power,
    weighted_difference,
)
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Small resampling counts keep each analysis well under a second.
FAST = {"permutations": 999, "bootstraps": 400, "power_sims": 40}


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _analyze(log: HoldoutLog, **kwargs: object) -> HoldoutReport:
    options: dict[str, object] = {"days": DAYS, "until": UNTIL, "seed": 7, **FAST}
    options.update(kwargs)
    return analyze_holdout(log.event_log, **options)  # type: ignore[arg-type]


def _arms(counts: object) -> dict[str, int]:
    return counts.model_dump()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Primary analysis on seeded and null experiments
# ---------------------------------------------------------------------------


class TestEffectRecovery:
    def test_a_seeded_effect_is_recovered_with_its_sign_and_size(
        self, log: HoldoutLog
    ) -> None:
        """Withheld tasks take 1.5x the turns: served minus withheld ~ -log(1.5)."""
        written = seed_experiment(log, effect=1.5, seed=101)
        log.sweep()

        report = _analyze(log, permutations=2000, bootstraps=1000)

        inference = report.inference
        assert report.funnel.analysed == written == 120
        assert inference.status == "ok"
        assert inference.strata_both_arms == 12
        assert inference.difference is not None
        assert abs(inference.difference - (-math.log(1.5))) < 0.15
        assert inference.p_value is not None
        assert inference.p_value < 0.05
        assert inference.ci_low is not None
        assert inference.ci_high is not None
        assert inference.ci_low < inference.difference < inference.ci_high < 0
        assert inference.verdict == DETECTED_VERDICT

    def test_a_null_experiment_is_not_significant_at_a_fixed_seed(
        self, log: HoldoutLog
    ) -> None:
        seed_experiment(log, effect=1.0, seed=202)
        log.sweep()

        report = _analyze(log, permutations=2000, bootstraps=1000)

        inference = report.inference
        assert inference.status == "ok"
        assert inference.p_value is not None
        assert inference.p_value >= 0.05
        assert inference.ci_low is not None
        assert inference.ci_high is not None
        assert inference.ci_low < 0 < inference.ci_high
        assert inference.verdict == NO_EFFECT_VERDICT

    def test_the_same_seed_reproduces_the_inference(self, log: HoldoutLog) -> None:
        seed_experiment(log, effect=1.2, seed=303, parents=3)
        log.sweep()

        first = _analyze(log, seed=11)
        second = _analyze(log, seed=11)

        assert first.inference.model_dump() == second.inference.model_dump()
        assert first.power.model_dump() == second.power.model_dump()


class TestPermutationCalibration:
    def test_null_p_values_are_roughly_uniform_over_200_seeds(self) -> None:
        """Rejections at 0.05 sit inside a 99.9% binomial band for 200 nulls.

        199 permutations make p = (1 + b) / 200, so under the null
        P(p < 0.05) is 9/200 and P(p < 0.5) is 99/200. The bands are the
        0.05%-99.95% quantiles of Binomial(200, 0.045) and
        Binomial(200, 0.495). Data and permutations come from fixed seeds,
        so the test cannot flake.
        """
        strata = np.repeat(np.arange(5), 8)
        p_values = []
        for seed in range(200):
            rng = np.random.default_rng(seed)
            y = rng.normal(0.0, 1.0, strata.size) + strata * 0.7
            served = rng.random(strata.size) < 0.5
            p = permutation_p_value(y, served, strata, rng, permutations=199)
            assert p is not None
            p_values.append(p)
        rejections = sum(p < 0.05 for p in p_values)
        below_half = sum(p < 0.5 for p in p_values)
        assert 1 <= rejections <= 20
        assert 76 <= below_half <= 122

    def test_permutations_keep_every_strata_arm_count(self) -> None:
        served = np.array([1, 1, 0, 0, 0] + [1] * 6 + [0] + [0] * 3 + [1] * 4) == 1
        strata = np.array(["a"] * 5 + ["b"] * 7 + ["c"] * 3 + ["d"] * 4)

        perms = permute_within_strata(served, strata, np.random.default_rng(3), 400)

        assert perms.shape == (400, served.size)
        for label in "abcd":
            mask = strata == label
            assert (perms[:, mask].sum(axis=1) == served[mask].sum()).all(), label
        # The labels do move: each unit of stratum a is served in about 2/5
        # of the permutations, and most permutations differ from the data.
        share_a = perms[:, strata == "a"].mean(axis=0)
        assert ((share_a > 0.25) & (share_a < 0.55)).all()
        assert (perms != served).any(axis=1).mean() > 0.5

    def test_the_observed_arrangement_counts_once(self) -> None:
        """A perfect split at 99 permutations gives exactly 1/100, never 0.

        Twenty served tasks score 1 and twenty withheld tasks score 0. Only
        that arrangement and its mirror reach the observed gap, 2 of
        C(40, 20) or about 1.4e11, so the draws find neither and p is the
        +1 term alone.
        """
        served = np.repeat([True, False], 20)
        strata = np.zeros(served.size, dtype=int)

        p = permutation_p_value(
            served.astype(float),
            served,
            strata,
            np.random.default_rng(0),
            permutations=99,
        )

        assert p == pytest.approx(1 / 100)


class TestBootstrapInterval:
    def test_tasks_are_redrawn_per_stratum_so_pair_strata_still_vary(self) -> None:
        """Ten strata of one served and one withheld task each.

        Resampling inside each stratum x arm cell would redraw the same task
        every time: a zero-width interval. Redrawing each stratum's tasks,
        arm labels attached, lets its arm split vary, so the interval has
        width and covers the estimate.
        """
        y = np.random.default_rng(5).normal(0.0, 1.0, 20)
        served = np.tile([True, False], 10)
        strata = np.repeat(np.arange(10), 2)

        interval = bootstrap_ci(y, served, strata, np.random.default_rng(6), 2000)
        estimate = weighted_difference(y, served, strata)

        assert interval is not None
        assert estimate is not None
        low, high = interval
        assert low < estimate < high
        assert high - low > 0.5

    def test_resamples_never_mix_strata(self) -> None:
        """Six strata whose baselines sit 10 apart, with noise SD 0.1.

        Drawing inside each stratum keeps the baselines out of every
        replicate's gap, so the interval is about 0.1 wide. A draw across
        strata would carry baseline gaps into the arms and span several
        units.
        """
        strata = np.repeat(np.arange(6), 10)
        served = np.tile([True, False], 30)
        noise = np.random.default_rng(8).normal(0.0, 0.1, strata.size)
        y = strata * 10.0 + served * 0.2 + noise

        interval = bootstrap_ci(y, served, strata, np.random.default_rng(9), 2000)
        estimate = weighted_difference(y, served, strata)

        assert interval is not None
        assert estimate is not None
        low, high = interval
        assert low < estimate < high
        assert high - low < 0.3

    def test_a_95_percent_interval_spans_about_3_92_standard_errors(self) -> None:
        """One stratum of 400 tasks per arm, where the bootstrap is near normal.

        A 95% percentile interval spans about 2 x 1.96 = 3.92 standard errors
        of the difference, a 90% one 3.29 and a 99% one 5.15. Over 30 data
        and resample seeds the ratio stayed within 3.82-4.01.
        """
        served = np.tile([True, False], 400)
        y = np.random.default_rng(3).normal(0.0, 1.0, served.size) + 0.5 * served
        standard_error = math.sqrt(
            y[served].var(ddof=1) / served.sum()
            + y[~served].var(ddof=1) / (~served).sum()
        )
        strata = np.zeros(served.size, dtype=int)

        interval = bootstrap_ci(y, served, strata, np.random.default_rng(4), 4000)

        assert interval is not None
        low, high = interval
        assert 3.6 < (high - low) / standard_error < 4.25

    def test_no_stratum_with_both_arms_has_no_interval(self) -> None:
        y = np.array([1.0, 2.0, 3.0, 4.0])
        served = np.array([True, True, False, False])
        strata = np.array(["s1", "s1", "s2", "s2"])

        assert bootstrap_ci(y, served, strata, np.random.default_rng(1), 200) is None


# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------


class TestWeightedDifference:
    def test_strata_are_weighted_by_n1_n0_over_n(self) -> None:
        """Stratum 1: diff 0, w 2/3. Stratum 2: diff 3, w 3/4. 27/17 overall.

        The unweighted difference is 1.0, equal stratum weights give 1.5 and
        size weights give 12/7, so only the n1*n0/n weighting lands on 27/17.
        """
        y = np.array([1.0, 3.0, 2.0, 5.0, 1.0, 2.0, 3.0])
        served = np.array([True, True, False, True, False, False, False])
        strata = np.array(["s1", "s1", "s1", "s2", "s2", "s2", "s2"])

        assert weighted_difference(y, served, strata) == pytest.approx(27 / 17)

    def test_no_stratum_with_both_arms_has_no_difference(self) -> None:
        y = np.array([1.0, 2.0, 3.0])
        served = np.array([True, True, False])
        strata = np.array(["s1", "s1", "s2"])

        assert weighted_difference(y, served, strata) is None

    def test_the_report_uses_the_weighted_difference(self, log: HoldoutLog) -> None:
        layout = [
            (1, False, "parent-a"),
            (3, False, "parent-a"),
            (2, True, "parent-a"),
            (5, False, "parent-b"),
            (1, True, "parent-b"),
            (2, True, "parent-b"),
            (3, True, "parent-b"),
        ]
        for hour, (commits, withheld, parent) in enumerate(layout):
            log.task(
                start=MONDAY + timedelta(hours=hour),
                parent=parent,
                arms=[withheld],
                commits=commits,
            )
        log.sweep()

        report = _analyze(log, outcome="commits")

        assert report.inference.difference == pytest.approx(27 / 17)
        assert report.inference.mean_served == pytest.approx(3.0)
        assert report.inference.mean_withheld == pytest.approx(2.0)

    def test_one_arm_strata_contribute_nothing_and_are_counted(
        self, log: HoldoutLog
    ) -> None:
        week2 = MONDAY + timedelta(weeks=1)
        layout = [
            ("parent-a", MONDAY, [False, True]),
            ("parent-a", week2, [False, False]),
            ("parent-b", MONDAY, [True]),
            ("parent-b", week2, [False, True, True]),
        ]
        for minutes, (parent, week, arms) in enumerate(layout):
            for offset, withheld in enumerate(arms):
                log.task(
                    start=week + timedelta(hours=offset, minutes=10 * minutes),
                    parent=parent,
                    arms=[withheld],
                    turns=10 + offset,
                )
        log.sweep()

        inference = _analyze(log).inference

        assert inference.strata == 4
        assert inference.strata_both_arms == 2
        assert inference.strata_one_arm == 2
        assert inference.tasks_in_one_arm_strata == 3


# ---------------------------------------------------------------------------
# Units, arms and the funnel
# ---------------------------------------------------------------------------


class TestUnitsAndArms:
    def test_a_task_keeps_the_arm_of_its_first_call(self, log: HoldoutLog) -> None:
        """First calls: 3 withheld, 2 served. Last calls would give 2 and 3."""
        for arms, turns in [
            ([True, False], 10),
            ([True, False], 20),
            ([True], 30),
            ([False, True], 40),
            ([False], 50),
        ]:
            log.task(start=MONDAY + timedelta(hours=turns), arms=arms, turns=turns)
        log.sweep()

        report = _analyze(log)

        assert _arms(report.descriptive.eligible_by_arm) == {
            "served": 2,
            "withheld": 3,
        }
        assert report.descriptive.tasks_with_both_arms == 3
        assert report.inference.mean_withheld == pytest.approx(
            (math.log1p(10) + math.log1p(20) + math.log1p(30)) / 3
        )
        assert report.inference.mean_served == pytest.approx(
            (math.log1p(40) + math.log1p(50)) / 2
        )

    def test_a_withheld_first_pack_is_eligible_by_its_would_be_items(
        self, log: HoldoutLog
    ) -> None:
        for hour, (withheld, items, sectioned) in enumerate(
            [
                (True, 3, False),  # withheld flat: holdout_items has 3
                (True, 2, True),  # withheld sectioned: holdout_sections has 2
                (True, 0, False),  # withheld, would-be pack empty
                (False, 0, False),  # served empty
                (False, 4, False),
                (False, 1, True),
            ]
        ):
            log.task(
                start=MONDAY + timedelta(hours=hour),
                arms=[withheld],
                items=items,
                sectioned=sectioned,
            )
        log.sweep()

        report = _analyze(log)

        assert report.funnel.eligible == 4
        assert report.funnel.empty_first_pack == 2
        assert _arms(report.descriptive.eligible_by_arm) == {
            "served": 2,
            "withheld": 2,
        }

    def test_rows_from_builds_without_a_holdout_key_are_counted_and_left_out(
        self, log: HoldoutLog
    ) -> None:
        for hour, arms in enumerate(
            [[None], [None, False], [False, None], [False], [True]]
        ):
            log.task(start=MONDAY + timedelta(hours=hour), arms=arms)
        log.sweep()

        report = _analyze(log)

        assert report.funnel.first_pack_old_build == 2
        assert report.funnel.eligible == 3
        assert report.rows.scanned == 7
        assert report.rows.without_holdout_key == 3
        assert report.rows.at_rate == 4
        assert report.rows.withheld_at_rate == 1

    def test_several_rates_are_refused_until_one_is_named(
        self, log: HoldoutLog
    ) -> None:
        hour = 0
        for rate, arms in [(0.5, [False, True, False]), (0.25, [False, True])]:
            for withheld in arms:
                hour += 1
                log.task(
                    start=MONDAY + timedelta(hours=hour), arms=[withheld], rate=rate
                )
        log.sweep()

        with pytest.raises(HoldoutAnalysisError) as refused:
            _analyze(log)
        message = str(refused.value)
        assert "0.25" in message
        assert "0.5" in message
        assert "--rate" in message

        quarter = _analyze(log, rate=0.25)
        assert quarter.rate == 0.25
        assert quarter.funnel.other_rate == 3
        assert quarter.funnel.eligible == 2
        # Power is simulated at the analysed rate's withheld share.
        assert quarter.power.withheld_share == 0.25

        half = _analyze(log, rate=0.5)
        assert half.funnel.other_rate == 2
        assert half.funnel.eligible == 3

    def test_the_funnel_counts_every_join_that_is_not_a_unit(
        self, log: HoldoutLog
    ) -> None:
        start = MONDAY
        log.task(start=start, parent=None)  # a main session
        log.task(start=start + timedelta(hours=1), with_outcome=False)
        log.join(
            "task-empty",
            at=start + timedelta(hours=2),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
        log.join(
            "task-unknown-pack",
            at=start + timedelta(hours=3),
            pack_ids=["pack-not-recorded"],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
        late = log.pack(at=UNTIL + timedelta(hours=1), withheld=False, items=2)
        log.join(
            "task-after-until",
            at=UNTIL + timedelta(hours=2),
            pack_ids=[late],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
        # One task joined twice: the later join, with its later outcome, wins.
        first_pack = log.pack(at=start + timedelta(hours=4), withheld=False, items=2)
        log.join(
            "task-rejoined",
            at=start + timedelta(hours=5),
            pack_ids=[first_pack],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
        log.join(
            "task-rejoined",
            at=start + timedelta(hours=9),
            pack_ids=[first_pack],
            parent="parent-a",
            outcome=outcome_payload(turns=50),
        )
        log.sweep()

        report = _analyze(log)

        funnel = report.funnel
        assert funnel.joins_scanned == 7
        assert funnel.sessions == 6
        assert funnel.main_sessions == 1
        assert funnel.no_outcome == 1
        assert funnel.no_retrieval == 1
        assert funnel.first_pack_not_found == 1
        assert funnel.outside_window == 1
        assert funnel.eligible == 1
        assert report.inference.mean_served == pytest.approx(math.log1p(50))


class TestFinished:
    def test_tasks_not_yet_settled_by_a_later_sweep_are_unfinished(
        self, log: HoldoutLog
    ) -> None:
        log.task(start=MONDAY)
        log.task(start=MONDAY + timedelta(hours=1))
        sweep_at = MONDAY + timedelta(hours=8)
        log.sweep(at=sweep_at)
        # Joined 59 minutes before the sweep: under the 3-hour settle time.
        log.task(start=sweep_at - timedelta(hours=2))
        # Joined after the last real sweep; a later dry run settles nothing.
        log.task(start=sweep_at + timedelta(hours=1))
        log.sweep(at=sweep_at + timedelta(hours=12), dry_run=True)

        report = _analyze(log)

        assert report.funnel.unfinished == 2
        assert report.funnel.eligible == 2

    def test_without_any_sweep_no_task_is_finished(self, log: HoldoutLog) -> None:
        log.task(start=MONDAY)
        log.task(start=MONDAY + timedelta(hours=1))

        report = _analyze(log)

        assert report.funnel.unfinished == 2
        assert report.funnel.eligible == 0
        assert any("sweep" in note for note in report.notes)


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


class TestExclusions:
    @staticmethod
    def _seed_cut_offs(log: HoldoutLog) -> None:
        cut_off = {0, 2, 3}  # tasks 0 and 2 served, task 3 withheld
        for index in range(10):
            log.task(
                start=MONDAY + timedelta(hours=index),
                arms=[index % 2 == 1],
                turns=10 + index,
                ended_on_error=index in cut_off,
            )
        log.sweep()

    def test_cut_offs_are_excluded_by_default(self, log: HoldoutLog) -> None:
        self._seed_cut_offs(log)

        report = _analyze(log)

        assert report.funnel.eligible == 10
        assert report.funnel.cut_offs_excluded == 3
        assert report.funnel.analysed == 7
        assert _arms(report.descriptive.cut_offs_by_arm) == {
            "served": 2,
            "withheld": 1,
        }
        assert _arms(report.descriptive.analysed_by_arm) == {
            "served": 3,
            "withheld": 4,
        }
        assert report.descriptive.cut_off_share == pytest.approx(0.3)
        cut_offs = next(e for e in report.exclusions if e.name == "cut_offs")
        assert cut_offs.applied is True
        assert cut_offs.excluded == 3

    def test_itt_keeps_the_cut_offs(self, log: HoldoutLog) -> None:
        self._seed_cut_offs(log)

        report = _analyze(log, itt=True)

        assert report.itt is True
        assert report.funnel.cut_offs_excluded == 0
        assert report.funnel.analysed == 10
        assert _arms(report.descriptive.analysed_by_arm) == {
            "served": 5,
            "withheld": 5,
        }
        assert report.descriptive.cut_off_share == pytest.approx(0.3)
        cut_offs = next(e for e in report.exclusions if e.name == "cut_offs")
        assert cut_offs.applied is False
        assert cut_offs.excluded == 0

    def test_the_pre_treatment_exclusion_is_reported_as_not_applied(
        self, log: HoldoutLog
    ) -> None:
        self._seed_cut_offs(log)

        report = _analyze(log)

        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is False
        assert pre.excluded is None
        assert "first retrieval" in pre.note
        assert report.descriptive.post_hoc_share is None
        assert report.descriptive.post_hoc_note

    def test_rules_applied_upstream_or_not_at_all_are_reported(
        self, log: HoldoutLog
    ) -> None:
        """Non-ephemeral is applied by capture; the covariate analysis never runs."""
        self._seed_cut_offs(log)

        report = _analyze(log)

        population = next(e for e in report.exclusions if e.name == "non_ephemeral")
        assert population.applied is True
        assert population.excluded is None
        assert "ephemeral project" in population.note
        assert [e.name for e in report.exclusions] == [
            "non_ephemeral",
            "pre_treatment",
            "cut_offs",
        ]
        assert sum("brief-length" in note for note in report.notes) == 1


# ---------------------------------------------------------------------------
# Descriptive block (the [R] re-measure) and the flag-off store
# ---------------------------------------------------------------------------


class TestDescriptive:
    def test_flag_off_store_reports_the_re_measure_and_no_withheld_arm(
        self, log: HoldoutLog
    ) -> None:
        week2 = MONDAY + timedelta(weeks=1)
        for index in range(12):
            log.task(
                start=(MONDAY if index < 6 else week2) + timedelta(hours=index),
                parent="parent-a" if index % 2 else "parent-b",
                arms=[False],
                rate=0.0,
                turns=5 + 3 * index,
            )
        log.sweep()

        report = _analyze(log)

        assert report.rate == 0.0
        assert report.descriptive.eligible_tasks == 12
        assert report.descriptive.outcome_sd_within_parent is not None
        assert report.inference.status == NO_WITHHELD_ARM
        assert report.inference.verdict == NO_WITHHELD_ARM
        assert report.inference.p_value is None
        assert report.inference.difference is None
        rows_check = next(
            c for c in report.descriptive.arm_ratio if c.population == "rows"
        )
        assert (rows_check.n, rows_check.withheld) == (12, 0)
        assert rows_check.p_value == 1.0
        # Flag off, power is simulated at the planned 50/50 split.
        assert report.power.status == "ok"
        assert report.power.withheld_share == 0.5

    def test_descriptive_values_match_a_hand_computation(self, log: HoldoutLog) -> None:
        """Commits: parent-a [1, 3], parent-b [4, 6, 8], plus one cut-off.

        Within-parent SS 2 + 8 = 10 on 5 - 2 df; total SS 29.2 on 4 df. The
        between-parent share is bias-adjusted, as in the prestudy: one minus
        the within variance over the total variance, 1 - (10/3) / (29.2/4).
        """
        log.deployed_before_window(rate=0.0)
        layout = [
            ("parent-a", 1, 2, 1),  # parent, commits, packs, prs_created
            ("parent-a", 3, 1, 0),
            ("parent-b", 4, 1, 2),
            ("parent-b", 6, 1, 0),
            ("parent-b", 8, 1, 0),
        ]
        for hour, (parent, commits, packs, prs) in enumerate(layout):
            log.task(
                start=MONDAY + timedelta(hours=hour),
                parent=parent,
                arms=[False] * packs,
                rate=0.0,
                commits=commits,
                prs_created=prs,
            )
        log.task(
            start=MONDAY + timedelta(hours=9),
            parent="parent-b",
            arms=[False],
            rate=0.0,
            commits=100,
            ended_on_error=True,
        )
        log.sweep()

        d = _analyze(log, outcome="commits").descriptive

        assert d.eligible_tasks == 6
        assert d.analysed_tasks == 5
        assert d.eligible_per_30d == pytest.approx(6 * 30 / DAYS)
        assert d.analysed_per_30d == pytest.approx(5 * 30 / DAYS)
        assert d.outcome_sd_within_parent == pytest.approx(math.sqrt(10 / 3))
        assert d.outcome_sd_total == pytest.approx(math.sqrt(29.2 / 4))
        assert d.between_parent_share == pytest.approx(1 - (10 / 3) / (29.2 / 4))
        assert d.parents == 2
        assert d.packs_per_task == pytest.approx(7 / 6)
        assert d.tasks_with_several_packs == 1
        assert d.cut_off_share == pytest.approx(1 / 6)
        assert d.pr_base_rate_served == pytest.approx(2 / 5)
        assert d.prs_created_mean == pytest.approx(3 / 5)

    def test_outcomes_read_the_captured_fields(self, log: HoldoutLog) -> None:
        log.task(start=MONDAY, arms=[False], prs_created=2, commits=3, tokens=10_000)
        log.task(start=MONDAY + timedelta(hours=1), arms=[True], prs_created=0)
        log.task(
            start=MONDAY + timedelta(hours=2),
            arms=[True],
            prs_created=1,
            commits=1,
            tokens=None,
        )
        log.sweep()

        prs = _analyze(log, outcome="prs_created").inference
        assert prs.mean_served == pytest.approx(2.0)
        assert prs.mean_withheld == pytest.approx(0.5)

        tokens = _analyze(log, outcome="log1p_tokens")
        # input 10,000 + output 1,000; the third task recorded no tokens.
        assert tokens.inference.mean_served == pytest.approx(math.log1p(11_000))
        assert tokens.funnel.outcome_missing == 1

        # Commits 3 served; 0 and 1 withheld.
        log1p_commits = _analyze(log, outcome="log1p_commits").inference
        assert log1p_commits.mean_served == pytest.approx(math.log1p(3))
        assert log1p_commits.mean_withheld == pytest.approx(math.log1p(1) / 2)
        any_commit = _analyze(log, outcome="any_commit").inference
        assert any_commit.mean_served == pytest.approx(1.0)
        assert any_commit.mean_withheld == pytest.approx(0.5)

        with pytest.raises(HoldoutAnalysisError):
            _analyze(log, outcome="turns")

    def test_no_id_intent_or_text_reaches_the_report(self, log: HoldoutLog) -> None:
        seed_experiment(log, effect=1.5, seed=404, parents=2)
        log.sweep()

        dumped = json.dumps(_analyze(log).model_dump())

        for needle in (INTENT_MARKER, "task-", "pack-0", "parent-", "item-"):
            assert needle not in dumped


# ---------------------------------------------------------------------------
# Binomial check and power
# ---------------------------------------------------------------------------


class TestBinomialAndPower:
    def test_binomial_two_sided_known_values(self) -> None:
        assert binomial_two_sided(0, 10, 0.5) == pytest.approx(2 / 1024)
        assert binomial_two_sided(3, 10, 0.5) == pytest.approx(352 / 1024)
        assert binomial_two_sided(5, 10, 0.5) == pytest.approx(1.0)
        assert binomial_two_sided(2, 10, 0.1) == pytest.approx(
            1 - 0.9**10 - 10 * 0.1 * 0.9**9
        )
        assert binomial_two_sided(0, 50, 0.0) == 1.0
        assert binomial_two_sided(1, 50, 0.0) == 0.0

    def test_simulated_power_rises_with_the_shift(self) -> None:
        strata = np.repeat(np.arange(6), 20)
        pool = np.random.default_rng(11).normal(0.0, 0.6, strata.size) + strata * 0.3

        def run() -> list[float]:
            return simulate_power(
                pool,
                strata,
                np.random.default_rng(5),
                [0.0, 0.33, 1.0],
                sims=200,
                permutations=199,
            )

        power = run()
        assert power[0] <= 0.12
        assert 0.6 < power[1] < 0.97
        assert power[2] >= 0.99
        assert run() == power

    def test_planned_figures_are_options_echoed_in_the_report(
        self, log: HoldoutLog
    ) -> None:
        seed_experiment(log, effect=1.0, seed=505, parents=3)
        log.sweep()

        power = _analyze(log, planned_mde=0.4, planned_n=200).power

        assert power.status == "ok"
        assert (power.planned_mde, power.planned_n) == (0.4, 200)
        assert power.realised_n == 60
        assert power.power_at_planned_mde is not None
        assert 0.0 <= power.power_at_planned_mde <= 1.0
        assert power.mde_at_target_power is not None
        assert power.mde_at_target_power > 0
