"""``analyze health`` reports whether agents retrieve, from the capture joins.

Each test seeds ``CAPTURE_SESSION_PACKS`` events into a real SQLite event log
with the timestamps the test chose: the window's edge and a session's later
re-join are the things under test, and both are about ``occurred_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.ops.retrieval_adoption import (
    RETRIEVAL_ADOPTION_FLOOR,
    RETRIEVAL_ADOPTION_MIN_SESSIONS,
    retrieval_adoption_reasons,
    summarize_retrieval_adoption,
)
from trellis.ops.write_health import summarize_backend_health
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _join(
    log: SQLiteEventLog,
    session: str,
    pack_ids: Any,
    *,
    at: datetime,
) -> None:
    log.append(
        Event(
            event_type=EventType.CAPTURE_SESSION_PACKS,
            source="worker:session-capture",
            entity_id=session,
            entity_type="capture_session",
            occurred_at=at,
            payload={"pack_ids": pack_ids, "parent_session_id": None},
        )
    )


def _seed(log: SQLiteEventLog, *, with_packs: int, without: int) -> None:
    at = NOW - timedelta(days=1)
    for n in range(with_packs):
        _join(log, f"s-with-{n}", [f"pack-{n}"], at=at)
    for n in range(without):
        _join(log, f"s-without-{n}", [], at=at)


class TestAdoptionState:
    def test_ok_at_or_above_the_floor(self, log: SQLiteEventLog) -> None:
        _seed(log, with_packs=6, without=6)  # 12 sessions, exactly 0.5

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.sessions == 12
        assert report.sessions_with_packs == 6
        assert report.adoption == pytest.approx(RETRIEVAL_ADOPTION_FLOOR)
        assert report.state == "ok"
        assert retrieval_adoption_reasons(report) == []

    def test_warn_below_the_floor(self, log: SQLiteEventLog) -> None:
        _seed(log, with_packs=3, without=9)

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.adoption == pytest.approx(0.25)
        assert report.state == "warn"
        (reason,) = retrieval_adoption_reasons(report)
        assert "25%" in reason
        assert "3/12" in reason
        assert "50% floor" in reason

    def test_insufficient_data_never_warns(self, log: SQLiteEventLog) -> None:
        # Nine sessions, none retrieving: a 0% reading with nothing behind it.
        _seed(log, with_packs=0, without=RETRIEVAL_ADOPTION_MIN_SESSIONS - 1)

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.sessions == RETRIEVAL_ADOPTION_MIN_SESSIONS - 1
        assert report.state == "insufficient_data"
        assert report.adoption is None
        assert retrieval_adoption_reasons(report) == []

    def test_minimum_is_inclusive(self, log: SQLiteEventLog) -> None:
        _seed(log, with_packs=0, without=RETRIEVAL_ADOPTION_MIN_SESSIONS)

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.state == "warn"
        assert report.adoption == 0.0

    def test_empty_log_is_insufficient_data(self, log: SQLiteEventLog) -> None:
        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.sessions == 0
        assert report.state == "insufficient_data"
        assert report.adoption is None


class TestWindowEdge:
    def test_a_join_just_outside_the_window_is_not_counted(
        self, log: SQLiteEventLog
    ) -> None:
        inside = NOW - timedelta(days=7) + timedelta(minutes=1)
        outside = NOW - timedelta(days=7) - timedelta(minutes=1)
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS):
            _join(log, f"in-{n}", [], at=inside)
        for n in range(5):
            _join(log, f"out-{n}", ["pack"], at=outside)

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.sessions == RETRIEVAL_ADOPTION_MIN_SESSIONS
        assert report.sessions_with_packs == 0
        assert report.state == "warn"

    def test_the_window_follows_days(self, log: SQLiteEventLog) -> None:
        at = NOW - timedelta(days=3)
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS):
            _join(log, f"s-{n}", ["pack"], at=at)

        assert summarize_retrieval_adoption(log, days=2, now=NOW).sessions == 0
        assert (
            summarize_retrieval_adoption(log, days=4, now=NOW).sessions
            == RETRIEVAL_ADOPTION_MIN_SESSIONS
        )


class TestSessionJoin:
    def test_a_session_counts_once_and_its_latest_join_decides(
        self, log: SQLiteEventLog
    ) -> None:
        """A grown transcript re-writes its join; the earlier row is superseded."""
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS):
            _join(log, f"s-{n}", [], at=NOW - timedelta(days=2))
        # The first session retrieves later; the second only ever had one join.
        _join(log, "s-0", ["pack-late"], at=NOW - timedelta(days=1))

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.sessions == RETRIEVAL_ADOPTION_MIN_SESSIONS
        assert report.sessions_with_packs == 1

    @pytest.mark.parametrize("pack_ids", [None, "pack-1", [""], ["  "], [None, 3], {}])
    def test_a_malformed_pack_list_is_not_a_retrieval(
        self, log: SQLiteEventLog, pack_ids: Any
    ) -> None:
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS):
            _join(log, f"s-{n}", pack_ids, at=NOW - timedelta(days=1))

        report = summarize_retrieval_adoption(log, days=7, now=NOW)

        assert report.sessions_with_packs == 0

    def test_other_event_types_are_ignored(self, log: SQLiteEventLog) -> None:
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS):
            log.append(
                Event(
                    event_type=EventType.PACK_ASSEMBLED,
                    source="test",
                    entity_id=f"pack-{n}",
                    entity_type="pack",
                    occurred_at=NOW - timedelta(days=1),
                    payload={"pack_ids": ["x"]},
                )
            )

        assert summarize_retrieval_adoption(log, days=7, now=NOW).sessions == 0


class TestComposedHealth:
    def test_warn_reaches_the_backend_health_verdict(self, log: SQLiteEventLog) -> None:
        # Real clock here: ``summarize_backend_health`` has no pinned ``now``.
        at = datetime.now(tz=UTC) - timedelta(hours=1)
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS):
            _join(log, f"s-{n}", [], at=at)

        report = summarize_backend_health(log, days=7)

        assert report.retrieval.state == "warn"
        assert report.status == "warn"
        assert any("retrieval adoption 0%" in reason for reason in report.reasons)

    def test_insufficient_data_leaves_status_ok(self, log: SQLiteEventLog) -> None:
        at = datetime.now(tz=UTC) - timedelta(hours=1)
        for n in range(RETRIEVAL_ADOPTION_MIN_SESSIONS - 1):
            _join(log, f"s-{n}", [], at=at)

        report = summarize_backend_health(log, days=7)

        assert report.retrieval.state == "insufficient_data"
        assert not any("retrieval adoption" in reason for reason in report.reasons)
        assert report.status == "ok"
