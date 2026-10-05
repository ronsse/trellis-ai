"""Tests for the guardrails block and the task window of ``trellis analyze holdout``.

The pre-registration lists guardrails that are reported per arm and never
decide anything, and its per-30-day rates assume the window holds rows
at the analysed rate throughout. Every figure here is checked on a
synthetic store whose answer is worked out by hand in the test; every id
the fixture writes is synthetic.
"""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    KEYED_FROM,
    MONDAY,
    UNTIL,
    WINDOW_START,
    HoldoutLog,
    outcome_payload,
    seed_guardrails,
    seed_late_build,
)
from trellis.analyze import holdout
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Small resampling counts keep each analysis well under a second.
FAST = {"permutations": 999, "bootstraps": 400, "power_sims": 40}
ANALYSED = "analysed tasks"
ELIGIBLE = "eligible tasks, cut-offs included"


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _analyze(log: HoldoutLog, **kwargs: object) -> holdout.HoldoutReport:
    options: dict[str, object] = {"days": DAYS, "until": UNTIL, "seed": 7, **FAST}
    options.update(kwargs)
    return holdout.analyze_holdout(log.event_log, **options)  # type: ignore[arg-type]


def _measures(report: holdout.HoldoutReport) -> dict[str, Any]:
    return {measure.name: measure for measure in report.guardrails.measures}


def _keys(value: object) -> set[str]:
    """Every mapping key at any depth of a dumped model."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


# ---------------------------------------------------------------------------
# Guardrails by arm
# ---------------------------------------------------------------------------


class TestGuardrails:
    def test_each_guardrail_matches_a_hand_computation_per_arm(
        self, log: HoldoutLog
    ) -> None:
        """Analysed tasks are the four non-cut-offs per arm.

        Served PRs 1, 0, 2, 0; commits 0, 3, 1, 0; tool errors 2, 0, 4, 1;
        pack ids 1, 2, 1, 3. Withheld PRs 0, 1, 0, 0; commits 0, 2, 0, 0;
        tool errors 3, 5, 0, 1; pack ids 2, 2, 3, 1. The cut-off rate counts
        the cut-offs too: 1 of 5 served, 2 of 6 withheld.
        """
        seed_guardrails(log)

        report = _analyze(log)

        expected = {
            "prs_created": ((4, 0.75), (4, 0.25)),
            "any_commit": ((4, 0.5), (4, 0.25)),
            "log1p_commits": ((4, 3 * math.log(2) / 4), (4, math.log(3) / 4)),
            "cut_off_rate": ((5, 0.2), (6, 1 / 3)),
            "tool_errors": ((4, 1.75), (4, 2.25)),
            "recall_rate": ((4, 0.5), (4, 0.75)),
        }
        measures = _measures(report)
        assert list(measures) == list(expected)
        for name, ((n1, served), (n0, withheld)) in expected.items():
            measure = measures[name]
            assert measure.withheld is not None, name
            assert (measure.served.n, measure.withheld.n) == (n1, n0), name
            assert measure.served.value == pytest.approx(served), name
            assert measure.withheld.value == pytest.approx(withheld), name
            assert measure.served.not_measurable is None, name
            assert measure.withheld.not_measurable is None, name
        assert report.guardrails.status == "ok"
        assert measures["cut_off_rate"].task_set == ELIGIBLE
        assert {m.task_set for n, m in measures.items() if n != "cut_off_rate"} == {
            ANALYSED
        }
        # The block sits beside the primary, which still reads 8 tasks.
        assert report.funnel.analysed == 8

    def test_the_block_carries_no_p_value_and_no_verdict(self, log: HoldoutLog) -> None:
        seed_guardrails(log)

        report = _analyze(log)

        assert report.inference.p_value is not None
        keys = _keys(report.guardrails.model_dump())
        assert {"n", "value", "not_measurable"} <= keys
        assert not keys & {"p_value", "verdict", "ci_low", "ci_high", "difference"}
        assert "never decisive" in report.guardrails.note

    def test_itt_widens_the_analysed_guardrails_to_the_cut_offs(
        self, log: HoldoutLog
    ) -> None:
        """Kept cut-offs join the analysed set: PRs 8 / 5 served, 1 / 6 withheld."""
        seed_guardrails(log)

        measures = _measures(_analyze(log, itt=True))

        prs = measures["prs_created"]
        assert prs.withheld is not None
        assert (prs.served.n, prs.withheld.n) == (5, 6)
        assert prs.served.value == pytest.approx(1.6)
        assert prs.withheld.value == pytest.approx(1 / 6)
        cut_offs = measures["cut_off_rate"]
        assert cut_offs.withheld is not None
        assert cut_offs.served.value == pytest.approx(0.2)
        assert cut_offs.withheld.value == pytest.approx(1 / 3)

    @pytest.mark.parametrize("primary", ["prs_created", "any_commit"])
    def test_a_guardrail_that_is_the_primary_is_not_repeated(
        self, log: HoldoutLog, primary: str
    ) -> None:
        seed_guardrails(log)

        report = _analyze(log, outcome=primary)

        names = list(_measures(report))
        assert primary not in names
        assert len(names) == 5
        assert {"prs_created", "any_commit"} - {primary} <= set(names)
        assert f"{primary} is the primary outcome" in report.guardrails.note

    def test_the_re_call_rate_counts_tasks_that_parsed_a_second_pack(
        self, log: HoldoutLog
    ) -> None:
        """Served: 1 of 5 tasks parsed a second pack id; withheld: 3 of 4.

        The four served single-pack tasks each report three retrieval
        results, so a re-call read from ``retrieval_results`` would put
        every served task at 1.
        """
        for index in range(4):
            at = MONDAY + timedelta(hours=index)
            pack = log.pack(at=at, withheld=False, items=2)
            log.join(
                f"task-recall-{index}",
                at=at + timedelta(hours=1),
                pack_ids=[pack],
                parent="parent-a",
                outcome=outcome_payload(turns=8 + index),
                retrieval_results=3,
            )
        log.task(start=MONDAY + timedelta(hours=4), arms=[False, True], turns=13)
        for index, arms in enumerate(
            [(True, True), (True, False, True), (True, True), (True,)]
        ):
            log.task(
                start=MONDAY + timedelta(hours=5 + index), arms=arms, turns=14 + index
            )
        log.sweep()

        recall = _measures(_analyze(log))["recall_rate"]

        assert recall.withheld is not None
        assert (recall.served.n, recall.withheld.n) == (5, 4)
        assert recall.served.value == pytest.approx(0.2)
        assert recall.withheld.value == pytest.approx(0.75)

    def test_a_flag_off_store_shows_the_served_arm_only(self, log: HoldoutLog) -> None:
        """Rate 0: PRs 1, 0, 0 across three served tasks."""
        for index in range(3):
            log.task(
                start=MONDAY + timedelta(hours=index),
                rate=0.0,
                turns=6 + index,
                prs_created=1 if index == 0 else 0,
            )
        log.sweep()

        report = _analyze(log)

        guardrails = report.guardrails
        assert guardrails.status == holdout.NO_WITHHELD_ARM
        assert "served arm only" in guardrails.note
        assert len(guardrails.measures) == 6
        assert all(m.withheld is None for m in guardrails.measures)
        prs = _measures(report)["prs_created"]
        assert prs.served.n == 3
        assert prs.served.value == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# The task window
# ---------------------------------------------------------------------------


class TestTaskWindow:
    @pytest.mark.parametrize(
        ("before", "early"),
        [(None, (None, 0.5)), ((None, 0.5), (None, 0.5)), ((False, 0.0), (False, 0.0))],
        ids=["nothing-before", "old-build-before", "flag-off-before"],
    )
    def test_task_rates_divide_by_the_span_from_the_first_row_at_the_rate(
        self,
        log: HoldoutLog,
        before: tuple[bool | None, float] | None,
        early: tuple[bool | None, float],
    ) -> None:
        """Rows at rate 0.5 start 20 days into the 60-day window.

        10 eligible and 8 analysed tasks over 40 days are 7.5 and 6 per 30
        days, so N is 6 / 12 / 18. The two main sessions count packs from
        any build, so they keep the whole window: 1 per 30 days.
        """
        if before is not None:
            log.pack(
                at=WINDOW_START - timedelta(days=1),
                withheld=before[0],
                items=1,
                rate=before[1],
            )
        seed_late_build(log, early_arm=early[0], early_rate=early[1])

        report = _analyze(log, rate=0.5)

        d = report.descriptive
        assert (d.eligible_tasks, d.analysed_tasks) == (10, 8)
        assert d.eligible_per_30d == pytest.approx(7.5)
        assert d.analysed_per_30d == pytest.approx(6.0)
        assert [h.n for h in d.mde_by_horizon] == pytest.approx([6, 12, 18])
        s = d.sessions
        assert (s.rolled_up, s.eligible, s.analysed) == (2, 2, 2)
        assert s.analysed_per_30d == pytest.approx(1.0)
        assert [h.n for h in s.mde_by_horizon] == pytest.approx([1, 2, 3])
        assert "whole 60-day window" in s.note
        assert report.task_window_since == KEYED_FROM.isoformat()
        assert report.task_window_days == pytest.approx(40.0)
        assert report.window_days == DAYS
        span = [n for n in report.notes if "not by the 60-day window" in n]
        assert len(span) == 1
        assert "40 days from 2026-08-19T00:00:00+00:00" in span[0]

    def test_a_store_keyed_throughout_keeps_the_whole_window(
        self, log: HoldoutLog
    ) -> None:
        """Rate 0.5 was written before the window opened: 5 and 4 per 30 days."""
        log.deployed_before_window()
        seed_late_build(log, early_arm=False)

        report = _analyze(log, rate=0.5)

        d = report.descriptive
        assert (d.eligible_tasks, d.analysed_tasks) == (10, 8)
        assert d.eligible_per_30d == pytest.approx(5.0)
        assert d.analysed_per_30d == pytest.approx(4.0)
        assert [h.n for h in d.mde_by_horizon] == pytest.approx([4, 8, 12])
        assert d.sessions.analysed_per_30d == pytest.approx(1.0)
        assert not any("not by the 60-day window" in n for n in report.notes)
        assert report.task_window_since == report.since
        assert report.task_window_days == pytest.approx(60.0)
