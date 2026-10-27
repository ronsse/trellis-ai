"""The memory-extraction state signal and its reader (#987).

Each test seeds ``MEMORY_EXTRACTION_STATE`` rows into a real SQLite event log
with timestamps the test chose, because the writer's own clock is "now" and
the reader's job is the order and age of rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trellis.ops.memory_extraction_health import (
    emit_memory_extraction_state,
    summarize_memory_extraction,
)
from trellis.ops.write_health import summarize_backend_health
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def _row(
    event_log: SQLiteEventLog,
    state: str,
    *,
    hours_ago: float,
    reason: str | None = None,
) -> datetime:
    at = _now() - timedelta(hours=hours_ago)
    event_log.append(
        Event(
            event_type=EventType.MEMORY_EXTRACTION_STATE,
            source="mcp:memory_extraction",
            occurred_at=at,
            payload={"state": state, "reason": reason, "error_type": None},
        )
    )
    return at


class TestEmit:
    def test_writes_one_row_with_the_documented_payload(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")

        landed = emit_memory_extraction_state(
            log, state="unavailable", reason="build_failed", error_type="ValueError"
        )

        (event,) = log.get_events(event_type=EventType.MEMORY_EXTRACTION_STATE)
        assert landed is True
        assert event.source == "mcp:memory_extraction"
        assert event.payload == {
            "state": "unavailable",
            "reason": "build_failed",
            "error_type": "ValueError",
        }

    def test_a_broken_event_log_costs_the_row_not_the_caller(self) -> None:
        class _Broken:
            def emit(self, *args: Any, **kwargs: Any) -> None:
                msg = "event log down"
                raise OSError(msg)

        assert emit_memory_extraction_state(_Broken(), state="available") is False  # type: ignore[arg-type]


class TestSummarize:
    def test_a_window_with_no_row_is_unobserved_not_healthy(
        self, tmp_path: Path
    ) -> None:
        report = summarize_memory_extraction(SQLiteEventLog(tmp_path / "e.db"))

        assert report.state == "unobserved"
        assert report.since is None
        assert report.unavailable_events == 0

    def test_newest_unavailable_row_wins_and_dates_the_outage(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        _row(log, "available", hours_ago=30)
        first = _row(log, "unavailable", hours_ago=20, reason="no_llm_client")
        _row(log, "unavailable", hours_ago=2, reason="no_llm_client")

        report = summarize_memory_extraction(log)

        assert report.state == "unavailable"
        assert report.reason == "no_llm_client"
        # "Dark since" is the start of the newest run, not the newest row:
        # the refresh row two hours ago is the same outage.
        assert report.since == first
        assert report.unavailable_events == 2

    def test_a_later_available_row_clears_it(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        _row(log, "unavailable", hours_ago=5, reason="no_llm_client")
        recovered = _row(log, "available", hours_ago=1)

        report = summarize_memory_extraction(log)

        assert report.state == "available"
        assert report.reason is None
        assert report.since == recovered

    def test_a_row_older_than_the_window_is_not_read(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        _row(log, "unavailable", hours_ago=24 * 10, reason="no_llm_client")

        assert summarize_memory_extraction(log, days=7).state == "unobserved"

    def test_a_malformed_row_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        log.append(
            Event(
                event_type=EventType.MEMORY_EXTRACTION_STATE,
                source="mcp:memory_extraction",
                payload={"state": "bogus"},
            )
        )

        assert summarize_memory_extraction(log).state == "unobserved"


class TestBackendHealth:
    def test_unavailable_warns_with_reason_and_onset(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        since = _row(log, "unavailable", hours_ago=3, reason="no_llm_client")

        report = summarize_backend_health(log, days=7)

        assert report.status == "warn"
        assert report.memory_extraction.state == "unavailable"
        (reason,) = [r for r in report.reasons if "memory extraction" in r]
        assert "no_llm_client" in reason
        assert f"{since:%Y-%m-%d %H:%M} UTC" in reason
        # A report about the newest event, scoped to one process: several
        # processes share the log, so it must not claim every save is bare.
        assert "most recent extraction-state report" in reason
        assert "saves on that process" in reason
        assert "every save" not in reason

    def test_available_and_unobserved_do_not_warn(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        assert summarize_backend_health(log, days=7).status == "ok"

        _row(log, "unavailable", hours_ago=4, reason="no_llm_client")
        _row(log, "available", hours_ago=1)
        report = summarize_backend_health(log, days=7)

        assert report.memory_extraction.state == "available"
        assert not [r for r in report.reasons if "memory extraction" in r]
        assert report.status == "ok"
