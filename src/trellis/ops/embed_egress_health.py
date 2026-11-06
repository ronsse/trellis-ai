"""Documents the embed-on-write path kept away from the embedder, and why.

A stored document labelled above ``internal`` is not sent to an embedder that
``embeddings.local: true`` does not declare local
(:class:`trellis.llm.egress.EgressGate`). It is stored as usual and gets no
vector row, so keyword and graph retrieval still find it and only the semantic
axis cannot. The inline write has no report of its own, so the skip is an
event, :attr:`EventType.EMBED_WITHHELD`, and this module is its reader:
``trellis analyze health`` carries the count. It adds no probe.

The two verdicts are counted apart. ``withheld`` is a permanent fact about what
is stored (a label above ``internal``, a row that will not parse, an orphan
chunk) and is by design, so it is a count and not a warning. ``unreadable`` is
a store that could not answer; a later write may clear it, and it is a ``warn``
because nobody chose it.

A deployment that runs a local embedder without declaring it sees its labelled
documents here after upgrading: ``withheld`` is the line to read, and
``embeddings: {local: true}`` is the fix for documents written from then on.

The event carries a document id and a verdict, never content or a label.

**One document, one count.** A trace summary the trace-embed worker could not
judge is retried on every pass while its cursor is held, so the worker emits its
event once per :data:`UNREADABLE_REEMIT_AFTER` (a day) rather than once per
pass, and :func:`summarize_embed_egress` counts ``unreadable`` by distinct
document id, whatever the window. The key on both sides is the document id and
the verdict; the day keeps a still-held document inside any window of a day or
more, so it is neither inflated nor dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.core.reserved_doc_ids import is_trace_summary_doc_id
from trellis.llm.egress import EgressVerdict, egress_reason
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventLog,
    EventType,
    ScanCoverage,
    scan_events,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "UNREADABLE_REEMIT_AFTER",
    "EmbedEgressReport",
    "embed_egress_reasons",
    "emit_embed_withheld",
    "summarize_embed_egress",
]

#: Surface label on the event's ``source`` column.
EMBED_EGRESS_SURFACE = "embed:egress"

#: Ids named in one reason line. The full count is on the report.
_REASON_ID_CAP = 5

#: How long a worker that retries a document on every pass waits before it emits
#: that document's ``unreadable`` event again. Shorter than any health window a
#: caller reads, so a document still held stays visible; the reader counts
#: distinct documents, so the repeat is not a second one.
UNREADABLE_REEMIT_AFTER = timedelta(days=1)


def _already_recorded(
    event_log: EventLog, doc_id: str, verdict: EgressVerdict, within: timedelta
) -> bool:
    """Whether an event for this document and verdict is already on record."""
    return bool(
        event_log.get_events(
            event_type=EventType.EMBED_WITHHELD,
            entity_id=doc_id,
            since=datetime.now(tz=UTC) - within,
            payload_filters={"verdict": verdict.value},
            limit=1,
        )
    )


def emit_embed_withheld(
    event_log: EventLog | None,
    doc_id: str,
    verdict: EgressVerdict,
    *,
    source: str,
    once_within: timedelta | None = None,
) -> bool:
    """Emit one ``EMBED_WITHHELD`` row. Returns whether the skip is counted.

    Advisory and wrapped whole: it runs on the document write path, where one
    escaped exception fails the write, so no exception type is singled out.
    ``False`` tells the caller the skip is still uncounted (the caller's own
    return value carries the reason either way). The row holds the document id,
    the verdict and the surface that wrote it; never content.

    ``once_within`` is for a caller that sees the same document again on every
    pass: no row is written when one for this document and verdict is already
    on record within that long, and the answer is ``True`` because the skip is
    counted. An inline write leaves it unset, since each write is its own skip.
    """
    if event_log is None or verdict is EgressVerdict.PERMITTED:
        return False
    try:
        if once_within is not None and _already_recorded(
            event_log, doc_id, verdict, once_within
        ):
            return True
        event_log.emit(
            EventType.EMBED_WITHHELD,
            source=source or EMBED_EGRESS_SURFACE,
            entity_id=doc_id,
            entity_type="document",
            payload={"verdict": verdict.value, "reason": egress_reason(verdict)},
        )
    except Exception:
        logger.warning("embed_withheld_emit_failed", verdict=verdict.value)
        return False
    return True


class EmbedEgressReport(TrellisModel):
    """What the window's ``EMBED_WITHHELD`` events say about skipped embeds."""

    window_days: int
    #: Writes kept off the embedder for a label or a row that will not parse.
    #: By design, not a fault; the count is how an operator finds an undeclared
    #: local embedder.
    withheld: int = 0
    #: Documents kept off the embedder because a store could not be asked. A
    #: fault. One per document however many times it was retried in the window.
    unreadable: int = 0
    #: Distinct documents behind ``unreadable``, sorted, for the reason line.
    unreadable_docs: list[str] = Field(default_factory=list)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


def summarize_embed_egress(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> EmbedEgressReport:
    """Embeds withheld in the trailing *days*; ``unreadable`` counted apart."""
    scan = scan_events(
        event_log,
        event_type=EventType.EMBED_WITHHELD,
        since=datetime.now(tz=UTC) - timedelta(days=days),
        limit=limit,
    )
    report = EmbedEgressReport(window_days=days, scan=scan.coverage)
    unreadable_docs: set[str] = set()
    for event in scan.events:
        if event.payload.get("verdict") == EgressVerdict.UNREADABLE.value:
            # By document: a summary retried while its cursor is held is one
            # fault, not one per pass. A row that names no document counts alone.
            if not event.entity_id:
                report.unreadable += 1
            elif event.entity_id not in unreadable_docs:
                unreadable_docs.add(event.entity_id)
                report.unreadable += 1
        else:
            report.withheld += 1
    report.unreadable_docs = sorted(unreadable_docs)
    return report


def embed_egress_reasons(report: EmbedEgressReport) -> list[str]:
    """A ``warn`` reason for unreadable skips only; a withheld one is by design.

    Says how each kind of document gets its vector. A ``trace-summary:`` id is
    the worker's: the summary is stored, and the next ``worker embed-traces``
    pass retries the embed because its cursor is held. Any other document is
    embedded when it is next written, or by ``admin reindex-vectors``.
    """
    if not report.unreadable:
        return []
    shown = ", ".join(report.unreadable_docs[:_REASON_ID_CAP])
    extra = len(report.unreadable_docs) - _REASON_ID_CAP
    named = f"{shown} (+{extra} more)" if extra > 0 else shown
    summaries = any(is_trace_summary_doc_id(doc) for doc in report.unreadable_docs)
    others = any(not is_trace_summary_doc_id(doc) for doc in report.unreadable_docs)
    # An event that names no document counts, and offers no id to classify.
    remedy = []
    if summaries:
        remedy.append(
            "a trace-summary: id is retried by the next worker embed-traces pass"
        )
    if others or not report.unreadable_docs:
        remedy.append(
            "any other document is embedded when it is rewritten or by "
            "admin reindex-vectors"
        )
    return [
        (
            f"{report.unreadable} document(s) were kept off the embedder because a "
            "store could not be asked for the label (egress_unreadable) and "
            f"have no vector row yet ({'; '.join(remedy)}): {named}"
        )
    ]
