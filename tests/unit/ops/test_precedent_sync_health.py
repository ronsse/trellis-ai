"""``analyze health`` reads the re-stamp failures a reinstatement already records.

``PrecedentReinstateHandler`` stamps the node, then re-stamps the document and
vector rows, and fails soft on both once the node is current. The only trace is
``document_sync_failed`` / ``vector_sync_failed`` on the ``PRECEDENT_REINSTATED``
event. Each test seeds those events into a real SQLite event log, with the
timestamps the test chose, because the order of a failed reinstate against a
later demote or repair is the thing under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.ops.precedent_sync_health import (
    precedent_sync_reasons,
    summarize_precedent_sync,
)
from trellis.ops.write_health import summarize_backend_health
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

ALPHA = "precedent://learning/alpha"
BETA = "precedent://learning/beta"


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _emit(
    log: SQLiteEventLog,
    event_type: EventType,
    target: str,
    *,
    hours_ago: float,
    **flags: Any,
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source="mutation_executor",
            entity_id=target,
            entity_type="precedent",
            occurred_at=datetime.now(tz=UTC) - timedelta(hours=hours_ago),
            payload={"target_id": target, "reason": "fixture", **flags},
        )
    )


def _reinstate(
    log: SQLiteEventLog, target: str, *, hours_ago: float, **flags: Any
) -> None:
    _emit(log, EventType.PRECEDENT_REINSTATED, target, hours_ago=hours_ago, **flags)


def _demote(log: SQLiteEventLog, target: str, *, hours_ago: float) -> None:
    _emit(log, EventType.PRECEDENT_DEMOTED, target, hours_ago=hours_ago)


class TestUnresolvedFailures:
    def test_a_failed_document_restamp_is_listed_and_warns(
        self, log: SQLiteEventLog
    ) -> None:
        _reinstate(log, ALPHA, hours_ago=2, document_sync_failed=True)

        sync = summarize_precedent_sync(log, days=1)
        report = summarize_backend_health(log, days=1)

        assert sync.reinstatements == 1
        assert sync.failed_reinstatements == 1
        assert sync.document_unresolved == [ALPHA]
        assert sync.vector_unresolved == []
        assert sync.latest_failure_at is not None
        assert report.precedent_sync.document_unresolved == [ALPHA]
        assert report.status == "warn"
        (reason,) = report.reasons
        assert "document_sync_failed" in reason
        assert ALPHA in reason
        # The document repair is verified, so the reason names it.
        assert "reinstate-precedent" in reason

    def test_a_failed_vector_restamp_is_listed_and_warns_without_a_repair_claim(
        self, log: SQLiteEventLog
    ) -> None:
        _reinstate(log, ALPHA, hours_ago=2, vector_sync_failed=True)

        report = summarize_backend_health(log, days=1)

        assert report.precedent_sync.vector_unresolved == [ALPHA]
        assert report.precedent_sync.document_unresolved == []
        assert report.status == "warn"
        (reason,) = report.reasons
        assert "vector_sync_failed" in reason
        assert ALPHA in reason
        # A vector-only failure is refused on re-issue, so no repair is claimed.
        assert "reinstate-precedent" not in reason

    def test_both_flags_on_one_event_warn_once_per_axis(
        self, log: SQLiteEventLog
    ) -> None:
        _reinstate(
            log, ALPHA, hours_ago=2, document_sync_failed=True, vector_sync_failed=True
        )

        sync = summarize_precedent_sync(log, days=1)

        assert sync.failed_reinstatements == 1
        assert sync.document_unresolved == [ALPHA]
        assert sync.vector_unresolved == [ALPHA]
        assert len(precedent_sync_reasons(sync)) == 2

    def test_a_clean_window_is_ok_and_names_nothing(self, log: SQLiteEventLog) -> None:
        _reinstate(log, ALPHA, hours_ago=2)

        sync = summarize_precedent_sync(log, days=1)
        report = summarize_backend_health(log, days=1)

        assert sync.reinstatements == 1
        assert sync.failed_reinstatements == 0
        assert sync.document_unresolved == []
        assert sync.vector_unresolved == []
        assert sync.latest_failure_at is None
        assert report.status == "ok"
        assert report.reasons == []

    def test_an_empty_log_is_a_zero_report(self, log: SQLiteEventLog) -> None:
        sync = summarize_precedent_sync(log, days=1)

        assert sync.reinstatements == 0
        assert sync.failed_reinstatements == 0
        assert sync.latest_failure_at is None
        assert precedent_sync_reasons(sync) == []

    def test_a_flag_that_is_not_literally_true_is_not_a_failure(
        self, log: SQLiteEventLog
    ) -> None:
        # The handler writes the key as ``True`` or not at all; a truthy
        # string from some other writer is not that signal.
        _reinstate(log, ALPHA, hours_ago=2, document_sync_failed="yes")
        _reinstate(log, BETA, hours_ago=2, vector_sync_failed=False)

        sync = summarize_precedent_sync(log, days=1)

        assert sync.failed_reinstatements == 0
        assert sync.document_unresolved == []
        assert sync.vector_unresolved == []


class TestRepairedAndSuperseded:
    def test_a_later_clean_reinstate_repairs_the_failure(
        self, log: SQLiteEventLog
    ) -> None:
        _reinstate(log, ALPHA, hours_ago=5, document_sync_failed=True)
        _reinstate(log, ALPHA, hours_ago=1, document_resync_only=True)

        sync = summarize_precedent_sync(log, days=1)
        report = summarize_backend_health(log, days=1)

        # The failure still happened, so the rate sees it; nothing is open.
        assert sync.failed_reinstatements == 1
        assert sync.reinstatements == 2
        assert sync.document_unresolved == []
        assert report.status == "ok"

    def test_a_later_demote_supersedes_the_failure(self, log: SQLiteEventLog) -> None:
        # Demotion re-stamps the row and the vector copy as demoted, so a
        # reinstate that failed to restamp them is moot once the node is
        # demoted again.
        _reinstate(
            log, ALPHA, hours_ago=5, document_sync_failed=True, vector_sync_failed=True
        )
        _demote(log, ALPHA, hours_ago=1)

        sync = summarize_precedent_sync(log, days=1)
        report = summarize_backend_health(log, days=1)

        assert sync.failed_reinstatements == 1
        assert sync.document_unresolved == []
        assert sync.vector_unresolved == []
        assert report.status == "ok"

    def test_a_failure_after_a_demote_is_open(self, log: SQLiteEventLog) -> None:
        # The order decides: demote, then a reinstate that failed to re-stamp.
        _demote(log, ALPHA, hours_ago=5)
        _reinstate(log, ALPHA, hours_ago=1, vector_sync_failed=True)

        sync = summarize_precedent_sync(log, days=1)

        assert sync.vector_unresolved == [ALPHA]

    def test_each_precedent_is_judged_on_its_own_latest_event(
        self, log: SQLiteEventLog
    ) -> None:
        _reinstate(log, ALPHA, hours_ago=5, document_sync_failed=True)
        _reinstate(log, ALPHA, hours_ago=1)
        _reinstate(log, BETA, hours_ago=3, document_sync_failed=True)

        sync = summarize_precedent_sync(log, days=1)

        assert sync.document_unresolved == [BETA]
        assert sync.failed_reinstatements == 2

    def test_the_target_falls_back_to_the_entity_id(self, log: SQLiteEventLog) -> None:
        log.append(
            Event(
                event_type=EventType.PRECEDENT_REINSTATED,
                source="mutation_executor",
                entity_id=ALPHA,
                occurred_at=datetime.now(tz=UTC) - timedelta(hours=1),
                payload={"document_sync_failed": True},
            )
        )

        assert summarize_precedent_sync(log, days=1).document_unresolved == [ALPHA]


class TestWindowAndCoverage:
    def test_an_event_outside_the_window_is_not_read(self, log: SQLiteEventLog) -> None:
        _reinstate(log, ALPHA, hours_ago=72, document_sync_failed=True)

        sync = summarize_precedent_sync(log, days=1)

        assert sync.reinstatements == 0
        assert sync.document_unresolved == []
        assert summarize_backend_health(log, days=1).status == "ok"

    def test_other_event_types_are_not_read(self, log: SQLiteEventLog) -> None:
        _emit(
            log,
            EventType.PRECEDENT_PROMOTED,
            ALPHA,
            hours_ago=1,
            document_sync_failed=True,
        )

        assert summarize_precedent_sync(log, days=1).reinstatements == 0

    def test_a_capped_read_reaches_the_backend_report_and_warns(
        self, log: SQLiteEventLog
    ) -> None:
        for hours_ago in (4, 3, 2):
            _reinstate(log, ALPHA, hours_ago=hours_ago)

        sync = summarize_precedent_sync(log, days=1, limit=1)
        report = summarize_backend_health(log, days=1, limit=1)

        assert sync.scan.truncated is True
        assert report.scan.truncated is True
        assert report.status == "warn"
        # Newest-first, so the cap drops the oldest rows, not the newest.
        assert sync.reinstatements == 1

    def test_an_uncapped_read_is_not_marked_truncated(
        self, log: SQLiteEventLog
    ) -> None:
        _reinstate(log, ALPHA, hours_ago=2)

        assert summarize_precedent_sync(log, days=1).scan.truncated is False


class TestReasonText:
    def test_ids_are_capped_and_the_overflow_counted(self, log: SQLiteEventLog) -> None:
        for n in range(8):
            _reinstate(
                log,
                f"precedent://learning/fixture-{n}",
                hours_ago=2,
                document_sync_failed=True,
            )

        sync = summarize_precedent_sync(log, days=1)
        (reason,) = precedent_sync_reasons(sync)

        assert len(sync.document_unresolved) == 8
        assert reason.startswith("8 precedent reinstatement(s)")
        assert "(+3 more)" in reason
        assert "fixture-4" in reason
        assert "fixture-5" not in reason
