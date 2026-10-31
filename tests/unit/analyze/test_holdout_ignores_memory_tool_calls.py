"""A join's ``memory_tool_calls`` changes nothing the holdout report says.

Capture writes the count into each join's ``outcome`` block and no analysis
reads it. This runs the full analysis over two stores that hold the same
experiment, one whose joins carry the key (with values that differ by task)
and one whose joins do not, and requires the reports to be identical to the
byte, for every outcome the analysis accepts. A reader that rejected the key,
summed it into a figure, or listed it in the report would fail here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    UNTIL,
    HoldoutLog,
    seed_experiment,
    seed_guardrails,
)
from trellis.analyze.holdout import OUTCOMES, HoldoutReport, analyze_holdout
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Small resampling counts keep each analysis well under a second.
FAST = {"permutations": 99, "bootstraps": 50, "power_sims": 10}


class _CountingLog(HoldoutLog):
    """A log whose every join's outcome also carries ``memory_tool_calls``."""

    def join(self, task_id: str, *, outcome: dict[str, Any] | None, **kwargs: Any):
        if outcome is not None:
            # Differs by task, zero included, so no figure can match by chance.
            outcome = {**outcome, "memory_tool_calls": int(task_id[-4:]) % 4}
        super().join(task_id, outcome=outcome, **kwargs)


def _store(path: Path, log_type: type[HoldoutLog]) -> tuple[SQLiteEventLog, HoldoutLog]:
    event_log = SQLiteEventLog(path)
    log = log_type(event_log)
    seed_experiment(log, effect=0.8, seed=11, parents=3, weeks=2, per_stratum=6)
    seed_guardrails(log)
    log.sweep()
    return event_log, log


def _report(log: HoldoutLog, **kwargs: Any) -> HoldoutReport:
    return analyze_holdout(
        log.event_log, days=DAYS, until=UNTIL, seed=7, **FAST, **kwargs
    )


@pytest.fixture
def stores(tmp_path: Path):
    plain_events, plain = _store(tmp_path / "plain.db", HoldoutLog)
    keyed_events, keyed = _store(tmp_path / "keyed.db", _CountingLog)
    yield plain, keyed
    plain_events.close()
    keyed_events.close()


def _joins(log: HoldoutLog) -> list[dict[str, Any]]:
    return [
        dict(event.payload)
        for event in log.event_log.get_events(
            event_type=EventType.CAPTURE_SESSION_PACKS, limit=10_000
        )
    ]


def test_the_two_stores_differ_only_by_the_key(stores) -> None:
    """The comparison is not vacuous: one store has the key on every join
    with an outcome, with more than one value, and the other on none."""
    plain, keyed = stores
    plain_outcomes = [j["outcome"] for j in _joins(plain) if "outcome" in j]
    keyed_outcomes = [j["outcome"] for j in _joins(keyed) if "outcome" in j]
    assert plain_outcomes
    assert all("memory_tool_calls" not in o for o in plain_outcomes)
    assert all("memory_tool_calls" in o for o in keyed_outcomes)
    assert len({o["memory_tool_calls"] for o in keyed_outcomes}) > 1
    assert [
        {k: v for k, v in o.items() if k != "memory_tool_calls"} for o in keyed_outcomes
    ] == plain_outcomes


@pytest.mark.parametrize("outcome", OUTCOMES)
@pytest.mark.parametrize("itt", [False, True])
def test_the_report_is_identical_with_the_key_on_every_join(
    stores, outcome: str, itt: bool
) -> None:
    plain, keyed = stores

    without = _report(plain, outcome=outcome, itt=itt, by_commit=True)
    with_key = _report(keyed, outcome=outcome, itt=itt, by_commit=True)

    assert without.funnel.analysed > 0
    assert with_key.model_dump_json() == without.model_dump_json()
    assert "memory_tool_calls" not in with_key.model_dump_json()


def test_a_report_does_change_when_an_outcome_it_reads_changes(
    stores, tmp_path: Path
) -> None:
    """The guard can fail: the same comparison, with one turn count moved,
    reads as a different report."""
    plain, keyed = stores
    baseline = _report(plain).model_dump_json()
    assert _report(keyed).model_dump_json() == baseline

    class _MoreTurns(_CountingLog):
        def join(self, task_id: str, *, outcome: dict[str, Any] | None, **kwargs: Any):
            if outcome is not None:
                outcome = {**outcome, "assistant_turns": outcome["assistant_turns"] + 1}
            super().join(task_id, outcome=outcome, **kwargs)

    event_log = SQLiteEventLog(tmp_path / "moved.db")
    try:
        moved = _MoreTurns(event_log)
        seed_experiment(moved, effect=0.8, seed=11, parents=3, weeks=2, per_stratum=6)
        seed_guardrails(moved)
        moved.sweep()
        assert _report(moved).model_dump_json() != baseline
    finally:
        event_log.close()
