"""The readout form of ``analyze_holdout``: by-build differences, weights, MDE, window.

Every id, commit and figure is synthetic; the arithmetic is checked exactly.
"""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.analyze._holdout_fixture import (
    BEHAVIOUR_FLAT,
    BEHAVIOUR_VARIANTS,
    CHANGED_TASKS,
    DAYS,
    MONDAY,
    REFUSAL_KEYS,
    UNTIL,
    HoldoutLog,
    covariates_payload,
    outcome_payload,
    provenance,
    seed_arm_shift,
    seed_random_outcomes,
)
from trellis.analyze import holdout
from trellis.analyze.holdout import (
    WINDOW_NOT_SETTLED,
    HoldoutRefusal,
    HoldoutReport,
    analytic_mde,
    analyze_holdout,
    analyze_holdout_operational,
    n_for_mde,
    simulate_power,
)
from trellis.ops.serving_builds import build_label
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

FAST: dict[str, Any] = {"permutations": 299, "bootstraps": 100, "power_sims": 20}
COMMITS = {"a": "aaaaaaaaaaaa", "b": "bbbbbbbbbbbb", "c": "cccccccccccc"}
BUILD = {key: build_label("0.0.0", commit) for key, commit in COMMITS.items()}


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _seed(
    log: HoldoutLog,
    layout: list[tuple[str, bool, int]],
    *,
    parent: str = "parent-a",
    rate: float = 0.5,
) -> None:
    """One task per ``(build, withheld, commits)``, an hour apart, outcome = commits."""
    for hours, (build, withheld, commits) in enumerate(layout):
        log.task(
            start=MONDAY + timedelta(hours=hours),
            parent=parent,
            arms=[withheld],
            commits=commits,
            rate=rate,
            pack_metadata=[provenance(COMMITS[build])],
        )


def _analyze(log: HoldoutLog, **kwargs: Any) -> Any:
    """A report, or a :class:`HoldoutRefusal` when the window has not settled."""
    options: dict[str, Any] = {
        "days": DAYS,
        "until": UNTIL,
        "seed": 7,
        "outcome": "commits",
        "by_commit": True,
        **FAST,
    }
    options.update(kwargs)
    return analyze_holdout(log.event_log, **options)


def _rows(report: HoldoutReport) -> dict[str, Any]:
    assert report.by_commit is not None
    return {row.build: row for row in report.by_commit.rows}


# ---------------------------------------------------------------------------
# By-build differences and pooling
# ---------------------------------------------------------------------------


def _three_builds(log: HoldoutLog) -> None:
    """A: 5 withheld (stands). B: exactly 4 withheld, C: 1 withheld (both pooled)."""
    _seed(
        log,
        [("a", False, 2), ("a", False, 4)]
        + [("a", True, 1)] * 5
        + [("b", False, 10)]
        + [("b", True, 4)] * 4
        + [("c", False, 0), ("c", True, 2)],
    )
    log.sweep()


def test_a_standing_build_reports_served_minus_withheld_exactly(
    log: HoldoutLog,
) -> None:
    _three_builds(log)

    report = _analyze(log)

    row = _rows(report)[BUILD["a"]]
    assert (row.served.n, row.served.outcome_mean) == (2, 3.0)
    assert (row.withheld.n, row.withheld.outcome_mean) == (5, 1.0)
    assert row.difference == 2.0


def test_exactly_five_withheld_stands_and_exactly_four_pools(log: HoldoutLog) -> None:
    _three_builds(log)

    table = _analyze(log).by_commit

    assert table is not None
    assert [row.build for row in table.rows] == [BUILD["a"], holdout.OTHER_BUILD]
    assert table.pooled_builds == 2
    assert table.pool_threshold == 5


def test_the_pooled_row_is_computed_over_the_pooled_tasks(log: HoldoutLog) -> None:
    _three_builds(log)

    other = _analyze(log).by_commit.rows[-1]  # type: ignore[union-attr]

    assert other.build == holdout.OTHER_BUILD
    assert (other.served.n, other.served.outcome_mean) == (2, 5.0)
    assert (other.withheld.n, other.withheld.outcome_mean) == (5, 3.6)
    assert other.difference == pytest.approx(5.0 - 3.6)


def test_one_build_below_the_threshold_is_all_pooled(log: HoldoutLog) -> None:
    _seed(log, [("b", False, 7)] + [("b", True, 3)] * 4)
    log.sweep()

    table = _analyze(log).by_commit

    assert table is not None
    assert [row.build for row in table.rows] == [holdout.OTHER_BUILD]
    assert table.rows[0].difference == 4.0
    assert table.pooled_builds == 1


def test_an_empty_arm_gives_a_null_difference_never_zero(log: HoldoutLog) -> None:
    _seed(log, [("a", True, 3)] * 5 + [("b", False, 9)] * 2)
    log.sweep()

    rows = _analyze(log).by_commit.rows  # type: ignore[union-attr]

    assert [row.difference for row in rows] == [None, None]
    assert rows[0].served.outcome_mean is None


def test_the_table_is_labelled_descriptive_and_non_decisive(log: HoldoutLog) -> None:
    _three_builds(log)

    dumped = _analyze(log).model_dump(mode="json")["by_commit"]

    assert dumped["verdict"] == "descriptive, not decisive"
    assert "non-decisive" in dumped["note"]
    assert not {"p_value", "ci_low", "ci_high"} & set(dumped["rows"][0])


def test_operational_output_carries_no_outcome_or_difference(log: HoldoutLog) -> None:
    _three_builds(log)

    dumped = analyze_holdout_operational(
        log.event_log, days=DAYS, until=UNTIL, by_commit=True
    ).model_dump(mode="json")

    def keys(node: Any) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {k for v in node.values() for k in keys(v)}
        if isinstance(node, list):
            return {k for v in node for k in keys(v)}
        return set()

    found = keys(dumped["by_commit"])
    assert found, "the operational table must exist for this to mean anything"
    assert not {k for k in found if "difference" in k or "mean" in k or "outcome" in k}
    assert "settled_window" not in dumped


# ---------------------------------------------------------------------------
# Agent-type secondary
# ---------------------------------------------------------------------------


def test_both_secondaries_are_preregistered_and_report_used_and_dropped(
    log: HoldoutLog,
) -> None:
    for i in range(12):
        covariates = (
            covariates_payload(
                brief_chars=400 + 25 * i, agent_type="general" if i % 2 else "plan"
            )
            if i < 10
            else None
        )
        log.task(
            start=MONDAY + timedelta(hours=i),
            arms=[i % 3 == 0],
            commits=i,
            covariates=covariates,
        )
    log.sweep()

    block = _analyze(log).covariate_adjusted

    assert [(a.name, a.preregistered) for a in block.adjustments] == [
        ("brief_length", True),
        ("brief_length_and_agent_type", True),
    ]
    assert [(a.n_used, a.n_missing_covariate) for a in block.adjustments] == [
        (10, 2),
        (10, 2),
    ]


# ---------------------------------------------------------------------------
# Top-stratum weight share
# ---------------------------------------------------------------------------


def test_the_top_stratum_share_is_of_weight_not_of_tasks(log: HoldoutLog) -> None:
    # parent-a: 2 served, 2 withheld -> weight 2*2/4 = 1.
    # parent-b: 1 served, 3 withheld -> weight 1*3/4 = 0.75.
    # parent-c: 2 served, 0 withheld -> weight 0, though it holds 2 tasks.
    _seed(log, [("a", False, 1), ("a", False, 2), ("a", True, 3), ("a", True, 4)])
    _seed_parent(log, "parent-b", [False, True, True, True], offset=10)
    _seed_parent(log, "parent-c", [False, False], offset=20)
    log.sweep()

    inference = _analyze(log).inference

    assert inference.status == "ok"
    assert inference.top_stratum_weight_share == pytest.approx(1.0 / 1.75)
    # The heaviest stratum by tasks is also parent-a (4 of 10): a task share
    # would read 0.4, which this must not.
    assert inference.top_stratum_weight_share != pytest.approx(0.4)


def _seed_parent(
    log: HoldoutLog, parent: str, arms: list[bool], *, offset: int
) -> None:
    for i, withheld in enumerate(arms):
        log.task(
            start=MONDAY + timedelta(hours=offset + i),
            parent=parent,
            arms=[withheld],
            commits=i,
            pack_metadata=[provenance(COMMITS["a"])],
        )


def test_the_top_stratum_share_is_none_when_inference_does_not_run(
    log: HoldoutLog,
) -> None:
    _seed(log, [("a", False, 1), ("a", False, 2)])
    log.sweep()

    inference = _analyze(log).inference

    assert inference.status != "ok"
    assert inference.top_stratum_weight_share is None


# ---------------------------------------------------------------------------
# analytic_mde and the assignment rate
# ---------------------------------------------------------------------------


def test_the_mde_at_an_even_split_is_the_prestudy_formula() -> None:
    sd, n = 0.63, 114
    multiplier = holdout._t_quantile(0.975, n - 2) + holdout._t_quantile(0.8, n - 2)

    assert analytic_mde(sd, n) == pytest.approx(multiplier * sd * math.sqrt(4 / n))
    assert analytic_mde(sd, n, 0.5) == analytic_mde(sd, n)


def test_the_mde_scales_with_the_withheld_share() -> None:
    even = analytic_mde(1.0, 200, 0.5)
    uneven = analytic_mde(1.0, 200, 0.2)

    assert even is not None
    assert uneven == pytest.approx(even * 0.5 / math.sqrt(0.2 * 0.8))
    assert analytic_mde(1.0, 200, 0.8) == pytest.approx(uneven)


@pytest.mark.parametrize("share", [0.0, 1.0, -0.1, 1.5])
def test_a_share_outside_the_open_unit_interval_is_refused(share: float) -> None:
    with pytest.raises(ValueError, match="share"):
        analytic_mde(1.0, 100, share)


def test_the_n_for_an_effect_grows_as_the_split_leaves_even() -> None:
    assert n_for_mde(1.0, 0.3, 0.2) > n_for_mde(1.0, 0.3, 0.5)
    ratio = n_for_mde(1.0, 0.3, 0.2) / n_for_mde(1.0, 0.3, 0.5)
    assert ratio == pytest.approx(0.25 / (0.2 * 0.8), rel=0.05)


@pytest.mark.parametrize("share", [0.5, 0.2])
def test_the_analytic_mde_reaches_the_target_power_in_simulation(
    share: float,
) -> None:
    """At the MDE the simulated test should reject about 80% of the time.

    The pool is a standardised normal sample (sd exactly 1) so the analytic and
    simulated variances agree. The tolerance, 0.72 to 0.88, is a few simulation
    standard errors (about 0.01 at 1500 sims) plus the permutation test's
    slight conservatism at 199 permutations. Dropping the rate term moves the
    share-0.2 figure to about 0.55.
    """
    n = 120
    quantiles = np.linspace(0.5 / n, 1 - 0.5 / n, n)
    pool = np.array([holdout._t_quantile(q, 10_000) for q in quantiles], dtype=float)
    pool = (pool - pool.mean()) / pool.std(ddof=1)
    mde = analytic_mde(1.0, n, share)
    assert mde is not None

    power = simulate_power(
        pool,
        np.zeros(n, dtype=int),
        np.random.default_rng(11),
        [mde],
        sims=1500,
        permutations=199,
        withheld_share=share,
    )[0]

    assert 0.72 <= power <= 0.88


def test_the_descriptive_block_sizes_at_the_analysed_rate(log: HoldoutLog) -> None:
    layout = [("a", i % 5 == 0, i) for i in range(30)]
    _seed(log, layout, rate=0.2)
    log.sweep()

    report = _analyze(log, rate=0.2)

    d = report.descriptive
    assert d.mde_withheld_share == 0.2
    sd = d.outcome_sd_within_parent
    assert sd is not None
    expected = [analytic_mde(sd, h.n, 0.2) for h in d.mde_by_horizon if h.n]
    assert [h.mde for h in d.mde_by_horizon if h.n] == expected
    assert d.n_for_10pct_effect is None or d.n_for_10pct_effect > 0


def test_the_n_for_ten_percent_is_sized_at_the_analysed_share(
    log: HoldoutLog,
) -> None:
    _seed(log, [("a", i % 5 == 0, i) for i in range(30)], rate=0.2)
    log.sweep()

    d = _analyze(log, rate=0.2, outcome="log1p_commits").descriptive

    sd = d.outcome_sd_within_parent
    assert sd is not None
    effect = -math.log(0.9)
    assert d.n_for_10pct_effect == n_for_mde(sd, effect, 0.2)
    # The even-split default would size a different N, so a sizing that
    # ignored the analysed share cannot pass.
    assert d.n_for_10pct_effect != n_for_mde(sd, effect)


# ---------------------------------------------------------------------------
# Settled window
# ---------------------------------------------------------------------------


def _one_task_log(log: HoldoutLog) -> None:
    _seed(log, [("a", False, 1), ("a", True, 2), ("a", False, 3), ("a", True, 4)])


def test_a_read_before_any_sweep_past_until_is_a_refusal(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=MONDAY + timedelta(days=1))

    refusal = _analyze(log)

    assert isinstance(refusal, HoldoutRefusal)
    window = refusal.settled_window
    assert window.checked
    assert window.settled is False
    assert window.needs_sweep_after == UNTIL.isoformat()
    assert refusal.status == WINDOW_NOT_SETTLED
    assert any("--operational" in note for note in refusal.notes)
    # An allow-list: the keys are absent, not null, so nothing a report adds
    # reaches a refused read, and nothing under the window is computed.
    assert set(refusal.model_dump()) == REFUSAL_KEYS
    for absent in ("funnel", "descriptive", "by_commit", "inference", "guardrails"):
        assert not hasattr(refusal, absent)


def test_a_refusal_states_what_the_caller_asked(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=MONDAY + timedelta(days=1))

    refusal = _analyze(log, rate=0.5, outcome="prs_created", itt=True)

    assert isinstance(refusal, HoldoutRefusal)
    assert (refusal.outcome, refusal.rate, refusal.itt) == ("prs_created", 0.5, True)
    assert (refusal.window_days, refusal.until) == (DAYS, UNTIL.isoformat())
    assert refusal.since == (UNTIL - timedelta(days=DAYS)).isoformat()
    assert refusal.planned_mde > 0
    assert refusal.planned_n > 0


def test_an_unsettled_read_is_refused_and_a_settled_one_adjusts(
    log: HoldoutLog,
) -> None:
    for i in range(12):
        log.task(
            start=MONDAY + timedelta(hours=i),
            arms=[i % 3 == 0],
            commits=i,
            covariates=covariates_payload(
                brief_chars=400 + 25 * i, agent_type="general" if i % 2 else "plan"
            ),
        )
    log.sweep(at=MONDAY + timedelta(days=1))

    assert isinstance(_analyze(log), HoldoutRefusal)

    log.sweep(at=UNTIL + timedelta(hours=1))
    log.sweep(at=UNTIL + timedelta(hours=4))
    block = _analyze(log).covariate_adjusted
    assert [a.name for a in block.adjustments] == [
        "brief_length",
        "brief_length_and_agent_type",
    ]
    settled = block.adjustments[0]
    assert settled.inference is not None
    assert settled.inference.status == "ok"
    assert settled.inference.difference is not None


def test_one_sweep_after_until_is_not_enough_it_must_settle(log: HoldoutLog) -> None:
    _one_task_log(log)
    first = UNTIL + timedelta(hours=1)
    log.sweep(at=first)

    window = _analyze(log).settled_window

    assert window.settled is False
    assert window.needs_sweep_after == (first + timedelta(hours=3)).isoformat()
    assert window.reason is not None


def test_a_sweep_past_the_settle_allowance_completes_the_read(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=UNTIL + timedelta(hours=1))
    log.sweep(at=UNTIL + timedelta(hours=4))

    report = _analyze(log)

    assert report.settled_window.settled is True
    assert report.settled_window.needs_sweep_after is None
    assert report.inference.status == "ok"


def test_the_settle_allowance_is_the_one_asked_for(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=UNTIL + timedelta(hours=1))
    log.sweep(at=UNTIL + timedelta(hours=4))

    report = _analyze(log, settle_hours=6.0)

    assert report.settled_window.settled is False
    assert (
        report.settled_window.needs_sweep_after
        == (UNTIL + timedelta(hours=7)).isoformat()
    )


def test_without_until_the_window_is_not_checked(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=MONDAY + timedelta(days=1))

    report = analyze_holdout(log.event_log, days=400, outcome="commits", seed=7, **FAST)

    assert report.settled_window.checked is False
    assert report.settled_window.settled is None
    assert report.inference.status != WINDOW_NOT_SETTLED


def test_a_dry_run_sweep_does_not_settle_the_window(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=UNTIL + timedelta(hours=1))
    log.sweep(at=UNTIL + timedelta(hours=9), dry_run=True)

    assert _analyze(log).settled_window.settled is False


def _event(kind: EventType, at: Any, source_system: str) -> Event:
    return Event(
        event_type=kind,
        source="worker:session-capture",
        entity_id=f"capture:{source_system}",
        entity_type="capture_sweep",
        occurred_at=at,
        recorded_at=at,
        payload={"dry_run": False, "source_system": source_system},
    )


def test_every_source_system_must_settle() -> None:
    settle = timedelta(hours=3)
    sweeps = [
        _event(EventType.CAPTURE_SWEEP_COMPLETED, UNTIL + timedelta(hours=h), name)
        for name, hours in (("src-a", (1, 25)), ("src-b", (1,)))
        for h in hours
    ]
    joins = [
        _event(EventType.CAPTURE_SESSION_PACKS, MONDAY, name)
        for name in ("src-a", "src-b")
    ]

    window = holdout._settled_window(
        sweeps,
        holdout._SettleScope(joins=joins),
        end=UNTIL,
        settle=settle,
        checked=True,
    )

    assert window.settled is False
    assert window.needs_sweep_after == (UNTIL + timedelta(hours=4)).isoformat()
    assert window.reason is not None
    assert "src-b" in window.reason
    assert "src-a" not in window.reason


# ---------------------------------------------------------------------------
# Unfinished tasks block the window; the source set is the analysed scope
# ---------------------------------------------------------------------------

SETTLE = timedelta(hours=3)


def _restamp(
    log: HoldoutLog, count: int, at: Any, *, task_id: str | None = None
) -> None:
    """Rewrite ``count`` joins, or the one of ``task_id``, at ``at``.

    A join-only re-parse, never backdated.
    """
    joins = log.event_log.get_events(
        event_type=EventType.CAPTURE_SESSION_PACKS, limit=1000
    )
    chosen = (
        joins[:count]
        if task_id is None
        else [join for join in joins if join.entity_id == task_id]
    )
    for join in chosen:
        log.event_log.append(
            Event(
                event_type=EventType.CAPTURE_SESSION_PACKS,
                source="worker:session-capture",
                entity_id=join.entity_id,
                entity_type=join.entity_type,
                occurred_at=at,
                payload=join.payload,
                metadata=join.metadata,
            )
        )


def test_a_join_stamped_after_the_last_sweep_blocks_the_window(
    log: HoldoutLog,
) -> None:
    _one_task_log(log)
    log.sweep()
    assert _analyze(log).settled_window.settled is True
    stamped = UNTIL + timedelta(hours=26)
    _restamp(log, 2, stamped)

    report = _analyze(log)

    window = report.settled_window
    assert window.settled is False
    assert window.needs_sweep_after == (stamped + SETTLE).isoformat()
    assert window.reason is not None
    assert "not finished" in window.reason
    assert isinstance(report, HoldoutRefusal)

    # A sweep that ran the settle allowance after the restamp finishes them.
    log.sweep(at=stamped + SETTLE)
    settled = _analyze(log)
    assert settled.settled_window.settled is True
    assert settled.settled_window.reason is None
    assert settled.funnel.unfinished == 0
    assert settled.inference.status == "ok"


def test_a_long_task_the_second_sweep_rewrote_blocks_the_window(
    log: HoldoutLog,
) -> None:
    _one_task_log(log)
    log.task(
        start=UNTIL - timedelta(hours=2),
        arms=[True],
        commits=5,
        rate=0.5,
        pack_metadata=[provenance(COMMITS["a"])],
    )
    log.sweep()
    last_sweep = UNTIL + timedelta(hours=25)
    # The task was still running at the first sweep, so the second rewrote it.
    _restamp(log, 5, last_sweep)

    window = _analyze(log).settled_window

    assert window.settled is False
    assert window.reason is not None
    assert "not finished" in window.reason
    assert window.needs_sweep_after == (last_sweep + SETTLE).isoformat()


def test_a_join_outside_the_window_holds_nothing_open(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep()
    assert _analyze(log).settled_window.settled is True
    # A task from a month before the window, its join rewritten after the last
    # sweep: were it in scope it would hold the window open.
    old = log.task(
        start=UNTIL - timedelta(days=DAYS + 30),
        arms=[False],
        rate=0.5,
        pack_metadata=[provenance(COMMITS["a"])],
    )
    _restamp(log, 0, UNTIL + timedelta(hours=40), task_id=old)

    assert _analyze(log).settled_window.settled is True


def _retired_source_join(log: HoldoutLog) -> None:
    """A main session another capture source wrote: no pack, nothing analysed."""
    log.join(
        "retired-main",
        at=MONDAY,
        pack_ids=[],
        parent=None,
        outcome=None,
        source_system="retired-src",
    )


def test_a_retired_source_with_nothing_analysed_does_not_block(
    log: HoldoutLog,
) -> None:
    _one_task_log(log)
    log.sweep()
    _retired_source_join(log)

    window = _analyze(log).settled_window

    assert window.settled is True
    assert window.reason is None


def test_a_stopped_source_with_an_analysed_task_blocks_and_is_named(
    log: HoldoutLog,
) -> None:
    _one_task_log(log)
    log.sweep()
    _retired_source_join(log)
    pack = log.pack(at=MONDAY, withheld=True, items=3, rate=0.5)
    log.join(
        "stopped-task",
        at=MONDAY + timedelta(hours=2),
        pack_ids=[pack],
        parent="parent-a",
        outcome=outcome_payload(turns=5, commits=1),
        source_system="stopped-src",
    )

    window = _analyze(log).settled_window

    assert window.settled is False
    assert window.reason is not None
    assert "stopped-src" in window.reason
    assert "retired-src" not in window.reason


def test_a_store_with_no_joins_still_waits_for_the_default_source() -> None:
    window = holdout._settled_window(
        [], holdout._SettleScope(), end=UNTIL, settle=SETTLE, checked=True
    )

    assert window.settled is False
    assert window.needs_sweep_after == UNTIL.isoformat()
    assert window.reason is not None
    assert holdout._SOURCE_SYSTEM_DEFAULT in window.reason


def test_an_empty_store_read_with_until_is_not_settled(log: HoldoutLog) -> None:
    window = _analyze(log).settled_window

    assert window.checked is True
    assert window.settled is False
    assert window.reason is not None
    assert holdout._SOURCE_SYSTEM_DEFAULT in window.reason


# ---------------------------------------------------------------------------
# What counts as a sweep, and where the settle clock starts
# ---------------------------------------------------------------------------


def test_a_sweep_that_parsed_nothing_and_lost_sessions_settles_nothing(
    log: HoldoutLog,
) -> None:
    _one_task_log(log)
    log.sweep(sessions_parsed=0, sessions_errored=7)

    window = _analyze(log).settled_window

    assert window.settled is False
    assert window.latest_sweep is None
    assert window.reason is not None
    assert "no completed claude-code sweep" in window.reason


def test_a_sweep_that_parsed_nothing_and_lost_nothing_counts(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(sessions_parsed=0, sessions_errored=0)

    assert _analyze(log).settled_window.settled is True


def test_a_sweep_that_parsed_some_and_lost_some_counts(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(sessions_parsed=3, sessions_errored=2)

    assert _analyze(log).settled_window.settled is True


def test_an_errored_sweep_does_not_finish_a_join(log: HoldoutLog) -> None:
    """``_finished`` reads the same sweeps the window does, so they agree."""
    _one_task_log(log)
    log.sweep()
    stamped = UNTIL + timedelta(hours=26)
    _restamp(log, 1, stamped)
    log.sweep(at=stamped + SETTLE, sessions_parsed=0, sessions_errored=4)

    report = _analyze(log)

    assert report.settled_window.settled is False
    assert "not finished" in report.settled_window.reason


def test_the_settle_clock_starts_at_the_first_sweep_not_the_window_end(
    log: HoldoutLog,
) -> None:
    _one_task_log(log)
    # A task whose join lands just before the first sweep past the end.
    log.task(
        start=UNTIL - timedelta(hours=2),
        arms=[True],
        commits=5,
        rate=0.5,
        pack_metadata=[provenance(COMMITS["a"])],
    )
    first = UNTIL + timedelta(hours=10)
    log.sweep(at=first)
    log.sweep(at=first + timedelta(hours=2))

    window = _analyze(log).settled_window

    # Both sweeps are past end + 3 h, yet none ran 3 h after the first.
    assert window.settled is False
    assert window.needs_sweep_after == (first + SETTLE).isoformat()

    log.sweep(at=first + SETTLE)
    assert _analyze(log).settled_window.settled is True


def test_a_sweep_exactly_at_the_window_end_is_the_first(log: HoldoutLog) -> None:
    _one_task_log(log)
    log.sweep(at=UNTIL)
    log.sweep(at=UNTIL + SETTLE)

    assert _analyze(log).settled_window.settled is True


# ---------------------------------------------------------------------------
# One commit under two version strings is one build
# ---------------------------------------------------------------------------


def test_one_commit_under_two_versions_is_one_row_that_stands(
    log: HoldoutLog,
) -> None:
    layout = [("0.1.0", False, 4), ("0.2.0", False, 4)] * 3
    layout += [("0.1.0", True, 1), ("0.2.0", True, 1)] * 3
    for hours, (version, withheld, commits) in enumerate(layout):
        stamp = provenance(COMMITS["a"])
        stamp["write_provenance"]["version"] = version
        log.task(
            start=MONDAY + timedelta(hours=hours),
            arms=[withheld],
            commits=commits,
            rate=0.5,
            pack_metadata=[stamp],
        )
    log.sweep()

    table = _analyze(log).by_commit

    assert table is not None
    # Three withheld under each version is below the pooling threshold; as one
    # build there are six, so it stands rather than being pooled twice over.
    assert table.pooled_builds == 0
    assert [row.build for row in table.rows] == [f"0.1.0, 0.2.0 @ {COMMITS['a']}"]
    row = table.rows[0]
    assert (row.served.n, row.withheld.n) == (6, 6)
    assert row.difference == 3.0


# ---------------------------------------------------------------------------
# An unsettled checked read is a refusal and the store cannot move it
# ---------------------------------------------------------------------------

#: The wall-clock stamps a read writes for itself: not a figure.
_CLOCK_KEYS = ("generated_at", "as_of")


def _without_clock(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_clock(v) for k, v in node.items() if k not in _CLOCK_KEYS}
    if isinstance(node, list):
        return [_without_clock(v) for v in node]
    return node


def _differing_paths(left: Any, right: Any, path: str = "") -> list[str]:
    """Every path at which two dumped reads differ, so a failure names them."""
    if isinstance(left, dict) and isinstance(right, dict):
        keys = sorted(set(left) | set(right))
        return [
            found
            for key in keys
            for found in _differing_paths(
                left.get(key), right.get(key), f"{path}.{key}" if path else key
            )
        ]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [
            found
            for index, (a, b) in enumerate(zip(left, right, strict=True))
            for found in _differing_paths(a, b, f"{path}[{index}]")
        ]
    return [] if left == right else [path]


def _behaviour_read(
    tmp_path: Path, name: str, seed: dict[str, Any], *, settled: bool, **kw: Any
) -> Any:
    """One synthetic store, read once. Sweeps are written at fixed times, never
    relative to a join, so two stores differ only in what ``seed`` varies."""
    event_log = SQLiteEventLog(tmp_path / f"{name}.db")
    log = HoldoutLog(event_log)
    seed_arm_shift(log, **seed)
    if settled:
        log.sweep()
    else:
        log.sweep(at=UNTIL - timedelta(hours=1))
    result = _analyze(log, rate=0.2, **kw)
    event_log.close()
    assert result.settled_window.settled is settled
    return _without_clock(result.model_dump(mode="json"))


@pytest.mark.parametrize("variant", sorted(BEHAVIOUR_VARIANTS))
@pytest.mark.parametrize(
    ("outcome", "itt"), [("log1p_turns", False), ("commits", True)]
)
def test_an_unsettled_read_is_the_same_whatever_the_store_holds(
    tmp_path: Path, variant: str, outcome: str, itt: bool
) -> None:
    """Two stores that differ in what the sessions did read the same, whole.

    The pairs vary the withheld arm's values, cut-offs and missing outcomes, how
    many pack calls its tasks made, when their joins were written, the tool
    calls and hook messages the timing counts, and whether the join has an
    outcome block. The whole payload is compared: a list of keys would only
    name the figures someone remembered, and three such lists each missed one.
    """
    flat = _behaviour_read(
        tmp_path, "flat", BEHAVIOUR_FLAT, settled=False, outcome=outcome, itt=itt
    )
    changed = _behaviour_read(
        tmp_path,
        "changed",
        BEHAVIOUR_VARIANTS[variant],
        settled=False,
        outcome=outcome,
        itt=itt,
    )

    assert set(flat) == REFUSAL_KEYS
    moved = _differing_paths(flat, changed)
    assert not moved, "an unsettled read moved at:\n" + "\n".join(moved)


#: A settled read of a store whose joins were written three hours later has
#: every join finished, so it reads the same; the residual test below is where
#: that variant moves a read.
@pytest.mark.parametrize("variant", sorted(set(BEHAVIOUR_VARIANTS) - {"join_time"}))
def test_the_same_pairs_do_differ_when_the_window_is_settled(
    tmp_path: Path, variant: str
) -> None:
    """The guard is not vacuous: settled, the same pairs read differently."""
    flat = _behaviour_read(tmp_path, "flat", BEHAVIOUR_FLAT, settled=True)
    changed = _behaviour_read(
        tmp_path, "changed", BEHAVIOUR_VARIANTS[variant], settled=True
    )

    assert "funnel" in flat
    assert flat != changed
    if variant == "cut_offs":
        assert flat["funnel"]["cut_offs_excluded"] == 0
        assert changed["funnel"]["cut_offs_excluded"] == CHANGED_TASKS
    if variant == "missing":
        assert flat["funnel"]["outcome_missing"] == 0
        assert changed["funnel"]["outcome_missing"] == CHANGED_TASKS
    if variant == "no_outcome_block":
        assert changed["funnel"]["no_outcome"] > flat["funnel"]["no_outcome"]
    if variant == "pack_calls":
        assert changed["descriptive"]["tasks_with_several_packs"] > 0


def _unfinished_read(tmp_path: Path, name: str, restamped: int) -> Any:
    event_log = SQLiteEventLog(tmp_path / f"{name}.db")
    log = HoldoutLog(event_log)
    seed_arm_shift(log)
    log.sweep(at=UNTIL - timedelta(hours=1))
    # Joins rewritten after the last completed sweep: a re-parse.
    _restamp(log, restamped, UNTIL - timedelta(minutes=30))
    result = _analyze(log, rate=0.2)
    event_log.close()
    assert isinstance(result, HoldoutRefusal)
    return _without_clock(result.model_dump(mode="json"))


def test_the_one_residual_is_whether_a_join_was_written_after_the_last_sweep(
    tmp_path: Path,
) -> None:
    """``needs_sweep_after`` and the reason's unfinished clause are all that move.

    An in-scope join written after its source's last completed sweep holds the
    window open, and what the operator needs is the sweep that would finish it,
    so ``needs_sweep_after`` is a timestamp read off that join. It is the one
    value a session's behaviour can move; the reason gains one constant clause
    and no count.
    """
    none = _unfinished_read(tmp_path, "none", 0)
    one = _unfinished_read(tmp_path, "one", 1)
    many = _unfinished_read(tmp_path, "many", 40)

    assert sorted(_differing_paths(none, one)) == [
        "settled_window.needs_sweep_after",
        "settled_window.reason",
    ]
    assert "not finished" not in none["settled_window"]["reason"]
    assert "not finished" in one["settled_window"]["reason"]
    # How many joins were rewritten is not in the read at all.
    assert one == many


def _random_read(
    tmp_path: Path, *, structure: int, outcomes: int, settled: bool, **kw: Any
) -> Any:
    event_log = SQLiteEventLog(tmp_path / f"{structure}-{outcomes}-{settled}.db")
    log = HoldoutLog(event_log)
    seed_random_outcomes(log, structure=structure, outcomes=outcomes)
    if settled:
        log.sweep()
    else:
        log.sweep(at=UNTIL - timedelta(hours=1))
    report = _analyze(log, rate=0.3, **kw)
    assert report.settled_window.settled is settled
    event_log.close()
    return _without_clock(report.model_dump(mode="json"))


@pytest.mark.parametrize("structure", range(6))
@pytest.mark.parametrize(
    ("outcome", "itt"), [("log1p_turns", False), ("commits", True)]
)
def test_an_unsettled_read_ignores_outcomes_drawn_at_random(
    tmp_path: Path, structure: int, outcome: str, itt: bool
) -> None:
    """Two stores with one layout and independently drawn outcomes read the same."""
    reads = [
        _random_read(
            tmp_path,
            structure=structure,
            outcomes=outcomes,
            settled=False,
            outcome=outcome,
            itt=itt,
        )
        for outcomes in (101, 202)
    ]
    moved = _differing_paths(reads[0], reads[1])
    assert not moved, "an unsettled read moved at:\n" + "\n".join(moved)


@pytest.mark.parametrize("structure", range(6))
def test_randomly_drawn_outcomes_do_move_a_settled_read(
    tmp_path: Path, structure: int
) -> None:
    """The random differential is not vacuous: settled, the same pair differs."""
    reads = [
        _random_read(tmp_path, structure=structure, outcomes=outcomes, settled=True)
        for outcomes in (101, 202)
    ]
    moved = set(_differing_paths(reads[0], reads[1]))
    assert {
        "funnel.analysed",
        "funnel.cut_offs_excluded",
        "descriptive.analysed_tasks",
        "inference.difference",
    } <= moved
