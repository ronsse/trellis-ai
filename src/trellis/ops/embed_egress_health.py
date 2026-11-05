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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
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
    "EmbedEgressReport",
    "embed_egress_reasons",
    "emit_embed_withheld",
    "summarize_embed_egress",
]

#: Surface label on the event's ``source`` column.
EMBED_EGRESS_SURFACE = "embed:egress"

#: Ids named in one reason line. The full count is on the report.
_REASON_ID_CAP = 5


def emit_embed_withheld(
    event_log: EventLog | None,
    doc_id: str,
    verdict: EgressVerdict,
    *,
    source: str,
) -> bool:
    """Emit one ``EMBED_WITHHELD`` row. Returns whether it landed.

    Advisory and wrapped whole: it runs on the document write path, where one
    escaped exception fails the write, so no exception type is singled out.
    ``False`` tells the caller the skip is still uncounted (the caller's own
    return value carries the reason either way). The row holds the document id,
    the verdict and the surface that wrote it; never content.
    """
    if event_log is None or verdict is EgressVerdict.PERMITTED:
        return False
    try:
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
    #: Writes kept off the embedder because a store could not be asked. A fault.
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
            report.unreadable += 1
            if event.entity_id:
                unreadable_docs.add(event.entity_id)
        else:
            report.withheld += 1
    report.unreadable_docs = sorted(unreadable_docs)
    return report


def embed_egress_reasons(report: EmbedEgressReport) -> list[str]:
    """A ``warn`` reason for unreadable skips only; a withheld one is by design."""
    if not report.unreadable:
        return []
    shown = ", ".join(report.unreadable_docs[:_REASON_ID_CAP])
    extra = len(report.unreadable_docs) - _REASON_ID_CAP
    named = f"{shown} (+{extra} more)" if extra > 0 else shown
    return [
        (
            f"{report.unreadable} embed(s) were kept off the embedder because a "
            "document store could not be asked for the label (egress_unreadable), "
            f"so those documents have no vector row until rewritten: {named}"
        )
    ]
