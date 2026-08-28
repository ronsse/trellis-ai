"""A capped analyzer report must say it was capped (#374).

``scan_events`` is unit-tested in ``tests/unit/stores/test_event_scan.py``.
This file pins the property at the level an operator actually reads: each
analyzer's *output*. Two things must hold for every one of them, and both
are asserted in the same test so neither can be quietly dropped:

1. The numbers are computed from the **newest** events in the window.
2. The report **says** its window was cut short.

Every assertion has a paired negative case — an untruncated run of the
same analyzer — because a disclosure that is always on is exactly as
useless as one that is always off, and this repo's recurring defect is a
measurement wired to a constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.ops.capture_coverage import summarize_capture_coverage
from trellis.ops.write_health import (
    summarize_backend_health,
    summarize_serve_attribution,
    summarize_write_health,
)
from trellis.retrieve.metrics_timeseries import (
    METRIC_NOISE_TAG_VOLUME,
    compute_timeseries,
)
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _at(minutes_ago: int) -> datetime:
    return datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)


def _append(
    log: SQLiteEventLog,
    event_type: EventType,
    *,
    minutes_ago: int,
    payload: dict[str, object] | None = None,
    source: str = "test",
    entity_id: str | None = None,
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source=source,
            entity_id=entity_id,
            occurred_at=_at(minutes_ago),
            payload=payload or {},
        )
    )


# ---------------------------------------------------------------------------
# write_health — the surface that is supposed to notice today's outage
# ---------------------------------------------------------------------------


def _seed_writes(log: SQLiteEventLog, *, old: int, new_tool: str) -> None:
    """``old`` stale accepted writes, then one rejection from a new tool."""
    for index in range(old):
        _append(
            log,
            EventType.MUTATION_EXECUTED,
            minutes_ago=1000 - index,
            payload={"requested_by": "mcp:stale_tool"},
        )
    _append(
        log,
        EventType.WRITE_REJECTED,
        minutes_ago=1,
        payload={"tool": new_tool, "rejections": [{"kind": "enum", "loc": "source"}]},
    )


def test_write_health_capped_report_says_so_and_keeps_the_newest(
    log: SQLiteEventLog,
) -> None:
    """The motivating incident: a new rejection behind a wall of old accepts.

    ``MUTATION_EXECUTED`` is the highest-volume event type any analyzer
    reads (1,473 in 30 days on the reference deployment), so it is the one
    that reaches the cap first and buries everything newer.
    """
    _seed_writes(log, old=6, new_tool="save_experience")
    report = summarize_write_health(log, days=30, limit=3)

    assert report.scan.truncated is True
    assert report.status == "warn"
    assert any("TRUNCATED" in reason for reason in report.reasons)
    # Summed across all three reads: 6 accepts (3 kept, 3 dropped) plus
    # the single boundary rejection, which was never at risk of the cap.
    assert report.scan.scanned == 4
    assert report.scan.matched == 7
    assert report.scan.dropped == 3
    # The rejection is one minute old; under an ascending cap it was the
    # first row dropped and this surface did not exist in the report.
    assert "mcp:save_experience" in report.by_tool


def test_write_health_uncapped_report_makes_no_truncation_claim(
    log: SQLiteEventLog,
) -> None:
    _seed_writes(log, old=6, new_tool="save_experience")
    report = summarize_write_health(log, days=30, limit=100)

    assert report.scan.truncated is False
    assert report.scan.note == ""
    assert not any("TRUNCATED" in reason for reason in report.reasons)
    assert "mcp:save_experience" in report.by_tool


def test_serve_attribution_reports_its_own_coverage(log: SQLiteEventLog) -> None:
    for index in range(6):
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            minutes_ago=100 - index,
            entity_id=f"pack_{index}",
            payload={"injected_items": [{"item_id": "a"}]},
        )
    capped = summarize_serve_attribution(log, days=30, limit=3)
    clean = summarize_serve_attribution(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.packs == 3
    assert clean.scan.truncated is False
    assert clean.packs == 6


def test_backend_health_never_reports_ok_over_a_shortened_window(
    log: SQLiteEventLog,
) -> None:
    """A clean ``ok`` on a truncated scan is the failure #374 describes."""
    for index in range(6):
        _append(
            log,
            EventType.MUTATION_EXECUTED,
            minutes_ago=100 - index,
            payload={"requested_by": "mcp:save_memory"},
        )
    capped = summarize_backend_health(log, days=30, limit=3)
    clean = summarize_backend_health(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.status == "warn"
    assert clean.scan.truncated is False
    assert clean.status == "ok"


# ---------------------------------------------------------------------------
# capture_coverage — last_sweep_at is the field truncation lied to
# ---------------------------------------------------------------------------


def test_capture_coverage_last_sweep_is_the_newest_sweep(
    log: SQLiteEventLog,
) -> None:
    """Under an ascending cap ``last_sweep_at`` became the newest sweep of
    the *oldest* slice — a plausible timestamp for a pipeline that has
    since stopped, which is precisely the state this module distinguishes.
    """
    for index in range(6):
        _append(
            log,
            EventType.CAPTURE_SWEEP_COMPLETED,
            minutes_ago=600 - index * 100,
            source="worker:session-capture",
            payload={"sessions_triggered": 1, "sessions_with_memory": 1},
        )
    capped = summarize_capture_coverage(log, days=30, limit=3)
    clean = summarize_capture_coverage(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert any("TRUNCATED" in note for note in capped.notes)
    assert clean.scan.truncated is False
    assert not any("TRUNCATED" in note for note in clean.notes)
    # Both agree on when capture last ran, because the cap drops the old end.
    assert capped.last_sweep_at == clean.last_sweep_at


# ---------------------------------------------------------------------------
# metrics_timeseries — a truncated series goes flat, not empty
# ---------------------------------------------------------------------------


def test_timeseries_reports_its_scan_coverage(log: SQLiteEventLog) -> None:
    for index in range(6):
        _append(
            log,
            EventType.TAGS_REFRESHED,
            minutes_ago=100 - index,
            payload={"after": {"signal_quality": "noise"}},
        )
    capped = compute_timeseries(log, metric=METRIC_NOISE_TAG_VOLUME, days=30, limit=3)
    clean = compute_timeseries(log, metric=METRIC_NOISE_TAG_VOLUME, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.scan.covered_since != ""
    assert clean.scan.truncated is False
    assert clean.scan.covered_since == ""


# ---------------------------------------------------------------------------
# pack_value — the truncation note leads the caveats
# ---------------------------------------------------------------------------


def test_pack_value_puts_truncation_first_among_its_notes(
    log: SQLiteEventLog,
) -> None:
    """If the window is not the window, nothing below it means what it says."""
    for index in range(6):
        pack_id = f"pack_{index}"
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            minutes_ago=100 - index,
            entity_id=pack_id,
            payload={
                "injected_items": [
                    {
                        "item_id": f"{pack_id}_a",
                        "estimated_tokens": 100,
                        "strategy_source": "semantic",
                        "item_type": "vector",
                    }
                ]
            },
        )
        _append(
            log,
            EventType.FEEDBACK_RECORDED,
            minutes_ago=99 - index,
            payload={"pack_id": pack_id, "helpful_item_ids": [f"{pack_id}_a"]},
        )
    capped = summarize_pack_value(log, days=30, limit=3)
    clean = summarize_pack_value(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert "TRUNCATED" in capped.notes[1]
    assert clean.scan.truncated is False
    assert not any("TRUNCATED" in note for note in clean.notes)
