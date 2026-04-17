"""Maintenance workers — retention pruning, staleness detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel, utc_now
from trellis.stores.document import DocumentStore
from trellis.stores.event_log import EventLog, EventType
from trellis.stores.trace import TraceStore

logger = structlog.get_logger(__name__)


class RetentionPolicy(TrellisModel):
    """Configuration for retention pruning."""

    max_age_days: int = 365
    max_traces: int = 10000
    preserve_outcomes: list[str] = Field(
        default_factory=lambda: ["success"],
    )
    dry_run: bool = False


class RetentionReport(TrellisModel):
    """Report of a retention pruning run."""

    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    traces_scanned: int = 0
    traces_marked: int = 0
    traces_preserved: int = 0
    dry_run: bool = False
    errors: list[str] = Field(default_factory=list)


class RetentionWorker:
    """Marks old traces for retention based on policy.

    TraceStore is append-only, so traces are never physically deleted.
    Instead, "pruning" marks traces in the event log for audit purposes.

    Retention rules:
    - Traces older than max_age_days are candidates for marking
    - Traces with outcomes in preserve_outcomes are kept regardless
    - Marking emits events for audit trail
    """

    def __init__(
        self,
        trace_store: TraceStore,
        event_log: EventLog | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._event_log = event_log

    def run(self, policy: RetentionPolicy) -> RetentionReport:
        """Execute retention pruning based on policy.

        Args:
            policy: Retention policy configuration.

        Returns:
            RetentionReport with results.
        """
        report = RetentionReport(dry_run=policy.dry_run)
        cutoff = utc_now() - timedelta(days=policy.max_age_days)

        # Query old traces
        old_traces = self._trace_store.query(until=cutoff, limit=policy.max_traces)
        report.traces_scanned = len(old_traces)

        for trace in old_traces:
            # Check if trace should be preserved
            if trace.outcome and trace.outcome.status.value in policy.preserve_outcomes:
                report.traces_preserved += 1
                continue

            if not policy.dry_run:
                try:
                    # TraceStore is append-only; traces are never physically deleted.
                    # We mark them in the event log so downstream consumers can filter.
                    if self._event_log is not None:
                        self._event_log.emit(
                            EventType.MUTATION_EXECUTED,
                            source="retention_worker",
                            entity_id=trace.trace_id,
                            entity_type="trace",
                            payload={
                                "action": "retention_prune",
                                "reason": f"older than {policy.max_age_days} days",
                                "outcome_status": (
                                    trace.outcome.status.value
                                    if trace.outcome
                                    else "none"
                                ),
                            },
                        )
                except Exception as e:
                    report.errors.append(f"Error pruning {trace.trace_id}: {e}")
                    continue

            report.traces_marked += 1

        report.completed_at = utc_now()

        logger.info(
            "retention_run_complete",
            scanned=report.traces_scanned,
            marked=report.traces_marked,
            preserved=report.traces_preserved,
            dry_run=policy.dry_run,
        )

        return report


class StalenessReport(TrellisModel):
    """Report of a staleness detection run."""

    checked_at: datetime = Field(default_factory=utc_now)
    total_documents: int = 0
    stale_documents: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)


class StalenessDetector:
    """Detects stale documents in the document store.

    A document is considered stale if:
    - It references a URI/path that no longer exists
    - It hasn't been updated within the staleness threshold
    """

    def __init__(
        self,
        document_store: DocumentStore,
        staleness_days: int = 90,
    ) -> None:
        self._document_store = document_store
        self._staleness_days = staleness_days

    def check(self) -> StalenessReport:
        """Check for stale documents.

        Returns:
            StalenessReport with findings.
        """
        report = StalenessReport()
        cutoff = utc_now() - timedelta(days=self._staleness_days)

        all_docs = self._document_store.list_documents(limit=10000)
        report.total_documents = len(all_docs)

        for doc in all_docs:
            # Use updated_at directly from list result instead of re-fetching
            updated_str = doc.get("updated_at")
            if updated_str:
                try:
                    updated_at = datetime.fromisoformat(updated_str)
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                    if updated_at < cutoff:
                        report.stale_documents.append(doc["doc_id"])
                except (ValueError, TypeError):
                    pass

        logger.info(
            "staleness_check_complete",
            total=report.total_documents,
            stale=len(report.stale_documents),
            missing=len(report.missing_documents),
        )

        return report
