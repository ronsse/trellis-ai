"""Tests for maintenance workers — retention pruning, staleness detection."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from trellis.core.base import utc_now
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.document import SQLiteDocumentStore
from trellis.stores.event_log import EventType, SQLiteEventLog
from trellis.stores.trace import SQLiteTraceStore
from trellis_workers.maintenance.retention import (
    RetentionPolicy,
    RetentionWorker,
    StalenessDetector,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trace(
    *,
    intent: str = "do something",
    status: OutcomeStatus = OutcomeStatus.UNKNOWN,
    domain: str | None = "testing",
    no_outcome: bool = False,
    created_at: datetime | None = None,
) -> Trace:
    """Create a minimal Trace for testing."""
    outcome = None if no_outcome else Outcome(status=status)
    kwargs: dict[str, Any] = {
        "source": TraceSource.AGENT,
        "intent": intent,
        "outcome": outcome,
        "context": TraceContext(domain=domain),
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
        kwargs["updated_at"] = created_at
    return Trace(**kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trace_store(tmp_path: Any) -> SQLiteTraceStore:
    store = SQLiteTraceStore(tmp_path / "traces.db")
    yield store  # type: ignore[misc]
    store.close()


@pytest.fixture
def event_log(tmp_path: Any) -> SQLiteEventLog:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log  # type: ignore[misc]
    log.close()


@pytest.fixture
def document_store(tmp_path: Any) -> SQLiteDocumentStore:
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store  # type: ignore[misc]
    store.close()


# ---------------------------------------------------------------------------
# RetentionPolicy — defaults and custom values
# ---------------------------------------------------------------------------


class TestRetentionPolicy:
    """Test RetentionPolicy model defaults and overrides."""

    def test_default_values(self) -> None:
        policy = RetentionPolicy()
        assert policy.max_age_days == 365
        assert policy.max_traces == 10000
        assert policy.preserve_outcomes == ["success"]
        assert policy.dry_run is False

    def test_custom_values(self) -> None:
        policy = RetentionPolicy(
            max_age_days=30,
            max_traces=500,
            preserve_outcomes=["success", "partial"],
            dry_run=True,
        )
        assert policy.max_age_days == 30
        assert policy.max_traces == 500
        assert policy.preserve_outcomes == ["success", "partial"]
        assert policy.dry_run is True


# ---------------------------------------------------------------------------
# RetentionWorker
# ---------------------------------------------------------------------------


class TestRetentionWorker:
    """Tests for the RetentionWorker."""

    def test_prunes_old_traces(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Old traces with non-preserved outcomes are pruned."""
        old_date = utc_now() - timedelta(days=400)
        for i in range(3):
            t = _make_trace(
                intent=f"old-task-{i}",
                status=OutcomeStatus.FAILURE,
                created_at=old_date,
            )
            trace_store.append(t)

        worker = RetentionWorker(trace_store, event_log=event_log)
        report = worker.run(RetentionPolicy(max_age_days=365))

        assert report.traces_scanned == 3
        assert report.traces_marked == 3
        assert report.traces_preserved == 0
        assert report.dry_run is False
        assert report.completed_at is not None
        assert len(report.errors) == 0

    def test_preserves_successful_traces(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Traces with outcomes in preserve_outcomes are kept."""
        old_date = utc_now() - timedelta(days=400)
        # One success (preserved), one failure (pruned)
        t_success = _make_trace(
            intent="good-task",
            status=OutcomeStatus.SUCCESS,
            created_at=old_date,
        )
        t_failure = _make_trace(
            intent="bad-task",
            status=OutcomeStatus.FAILURE,
            created_at=old_date,
        )
        trace_store.append(t_success)
        trace_store.append(t_failure)

        worker = RetentionWorker(trace_store, event_log=event_log)
        report = worker.run(RetentionPolicy(max_age_days=365))

        assert report.traces_scanned == 2
        assert report.traces_preserved == 1
        assert report.traces_marked == 1

    def test_dry_run_does_not_emit_events(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Dry run counts traces but doesn't emit events."""
        old_date = utc_now() - timedelta(days=400)
        t = _make_trace(
            intent="dry-run-task",
            status=OutcomeStatus.FAILURE,
            created_at=old_date,
        )
        trace_store.append(t)

        worker = RetentionWorker(trace_store, event_log=event_log)
        report = worker.run(RetentionPolicy(max_age_days=365, dry_run=True))

        assert report.traces_marked == 1
        assert report.dry_run is True

        events = event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
        assert len(events) == 0

    def test_event_emission_on_prune(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Pruning emits MUTATION_EXECUTED events for audit trail."""
        old_date = utc_now() - timedelta(days=400)
        t = _make_trace(
            intent="emit-task",
            status=OutcomeStatus.FAILURE,
            created_at=old_date,
        )
        trace_store.append(t)

        worker = RetentionWorker(trace_store, event_log=event_log)
        worker.run(RetentionPolicy(max_age_days=365))

        events = event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
        assert len(events) == 1
        evt = events[0]
        assert evt.entity_id == t.trace_id
        assert evt.entity_type == "trace"
        assert evt.source == "retention_worker"
        assert evt.payload["action"] == "retention_prune"
        assert evt.payload["outcome_status"] == "failure"

    def test_empty_traces_returns_zero_report(
        self,
        trace_store: SQLiteTraceStore,
    ) -> None:
        """No traces → report with all zeros."""
        worker = RetentionWorker(trace_store)
        report = worker.run(RetentionPolicy(max_age_days=365))

        assert report.traces_scanned == 0
        assert report.traces_marked == 0
        assert report.traces_preserved == 0
        assert report.completed_at is not None


# ---------------------------------------------------------------------------
# StalenessDetector
# ---------------------------------------------------------------------------


class TestStalenessDetector:
    """Tests for the StalenessDetector."""

    def test_detects_stale_documents(
        self,
        document_store: SQLiteDocumentStore,
    ) -> None:
        """Documents with old updated_at are flagged as stale."""
        # Insert a document, then manually set its updated_at to be old
        doc_id = document_store.put(None, "stale content", metadata={"key": "val"})

        # Backdate the updated_at via raw SQL
        old_date = (utc_now() - timedelta(days=120)).isoformat()
        document_store._conn.execute(
            "UPDATE documents SET updated_at = ? WHERE doc_id = ?",
            (old_date, doc_id),
        )
        document_store._conn.commit()

        detector = StalenessDetector(document_store, staleness_days=90)
        report = detector.check()

        assert report.total_documents == 1
        assert doc_id in report.stale_documents
        assert len(report.missing_documents) == 0

    def test_empty_store_returns_zero_report(
        self,
        document_store: SQLiteDocumentStore,
    ) -> None:
        """Empty store → zero counts."""
        detector = StalenessDetector(document_store, staleness_days=90)
        report = detector.check()

        assert report.total_documents == 0
        assert len(report.stale_documents) == 0
        assert len(report.missing_documents) == 0

    def test_fresh_documents_not_flagged(
        self,
        document_store: SQLiteDocumentStore,
    ) -> None:
        """Documents within threshold are not flagged."""
        document_store.put(None, "fresh content")

        detector = StalenessDetector(document_store, staleness_days=90)
        report = detector.check()

        assert report.total_documents == 1
        assert len(report.stale_documents) == 0
        assert len(report.missing_documents) == 0
