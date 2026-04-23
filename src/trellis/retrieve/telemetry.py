"""Pack assembly telemetry — consume rejection / budget / strategy signals.

Closes Gap 3.4: :attr:`~trellis.stores.base.event_log.EventType.PACK_ASSEMBLED`
payloads already carry ``rejected_items``, ``budget_trace``, and
``strategies_used`` but no consumer reads them. Without this analyzer,
operators can't tell whether budget is constantly saturating (ranking or
budget problem), which rejection reasons dominate (filter-chain imbalance),
or which strategies actually earn their keep.

This is a read-only analytics pass. It does not propose parameter changes
or mutate tuner state — consumers decide what to do with the signals. A
future tuning rule can read the same aggregates to propose budget
adjustments, but that wiring is intentionally separate.
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

_TELEMETRY_EVENT_LIMIT = 5000

#: Rejection reasons emitted by :class:`~trellis.retrieve.pack_builder.PackBuilder`.
#: Listed here as a documented surface — analyzer treats any string the payload
#: carries, so new reasons are reported without a code change.
KNOWN_REJECTION_REASONS = (
    "dedup",
    "structural_filter",
    "max_items",
    "token_budget",
    "session_dedup",
    "semantic_dedup",
)

#: Finding thresholds — operator-facing, not tuning knobs.
_HIGH_BUDGET_HIT_RATE = 0.5
_LOW_STRATEGY_YIELD = 0.25
_MIN_STRATEGY_SAMPLES = 20


class StrategyContribution(TrellisModel):
    """Per-strategy yield stats across the analysis window."""

    strategy: str
    injected: int
    rejected: int
    yield_rate: float
    top_rejection_reasons: list[tuple[str, int]] = Field(default_factory=list)


class PackTelemetryReport(TrellisModel):
    """Aggregated pack-assembly telemetry over an analysis window."""

    total_packs: int
    total_injected_items: int
    total_rejected_items: int
    max_items_hit_rate: float
    max_tokens_hit_rate: float
    any_budget_hit_rate: float
    mean_items_per_pack: float
    mean_rejected_per_pack: float
    rejection_reason_counts: dict[str, int] = Field(default_factory=dict)
    rejection_reason_rates: dict[str, float] = Field(default_factory=dict)
    strategy_contributions: list[StrategyContribution] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def analyze_pack_telemetry(  # noqa: PLR0915
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _TELEMETRY_EVENT_LIMIT,
) -> PackTelemetryReport:
    """Aggregate rejection, budget, and strategy signals from PACK_ASSEMBLED.

    Parameters
    ----------
    event_log:
        The event log to read.
    days:
        Window of history to analyze.
    limit:
        Maximum number of ``PACK_ASSEMBLED`` events to scan. Defaults to
        5000 — enough to cover a month of light-traffic deployments
        without pulling unbounded memory. Bump when window exceeds capacity.

    Returns
    -------
    PackTelemetryReport
        Aggregated counts and rates. Empty window → zero-valued report with
        a descriptive note rather than raising.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    since = datetime.now(tz=UTC) - timedelta(days=days)
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        limit=limit,
    )

    total_packs = len(events)
    total_injected = 0
    total_rejected = 0
    packs_hit_max_items = 0
    packs_hit_max_tokens = 0
    packs_hit_any_budget = 0
    rejection_counts: Counter[str] = Counter()
    #: strategy -> {"injected": int, "rejected": int, "reasons": Counter}
    per_strategy: dict[str, dict] = defaultdict(
        lambda: {"injected": 0, "rejected": 0, "reasons": Counter()}
    )

    for event in events:
        payload = event.payload or {}
        injected_items = payload.get("injected_items") or []
        rejected_items = payload.get("rejected_items") or []

        total_injected += len(injected_items)
        total_rejected += len(rejected_items)

        for item in injected_items:
            strategy = item.get("strategy_source") or "unknown"
            per_strategy[strategy]["injected"] += 1

        hit_max_items = False
        hit_max_tokens = False
        for rej in rejected_items:
            reason = rej.get("reason") or "unknown"
            rejection_counts[reason] += 1
            strategy = rej.get("strategy_source") or "unknown"
            per_strategy[strategy]["rejected"] += 1
            per_strategy[strategy]["reasons"][reason] += 1
            if reason == "max_items":
                hit_max_items = True
            elif reason == "token_budget":
                hit_max_tokens = True
        if hit_max_items:
            packs_hit_max_items += 1
        if hit_max_tokens:
            packs_hit_max_tokens += 1
        if hit_max_items or hit_max_tokens:
            packs_hit_any_budget += 1

    max_items_hit_rate = packs_hit_max_items / total_packs if total_packs else 0.0
    max_tokens_hit_rate = packs_hit_max_tokens / total_packs if total_packs else 0.0
    any_budget_hit_rate = packs_hit_any_budget / total_packs if total_packs else 0.0
    mean_items = total_injected / total_packs if total_packs else 0.0
    mean_rejected = total_rejected / total_packs if total_packs else 0.0

    total_rejections_with_reason = sum(rejection_counts.values())
    if total_rejections_with_reason:
        rejection_rates = {
            r: count / total_rejections_with_reason
            for r, count in rejection_counts.items()
        }
    else:
        rejection_rates = {}

    contributions: list[StrategyContribution] = []
    for strategy, stats in sorted(per_strategy.items()):
        seen = stats["injected"] + stats["rejected"]
        yield_rate = stats["injected"] / seen if seen else 0.0
        contributions.append(
            StrategyContribution(
                strategy=strategy,
                injected=stats["injected"],
                rejected=stats["rejected"],
                yield_rate=yield_rate,
                top_rejection_reasons=list(stats["reasons"].most_common(3)),
            )
        )

    findings = _build_findings(
        total_packs=total_packs,
        max_items_hit_rate=max_items_hit_rate,
        max_tokens_hit_rate=max_tokens_hit_rate,
        contributions=contributions,
    )
    notes: list[str] = []
    if total_packs == 0:
        notes.append(
            "No PACK_ASSEMBLED events in this window. Either the window is "
            "too narrow, or no PackBuilder instance is emitting telemetry."
        )

    report = PackTelemetryReport(
        total_packs=total_packs,
        total_injected_items=total_injected,
        total_rejected_items=total_rejected,
        max_items_hit_rate=max_items_hit_rate,
        max_tokens_hit_rate=max_tokens_hit_rate,
        any_budget_hit_rate=any_budget_hit_rate,
        mean_items_per_pack=mean_items,
        mean_rejected_per_pack=mean_rejected,
        rejection_reason_counts=dict(rejection_counts),
        rejection_reason_rates=rejection_rates,
        strategy_contributions=contributions,
        findings=findings,
        notes=notes,
    )
    logger.info(
        "pack_telemetry_analyzed",
        days=days,
        total_packs=total_packs,
        any_budget_hit_rate=any_budget_hit_rate,
    )
    return report


def _build_findings(
    *,
    total_packs: int,
    max_items_hit_rate: float,
    max_tokens_hit_rate: float,
    contributions: list[StrategyContribution],
) -> list[str]:
    findings: list[str] = []
    if total_packs == 0:
        return findings
    if max_items_hit_rate >= _HIGH_BUDGET_HIT_RATE:
        findings.append(
            f"budget: {max_items_hit_rate:.0%} of packs hit `max_items` — "
            "consider raising the item budget or tightening retrieval ranking "
            "so fewer strong candidates get rejected."
        )
    if max_tokens_hit_rate >= _HIGH_BUDGET_HIT_RATE:
        findings.append(
            f"budget: {max_tokens_hit_rate:.0%} of packs hit `token_budget` — "
            "agents are getting budget-truncated; raise `max_tokens` or trim "
            "long excerpts upstream."
        )
    for entry in contributions:
        seen = entry.injected + entry.rejected
        if (
            seen >= _MIN_STRATEGY_SAMPLES
            and entry.yield_rate < _LOW_STRATEGY_YIELD
            and entry.strategy != "unknown"
        ):
            findings.append(
                f"strategy: `{entry.strategy}` yields {entry.yield_rate:.0%} "
                f"(injected {entry.injected} / {seen} seen) — investigate "
                "whether its candidates are getting filtered out too aggressively "
                "or the strategy needs tuning."
            )
    return findings


__all__ = [
    "KNOWN_REJECTION_REASONS",
    "PackTelemetryReport",
    "StrategyContribution",
    "analyze_pack_telemetry",
]
