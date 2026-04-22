"""Extractor fallback telemetry — consume EXTRACTOR_FALLBACK events.

Closes Gap 4.3 by turning the per-dispatch fallback events emitted by
:class:`~trellis.extract.dispatcher.ExtractionDispatcher` into an operator-
readable report. Two fallback signals are tracked today:

* ``prefer_tier_override`` — caller forced a lower-priority tier.
* ``empty_result`` — chosen extractor ran but produced no drafts. Strongest
  single graduation signal ("deterministic silently fails for this source
  → promote to hybrid / LLM").

Per-source aggregates let consumers ask "where do rules keep losing?" and
"is this source stable enough to graduate down a tier?" without re-running
the dispatcher. The report is read-only — proposing tier changes or
retiring extractors is left to operators (and, later, a dedicated tuning
rule that watches these aggregates).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_FALLBACK_EVENT_LIMIT = 5000
_DISPATCH_EVENT_LIMIT = 5000

#: Fraction of dispatches on a single source_hint that must fall back before
#: the source is flagged in findings. Deliberately high — we want a loud
#: signal, not alerts for every occasional override.
_HIGH_FALLBACK_RATE = 0.5
_MIN_SOURCE_SAMPLES = 10


class SourceFallbackStats(TrellisModel):
    """Per-source-hint fallback aggregates."""

    source_hint: str
    total_dispatches: int
    fallback_events: int
    fallback_rate: float
    reasons: dict[str, int] = Field(default_factory=dict)
    chosen_tiers: dict[str, int] = Field(default_factory=dict)


class ExtractorFallbackReport(TrellisModel):
    """Aggregated extractor-fallback telemetry over an analysis window."""

    total_dispatches: int
    total_fallbacks: int
    overall_fallback_rate: float
    reason_counts: dict[str, int] = Field(default_factory=dict)
    per_source: list[SourceFallbackStats] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def analyze_extractor_fallbacks(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = _FALLBACK_EVENT_LIMIT,
) -> ExtractorFallbackReport:
    """Summarize extractor-fallback telemetry since ``days`` ago.

    Reads ``EXTRACTOR_FALLBACK`` and ``EXTRACTION_DISPATCHED`` events to
    compute an overall fallback rate and per-source aggregates. When no
    dispatch events exist in the window, returns a zero-valued report with
    a descriptive note rather than raising.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    since = datetime.now(tz=UTC) - timedelta(days=days)
    fallback_events = event_log.get_events(
        event_type=EventType.EXTRACTOR_FALLBACK,
        since=since,
        limit=limit,
    )
    dispatch_events = event_log.get_events(
        event_type=EventType.EXTRACTION_DISPATCHED,
        since=since,
        limit=_DISPATCH_EVENT_LIMIT,
    )

    total_fallbacks = len(fallback_events)
    total_dispatches = len(dispatch_events)

    reason_counts: Counter[str] = Counter()
    per_source_dispatches: Counter[str] = Counter()
    #: source_hint -> {"fallbacks": int, "reasons": Counter, "chosen_tiers": Counter}
    per_source_fallback: dict[str, dict] = defaultdict(
        lambda: {"fallbacks": 0, "reasons": Counter(), "chosen_tiers": Counter()}
    )

    for event in dispatch_events:
        source_hint = (event.payload or {}).get("source_hint") or "<none>"
        per_source_dispatches[source_hint] += 1

    for event in fallback_events:
        payload = event.payload or {}
        reason = payload.get("reason") or "unknown"
        source_hint = payload.get("source_hint") or "<none>"
        chosen_tier = payload.get("chosen_tier") or "unknown"
        reason_counts[reason] += 1
        entry = per_source_fallback[source_hint]
        entry["fallbacks"] += 1
        entry["reasons"][reason] += 1
        entry["chosen_tiers"][chosen_tier] += 1

    per_source: list[SourceFallbackStats] = []
    all_sources = set(per_source_dispatches) | set(per_source_fallback)
    for source_hint in sorted(all_sources):
        dispatches = per_source_dispatches.get(source_hint, 0)
        fallbacks = per_source_fallback.get(source_hint, {}).get("fallbacks", 0)
        rate = fallbacks / dispatches if dispatches else 0.0
        reasons = dict(per_source_fallback.get(source_hint, {}).get("reasons") or {})
        chosen_tiers = dict(
            per_source_fallback.get(source_hint, {}).get("chosen_tiers") or {}
        )
        per_source.append(
            SourceFallbackStats(
                source_hint=source_hint,
                total_dispatches=dispatches,
                fallback_events=fallbacks,
                fallback_rate=rate,
                reasons=reasons,
                chosen_tiers=chosen_tiers,
            )
        )

    overall_rate = total_fallbacks / total_dispatches if total_dispatches else 0.0

    findings = _build_findings(per_source)
    notes: list[str] = []
    if total_dispatches == 0:
        notes.append(
            "No EXTRACTION_DISPATCHED events in this window. Either the "
            "dispatcher is unused or no ``event_log`` is wired — check "
            "``ExtractionDispatcher(event_log=...)`` at construction."
        )

    report = ExtractorFallbackReport(
        total_dispatches=total_dispatches,
        total_fallbacks=total_fallbacks,
        overall_fallback_rate=overall_rate,
        reason_counts=dict(reason_counts),
        per_source=per_source,
        findings=findings,
        notes=notes,
    )
    logger.info(
        "extractor_fallbacks_analyzed",
        days=days,
        total_dispatches=total_dispatches,
        total_fallbacks=total_fallbacks,
        overall_rate=overall_rate,
    )
    return report


def _build_findings(per_source: list[SourceFallbackStats]) -> list[str]:
    findings: list[str] = []
    for stats in per_source:
        if stats.total_dispatches < _MIN_SOURCE_SAMPLES:
            continue
        if stats.fallback_rate < _HIGH_FALLBACK_RATE:
            continue
        top_reason = max(stats.reasons.items(), key=lambda kv: kv[1])[0]
        if top_reason == "empty_result":
            findings.append(
                f"source `{stats.source_hint}` falls back "
                f"{stats.fallback_rate:.0%} of dispatches ({stats.fallback_events}"
                f" of {stats.total_dispatches}) with top reason `empty_result` — "
                "the chosen extractor keeps producing no drafts; candidate "
                "for graduation (register a hybrid/LLM extractor, or "
                "retire the rule set)."
            )
        elif top_reason == "prefer_tier_override":
            findings.append(
                f"source `{stats.source_hint}` overrides to a lower tier "
                f"{stats.fallback_rate:.0%} of the time — either the default "
                "priority is wrong for this source, or callers are routinely "
                "opting out of deterministic. Worth auditing the callsite."
            )
        else:
            findings.append(
                f"source `{stats.source_hint}` falls back "
                f"{stats.fallback_rate:.0%} (top reason: {top_reason})."
            )
    return findings


__all__ = [
    "ExtractorFallbackReport",
    "SourceFallbackStats",
    "analyze_extractor_fallbacks",
]
