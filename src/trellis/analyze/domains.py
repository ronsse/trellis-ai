"""Domain usage report — read-only join of observed ``domain`` values.

``domain`` is the primary retrieval slice, yet a deployment has no built-in
visibility into which domains actually exist in its data. This module joins the
three places a domain value surfaces:

* :attr:`trellis.schemas.trace.TraceContext.domain` (TraceStore),
* ``ContentTags.domain`` / flat ``domain`` in document metadata (DocumentStore),
* pack + feedback events (EventLog), grouped by the pack payload's ``domain``.

The output is the empirical substrate a human needs to decide domain slices.

**Out of scope (deliberately not built here):** automatic domain
discovery / clustering and a domain *promotion* analyzer. Those follow the
column-leaf pattern — contract first, implementation gated on production
telemetry (see ``docs/design/adr-column-leaf-modeling-guardrails.md`` and
``docs/design/adr-autonomy-ladder.md`` tier 2). If this report proves
valuable, a future ADR amendment defines the analyzer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.learning.pack_observations import join_pack_feedback

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.trace import TraceStore

logger = structlog.get_logger(__name__)

#: Sentinel key for items / traces / packs that carry no domain. Surfaced as a
#: row so coverage gaps are visible rather than silently dropped.
NO_DOMAIN_KEY = "(none)"

#: Default scan limit for trace + document listing and event windows.
_DEFAULT_SCAN_LIMIT = 1000


@dataclass
class DomainUsage:
    """Per-domain usage tally across traces, documents, and pack feedback."""

    domain: str
    document_count: int = 0
    trace_count: int = 0
    packs_served: int = 0
    graded_packs: int = 0
    graded_successes: int = 0

    @property
    def success_rate(self) -> float | None:
        """Fraction of graded packs that succeeded, or ``None`` if none graded."""
        if self.graded_packs == 0:
            return None
        return self.graded_successes / self.graded_packs

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "document_count": self.document_count,
            "trace_count": self.trace_count,
            "packs_served": self.packs_served,
            "graded_packs": self.graded_packs,
            "graded_successes": self.graded_successes,
            "success_rate": self.success_rate,
        }


@dataclass
class DomainReport:
    """Aggregate domain usage report."""

    days: int
    domains: list[DomainUsage] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "days": self.days,
            "domains": [d.to_dict() for d in self.domains],
        }


def _document_domains(metadata: Mapping[str, Any]) -> list[str]:
    """Extract domain value(s) from one document's metadata.

    Prefers the canonical ``content_tags.domain`` facet (a list, written by the
    classification pipeline). Falls back to a flat ``metadata['domain']``
    (string or list) used by simpler ingest paths. Returns an empty list when
    no domain is present so the caller can attribute it to ``(none)``.
    """
    content_tags = metadata.get("content_tags")
    if isinstance(content_tags, Mapping):
        raw = content_tags.get("domain")
        if isinstance(raw, list):
            return [str(d) for d in raw if str(d)]
        if isinstance(raw, str) and raw:
            return [raw]
    flat = metadata.get("domain")
    if isinstance(flat, list):
        return [str(d) for d in flat if str(d)]
    if isinstance(flat, str) and flat:
        return [flat]
    return []


def analyze_domains(
    trace_store: TraceStore,
    document_store: DocumentStore,
    event_log: EventLog,
    *,
    days: int = 30,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> DomainReport:
    """Build the per-domain usage report across the three sources.

    Args:
        trace_store: Source for :attr:`TraceContext.domain` tallies.
        document_store: Source for document ``domain`` tallies.
        event_log: Source for pack-served / graded-pack / success tallies,
            windowed to the last ``days``.
        days: Event look-back window for pack + feedback events.
        scan_limit: Max traces, documents, and per-event-type events to scan.

    Returns:
        A :class:`DomainReport`. Every observed domain gets one row; items,
        traces, and packs with no domain are attributed to :data:`NO_DOMAIN_KEY`
        so coverage gaps stay visible.
    """
    usage: dict[str, DomainUsage] = {}

    def _row(domain: str) -> DomainUsage:
        if domain not in usage:
            usage[domain] = DomainUsage(domain=domain)
        return usage[domain]

    # 1. Traces — TraceContext.domain.
    for trace in trace_store.query(limit=scan_limit):
        domain = (trace.context.domain if trace.context else None) or NO_DOMAIN_KEY
        _row(domain).trace_count += 1

    # 2. Documents — ContentTags.domain (or flat domain) in metadata. A
    #    multi-domain document counts once per domain it carries; a document
    #    with no domain counts once under (none).
    offset = 0
    while True:
        batch = document_store.list_documents(limit=scan_limit, offset=offset)
        if not batch:
            break
        for doc in batch:
            domains = _document_domains(doc.get("metadata") or {})
            if not domains:
                _row(NO_DOMAIN_KEY).document_count += 1
            else:
                for domain in domains:
                    _row(domain).document_count += 1
        if len(batch) < scan_limit:
            break
        offset += scan_limit

    # 3. Pack + feedback events — grouped by the pack payload's domain. Reuse
    #    the shared join so semantics can't drift from the learning loop.
    since = datetime.now(tz=UTC) - timedelta(days=days)
    feedback_events, pack_payloads = join_pack_feedback(
        event_log, since=since, limit=scan_limit
    )

    for payload in pack_payloads.values():
        domain = str(payload.get("domain") or "").strip() or NO_DOMAIN_KEY
        _row(domain).packs_served += 1

    for event in feedback_events:
        payload = event.payload or {}
        pack_id = str(payload.get("pack_id") or "").strip()
        pack_payload = pack_payloads.get(pack_id) if pack_id else None
        if pack_payload is None:
            # No matching assembly event → can't attribute a domain. Skip
            # rather than guess; the pack-served tally already counts only
            # assemblies, so unmatched feedback is genuinely unattributable.
            continue
        domain = str(pack_payload.get("domain") or "").strip() or NO_DOMAIN_KEY
        row = _row(domain)
        row.graded_packs += 1
        if bool(payload.get("success")):
            row.graded_successes += 1

    report = DomainReport(
        days=days,
        domains=sorted(
            usage.values(),
            key=lambda u: (-u.document_count, -u.trace_count, u.domain),
        ),
    )
    logger.debug(
        "domain_report_built",
        domains=len(report.domains),
        days=days,
    )
    return report


__all__ = [
    "NO_DOMAIN_KEY",
    "DomainReport",
    "DomainUsage",
    "analyze_domains",
]
