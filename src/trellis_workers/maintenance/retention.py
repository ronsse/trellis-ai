"""Maintenance workers — retention pruning, staleness detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel, utc_now
from trellis.extract.telemetry import emit_extraction_failure
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.trace import TraceStore

logger = structlog.get_logger(__name__)

#: Maximum fraction of scanned documents whose ``updated_at`` may fail to
#: parse before :class:`StalenessDetector` raises :class:`RetentionDriftError`.
#: 1% is ~10x the noise floor we'd accept from a healthy ingest pipeline;
#: anything above it is operator-visible drift, not transient bad data.
MALFORMED_DOCUMENT_THRESHOLD = 0.01


class RetentionDriftError(RuntimeError):
    """Raised when too many documents have unparseable ``updated_at`` strings.

    Surfaces what the previous silent ``except (ValueError, TypeError): pass``
    used to hide. The message embeds the malformed ratio and the first five
    offending document ids so operators have something to grep for in logs.
    """

    def __init__(
        self,
        *,
        malformed_count: int,
        total_documents: int,
        sample_doc_ids: list[str],
    ) -> None:
        ratio = malformed_count / max(total_documents, 1)
        sample = ", ".join(sample_doc_ids[:5]) or "<none>"
        message = (
            f"Retention drift: {malformed_count}/{total_documents} documents "
            f"({ratio:.2%}) have unparseable updated_at strings, exceeding "
            f"threshold {MALFORMED_DOCUMENT_THRESHOLD:.2%}. "
            f"First offending doc_ids: {sample}."
        )
        super().__init__(message)
        self.malformed_count = malformed_count
        self.total_documents = total_documents
        self.ratio = ratio
        self.sample_doc_ids = list(sample_doc_ids[:5])


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
                # AGGREGATE: per-trace failures are collected into
                # ``report.errors``; the caller inspects the returned
                # report after the loop so one bad trace doesn't halt
                # the rest of the retention pass.
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
    #: Documents whose ``updated_at`` string could not be parsed. Surfaced
    #: as a first-class report field (rather than silently dropped) so
    #: operators can see drift accumulating before it crosses
    #: :data:`MALFORMED_DOCUMENT_THRESHOLD` and starts raising.
    malformed_documents: list[str] = Field(default_factory=list)


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
        event_log: EventLog | None = None,
    ) -> None:
        self._document_store = document_store
        self._staleness_days = staleness_days
        self._event_log = event_log

    def check(self) -> StalenessReport:
        """Check for stale documents.

        Returns:
            StalenessReport with findings.

        Raises:
            RetentionDriftError: when more than
                :data:`MALFORMED_DOCUMENT_THRESHOLD` of scanned documents
                have unparseable ``updated_at`` strings — invisible drift
                that the operator must investigate.
        """
        report = StalenessReport()
        cutoff = utc_now() - timedelta(days=self._staleness_days)

        all_docs = self._document_store.list_documents(limit=10000)
        report.total_documents = len(all_docs)

        for doc in all_docs:
            # Use updated_at directly from list result instead of re-fetching
            updated_str = doc.get("updated_at")
            if not updated_str:
                continue
            doc_id = doc.get("doc_id", "<unknown>")
            try:
                updated_at = datetime.fromisoformat(updated_str)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                if updated_at < cutoff:
                    report.stale_documents.append(doc_id)
            except (ValueError, TypeError) as exc:
                # Surface the failure instead of swallowing it. The doc
                # is tracked on the report so the loop can decide if the
                # rate of failures has crossed the drift threshold.
                report.malformed_documents.append(doc_id)
                emit_extraction_failure(
                    event_log=self._event_log,
                    extractor_id="retention.staleness",
                    extractor_tier="deterministic",
                    failure_kind="parse_error",
                    source_hint="document_store.updated_at",
                    error_class=type(exc).__name__,
                    error_excerpt=(
                        f"doc_id={doc_id} updated_at={updated_str!r}: {exc}"
                    ),
                )
                logger.exception(
                    "staleness_check_malformed_updated_at",
                    doc_id=doc_id,
                    updated_at=updated_str,
                    error_class=type(exc).__name__,
                )

        logger.info(
            "staleness_check_complete",
            total=report.total_documents,
            stale=len(report.stale_documents),
            missing=len(report.missing_documents),
            malformed=len(report.malformed_documents),
        )

        malformed = len(report.malformed_documents)
        if malformed > 0:
            ratio = malformed / max(report.total_documents, 1)
            if ratio > MALFORMED_DOCUMENT_THRESHOLD:
                raise RetentionDriftError(
                    malformed_count=malformed,
                    total_documents=report.total_documents,
                    sample_doc_ids=report.malformed_documents,
                )

        return report
