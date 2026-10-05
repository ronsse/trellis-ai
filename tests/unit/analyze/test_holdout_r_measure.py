"""Tests for the [R] re-measure figures of ``trellis analyze holdout``.

The pre-registration marks each number it took from the prestudy with
[R] and asks for it to be re-measured on capture rows before sign-off.
Every figure here is checked on a synthetic store whose answer is worked
out by hand in the test, so a mutant that drops a figure, moves a horizon
or counts the wrong arm fails. Every id the fixture writes is synthetic.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    MONDAY,
    UNTIL,
    HoldoutLog,
    outcome_payload,
)
from trellis.analyze import holdout
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Small resampling counts keep each analysis well under a second.
FAST = {"permutations": 999, "bootstraps": 400, "power_sims": 40}

#: Exact t(0.975) + t(0.80) at 4, 10 and 16 degrees of freedom, to six
#: decimals: the prestudy's MDE multiplier at N = 6, 12 and 18.
T_SUM = {6: 3.717410, 12: 3.107197, 18: 2.984572}


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _analyze(log: HoldoutLog, **kwargs: object) -> holdout.HoldoutReport:
    options: dict[str, object] = {"days": DAYS, "until": UNTIL, "seed": 7, **FAST}
    options.update(kwargs)
    return holdout.analyze_holdout(log.event_log, **options)  # type: ignore[arg-type]


def _exact_mde(sd: float, n: int) -> float:
    return T_SUM[n] * sd * math.sqrt(4 / n)


# ---------------------------------------------------------------------------
# The prestudy's MDE formula
# ---------------------------------------------------------------------------


class TestAnalyticMde:
    @pytest.mark.parametrize("n", [6, 12, 18])
    def test_the_formula_uses_t_quantiles_at_n_minus_2_df(self, n: int) -> None:
        assert holdout.analytic_mde(1.0, n) == pytest.approx(
            _exact_mde(1.0, n), rel=3e-4
        )
        assert holdout.analytic_mde(2.5, n) == pytest.approx(
            _exact_mde(2.5, n), rel=3e-4
        )

    def test_below_six_units_the_formula_is_undefined(self) -> None:
        assert holdout.analytic_mde(1.0, 5.99) is None
        assert holdout.analytic_mde(1.0, 6.0) is not None

    def test_the_prestudy_anchors_are_reproduced(self) -> None:
        """prestudy.md: MDE 0.33 at SD 0.63 and N 114; x13 at SD 1.83 and N 18."""
        assert holdout.analytic_mde(0.63, 114) == pytest.approx(0.3335, abs=1e-3)
        session = holdout.analytic_mde(1.83, 18)
        assert session is not None
        assert math.exp(session) == pytest.approx(13.13, abs=0.05)
        # "A 10% reduction in turns needs about 1,100" (SD 0.63).
        assert holdout.n_for_mde(0.63, -math.log(0.9)) == 1125

    def test_n_for_mde_is_the_smallest_n_that_reaches_the_effect(self) -> None:
        effect = -math.log(0.9)
        n = holdout.n_for_mde(0.63, effect)
        assert holdout.analytic_mde(0.63, n) <= effect
        assert holdout.analytic_mde(0.63, n - 1) > effect
        # The formula's minimum already reaches a large effect.
        assert holdout.n_for_mde(1.0, 100.0) == 6
        with pytest.raises(ValueError, match="positive"):
            holdout.n_for_mde(1.0, 0.0)


# ---------------------------------------------------------------------------
# Task figures on a flag-off store worked out by hand
# ---------------------------------------------------------------------------


def _seed_hand_store(log: HoldoutLog) -> None:
    """Flag off: 12 analysed tasks and 2 cut-offs in 60 days.

    parent-a: 8 tasks, turns alternating 3 / 15 (log1p 2 ln 2 / 4 ln 2),
    commits alternating 2 / 4. parent-b: 4 tasks, turns alternating 1 / 7
    (ln 2 / 3 ln 2), commits alternating 6 / 10. parent-a also holds the
    two cut-offs, so it has 10 of the 14 eligible tasks.
    """
    log.deployed_before_window(rate=0.0)
    rows = [("parent-a", (3, 15)[i % 2], (2, 4)[i % 2], False) for i in range(8)]
    rows += [("parent-b", (1, 7)[i % 2], (6, 10)[i % 2], False) for i in range(4)]
    rows += [("parent-a", 40, 50, True)] * 2
    for hour, (parent, turns, commits, cut_off) in enumerate(rows):
        log.task(
            start=MONDAY + timedelta(hours=hour),
            parent=parent,
            arms=[False],
            rate=0.0,
            turns=turns,
            commits=commits,
            ended_on_error=cut_off,
        )
    log.sweep()


class TestTaskFigures:
    def test_log_outcome_figures_match_a_hand_computation(
        self, log: HoldoutLog
    ) -> None:
        """Within-parent residuals are all +-ln 2: SS 12 (ln 2)^2 on 12 - 2 df.

        12 analysed tasks in 60 days is 6 per 30 days, so N is 6 / 12 / 18.
        """
        _seed_hand_store(log)

        d = _analyze(log).descriptive

        sd = math.log(2) * math.sqrt(1.2)
        assert (d.eligible_tasks, d.analysed_tasks) == (14, 12)
        assert d.outcome_sd_within_parent == pytest.approx(sd)
        # Cut-offs count: parent-a holds 10 of 14 eligible (8 of 12 analysed).
        assert d.top_parent_share == pytest.approx(10 / 14)
        horizons = d.mde_by_horizon
        assert [h.days for h in horizons] == [30, 60, 90]
        assert [h.n for h in horizons] == pytest.approx([6, 12, 18])
        expected = [_exact_mde(sd, n) for n in (6, 12, 18)]
        assert [h.mde for h in horizons] == pytest.approx(expected, rel=1e-3)
        assert [h.ratio for h in horizons] == pytest.approx(
            [math.exp(m) for m in expected], rel=1e-3
        )
        assert all(h.share_of_mean is None for h in horizons)
        assert all(h.not_measurable is None for h in horizons)
        # x0.9 is ln(1/0.9) = 0.10536 on log1p turns; the formula's N is 1633
        # (normal approximation 1630.6).
        assert d.n_for_10pct_effect == 1633
        assert set(d.not_measurable) == {
            "post_hoc_share",
            "cut_off_share_by_arm.withheld",
        }

    def test_raw_outcome_figures_are_shares_of_the_served_mean(
        self, log: HoldoutLog
    ) -> None:
        """Commits: within-parent SS 8 + 16 = 24 on 10 df; served mean 14/3."""
        _seed_hand_store(log)

        d = _analyze(log, outcome="commits").descriptive

        sd = math.sqrt(2.4)
        mean = 14 / 3
        assert d.outcome_sd_within_parent == pytest.approx(sd)
        expected = [_exact_mde(sd, n) for n in (6, 12, 18)]
        horizons = d.mde_by_horizon
        assert [h.mde for h in horizons] == pytest.approx(expected, rel=1e-3)
        assert [h.share_of_mean for h in horizons] == pytest.approx(
            [m / mean for m in expected], rel=1e-3
        )
        assert all(h.ratio is None for h in horizons)
        # 10% of the served mean is 0.4667 commits; N 348 (normal approx 346.0).
        assert d.n_for_10pct_effect == 348


class TestBetweenParentShare:
    def test_the_share_is_bias_adjusted_and_floored_at_zero(
        self, log: HoldoutLog
    ) -> None:
        """Commits parent-a [1, 3], parent-b [2, 3].

        Within variance 2.5 / 2 = 1.25 exceeds the total 2.75 / 3, so the
        adjusted share 1 - 1.25 / 0.917 is negative and floors at 0; the raw
        eta-squared would read 0.25 / 2.75 = 0.09.
        """
        for hour, (parent, commits) in enumerate(
            [("parent-a", 1), ("parent-a", 3), ("parent-b", 2), ("parent-b", 3)]
        ):
            log.task(
                start=MONDAY + timedelta(hours=hour),
                parent=parent,
                arms=[False],
                rate=0.0,
                commits=commits,
            )
        log.sweep()

        d = _analyze(log, outcome="commits").descriptive

        assert d.outcome_sd_within_parent == pytest.approx(math.sqrt(1.25))
        assert d.between_parent_share == 0.0


# ---------------------------------------------------------------------------
# The PR base rate counts the served arm only
# ---------------------------------------------------------------------------


class TestPrBaseRate:
    def test_the_base_rate_counts_only_served_arm_tasks(self, log: HoldoutLog) -> None:
        """Served prs_created [1, 0, 2, 0, 0], withheld [0, 1, 0].

        Served arm: 2 of 5 open a PR. Over both arms it would be 3 of 8.
        """
        arms = [(False, prs) for prs in (1, 0, 2, 0, 0)]
        arms += [(True, prs) for prs in (0, 1, 0)]
        for hour, (withheld, prs) in enumerate(arms):
            log.task(
                start=MONDAY + timedelta(hours=hour),
                parent="parent-a" if hour % 2 else "parent-b",
                arms=[withheld],
                prs_created=prs,
                turns=6 + hour,
            )
        log.sweep()

        d = _analyze(log).descriptive

        assert (d.analysed_by_arm.served, d.analysed_by_arm.withheld) == (5, 3)
        assert d.pr_base_rate_served == pytest.approx(2 / 5)
        # The mean stays over both arms: 4 PRs across 8 tasks.
        assert d.prs_created_mean == pytest.approx(4 / 8)
        assert "pr_base_rate" not in d.model_dump()


# ---------------------------------------------------------------------------
# Rolled-up main sessions
# ---------------------------------------------------------------------------


class TestMainSessions:
    def test_rolled_up_sessions_are_counted_and_powered(self, log: HoldoutLog) -> None:
        """Nine finished sessions S1-S9, each commented with what it adds.

        Reaching a retrieval: 8 (all but S4). A non-empty pack: S1, S2, S6,
        S7, S8, S9. Analysed (every join carries an outcome holding the
        turns): S1, S2, S6, S8, whose rolled turns are 60, 20, 40 and 21.
        """

        def day(index: int) -> datetime:
            return MONDAY + timedelta(days=index)

        # S1: no pack on the main; one sub served 3 items, one asked nothing.
        s1 = log.task(start=day(0), parent=None, arms=(), turns=10)
        log.task(start=day(0) + timedelta(hours=2), parent=s1, items=3, turns=20)
        log.task(start=day(0) + timedelta(hours=3), parent=s1, arms=(), turns=30)
        # S2: the main's pack is withheld; its would-be 2 items count.
        s2 = log.task(start=day(1), parent=None, arms=[True], items=2, turns=5)
        log.task(start=day(1) + timedelta(hours=2), parent=s2, arms=(), turns=15)
        # S3: a served pack of 0 items reaches a retrieval, not eligibility.
        log.task(start=day(2), parent=None, items=0, turns=8)
        # S4: no retrieval anywhere.
        s4 = log.task(start=day(3), parent=None, arms=(), turns=12)
        log.task(start=day(3) + timedelta(hours=2), parent=s4, arms=(), turns=4)
        # S5: a retrieval returned results but no pack id was parsed.
        log.join(
            "main-s5",
            at=day(4),
            pack_ids=[],
            parent=None,
            outcome=outcome_payload(turns=6),
            retrieval_results=1,
        )
        # S6: the main and its sub were both served.
        s6 = log.task(start=day(5), parent=None, items=1, turns=9)
        log.task(start=day(5) + timedelta(hours=2), parent=s6, items=4, turns=31)
        # S7: eligible, but the main's join carries no outcome; its sub's does.
        s7 = log.task(start=day(6), parent=None, items=2, with_outcome=False)
        log.task(start=day(6) + timedelta(hours=2), parent=s7, arms=(), turns=13)
        # S8: three members, two of them served.
        s8 = log.task(start=day(7), parent=None, items=2, turns=7)
        log.task(start=day(7) + timedelta(hours=2), parent=s8, arms=(), turns=11)
        log.task(start=day(7) + timedelta(hours=3), parent=s8, items=1, turns=3)
        # S9: eligible, but its sub's outcome lacks the turns field.
        s9 = log.task(start=day(9), parent=None, items=2, turns=7)
        no_turns = outcome_payload(turns=4)
        del no_turns["assistant_turns"]
        log.join(
            "sub-s9",
            at=day(9) + timedelta(hours=3),
            pack_ids=[],
            parent=s9,
            outcome=no_turns,
        )
        # Left out: a sub whose main transcript never joined ...
        log.task(start=day(8), parent="parent-orphan", items=2, turns=5)
        # ... and a session whose sub joined an hour before the only sweep.
        sweep_at = day(12)
        s10 = log.task(start=day(10), parent=None, items=2, turns=5)
        log.task(start=sweep_at - timedelta(hours=2, minutes=1), parent=s10, turns=5)
        # Not counted at all: a session whose latest join is after the window.
        s11 = log.task(start=day(11), parent=None, items=2, turns=5)
        log.task(start=UNTIL + timedelta(hours=1), parent=s11, turns=5)
        log.sweep(at=sweep_at)

        report = _analyze(log)

        s = report.descriptive.sessions
        assert (s.rolled_up, s.not_finished, s.without_main_join) == (9, 1, 1)
        assert (s.reaching_retrieval, s.eligible, s.analysed) == (8, 6, 4)
        assert s.reaching_retrieval_per_30d == pytest.approx(8 * 30 / DAYS)
        assert s.eligible_per_30d == pytest.approx(6 * 30 / DAYS)
        assert s.analysed_per_30d == pytest.approx(4 * 30 / DAYS)
        sd = statistics.stdev(math.log1p(turns) for turns in (60, 20, 40, 21))
        assert s.outcome_sd == pytest.approx(sd)
        horizons = s.mde_by_horizon
        assert [h.n for h in horizons] == pytest.approx([2, 4, 6])
        assert [h.mde for h in horizons[:2]] == [None, None]
        assert horizons[0].not_measurable == "N 2 is below 6, the formula's minimum"
        assert horizons[1].not_measurable == "N 4 is below 6, the formula's minimum"
        assert horizons[2].mde == pytest.approx(_exact_mde(sd, 6), rel=1e-3)
        assert horizons[2].ratio == pytest.approx(math.exp(_exact_mde(sd, 6)), rel=1e-3)
        assert any("no join for the main transcript" in n for n in report.notes)

    def test_a_session_counts_packs_from_any_build_and_rate(
        self, log: HoldoutLog
    ) -> None:
        """Three sessions, each with one served sub-agent task.

        S1's sub got its pack from a build that records no holdout and S2's
        at rate 0.25, so the task figures at rate 0.5 leave both tasks out;
        the session figures count all three sessions.
        """
        for day, (arm, rate) in enumerate([(None, 0.5), (False, 0.25), (False, 0.5)]):
            start = MONDAY + timedelta(days=day)
            main = log.task(start=start, parent=None, arms=(), turns=10 + day)
            log.task(
                start=start + timedelta(hours=2),
                parent=main,
                arms=[arm],
                rate=rate,
                turns=20 + day,
            )
        log.sweep()

        report = _analyze(log, rate=0.5)

        f = report.funnel
        assert (f.first_pack_old_build, f.other_rate, f.eligible) == (1, 1, 1)
        s = report.descriptive.sessions
        counts = (s.rolled_up, s.reaching_retrieval, s.eligible, s.analysed)
        assert counts == (3, 3, 3, 3)
        note = next(n for n in report.notes if "record no holdout" in n)
        assert "the main-session figures count them" in note


# ---------------------------------------------------------------------------
# Funnel notes (#704 gate follow-up 5)
# ---------------------------------------------------------------------------


class TestFunnelNotes:
    def test_a_task_with_no_sweep_for_its_source_system_is_named_so(
        self, log: HoldoutLog
    ) -> None:
        log.task(start=MONDAY)
        pack = log.pack(at=MONDAY + timedelta(hours=1), withheld=False, items=2)
        log.join(
            "task-other-source",
            at=MONDAY + timedelta(hours=2),
            pack_ids=[pack],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
            source_system="codex",
        )
        log.sweep()  # a claude-code sweep only, 4 h after the latest join

        report = _analyze(log, settle_hours=1.5)

        assert (report.funnel.unfinished, report.funnel.eligible) == (1, 1)
        note = next(n for n in report.notes if "not finished" in n)
        assert "for their source system" in note
        assert "at least 1.5 h after" in note
        assert "completed sweeps found for: claude-code" in note

    def test_eligible_tasks_with_an_unparsed_pack_id_are_counted(
        self, log: HoldoutLog
    ) -> None:
        for index, unparsed in enumerate([0, 1, 0, 3]):
            pack = log.pack(
                at=MONDAY + timedelta(hours=index), withheld=index % 2 == 1, items=2
            )
            log.join(
                f"task-{index}",
                at=MONDAY + timedelta(hours=index, minutes=30),
                pack_ids=[pack],
                parent="parent-a",
                outcome=outcome_payload(turns=5 + index),
                pack_ids_unparsed=unparsed,
            )
        # Neither a main session's nor a pack-less task's unparsed id counts.
        log.join(
            "main-0",
            at=MONDAY + timedelta(hours=5),
            pack_ids=[],
            parent=None,
            outcome=outcome_payload(turns=3),
            pack_ids_unparsed=2,
        )
        log.join(
            "task-no-pack",
            at=MONDAY + timedelta(hours=6),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=4),
            pack_ids_unparsed=1,
        )
        log.sweep()

        report = _analyze(log)

        assert report.funnel.eligible == 4
        assert report.funnel.eligible_with_unparsed_pack_ids == 2
        assert any("could not parse" in note for note in report.notes)
