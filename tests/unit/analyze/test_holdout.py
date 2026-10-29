"""Tests for the pack holdout analysis (``trellis.analyze.holdout``).

The fixtures write synthetic ``PACK_ASSEMBLED`` rows, capture joins and
sweeps through :mod:`tests.unit.analyze._holdout_fixture`; every id is
synthetic. Exact counts are asserted wherever a fixture fixes them, so a
mutant that moves one task between arms or funnel stages fails here.
"""

from __future__ import annotations

import json
import math
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    INTENT_MARKER,
    MONDAY,
    UNTIL,
    HoldoutLog,
    covariates_payload,
    legacy_timing_payload,
    outcome_payload,
    seed_experiment,
    timing_payload,
)
from trellis.analyze.holdout import (
    DETECTED_VERDICT,
    NO_BOTH_ARM_STRATUM,
    NO_EFFECT_VERDICT,
    NO_WITHHELD_ARM,
    SECONDARY_VERDICT,
    HoldoutAnalysisError,
    HoldoutReport,
    _covariates,
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

    def test_a_join_without_a_pack_is_split_by_why(self, log: HoldoutLog) -> None:
        cases = {
            # No retrieval result in the transcript at all.
            "never": {"retrieval_results": 0},
            # Every result was an error: a transport failure.
            "errored": {"retrieval_results": 2, "retrieval_errors": 2},
            # A result named a pack the capture could not read.
            "unparsed": {"retrieval_results": 1, "pack_ids_unparsed": 1},
            # A result came back with no pack, as an empty answer does.
            "empty": {"retrieval_results": 1},
        }
        for index, (name, counters) in enumerate(cases.items()):
            log.join(
                f"task-{name}",
                at=MONDAY + timedelta(hours=index),
                pack_ids=[],
                parent="parent-a",
                outcome=outcome_payload(turns=5),
                **counters,  # type: ignore[arg-type]
            )
        # An errored retry beside a good answer: not "errored" (no pack was
        # parsed, but one result did come back).
        log.join(
            "task-mixed",
            at=MONDAY + timedelta(hours=9),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
            retrieval_results=2,
            retrieval_errors=1,
        )
        log.sweep()

        funnel = _analyze(log).funnel

        assert funnel.no_retrieval == 5
        assert funnel.no_retrieval_never_called == 1
        assert funnel.no_retrieval_errored == 1
        assert funnel.no_retrieval_unparsed == 1
        assert funnel.no_retrieval_empty_answer == 2
        assert (
            funnel.no_retrieval_never_called
            + funnel.no_retrieval_errored
            + funnel.no_retrieval_unparsed
            + funnel.no_retrieval_empty_answer
            == funnel.no_retrieval
        )


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
        # Every join here predates the timing, so every unit is undecidable.
        assert pre.unknown == report.funnel.eligible == 10
        assert report.funnel.pre_treatment_unknown == 10
        assert report.funnel.pre_treatment_excluded == 0
        assert "first retrieval" in pre.note
        assert pre.note.startswith("Not applied")
        assert report.descriptive.post_hoc_share is None
        assert report.descriptive.post_hoc_note
        assert report.descriptive.not_measurable["post_hoc_share"] == (
            report.descriptive.post_hoc_note
        )
        assert "none of the 10 tasks" in report.descriptive.post_hoc_note

    @staticmethod
    def _seed_timed(
        log: HoldoutLog,
        *,
        after: int,
        before: int,
        unknown: int,
        withheld: bool = False,
    ) -> None:
        """Tasks by where the first retrieval sat, then one settling sweep.

        ``after`` follow a hook's feedback (excluded), ``before`` precede
        any, ``unknown`` are joins from before the timing was recorded.
        """
        hour = 0
        for count, timing in (
            (after, timing_payload(hook_before=True)),
            (before, timing_payload(hook_before=False)),
            (unknown, None),
        ):
            for _ in range(count):
                log.task(
                    start=MONDAY + timedelta(hours=hour),
                    arms=[withheld if hour % 2 else not withheld],
                    timing=timing,
                )
                hour += 1
        log.sweep()

    def test_a_first_retrieval_after_hook_feedback_is_excluded_and_counted(
        self, log: HoldoutLog
    ) -> None:
        self._seed_timed(log, after=4, before=5, unknown=0)

        report = _analyze(log)

        funnel = report.funnel
        assert funnel.sessions == 9
        assert funnel.pre_treatment_excluded == 4
        assert funnel.eligible == 5
        assert funnel.pre_treatment_unknown == 0
        assert funnel.pre_treatment_hook_feedback_seen == 4
        assert funnel.analysed == 5
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is True
        assert pre.excluded == 4
        assert pre.unknown == 0
        assert pre.note.startswith("Applied")
        assert "commit" in pre.note
        assert not any("HOOK_FEEDBACK_PATTERN" in note for note in report.notes)

    def test_every_decided_task_excluded_still_reads_applied(
        self, log: HoldoutLog
    ) -> None:
        # Nothing decided is left in eligible, so the decided count must come
        # from the exclusion stage too, or the report says "not applied".
        self._seed_timed(log, after=3, before=0, unknown=2)

        report = _analyze(log)

        assert report.funnel.pre_treatment_excluded == 3
        assert report.funnel.eligible == 2
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is True
        assert pre.excluded == 3

    def test_the_excluded_tasks_leave_every_arm_count(self, log: HoldoutLog) -> None:
        # Arms alternate by task index, so the four excluded tasks (the first
        # four) are two served and two withheld.
        self._seed_timed(log, after=4, before=4, unknown=0)

        report = _analyze(log)

        assert _arms(report.descriptive.eligible_by_arm) == {
            "served": 2,
            "withheld": 2,
        }
        assert _arms(report.descriptive.analysed_by_arm) == {
            "served": 2,
            "withheld": 2,
        }

    def test_a_first_retrieval_before_any_hook_feedback_stays_in(
        self, log: HoldoutLog
    ) -> None:
        self._seed_timed(log, after=0, before=6, unknown=0)

        report = _analyze(log)

        assert report.funnel.pre_treatment_excluded == 0
        assert report.funnel.eligible == 6
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is True
        assert pre.excluded == 0
        # Applied, yet no session held hook feedback: the reader is told the
        # capture's pattern may match nothing.
        assert report.funnel.pre_treatment_hook_feedback_seen == 0
        assert any("HOOK_FEEDBACK_PATTERN" in note for note in report.notes)

    def test_joins_without_the_timing_stay_in_and_the_exclusion_is_partial(
        self, log: HoldoutLog
    ) -> None:
        self._seed_timed(log, after=2, before=3, unknown=4)

        report = _analyze(log)

        funnel = report.funnel
        assert funnel.pre_treatment_excluded == 2
        assert funnel.pre_treatment_unknown == 4
        assert funnel.eligible == 7
        assert funnel.analysed == 7
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is True
        assert pre.excluded == 2
        assert pre.unknown == 4
        assert pre.note.startswith("Partially applied")
        assert any(
            "4 eligible tasks have joins that record no usable retrieval timing" in note
            for note in report.notes
        )

    def test_a_timing_without_a_boolean_decides_nothing(self, log: HoldoutLog) -> None:
        for hour, timing in enumerate(
            [
                timing_payload(hook_before=None),
                {"first_retrieval_ordinal": 2},
                {**timing_payload(hook_before=False), "tool_call_count": None},
            ]
        ):
            log.task(start=MONDAY + timedelta(hours=hour), timing=timing)
        log.join(
            "task-bad-type",
            at=MONDAY + timedelta(hours=9),
            pack_ids=[
                log.pack(at=MONDAY + timedelta(hours=8), withheld=False, items=2)
            ],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
            timing={"hook_feedback_before_first_retrieval": "yes"},
        )
        log.sweep()

        report = _analyze(log)

        # Only the third carries a boolean; the null, the missing key and the
        # string are undecidable, not read as False.
        assert report.funnel.pre_treatment_unknown == 3
        assert report.funnel.pre_treatment_excluded == 0
        assert report.funnel.eligible == 4

    def test_the_exclusion_sits_after_the_unfinished_stage(
        self, log: HoldoutLog
    ) -> None:
        sweep_at = MONDAY + timedelta(hours=8)
        log.task(start=MONDAY, timing=timing_payload(hook_before=True))
        log.sweep(at=sweep_at)
        # Joined an hour before the sweep: unfinished, and also after a hook.
        log.task(
            start=sweep_at - timedelta(hours=2),
            timing=timing_payload(hook_before=True),
        )

        report = _analyze(log)

        assert report.funnel.unfinished == 1
        assert report.funnel.pre_treatment_excluded == 1

    def test_every_session_is_in_exactly_one_stage(self, log: HoldoutLog) -> None:
        self._seed_timed(log, after=3, before=2, unknown=2)
        log.task(start=MONDAY + timedelta(days=1), parent=None)  # main session

        report = _analyze(log)

        f = report.funnel
        removed = (
            f.main_sessions
            + f.no_outcome
            + f.no_retrieval
            + f.first_pack_not_found
            + f.outside_window
            + f.first_pack_old_build
            + f.other_rate
            + f.empty_first_pack
            + f.unfinished
            + f.pre_treatment_excluded
        )
        assert removed + f.eligible == f.sessions == 8

    def test_a_join_rewritten_with_the_timing_counts_once_as_decided(
        self, log: HoldoutLog
    ) -> None:
        """A session re-parsed after the change has two joins; the latest counts."""
        pack = log.pack(at=MONDAY, withheld=False, items=3)
        for hour, timing in ((1, None), (2, timing_payload(hook_before=True))):
            log.join(
                "task-reparsed",
                at=MONDAY + timedelta(hours=hour),
                pack_ids=[pack],
                parent="parent-a",
                outcome=outcome_payload(turns=5),
                timing=timing,
            )
        log.sweep()

        report = _analyze(log)

        assert report.funnel.joins_scanned == 2
        assert report.funnel.sessions == 1
        assert report.funnel.pre_treatment_unknown == 0
        assert report.funnel.pre_treatment_excluded == 1
        assert report.funnel.eligible == 0

    def test_a_task_with_no_unit_reached_reports_the_rule_as_not_applied(
        self, log: HoldoutLog
    ) -> None:
        log.task(start=MONDAY, parent=None)
        log.sweep()

        report = _analyze(log)

        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is False
        assert pre.unknown == 0

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


class TestCommitHalfAndPostHoc:
    """The commit half of ``pre_treatment`` and the post-hoc share.

    A first retrieval that came after the session's first commit is an
    afterthought, as one after a hook's feedback is. The post-hoc share is
    how much of the sample is that, or in the last tenth of the calls.
    """

    @staticmethod
    def _seed(log: HoldoutLog, *timings: dict[str, Any] | None) -> None:
        """One served-arm task per timing, then one settling sweep."""
        for hour, timing in enumerate(timings):
            log.task(start=MONDAY + timedelta(hours=hour), timing=timing)
        log.sweep()

    def test_a_commit_before_the_first_retrieval_is_excluded_and_counted(
        self, log: HoldoutLog
    ) -> None:
        self._seed(
            log,
            timing_payload(hook_before=False, commit_before=True),
            timing_payload(hook_before=False, commit_before=True),
            timing_payload(hook_before=False, commit_before=False),
        )

        report = _analyze(log)

        funnel = report.funnel
        assert funnel.pre_treatment_excluded == 2
        assert funnel.pre_treatment_excluded_after_commit == 2
        assert funnel.pre_treatment_unknown == 0
        assert funnel.eligible == 1
        assert funnel.analysed == 1
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is True
        assert (pre.excluded, pre.unknown) == (2, 0)
        assert pre.note == (
            "Applied: 2 tasks whose first retrieval followed a stop hook's "
            "feedback or the first commit were excluded (2 after a commit)."
        )

    def test_a_commit_after_the_first_retrieval_keeps_the_task(
        self, log: HoldoutLog
    ) -> None:
        # ``commit_before_first_retrieval`` is False: the first commit, if
        # any, came at or after the retrieval.
        self._seed(log, timing_payload(hook_before=False, commit_before=False))

        report = _analyze(log)

        assert report.funnel.pre_treatment_excluded == 0
        assert report.funnel.pre_treatment_excluded_after_commit == 0
        assert report.funnel.eligible == 1

    def test_either_half_alone_excludes_and_both_count_once(
        self, log: HoldoutLog
    ) -> None:
        self._seed(
            log,
            timing_payload(hook_before=True, commit_before=False),
            timing_payload(hook_before=False, commit_before=True),
            timing_payload(hook_before=True, commit_before=True),
        )

        report = _analyze(log)

        assert report.funnel.pre_treatment_excluded == 3
        assert report.funnel.pre_treatment_excluded_after_commit == 2
        assert report.funnel.pre_treatment_unknown == 0
        assert report.funnel.eligible == 0

    def test_a_join_from_before_commits_were_kept_is_unknown_not_passing(
        self, log: HoldoutLog
    ) -> None:
        """No hook before the retrieval and no commit position: the rule
        cannot say the commit half passes, so the task stays and is counted."""
        self._seed(
            log,
            legacy_timing_payload(hook_before=False),
            legacy_timing_payload(hook_before=False),
        )

        report = _analyze(log)

        assert report.funnel.pre_treatment_unknown == 2
        assert report.funnel.pre_treatment_excluded == 0
        assert report.funnel.eligible == 2
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.applied is False
        assert pre.unknown == 2

    def test_a_legacy_join_after_hook_feedback_is_still_excluded(
        self, log: HoldoutLog
    ) -> None:
        self._seed(
            log,
            legacy_timing_payload(hook_before=True),
            legacy_timing_payload(hook_before=False),
        )

        report = _analyze(log)

        assert report.funnel.pre_treatment_excluded == 1
        assert report.funnel.pre_treatment_excluded_after_commit == 0
        assert report.funnel.pre_treatment_unknown == 1
        pre = next(e for e in report.exclusions if e.name == "pre_treatment")
        assert pre.note.startswith("Partially applied")

    def test_a_commit_flag_that_is_not_a_boolean_decides_nothing(
        self, log: HoldoutLog
    ) -> None:
        self._seed(
            log,
            {**timing_payload(hook_before=False), "commit_before_first_retrieval": 1},
            {
                **timing_payload(hook_before=False),
                "commit_before_first_retrieval": "yes",
            },
            {
                **timing_payload(hook_before=False),
                "commit_before_first_retrieval": None,
            },
        )

        report = _analyze(log)

        assert report.funnel.pre_treatment_unknown == 3
        assert report.funnel.pre_treatment_excluded == 0
        assert report.descriptive.post_hoc_share is None

    def test_the_post_hoc_share_is_the_late_share_of_the_measured_tasks(
        self, log: HoldoutLog
    ) -> None:
        # Five measured as after the work (one per way), five as before it.
        self._seed(
            log,
            timing_payload(hook_before=True),
            timing_payload(hook_before=False, commit_before=True),
            timing_payload(hook_before=False, ordinal=10, calls=10),
            timing_payload(hook_before=True, commit_before=True),
            timing_payload(hook_before=False, ordinal=19, calls=20),
            *[timing_payload(hook_before=False) for _ in range(5)],
        )

        report = _analyze(log)

        funnel = report.funnel
        assert (funnel.post_hoc_measured, funnel.post_hoc_after_work) == (10, 5)
        assert report.descriptive.post_hoc_share == pytest.approx(0.5)
        note = report.descriptive.post_hoc_note
        assert note.startswith("5 of 10 tasks that reached the pre-treatment rule")
        assert "0 lacked a position" in note
        assert "post_hoc_share" not in report.descriptive.not_measurable

    @pytest.mark.parametrize(
        ("ordinal", "calls", "late"),
        [
            (9, 10, False),
            (10, 10, True),
            (18, 20, False),
            (19, 20, True),
            (1, 1, False),
            (5, 6, False),
            (6, 6, False),
            (2, 2, False),
        ],
    )
    def test_the_last_tenth_of_the_calls_is_late(
        self, log: HoldoutLog, ordinal: int, calls: int, late: bool
    ) -> None:
        self._seed(log, timing_payload(hook_before=False, ordinal=ordinal, calls=calls))

        report = _analyze(log)

        assert report.funnel.post_hoc_measured == 1
        assert report.funnel.post_hoc_after_work == int(late)
        assert report.descriptive.post_hoc_share == (1.0 if late else 0.0)
        # Lateness alone does not exclude: only a hook or a commit does.
        assert report.funnel.pre_treatment_excluded == 0

    @pytest.mark.parametrize(
        "override",
        [
            {"first_retrieval_ordinal": None},
            {"tool_call_count": None},
            {"first_retrieval_ordinal": 11},
            {"first_retrieval_ordinal": 0},
            {"first_retrieval_ordinal": True},
            {"tool_call_count": "10"},
        ],
    )
    def test_a_position_the_rule_cannot_use_leaves_the_task_unmeasured(
        self, log: HoldoutLog, override: dict[str, Any]
    ) -> None:
        # An ordinal past the count cannot be placed in the last tenth, and
        # counting it as late or as early would move the share either way.
        self._seed(
            log,
            {**timing_payload(hook_before=False), **override},
            timing_payload(hook_before=False),
        )

        report = _analyze(log)

        assert report.funnel.post_hoc_measured == 1
        assert report.descriptive.post_hoc_share == 0.0
        assert "1 lacked a position the rule needs" in report.descriptive.post_hoc_note

    def test_legacy_joins_are_reported_as_lacking_not_as_early(
        self, log: HoldoutLog
    ) -> None:
        self._seed(
            log,
            timing_payload(hook_before=True),
            legacy_timing_payload(hook_before=False),
            legacy_timing_payload(hook_before=False),
            None,
        )

        report = _analyze(log)

        # Measured: one, and it was late. The three others are not counted
        # as early, which would read 1 of 4.
        assert (report.funnel.post_hoc_measured, report.funnel.post_hoc_after_work) == (
            1,
            1,
        )
        assert report.descriptive.post_hoc_share == 1.0
        assert "3 lacked a position the rule needs" in report.descriptive.post_hoc_note

    def test_the_share_counts_the_tasks_the_exclusion_dropped(
        self, log: HoldoutLog
    ) -> None:
        """Every task here is excluded, so none is eligible. A share taken
        after the exclusion would read zero or nothing; it is taken before."""
        self._seed(
            log,
            timing_payload(hook_before=False, commit_before=True),
            timing_payload(hook_before=True),
        )

        report = _analyze(log)

        assert report.funnel.eligible == 0
        assert report.descriptive.post_hoc_share == 1.0

    def test_both_arms_count_toward_the_share(self, log: HoldoutLog) -> None:
        for hour, (withheld, timing) in enumerate(
            [
                (False, timing_payload(hook_before=True)),
                (True, timing_payload(hook_before=False)),
            ]
        ):
            log.task(
                start=MONDAY + timedelta(hours=hour),
                arms=[withheld],
                timing=timing,
            )
        log.sweep()

        report = _analyze(log)

        assert report.descriptive.post_hoc_share == pytest.approx(0.5)

    def test_no_joined_position_reads_none_with_the_reason(
        self, log: HoldoutLog
    ) -> None:
        self._seed(
            log,
            legacy_timing_payload(hook_before=False),
            legacy_timing_payload(hook_before=False),
            None,
        )

        report = _analyze(log)

        assert report.descriptive.post_hoc_share is None
        assert report.descriptive.not_measurable["post_hoc_share"] == (
            report.descriptive.post_hoc_note
        )
        assert report.descriptive.post_hoc_note.startswith(
            "none of the 3 tasks that reached the pre-treatment rule"
        )

    def test_no_task_reaching_the_rule_reads_none_with_the_reason(
        self, log: HoldoutLog
    ) -> None:
        log.task(start=MONDAY, parent=None)
        log.sweep()

        report = _analyze(log)

        assert report.descriptive.post_hoc_share is None
        assert (
            report.descriptive.post_hoc_note == "no task reached the pre-treatment rule"
        )


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


# ---------------------------------------------------------------------------
# Secondary analysis: covariate-adjusted permutation
# ---------------------------------------------------------------------------

#: Fake agent-type digests: 12 hex characters, as capture writes them.
TYPE_A, TYPE_B, TYPE_C, TYPE_D, TYPE_E = (f"0a1b2c3d4e5{n}" for n in "12345")


def _write_covariate_tasks(
    log: HoldoutLog,
    *,
    y_of: Any,
    covariates_of: Any,
    parents: int = 4,
    per_parent: int = 12,
) -> None:
    """``parents`` strata of ``per_parent`` tasks, arms alternating.

    ``y_of(p, t, withheld, rng)`` is the task's outcome on the log1p turns
    scale (written exactly: the turns are ``expm1(y)``), ``covariates_of(p, t)``
    its join's ``covariates`` or ``None``. Task ``t`` of every parent starts
    ``3 t`` hours into one ISO week, so a parent is one stratum.
    """
    rng = random.Random(11)  # noqa: S311 - test RNG, not crypto
    for p in range(parents):
        for t in range(per_parent):
            withheld = t % 2 == 1
            log.task(
                start=MONDAY + timedelta(hours=3 * t, minutes=7 * p),
                parent=f"parent-{p:02d}",
                arms=[withheld],
                turns=math.expm1(y_of(p, t, withheld, rng)),
                covariates=covariates_of(p, t),
            )
    log.sweep()


def _brief(t: int) -> int:
    """A brief length that repeats across both arms of every stratum."""
    return 50 * (t // 2 + 1)


def _adjustments(report: HoldoutReport) -> dict[str, Any]:
    return {a.name: a for a in report.covariate_adjusted.adjustments}


def _width(inference: Any) -> float:
    return inference.ci_high - inference.ci_low


class TestCovariateAdjusted:
    """The secondary analysis: residualise on strata and covariates, then
    run the primary's test on the residuals. It decides nothing and never
    changes the primary."""

    def test_the_fit_is_blind_to_the_arm(self, log: HoldoutLog) -> None:
        """y = 1.0 * served + 0.5 * log1p(brief) + a stratum level, with the
        brief independent of the arm: adjusting for the brief must leave the
        effect at about 1.0. A fit that saw the arm would take the effect
        out of the residuals and read 0."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0
                + p
                + (0.0 if withheld else 1.0)
                + 0.5 * math.log1p(_brief(t))
                + rng.gauss(0.0, 0.05)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=TYPE_A
            ),
        )

        report = _analyze(log)

        brief = _adjustments(report)["brief_length"]
        assert brief.inference is not None
        assert brief.inference.status == "ok"
        assert brief.inference.difference == pytest.approx(1.0, abs=0.05)
        assert report.inference.difference == pytest.approx(1.0, abs=0.1)
        assert brief.inference.p_value is not None
        assert brief.inference.p_value < 0.05
        assert _width(brief.inference) < _width(report.inference)
        assert brief.arm_balance_r2 is not None
        assert brief.arm_balance_r2 < 0.1

    def test_the_two_adjustments_come_in_a_fixed_order_with_their_shape(
        self, log: HoldoutLog
    ) -> None:
        types = (TYPE_A, TYPE_B, TYPE_C)
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + 2.0 * (t // 2 % 3) + rng.gauss(0.0, 0.05)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=types[t // 2 % 3]
            ),
        )

        report = _analyze(log)

        block = report.covariate_adjusted
        assert block.status == "ok"
        assert [(a.name, a.preregistered) for a in block.adjustments] == [
            ("brief_length", True),
            ("brief_length_and_agent_type", False),
        ]
        brief, both = block.adjustments
        assert (brief.n_used, brief.n_missing_covariate) == (48, 0)
        assert (both.n_used, both.n_missing_covariate) == (48, 0)
        assert (brief.agent_type_levels, both.agent_type_levels) == (None, 3)
        # 4 stratum indicators + log1p(brief) [+ two agent-type indicators].
        assert (brief.df_residual, both.df_residual) == (48 - 5, 48 - 7)
        # The brief and the type change every two tasks, so within a stratum
        # neither says which arm a task is in.
        assert brief.arm_balance_r2 == pytest.approx(0.0, abs=1e-9)
        assert both.arm_balance_r2 == pytest.approx(0.0, abs=1e-9)

    def test_adjusting_for_a_strong_covariate_narrows_the_interval(
        self, log: HoldoutLog
    ) -> None:
        """Within strata the outcome follows the agent type and not the arm:
        the exploratory adjustment's interval must be well inside the
        primary's. Without residualising it would be the primary's."""
        types = (TYPE_A, TYPE_B, TYPE_C)
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + 2.0 * (t // 2 % 3) + rng.gauss(0.0, 0.1)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=types[t // 2 % 3]
            ),
        )

        report = _analyze(log)

        both = _adjustments(report)["brief_length_and_agent_type"]
        assert both.inference is not None
        assert report.inference.status == both.inference.status == "ok"
        assert _width(both.inference) < 0.5 * _width(report.inference)
        assert both.inference.p_value is not None
        assert both.inference.p_value > 0.05

    def test_a_missing_or_unusable_covariate_is_dropped_and_counted(
        self, log: HoldoutLog
    ) -> None:
        """Per stratum of 12: tasks 0 and 6 have no brief, 2 and 8 a boolean
        brief, 3 and 9 a negative one, 1 and 7 no agent type. None of them is
        read as zero or imputed."""

        def covariates(p: int, t: int) -> dict[str, Any]:
            brief: Any = _brief(t)
            agent: Any = TYPE_A
            match t % 6:
                case 0:
                    brief = None
                case 1:
                    agent = None
                case 2:
                    brief = True
                case 3:
                    brief = -5
            return covariates_payload(brief_chars=brief, agent_type=agent)

        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: 2.0 + p + rng.gauss(0.0, 0.2),
            covariates_of=covariates,
        )

        report = _analyze(log)

        brief, both = report.covariate_adjusted.adjustments
        assert report.funnel.analysed == 48
        assert (brief.n_used, brief.n_missing_covariate) == (24, 24)
        assert (both.n_used, both.n_missing_covariate) == (16, 32)
        for adjustment in (brief, both):
            assert adjustment.n_used + adjustment.n_missing_covariate == 48
            assert adjustment.inference is not None
        assert report.inference.status == "ok"

    def test_a_task_with_no_covariates_block_is_missing_both(
        self, log: HoldoutLog
    ) -> None:
        """Half the joins predate the block; the other half carry it."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: 2.0 + p + rng.gauss(0.0, 0.2),
            covariates_of=lambda p, t: (
                None
                if p < 2
                else covariates_payload(brief_chars=_brief(t), agent_type=TYPE_A)
            ),
        )

        brief, both = _analyze(log).covariate_adjusted.adjustments

        assert (brief.n_used, brief.n_missing_covariate) == (24, 24)
        assert (both.n_used, both.n_missing_covariate) == (24, 24)

    def test_no_covariates_anywhere_is_not_measurable_with_one_note(
        self, log: HoldoutLog
    ) -> None:
        seed_experiment(log, effect=1.5, seed=101)
        log.sweep()

        report = _analyze(log)

        block = report.covariate_adjusted
        assert block.status == "not_measurable"
        assert [a.status for a in block.adjustments] == ["not_measurable"] * 2
        for adjustment in block.adjustments:
            assert adjustment.reason == (
                "no analysed task's join records the covariates (joins written "
                "before capture recorded them)"
            )
            assert adjustment.inference is None
            assert adjustment.n_used == 0
            assert adjustment.n_missing_covariate == report.funnel.analysed == 120
        assert sum("brief-length" in note for note in report.notes) == 1
        assert block.note in report.notes
        assert "not measurable" in block.note
        # The primary is untouched by it.
        assert report.inference.status == "ok"

    def test_no_analysed_task_is_not_measurable_either(self, log: HoldoutLog) -> None:
        log.task(start=MONDAY, parent=None)
        log.sweep()

        report = _analyze(log)

        block = report.covariate_adjusted
        assert report.funnel.analysed == 0
        assert block.status == "not_measurable"
        assert {a.reason for a in block.adjustments} == {"no analysed task"}

    def test_the_note_counts_what_each_adjustment_used_and_dropped(
        self, log: HoldoutLog
    ) -> None:
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: 2.0 + p + rng.gauss(0.0, 0.2),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=None if t == 0 else TYPE_A
            ),
        )

        report = _analyze(log)

        (note,) = [n for n in report.notes if "brief-length" in n]
        assert (
            "brief-length (pre-registered): 48 of 48 analysed tasks used, "
            "0 lacked a covariate"
        ) in note
        assert (
            "brief-length and agent type (exploratory): 44 of 48 analysed tasks "
            "used, 4 lacked a covariate"
        ) in note
        assert "holds the fitted coefficients fixed" in note
        assert "unadjusted_same_subset" in note

    @pytest.mark.parametrize(
        ("third_type_tasks", "levels"),
        [
            pytest.param(3, 4, id="three-tasks-keep-their-level"),
            pytest.param(2, 3, id="two-tasks-are-pooled"),
        ],
    )
    def test_rare_agent_types_pool_into_one_level(
        self, log: HoldoutLog, third_type_tasks: int, levels: int
    ) -> None:
        """Types of 22 and 20 tasks stand alone. A third of 3 stands alone
        and one of 2 is pooled with the 2- and 1-task types."""
        assigned = (
            [TYPE_A] * 22
            + [TYPE_B] * 20
            + [TYPE_C] * third_type_tasks
            + [TYPE_D] * 2
            + [TYPE_E] * (3 - third_type_tasks + 1)
        )
        assert len(assigned) == 48
        random.Random(5).shuffle(assigned)  # noqa: S311 - test RNG, not crypto
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: 2.0 + p + rng.gauss(0.0, 0.2),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=assigned[12 * p + t]
            ),
        )

        both = _adjustments(_analyze(log))["brief_length_and_agent_type"]

        assert both.agent_type_levels == levels
        assert both.n_used == 48
        # 4 strata + log1p(brief) + (levels - 1) agent-type indicators.
        assert both.df_residual == 48 - (5 + levels - 1)

    def test_too_few_residual_degrees_of_freedom_is_not_measurable(
        self, log: HoldoutLog
    ) -> None:
        """Three strata of three tasks: 9 tasks less a rank of 4 leaves 5 for
        the brief alone, and the exploratory fit's two agent-type indicators
        (each type once per stratum, so none is a stratum's copy) leave 3,
        under the floor of 4."""
        types = (TYPE_A, TYPE_B, TYPE_C)  # one of each in every stratum
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: 2.0 + p + rng.gauss(0.0, 0.2),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=100 + 30 * t * (p + 1), agent_type=types[t]
            ),
            parents=3,
            per_parent=3,
        )

        brief, both = _analyze(log).covariate_adjusted.adjustments

        assert brief.status == "ok"
        assert brief.df_residual == 5
        assert both.status == "not_measurable"
        assert both.reason == (
            "3 residual degrees of freedom after the fit, fewer than 4"
        )
        assert (both.n_used, both.agent_type_levels, both.df_residual) == (9, 3, 3)
        assert both.inference is None

    def test_a_subset_with_no_stratum_holding_both_arms_is_not_measurable(
        self, log: HoldoutLog
    ) -> None:
        """Every parent's tasks are in one arm, so no stratum has both."""
        for p in range(4):
            for t in range(3):
                log.task(
                    start=MONDAY + timedelta(hours=3 * t, minutes=7 * p),
                    parent=f"parent-{p:02d}",
                    arms=[p % 2 == 1],
                    turns=10 + t,
                    covariates=covariates_payload(
                        brief_chars=100 + 40 * t + p, agent_type=TYPE_A
                    ),
                )
        log.sweep()

        report = _analyze(log)

        assert report.inference.status == NO_BOTH_ARM_STRATUM
        brief = _adjustments(report)["brief_length"]
        assert brief.status == "not_measurable"
        assert brief.reason == NO_BOTH_ARM_STRATUM
        assert brief.df_residual == 12 - 5

    @pytest.mark.parametrize("effect", [0.0, 1.5], ids=["no-effect", "detected"])
    def test_a_secondary_never_speaks_in_the_primarys_words(
        self, log: HoldoutLog, effect: float
    ) -> None:
        """Whatever the primary concludes, no secondary result carries its
        verdict, and the residual means sit under no outcome-mean key."""
        types = (TYPE_A, TYPE_B, TYPE_C)
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0
                + p
                + (0.0 if withheld else effect)
                + 0.4 * (t // 2 % 3)
                + rng.gauss(0.0, 0.1)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=types[t // 2 % 3]
            ),
        )

        report = _analyze(log)

        assert report.inference.verdict == (
            DETECTED_VERDICT if effect else NO_EFFECT_VERDICT
        )
        secondary = [
            result
            for adjustment in report.covariate_adjusted.adjustments
            for result in (adjustment.inference, adjustment.unadjusted_same_subset)
        ]
        assert len(secondary) == 4
        for result in secondary:
            assert result is not None
            assert result.status == "ok"
            assert result.verdict == SECONDARY_VERDICT
            assert result.verdict not in {DETECTED_VERDICT, NO_EFFECT_VERDICT}
        # Neither primary verdict string appears anywhere in the block.
        block = json.dumps(report.covariate_adjusted.model_dump())
        assert DETECTED_VERDICT not in block
        assert NO_EFFECT_VERDICT not in block
        for adjustment in report.covariate_adjusted.adjustments:
            adjusted, raw = adjustment.inference, adjustment.unadjusted_same_subset
            assert adjusted is not None
            assert raw is not None
            # An adjusted run's means are residual means, so no outcome-mean
            # key carries them; the raw run's means are outcome means.
            assert adjusted.mean_served is None
            assert adjusted.mean_withheld is None
            assert adjusted.mean_residual_served is not None
            assert adjusted.mean_residual_withheld is not None
            assert adjusted.difference == pytest.approx(
                adjusted.mean_residual_served - adjusted.mean_residual_withheld
            )
            assert abs(adjusted.mean_residual_served) < 1.0
            assert raw.mean_residual_served is None
            assert raw.mean_residual_withheld is None
            assert raw.mean_served == pytest.approx(report.inference.mean_served)
            assert raw.mean_withheld == pytest.approx(report.inference.mean_withheld)
            assert raw.mean_served is not None
            assert raw.mean_served > 2.0

    def test_the_unadjusted_run_is_the_primarys_test_on_the_same_tasks(
        self, log: HoldoutLog, tmp_path: Path
    ) -> None:
        """Only the last two parents carry covariates. Their raw outcomes,
        tested on their own as a primary, give the figures the adjustment's
        ``unadjusted_same_subset`` holds, which the full primary does not."""

        def y_of(p: int, t: int, withheld: bool) -> float:
            return 1.0 + p + 0.6 * (not withheld) + 0.4 * math.sin(17 * p + 5 * t)

        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: y_of(p, t, withheld),
            covariates_of=lambda p, t: (
                None
                if p < 2
                else covariates_payload(brief_chars=_brief(t), agent_type=TYPE_A)
            ),
        )
        subset_log = SQLiteEventLog(tmp_path / "subset.db")
        try:
            subset = HoldoutLog(subset_log)
            _write_covariate_tasks(
                subset,
                y_of=lambda p, t, withheld, rng: y_of(p + 2, t, withheld),
                covariates_of=lambda p, t: None,
                parents=2,
            )
            alone = _analyze(subset)
        finally:
            subset_log.close()

        report = _analyze(log)

        brief = _adjustments(report)["brief_length"]
        raw = brief.unadjusted_same_subset
        assert raw is not None
        assert (brief.n_used, brief.n_missing_covariate) == (24, 24)
        assert alone.funnel.analysed == 24
        assert raw.difference == pytest.approx(alone.inference.difference)
        assert raw.mean_served == pytest.approx(alone.inference.mean_served)
        assert raw.mean_withheld == pytest.approx(alone.inference.mean_withheld)
        assert (raw.strata, raw.strata_both_arms) == (
            alone.inference.strata,
            alone.inference.strata_both_arms,
        )
        assert alone.inference.p_value is not None
        assert raw.p_value is not None
        assert raw.p_value == pytest.approx(alone.inference.p_value, abs=0.06)
        # It is not the full primary, which tests all 48 tasks.
        assert report.funnel.analysed == 48
        assert raw.difference != pytest.approx(report.inference.difference, abs=1e-6)
        assert raw.strata == 2
        assert report.inference.strata == 4

    def test_the_covariate_note_says_the_secondary_covers_a_subset(
        self, log: HoldoutLog
    ) -> None:
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: 2.0 + p + rng.gauss(0.0, 0.2),
            covariates_of=lambda p, t: (
                None
                if p < 2
                else covariates_payload(brief_chars=_brief(t), agent_type=TYPE_A)
            ),
        )

        report = _analyze(log)

        (note,) = [n for n in report.notes if "brief-length" in n]
        assert "covers only the analysed tasks that carry its covariates" in note
        assert "mixes the adjustment with the subset" in note
        assert "24 of 48 analysed tasks used, 24 lacked a covariate" in note

    def test_the_unadjusted_run_does_not_share_the_primarys_streams(
        self, log: HoldoutLog
    ) -> None:
        """With every task used, the unadjusted run has the primary's
        difference but resamples from streams of its own: replaying the
        primary's would give its p-value and interval to the digit."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + 0.12 * (not withheld) + rng.gauss(0.0, 0.3)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=TYPE_A
            ),
        )

        report = _analyze(log)

        primary = report.inference
        raw_results = []
        for adjustment in report.covariate_adjusted.adjustments:
            raw = adjustment.unadjusted_same_subset
            assert raw is not None
            assert raw.difference == pytest.approx(primary.difference)
            assert (raw.p_value, raw.ci_low, raw.ci_high) != (
                primary.p_value,
                primary.ci_low,
                primary.ci_high,
            )
            raw_results.append(raw)
        # The two adjustments' unadjusted runs have streams of their own too.
        first, second = raw_results
        assert (first.p_value, first.ci_low) != (second.p_value, second.ci_low)

    def test_the_adjusted_and_unadjusted_streams_are_pinned(
        self, log: HoldoutLog
    ) -> None:
        """Children 3 to 6 of the seed drive the adjusted tests and 7 to 10
        the unadjusted runs. The adjusted figures were taken from the source
        before the unadjusted runs existed, so appending children moved
        nothing; the unadjusted ones pin the new streams."""
        digests = (TYPE_A, TYPE_B, TYPE_C)
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + 0.12 * (not withheld) + rng.gauss(0.0, 0.3)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=digests[t // 2 % 3]
            ),
        )

        brief, both = _analyze(log).covariate_adjusted.adjustments

        assert brief.inference is not None
        assert brief.inference.difference == pytest.approx(0.15477088151950544)
        assert brief.inference.p_value == pytest.approx(0.074)
        assert brief.inference.ci_low == pytest.approx(0.01087304586617753)
        assert brief.inference.ci_high == pytest.approx(0.31758929715456696)
        assert both.inference is not None
        assert both.inference.p_value == pytest.approx(0.06)
        assert both.inference.ci_low == pytest.approx(-0.003682356980984)
        assert both.inference.ci_high == pytest.approx(0.29491888544439493)
        assert brief.unadjusted_same_subset is not None
        assert brief.unadjusted_same_subset.p_value == pytest.approx(0.099)
        assert brief.unadjusted_same_subset.ci_low == pytest.approx(
            0.0014142481006423498
        )
        assert brief.unadjusted_same_subset.ci_high == pytest.approx(
            0.31802951358965886
        )
        assert both.unadjusted_same_subset is not None
        assert both.unadjusted_same_subset.p_value == pytest.approx(0.082)
        assert both.unadjusted_same_subset.ci_low == pytest.approx(0.008144597236544072)
        assert both.unadjusted_same_subset.ci_high == pytest.approx(0.31441129698486453)

    def test_a_covariate_that_is_the_arm_is_not_measurable(
        self, log: HoldoutLog
    ) -> None:
        """The brief is a function of the arm in every stratum, so the fit
        has nothing to separate the two: without the gate this reads as an
        ``ok`` difference of about nothing."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + (0.0 if withheld else 1.5) + rng.gauss(0.0, 0.1)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=300 if t % 2 == 0 else 100, agent_type=TYPE_A
            ),
        )

        report = _analyze(log)

        assert report.inference.verdict == DETECTED_VERDICT
        block = report.covariate_adjusted
        assert block.status == "not_measurable"
        for adjustment in block.adjustments:
            assert adjustment.status == "not_measurable"
            assert adjustment.inference is None
            assert adjustment.arm_balance_r2 == pytest.approx(1.0)
            assert adjustment.reason is not None
            assert adjustment.reason.startswith(
                "the covariates explain 1.00 of the arm's within-stratum variance, "
                "over the 0.9 limit"
            )
            assert adjustment.df_residual == 48 - 5
            # The unadjusted run on the same tasks is still reported.
            raw = adjustment.unadjusted_same_subset
            assert raw is not None
            assert raw.status == "ok"
            assert raw.difference == pytest.approx(report.inference.difference)
        assert "not measurable, the covariates explain 1.00" in block.note

    def test_an_agent_type_that_is_the_arm_spares_the_brief_adjustment(
        self, log: HoldoutLog
    ) -> None:
        """Served tasks all one type and withheld tasks all another: the
        exploratory adjustment is refused and the brief-length one stands."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + (0.0 if withheld else 1.5) + rng.gauss(0.0, 0.1)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=TYPE_B if t % 2 else TYPE_A
            ),
        )

        report = _analyze(log)

        brief, both = report.covariate_adjusted.adjustments
        assert report.covariate_adjusted.status == "ok"
        assert brief.status == "ok"
        assert brief.arm_balance_r2 == pytest.approx(0.0, abs=1e-9)
        assert brief.inference is not None
        assert brief.inference.difference == pytest.approx(1.5, abs=0.1)
        assert both.status == "not_measurable"
        assert both.inference is None
        assert both.agent_type_levels == 2
        assert both.arm_balance_r2 == pytest.approx(1.0)
        assert both.reason is not None
        assert both.reason.startswith("the covariates explain 1.00 of the arm's")
        assert both.unadjusted_same_subset is not None
        assert both.unadjusted_same_subset.status == "ok"

    @pytest.mark.parametrize(
        ("leak", "refused"),
        [
            pytest.param(2.0, False, id="explains-under-the-limit"),
            pytest.param(4.5, True, id="explains-over-the-limit"),
        ],
    )
    def test_the_arm_balance_limit_sits_at_nine_tenths(
        self, log: HoldoutLog, leak: float, refused: bool
    ) -> None:
        """The brief's log carries ``leak`` times the arm plus a within-pair
        trend, so it explains about 0.79 (leak 2) or 0.95 (leak 4.5) of the
        arm's within-stratum variance."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + (0.0 if withheld else 0.3) + rng.gauss(0.0, 0.3)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=round(math.expm1(4 + leak * (t % 2) + 0.3 * (t // 2))),
                agent_type=TYPE_A,
            ),
        )

        brief = _adjustments(_analyze(log))["brief_length"]

        assert brief.arm_balance_r2 is not None
        if refused:
            assert 0.9 < brief.arm_balance_r2 < 0.99
            assert brief.status == "not_measurable"
        else:
            assert 0.7 < brief.arm_balance_r2 < 0.9
            assert brief.status == "ok"
            assert brief.inference is not None

    def test_the_residual_degrees_of_freedom_floor_is_four(
        self, log: HoldoutLog, tmp_path: Path
    ) -> None:
        """One stratum, the brief alone: six tasks leave 6 - 2 = 4 degrees of
        freedom, which is enough, and five leave 3, which is not."""
        results = {}
        for per_parent in (6, 5):
            event_log = SQLiteEventLog(tmp_path / f"floor-{per_parent}.db")
            try:
                _write_covariate_tasks(
                    HoldoutLog(event_log),
                    y_of=lambda p, t, withheld, rng: 2.0 + rng.gauss(0.0, 0.2),
                    covariates_of=lambda p, t: covariates_payload(
                        brief_chars=_brief(t), agent_type=TYPE_A
                    ),
                    parents=1,
                    per_parent=per_parent,
                )
                results[per_parent] = _adjustments(_analyze(HoldoutLog(event_log)))[
                    "brief_length"
                ]
            finally:
                event_log.close()

        at_the_floor, under = results[6], results[5]
        assert (at_the_floor.n_used, at_the_floor.df_residual) == (6, 4)
        assert at_the_floor.status == "ok"
        assert at_the_floor.inference is not None
        assert (under.n_used, under.df_residual) == (5, 3)
        assert under.status == "not_measurable"
        assert under.reason == (
            "3 residual degrees of freedom after the fit, fewer than 4"
        )
        assert under.inference is None
        assert under.unadjusted_same_subset is None

    def test_the_primary_is_the_same_with_and_without_covariates(
        self, log: HoldoutLog, tmp_path: Path
    ) -> None:
        """Same rows, same seed: the primary and the power simulation do not
        move when the joins carry covariates."""
        other_log = SQLiteEventLog(tmp_path / "plain.db")
        try:
            plain = HoldoutLog(other_log)
            for target, with_cov in ((log, True), (plain, False)):
                _write_covariate_tasks(
                    target,
                    y_of=lambda p, t, withheld, rng: (
                        1.0 + p + 0.12 * (not withheld) + rng.gauss(0.0, 0.3)
                    ),
                    covariates_of=lambda p, t, on=with_cov: (
                        covariates_payload(brief_chars=_brief(t), agent_type=TYPE_A)
                        if on
                        else None
                    ),
                )

            with_covariates = _analyze(log)
            without = _analyze(plain)
        finally:
            other_log.close()

        assert with_covariates.covariate_adjusted.status == "ok"
        assert without.covariate_adjusted.status == "not_measurable"
        assert with_covariates.inference == without.inference
        assert with_covariates.power == without.power
        assert with_covariates.descriptive == without.descriptive

    def test_the_primary_keeps_its_seeded_streams(self, log: HoldoutLog) -> None:
        """Children 0 and 1 of the seed drive the primary's permutations and
        bootstrap. The figures below were taken from the source as it stood
        before the secondary analysis existed (seed 7, 999 permutations, 400
        bootstraps); adding children to the spawn must not move them."""
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + 0.12 * (not withheld) + rng.gauss(0.0, 0.3)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=TYPE_A
            ),
        )

        inference = _analyze(log).inference

        assert inference.difference == pytest.approx(0.15477088151950508)
        assert inference.p_value == pytest.approx(0.103)
        assert inference.ci_low == pytest.approx(0.0017409438710435726)
        assert inference.ci_high == pytest.approx(0.31136976263984073)

    @pytest.mark.parametrize(
        ("few", "many"),
        [(3, 11), (7, 11), (3, 7)],
        ids=["primary-and-power", "first-secondary-pairs", "primary-in-the-old-seven"],
    )
    def test_a_seed_sequence_child_does_not_depend_on_the_child_count(
        self, few: int, many: int
    ) -> None:
        fewer = np.random.SeedSequence(7).spawn(few)
        more = np.random.SeedSequence(7).spawn(many)
        for index in range(few):
            assert (
                np.random.default_rng(fewer[index]).random(4).tolist()
                == np.random.default_rng(more[index]).random(4).tolist()
            )

    def test_no_digest_reaches_the_report(self, log: HoldoutLog) -> None:
        types = (TYPE_A, TYPE_B, TYPE_C)
        _write_covariate_tasks(
            log,
            y_of=lambda p, t, withheld, rng: (
                1.0 + p + 2.0 * (t // 2 % 3) + rng.gauss(0.0, 0.1)
            ),
            covariates_of=lambda p, t: covariates_payload(
                brief_chars=_brief(t), agent_type=types[t // 2 % 3]
            ),
        )

        dumped = _analyze(log).model_dump_json()

        for digest in types:
            assert digest not in dumped
        assert "agent_type_digest" not in dumped


class TestReadingTheCovariates:
    """What the unit takes from the join's ``covariates`` block."""

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            pytest.param(
                {"brief_chars": 120, "agent_type_digest": "abc123def456"},
                (120.0, "abc123def456"),
                id="usable",
            ),
            pytest.param(
                {"brief_chars": 0, "agent_type_digest": None},
                (0.0, None),
                id="zero-is-a-length",
            ),
            pytest.param({"brief_chars": 12.0}, (12.0, None), id="float-length"),
            pytest.param({"brief_chars": None}, (None, None), id="none"),
            pytest.param({"brief_chars": True}, (None, None), id="bool"),
            pytest.param({"brief_chars": -1}, (None, None), id="negative"),
            pytest.param({"brief_chars": float("nan")}, (None, None), id="nan"),
            pytest.param({"brief_chars": float("inf")}, (None, None), id="inf"),
            pytest.param({"brief_chars": "120"}, (None, None), id="string-length"),
            pytest.param({"agent_type_digest": ""}, (None, None), id="empty-digest"),
            pytest.param({"agent_type_digest": 12}, (None, None), id="int-digest"),
            pytest.param({"agent_type_digest": ["a"]}, (None, None), id="list-digest"),
            pytest.param({}, (None, None), id="empty-block"),
        ],
    )
    def test_each_covariate_is_validated_alone(
        self, block: dict[str, Any], expected: tuple[float | None, str | None]
    ) -> None:
        assert _covariates({"covariates": block}) == expected

    @pytest.mark.parametrize("raw", [None, [], "x", 3, True])
    def test_a_block_that_is_not_an_object_has_neither(self, raw: object) -> None:
        assert _covariates({"covariates": raw}) == (None, None)

    def test_a_join_without_the_block_has_neither(self) -> None:
        assert _covariates({}) == (None, None)
