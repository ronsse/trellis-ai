"""Tests for the per-build table of ``trellis analyze holdout --by-commit``.

The table is descriptive: how the analysed tasks split across the builds
that wrote their packs, so a reader can see whether a deploy mixed builds
inside an arm. A build is the ``(version, commit)`` stamped on the pack that
sets the task's arm, named as ``analyze health`` names a serving build. Every
figure here is worked out by hand on a synthetic store; every id and commit
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
    MONDAY,
    UNTIL,
    HoldoutLog,
    outcome_payload,
    provenance,
    timing_payload,
)
from trellis.analyze import holdout
from trellis.ops.serving_builds import (
    NO_PROVENANCE_LABEL,
    build_label,
    summarize_serving_builds,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Small resampling counts keep each analysis well under a second.
FAST = {"permutations": 999, "bootstraps": 400, "power_sims": 40}
#: A full 40-character commit, and a short one. A build is named by the
#: whole pair the fixture stamps (version ``0.0.0``), never a prefix of it.
COMMIT_A = "a1b2c3d4e5f60123456789abcdef0123456789ab"
BUILD_A = build_label("0.0.0", COMMIT_A)
COMMIT_B = "9f8e7d6c5b4"
BUILD_B = build_label("0.0.0", COMMIT_B)


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _analyze(
    log: HoldoutLog, *, by_commit: bool = True, **kwargs: object
) -> holdout.HoldoutReport:
    options: dict[str, object] = {
        "days": DAYS,
        "until": UNTIL,
        "seed": 7,
        "outcome": "commits",
        "by_commit": by_commit,
        **FAST,
    }
    options.update(kwargs)
    return holdout.analyze_holdout(log.event_log, **options)  # type: ignore[arg-type]


def _rows(report: holdout.HoldoutReport) -> dict[str, holdout.HoldoutBuildRow]:
    assert report.by_commit is not None
    return {row.build: row for row in report.by_commit.rows}


def _stamped(version: str | None, commit: str | None) -> dict[str, Any]:
    """An event's ``metadata`` stamped by ``version`` and ``commit``."""
    return {"write_provenance": {"version": version, "commit": commit}}


def _health_labels(log: HoldoutLog) -> set[str]:
    """The labels ``analyze health`` gives the pack rows the fixture wrote."""
    packs = log.event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1000)
    return {group.label for group in summarize_serving_builds(packs).groups}


def _task(
    log: HoldoutLog, hours: int, commit: str | None, *, withheld: bool, **kwargs: Any
) -> None:
    """One single-pack task ``hours`` after the first Monday, stamped ``commit``."""
    log.task(
        start=MONDAY + timedelta(hours=hours),
        arms=[withheld],
        pack_metadata=[provenance(commit)],
        **kwargs,
    )


def _seed_two_builds(log: HoldoutLog) -> None:
    """Build B's tasks are written first but are later in time than build A's.

    Build A (hours 0-2): served 1 and 4 commits, withheld 2.
    Build B (hours 30-32): served 0, withheld 5 and 7.
    """
    for hours, withheld, commits in [(30, False, 0), (31, True, 5), (32, True, 7)]:
        _task(log, hours, COMMIT_B, withheld=withheld, commits=commits)
    for hours, withheld, commits in [(0, False, 1), (1, True, 2), (2, False, 4)]:
        _task(log, hours, COMMIT_A, withheld=withheld, commits=commits)
    log.sweep()


def test_each_build_reports_its_arms_counts_and_means_in_first_seen_order(
    log: HoldoutLog,
) -> None:
    _seed_two_builds(log)

    report = _analyze(log)

    assert report.by_commit is not None
    assert [row.build for row in report.by_commit.rows] == [BUILD_A, BUILD_B]
    rows = _rows(report)
    a, b = rows[BUILD_A], rows[BUILD_B]
    assert (a.served.n, a.served.outcome_mean) == (2, 2.5)
    assert (a.withheld.n, a.withheld.outcome_mean) == (1, 2.0)
    assert (b.served.n, b.served.outcome_mean) == (1, 0.0)
    assert (b.withheld.n, b.withheld.outcome_mean) == (2, 6.0)
    assert a.first_seen == MONDAY.isoformat()
    assert a.last_seen == (MONDAY + timedelta(hours=2)).isoformat()
    assert b.first_seen == (MONDAY + timedelta(hours=30)).isoformat()
    assert b.last_seen == (MONDAY + timedelta(hours=32)).isoformat()
    assert report.by_commit.units_spanning_builds == 0
    assert "no inference" in report.by_commit.note
    assert report.funnel.analysed == 6


def test_rows_follow_first_packs_not_the_order_joins_arrive(log: HoldoutLog) -> None:
    # Build A's task starts first, but capture writes its join last.
    pack_a = log.pack(at=MONDAY, withheld=False, items=3, metadata=provenance(COMMIT_A))
    pack_b = log.pack(
        at=MONDAY + timedelta(hours=5),
        withheld=True,
        items=3,
        metadata=provenance(COMMIT_B),
    )
    for task, pack, hours, commits in [
        ("task-late-join", pack_a, 40, 1),
        ("task-early-join", pack_b, 6, 2),
    ]:
        log.join(
            task,
            at=MONDAY + timedelta(hours=hours),
            pack_ids=[pack],
            parent="parent-a",
            outcome=outcome_payload(turns=5, commits=commits),
        )
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    assert [row.build for row in report.by_commit.rows] == [BUILD_A, BUILD_B]


def test_the_table_counts_the_same_tasks_as_the_primary(log: HoldoutLog) -> None:
    _seed_two_builds(log)

    report = _analyze(log)

    in_table = sum(
        arm.n for row in _rows(report).values() for arm in (row.served, row.withheld)
    )
    assert in_table == report.descriptive.analysed_tasks == 6
    assert sum(r.served.n for r in _rows(report).values()) == (
        report.descriptive.analysed_by_arm.served
    )


def test_the_means_read_the_outcome_column_the_primary_reads(log: HoldoutLog) -> None:
    _task(log, 0, COMMIT_A, withheld=False, turns=3)
    _task(log, 1, COMMIT_A, withheld=False, turns=8)
    _task(log, 2, COMMIT_A, withheld=True, turns=5)
    log.sweep()

    report = _analyze(log, outcome="log1p_turns")

    row = _rows(report)[BUILD_A]
    assert row.served.outcome_mean == pytest.approx((math.log1p(3) + math.log1p(8)) / 2)
    assert row.withheld.outcome_mean == pytest.approx(math.log1p(5))


def test_an_empty_arm_has_no_mean(log: HoldoutLog) -> None:
    _task(log, 0, COMMIT_A, withheld=False, commits=2)
    _task(log, 1, COMMIT_B, withheld=True, commits=3)
    log.sweep()

    rows = _rows(_analyze(log))

    assert (rows[BUILD_A].served.n, rows[BUILD_A].served.outcome_mean) == (1, 2.0)
    assert (rows[BUILD_A].withheld.n, rows[BUILD_A].withheld.outcome_mean) == (
        0,
        None,
    )
    assert (rows[BUILD_B].served.n, rows[BUILD_B].served.outcome_mean) == (0, None)
    assert rows[BUILD_B].withheld.outcome_mean == 3.0


def test_a_task_with_packs_from_two_builds_counts_once_under_its_first_pack(
    log: HoldoutLog,
) -> None:
    _task(log, 0, COMMIT_A, withheld=False, commits=1)
    _task(log, 1, COMMIT_B, withheld=True, commits=2)
    # Calls in order: build A first, build B second.
    log.task(
        start=MONDAY + timedelta(hours=2),
        arms=[False, False],
        pack_metadata=[provenance(COMMIT_A), provenance(COMMIT_B)],
        commits=9,
    )
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    assert report.by_commit.units_spanning_builds == 1
    rows = _rows(report)
    assert rows[BUILD_A].served.n == 2
    assert rows[BUILD_A].served.outcome_mean == 5.0
    assert rows[BUILD_B].served.n == 0
    assert rows[BUILD_B].withheld.n == 1
    assert sum(r.served.n + r.withheld.n for r in rows.values()) == 3
    # The task's first pack is build A's, so it is also what A's last_seen reads.
    assert rows[BUILD_A].last_seen == (MONDAY + timedelta(hours=2)).isoformat()


def test_a_task_that_spans_builds_is_not_counted_when_it_is_not_analysed(
    log: HoldoutLog,
) -> None:
    _task(log, 0, COMMIT_A, withheld=False, commits=1)
    log.task(
        start=MONDAY + timedelta(hours=1),
        arms=[False, False],
        pack_metadata=[provenance(COMMIT_A), provenance(COMMIT_B)],
        ended_on_error=True,
    )
    log.sweep()

    report = _analyze(log)
    kept = _analyze(log, itt=True)

    assert report.by_commit is not None
    assert report.by_commit.units_spanning_builds == 0
    assert kept.by_commit is not None
    assert kept.by_commit.units_spanning_builds == 1


def test_a_null_commit_and_a_missing_stamp_are_labelled_as_health_labels_them(
    log: HoldoutLog,
) -> None:
    _task(log, 0, COMMIT_A, withheld=False, commits=1)
    _task(log, 1, None, withheld=False, commits=2)  # version, commit: null
    log.task(  # no metadata at all
        start=MONDAY + timedelta(hours=2), arms=[False], commits=4
    )
    log.task(  # a blank commit reads as null
        start=MONDAY + timedelta(hours=3),
        arms=[True],
        pack_metadata=[provenance("   ")],
        commits=6,
    )
    log.task(  # a stamp that is not a mapping
        start=MONDAY + timedelta(hours=4),
        arms=[True],
        pack_metadata=[{"write_provenance": "oops"}],
        commits=8,
    )
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    labels = [row.build for row in report.by_commit.rows]
    # The same labels, as a set, as the serving-builds report gives the rows.
    assert set(labels) == _health_labels(log)
    assert labels == [BUILD_A, build_label("0.0.0", None), NO_PROVENANCE_LABEL]
    partial = _rows(report)[build_label("0.0.0", None)]
    assert (partial.served.n, partial.served.outcome_mean) == (1, 2.0)
    assert (partial.withheld.n, partial.withheld.outcome_mean) == (1, 6.0)
    assert partial.first_seen == (MONDAY + timedelta(hours=1)).isoformat()
    bare = _rows(report)[NO_PROVENANCE_LABEL]
    assert (bare.served.n, bare.served.outcome_mean) == (1, 4.0)
    assert (bare.withheld.n, bare.withheld.outcome_mean) == (1, 8.0)


def test_an_unstamped_first_pack_beside_a_stamped_one_names_the_unstamped_build(
    log: HoldoutLog,
) -> None:
    log.task(
        start=MONDAY,
        arms=[False, False],
        pack_metadata=[None, provenance(COMMIT_A)],
        commits=3,
    )
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    assert report.by_commit.units_spanning_builds == 1
    assert [row.build for row in report.by_commit.rows] == [NO_PROVENANCE_LABEL]


def test_one_commit_under_two_versions_is_two_builds(log: HoldoutLog) -> None:
    for hours, version, withheld, commits in [
        (0, "1.0.0", False, 2),
        (1, "1.0.0", True, 4),
        (2, "1.1.0", False, 6),
        (3, "1.1.0", True, 8),
    ]:
        log.task(
            start=MONDAY + timedelta(hours=hours),
            arms=[withheld],
            pack_metadata=[_stamped(version, COMMIT_A)],
            commits=commits,
        )
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    expected = [build_label("1.0.0", COMMIT_A), build_label("1.1.0", COMMIT_A)]
    assert [row.build for row in report.by_commit.rows] == expected
    assert set(expected) == _health_labels(log)
    old, new = (_rows(report)[label] for label in expected)
    assert (old.served.outcome_mean, old.withheld.outcome_mean) == (2.0, 4.0)
    assert (new.served.outcome_mean, new.withheld.outcome_mean) == (6.0, 8.0)


def test_two_commits_sharing_a_twelve_character_prefix_are_two_builds(
    log: HoldoutLog,
) -> None:
    sibling = COMMIT_A[:12] + "f" * 28
    _task(log, 0, COMMIT_A, withheld=False, commits=1)
    _task(log, 1, sibling, withheld=True, commits=5)
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    assert [row.build for row in report.by_commit.rows] == [
        build_label("0.0.0", COMMIT_A),
        build_label("0.0.0", sibling),
    ]
    assert {row.build for row in report.by_commit.rows} == _health_labels(log)


def test_a_join_listing_its_packs_out_of_time_order_takes_the_first_listed_build(
    log: HoldoutLog,
) -> None:
    earlier = log.pack(
        at=MONDAY + timedelta(hours=1),
        withheld=False,
        items=3,
        metadata=provenance(COMMIT_B),
    )
    later = log.pack(
        at=MONDAY + timedelta(hours=5),
        withheld=False,
        items=3,
        metadata=provenance(COMMIT_A),
    )
    # The arm, window and eligibility come from the first id listed, the later
    # pack, so its build names the task.
    log.join(
        "task-out-of-order",
        at=MONDAY + timedelta(hours=8),
        pack_ids=[later, earlier],
        parent="parent-a",
        outcome=outcome_payload(turns=5, commits=3),
    )
    log.sweep()

    report = _analyze(log)

    assert report.by_commit is not None
    assert report.by_commit.units_spanning_builds == 1
    assert [row.build for row in report.by_commit.rows] == [BUILD_A]
    row = _rows(report)[BUILD_A]
    assert (row.served.n, row.served.outcome_mean) == (1, 3.0)
    # The time is the first listed pack's too, so a build is never dated
    # before one of its own packs.
    assert row.first_seen == row.last_seen == (MONDAY + timedelta(hours=5)).isoformat()


def test_without_the_flag_the_payload_is_what_it_was(log: HoldoutLog) -> None:
    _seed_two_builds(log)

    plain = _analyze(log, by_commit=False)
    flagged = _analyze(log)

    assert plain.by_commit is None
    plain_dump = plain.model_dump()
    assert "by_commit" not in plain_dump
    flagged_dump = flagged.model_dump()
    assert "by_commit" in flagged_dump
    del flagged_dump["by_commit"]
    assert flagged_dump == plain_dump
    assert list(flagged_dump) == list(plain_dump)
    assert '"by_commit"' not in plain.model_dump_json()


def test_the_flag_defaults_off_in_the_library(log: HoldoutLog) -> None:
    _seed_two_builds(log)

    report = holdout.analyze_holdout(log.event_log, days=DAYS, until=UNTIL, **FAST)

    assert report.by_commit is None


def test_a_task_the_descriptive_block_excludes_is_not_in_the_table(
    log: HoldoutLog,
) -> None:
    other = "c0ffee000001"
    cut_off_commit = "dec0de000002"
    cut_off = build_label("0.0.0", cut_off_commit)
    _task(log, 0, COMMIT_A, withheld=False, commits=1)
    # Pre-treatment: the first retrieval followed a stop hook's feedback.
    _task(
        log,
        1,
        other,
        withheld=False,
        commits=2,
        timing=timing_payload(hook_before=True),
    )
    # A usage-limit cut-off is eligible but not analysed.
    _task(log, 2, cut_off_commit, withheld=True, commits=3, ended_on_error=True)
    _task(log, 3, COMMIT_A, withheld=True, commits=5, ended_on_error=True)
    log.sweep()

    report = _analyze(log)

    assert report.funnel.eligible == 3
    assert report.funnel.analysed == 1
    assert list(_rows(report)) == [BUILD_A]
    row = _rows(report)[BUILD_A]
    assert (row.served.n, row.withheld.n) == (1, 0)

    # Keeping the cut-offs (ITT) puts them in the analysed set, so in the table.
    itt = _analyze(log, itt=True)
    assert list(_rows(itt)) == [BUILD_A, cut_off]
    assert (_rows(itt)[BUILD_A].served.n, _rows(itt)[BUILD_A].withheld.n) == (1, 1)
    assert _rows(itt)[cut_off].withheld.n == 1


def test_no_analysed_task_gives_an_empty_table(log: HoldoutLog) -> None:
    report = _analyze(log)

    assert report.by_commit is not None
    assert report.by_commit.rows == []
    assert report.by_commit.units_spanning_builds == 0
