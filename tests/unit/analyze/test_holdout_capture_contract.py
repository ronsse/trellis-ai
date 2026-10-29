"""The keys capture writes into a join are the keys the holdout reads.

The core library does not import the workers, so ``trellis.analyze.holdout``
names the join's ``retrieval_timing`` keys as strings. This parses a
synthetic transcript with the capture parser, writes the digest's timing
into a join, and reads it back through the analysis, so renaming a key on
either side fails here and not silently as ``unknown``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    MONDAY,
    UNTIL,
    HoldoutLog,
    outcome_payload,
)
from tests.unit.workers.session_capture.conftest import (
    PACK_IDS,
    api_message,
    assistant_tools,
    bash_block,
    mcp_envelope,
    pack_markdown,
    pack_result_turn,
    result_block,
    tool_results,
    user_turn,
    write_transcript,
)
from trellis.analyze.holdout import HoldoutReport, analyze_holdout
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis_workers.session_capture.transcripts import parse_session

_HOOK = "Stop hook feedback:\n[fake-hook]: retrieve context before you stop."


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _session(*, hook_first: bool) -> list[dict]:
    work = [
        assistant_tools(("Bash", "b-1")),
        pack_result_turn("b-1", "ok"),
    ]
    hook = [user_turn(_HOOK)]
    served = [
        assistant_tools(("mcp__trellis__get_context", "u-1")),
        pack_result_turn("u-1", mcp_envelope(pack_markdown(PACK_IDS[0]))),
    ]
    return [
        user_turn("gather what we know about the widget deploy"),
        *(work + hook + served if hook_first else served + work + hook),
    ]


def _analyze(log: HoldoutLog) -> HoldoutReport:
    return analyze_holdout(
        log.event_log,
        days=DAYS,
        until=UNTIL,
        seed=7,
        permutations=99,
        bootstraps=50,
        power_sims=10,
    )


def test_a_parsed_timing_decides_the_exclusion(log: HoldoutLog, tmp_path: Path) -> None:
    for index, hook_first in enumerate([True, False, False]):
        path = tmp_path / f"s{index}.jsonl"
        write_transcript(path, _session(hook_first=hook_first))
        timing = parse_session(path).retrieval_timing_payload()
        at = MONDAY + timedelta(hours=index)
        log.join(
            f"task-{index}",
            at=at,
            pack_ids=[log.pack(at=at - timedelta(minutes=1), withheld=False, items=2)],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
            timing=timing,
        )
    log.sweep()

    report = _analyze(log)

    assert report.funnel.pre_treatment_unknown == 0
    assert report.funnel.pre_treatment_excluded == 1
    assert report.funnel.eligible == 2
    assert report.funnel.pre_treatment_hook_feedback_seen == 3
    pre = next(e for e in report.exclusions if e.name == "pre_treatment")
    assert (pre.applied, pre.excluded, pre.unknown) == (True, 1, 0)


def _bash(use_id: str, command: str) -> list[dict]:
    return [
        *api_message(f"msg-{use_id}", bash_block(use_id, command), at=None),
        tool_results(result_block(use_id, "ok"), at=None),
    ]


def _served() -> list[dict]:
    return [
        assistant_tools(("mcp__trellis__get_context", "u-1")),
        pack_result_turn("u-1", mcp_envelope(pack_markdown(PACK_IDS[0]))),
    ]


def _commit_session(order: str) -> list[dict]:
    """Twelve calls around one retrieval, so it is never in the last tenth."""
    work = [rec for n in range(10) for rec in _bash(f"w-{n}", "ls /fake/repo")]
    commit = _bash("c-1", "git commit -m fake")
    steps = {
        "commit_first": commit + _served() + work,
        "retrieval_first": _served() + commit + work,
        "no_commit": _served() + work + _bash("c-2", "echo git commit"),
    }[order]
    return [user_turn("gather what we know about the widget deploy"), *steps]


def test_a_parsed_commit_position_decides_the_commit_half(
    log: HoldoutLog, tmp_path: Path
) -> None:
    for index, order in enumerate(["commit_first", "retrieval_first", "no_commit"]):
        path = tmp_path / f"s{index}.jsonl"
        write_transcript(path, _commit_session(order))
        timing = parse_session(path).retrieval_timing_payload()
        at = MONDAY + timedelta(hours=index)
        log.join(
            f"task-{index}",
            at=at,
            pack_ids=[log.pack(at=at - timedelta(minutes=1), withheld=False, items=2)],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
            timing=timing,
        )
    log.sweep()

    report = _analyze(log)

    funnel = report.funnel
    assert funnel.pre_treatment_unknown == 0
    assert (funnel.pre_treatment_excluded, funnel.eligible) == (1, 2)
    assert funnel.pre_treatment_excluded_after_commit == 1
    # All three carry every position, and only the commit-first one is late.
    assert (funnel.post_hoc_measured, funnel.post_hoc_after_work) == (3, 1)
    assert report.descriptive.post_hoc_share == pytest.approx(1 / 3)
    pre = next(e for e in report.exclusions if e.name == "pre_treatment")
    assert (pre.applied, pre.excluded, pre.unknown) == (True, 1, 0)
