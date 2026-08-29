"""Sectioned pack analysis — audit section composition across recent packs.

Reads ``PACK_ASSEMBLED`` events with ``entity_type="sectioned_pack"`` and
aggregates per-section statistics: how often each section fires, how many
items it delivers, its empty rate, and which sections consistently come up
empty.  Use this to spot badly scoped section configurations before they
silently degrade the agent's context.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import (
    EventLog,
    EventType,
    ScanCoverage,
    scan_events,
)

#: Sections whose empty rate is at or above this threshold are flagged.
EMPTY_RATE_FLAG_THRESHOLD = 0.5


class SectionStats(TrellisModel):
    """Aggregate statistics for one section name across many packs."""

    name: str
    packs_count: int
    total_items: int
    empty_count: int
    unique_items: int

    @property
    def empty_rate(self) -> float:
        return self.empty_count / self.packs_count if self.packs_count else 0.0

    @property
    def avg_items(self) -> float:
        return self.total_items / self.packs_count if self.packs_count else 0.0


class PackSectionsReport(TrellisModel):
    """Report on sectioned pack composition."""

    total_sectioned_packs: int
    section_stats: list[SectionStats]
    empty_section_flags: list[str]
    #: Whether the ``PACK_ASSEMBLED`` read behind these stats hit its cap,
    #: and what that excluded (#374). Note the cap is applied to *all*
    #: pack events, then sectioned packs are filtered out of the result —
    #: so on a deployment where sectioned packs are a minority, truncation
    #: shrinks this report far more than ``limit`` suggests.
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


def analyze_pack_sections(
    event_log: EventLog,
    *,
    days: int = 30,
    empty_rate_threshold: float = EMPTY_RATE_FLAG_THRESHOLD,
    limit: int = 1000,
) -> PackSectionsReport:
    """Analyze sectioned pack composition over the given window.

    Args:
        event_log: Where to read ``PACK_ASSEMBLED`` events.
        days: How many days of history to analyze.
        empty_rate_threshold: Sections whose empty rate meets or exceeds
            this value are listed in ``empty_section_flags``.
        limit: Max events to read. The read goes through
            :func:`~trellis.stores.base.event_log.scan_events`, so hitting
            the cap keeps the **newest** packs and says so in
            ``report.scan`` (#374). Every aggregate below is an unordered
            count or set union, so the change is confined to *which* packs
            are dropped.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)
    scan = scan_events(
        event_log, event_type=EventType.PACK_ASSEMBLED, since=since, limit=limit
    )

    sectioned = [
        e
        for e in scan.events
        if e.entity_type == "sectioned_pack" or e.payload.get("sections")
    ]

    packs_count: dict[str, int] = defaultdict(int)
    total_items: dict[str, int] = defaultdict(int)
    empty_count: dict[str, int] = defaultdict(int)
    unique_items: dict[str, set[str]] = defaultdict(set)

    for event in sectioned:
        for section in event.payload.get("sections", []) or []:
            name = section.get("name")
            if not name:
                continue
            items_count = int(section.get("items_count", 0))
            item_ids = section.get("item_ids", []) or []
            packs_count[name] += 1
            total_items[name] += items_count
            if items_count == 0:
                empty_count[name] += 1
            unique_items[name].update(item_ids)

    stats = [
        SectionStats(
            name=name,
            packs_count=packs_count[name],
            total_items=total_items[name],
            empty_count=empty_count[name],
            unique_items=len(unique_items[name]),
        )
        for name in sorted(packs_count)
    ]
    stats.sort(key=lambda s: s.packs_count, reverse=True)

    flags = [s.name for s in stats if s.empty_rate >= empty_rate_threshold]

    return PackSectionsReport(
        total_sectioned_packs=len(sectioned),
        section_stats=stats,
        empty_section_flags=flags,
        scan=scan.coverage,
    )
