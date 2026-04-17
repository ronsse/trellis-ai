"""Advisory generator — deterministic analysis of outcome data.

Generates :class:`Advisory` objects by analyzing the correlation between
pack contents and task outcomes.  All analysis is statistical — no LLM
is used.  Advisory text is template-generated from findings.

The five analysis methods correspond to the ADR categories:

1. **Entity correlation** — entities disproportionately in successes
2. **Step-pattern mining** — trace step patterns in successes vs failures
3. **Scope analysis** — pack breadth correlation with outcome
4. **Anti-pattern detection** — patterns disproportionately in failures
5. **Query improvement** — query terms that lead to high-scoring packs
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trellis.core.base import TrellisModel
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)

# --- Thresholds ---
_MIN_SAMPLE_SIZE = 5
_MIN_EFFECT_SIZE = 0.15
_SUCCESS_RATING_THRESHOLD = 0.5
_CONFIDENCE_SCALE = 0.1  # multiplied by sample_size to cap at 1.0
_MIN_WORD_LENGTH = 3
_SCOPE_SMALL = 5
_SCOPE_LARGE = 15
_MIN_BIN_COUNT = 2


class AdvisoryReport(TrellisModel):
    """Summary of an advisory generation run."""

    advisories_generated: int
    advisories_stored: int
    total_packs: int
    total_feedback: int
    analysis_window_days: int


class AdvisoryGenerator:
    """Generate advisories from outcome data.

    Usage::

        generator = AdvisoryGenerator(event_log, advisory_store)
        report = generator.generate(days=30)
    """

    def __init__(
        self,
        event_log: EventLog,
        advisory_store: AdvisoryStore,
        *,
        min_sample_size: int = _MIN_SAMPLE_SIZE,
        min_effect_size: float = _MIN_EFFECT_SIZE,
    ) -> None:
        self._event_log = event_log
        self._advisory_store = advisory_store
        self._min_sample_size = min_sample_size
        self._min_effect_size = min_effect_size

    def generate(self, *, days: int = 30) -> AdvisoryReport:
        """Run all analysis methods and store resulting advisories.

        Returns an :class:`AdvisoryReport` summarising what was generated.
        """
        since = datetime.now(tz=UTC) - timedelta(days=days)

        # Gather raw data
        pack_events = self._event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED,
            since=since,
            limit=5000,
        )
        feedback_events = self._event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED,
            since=since,
            limit=5000,
        )

        # Build joined dataset
        packs = self._join_packs_feedback(pack_events, feedback_events)

        if not packs:
            return AdvisoryReport(
                advisories_generated=0,
                advisories_stored=0,
                total_packs=len(pack_events),
                total_feedback=len(feedback_events),
                analysis_window_days=days,
            )

        # Run all five analysis methods
        advisories: list[Advisory] = []
        advisories.extend(self._entity_correlation(packs))
        advisories.extend(self._strategy_correlation(packs))
        advisories.extend(self._scope_analysis(packs))
        advisories.extend(self._anti_pattern_detection(packs))
        advisories.extend(self._query_improvement(packs))

        # Store results (replaces previous advisories for same scope)
        stored = 0
        if advisories:
            stored = self._advisory_store.put_many(advisories)

        logger.info(
            "advisories_generated",
            count=len(advisories),
            stored=stored,
            packs_analyzed=len(packs),
        )

        return AdvisoryReport(
            advisories_generated=len(advisories),
            advisories_stored=stored,
            total_packs=len(pack_events),
            total_feedback=len(feedback_events),
            analysis_window_days=days,
        )

    # --- Data preparation ---

    @staticmethod
    def _join_packs_feedback(
        pack_events: list[Any],
        feedback_events: list[Any],
    ) -> list[dict[str, Any]]:
        """Join PACK_ASSEMBLED with FEEDBACK_RECORDED into analysis rows."""
        # Build pack_id → payload mapping
        pack_data: dict[str, dict[str, Any]] = {}
        for event in pack_events:
            pack_id = event.entity_id
            if pack_id:
                pack_data[pack_id] = event.payload

        # Build pack_id → success mapping
        pack_success: dict[str, bool] = {}
        for event in feedback_events:
            pack_id = event.payload.get("pack_id") or event.entity_id
            if pack_id and pack_id in pack_data:
                rating = event.payload.get("rating", 0.0)
                pack_success[pack_id] = event.payload.get(
                    "success", rating >= _SUCCESS_RATING_THRESHOLD
                )

        # Join into analysis rows
        rows: list[dict[str, Any]] = []
        for pack_id, payload in pack_data.items():
            if pack_id not in pack_success:
                continue
            rows.append(
                {
                    "pack_id": pack_id,
                    "success": pack_success[pack_id],
                    "item_ids": payload.get("injected_item_ids", []),
                    "items": payload.get("injected_items", []),
                    "strategies": payload.get("strategies_used", []),
                    "domain": payload.get("domain", "global"),
                    "intent": payload.get("intent", ""),
                    "rejected": payload.get("rejected_items", []),
                    "budget_trace": payload.get("budget_trace", []),
                }
            )
        return rows

    # --- Analysis methods ---

    def _entity_correlation(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find entities disproportionately present in successful packs."""
        # Count per-item appearances in success/failure
        item_success: dict[str, int] = defaultdict(int)
        item_failure: dict[str, int] = defaultdict(int)

        for pack in packs:
            for item_id in pack["item_ids"]:
                if pack["success"]:
                    item_success[item_id] += 1
                else:
                    item_failure[item_id] += 1

        all_items = set(item_success) | set(item_failure)
        total_success = sum(1 for p in packs if p["success"])

        advisories: list[Advisory] = []
        for item_id in all_items:
            s = item_success.get(item_id, 0)
            f = item_failure.get(item_id, 0)
            total = s + f
            if total < self._min_sample_size:
                continue

            rate_with = s / total if total > 0 else 0.0
            # Rate without: success rate of packs that don't contain item
            packs_without = len(packs) - total
            success_without = total_success - s
            rate_without = success_without / packs_without if packs_without > 0 else 0.0
            effect = rate_with - rate_without

            if effect < self._min_effect_size:
                continue

            # Determine domain scope from packs containing this item
            domains = {p["domain"] for p in packs if item_id in p["item_ids"]}
            scope = domains.pop() if len(domains) == 1 else "global"

            confidence = self._compute_confidence(total, effect)
            traces = [
                p["pack_id"] for p in packs if item_id in p["item_ids"] and p["success"]
            ][:5]

            advisories.append(
                Advisory(
                    category=AdvisoryCategory.ENTITY,
                    confidence=confidence,
                    message=(
                        f"Entity {item_id} appears in"
                        f" {rate_with:.0%} of successful packs"
                        f" (n={total}, effect=+{effect:.0%})."
                        f" Consider including it."
                    ),
                    evidence=AdvisoryEvidence(
                        sample_size=total,
                        success_rate_with=round(rate_with, 3),
                        success_rate_without=round(rate_without, 3),
                        effect_size=round(effect, 3),
                        representative_trace_ids=traces,
                    ),
                    scope=scope,
                    entity_id=item_id,
                )
            )

        return advisories

    def _strategy_correlation(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find strategies disproportionately present in successful packs.

        This is a proxy for step-pattern mining (ADR method 2) using the
        strategy_source data from Phase 1's decision trail.
        """
        strategy_success: dict[str, int] = defaultdict(int)
        strategy_failure: dict[str, int] = defaultdict(int)

        for pack in packs:
            strategies_in_pack: set[str] = set()
            for item in pack.get("items", []):
                src = item.get("strategy_source")
                if src:
                    strategies_in_pack.add(src)

            for strategy in strategies_in_pack:
                if pack["success"]:
                    strategy_success[strategy] += 1
                else:
                    strategy_failure[strategy] += 1

        total_success = sum(1 for p in packs if p["success"])

        advisories: list[Advisory] = []
        for strategy in set(strategy_success) | set(strategy_failure):
            s = strategy_success.get(strategy, 0)
            f = strategy_failure.get(strategy, 0)
            total = s + f
            if total < self._min_sample_size:
                continue

            rate_with = s / total if total > 0 else 0.0
            packs_without = len(packs) - total
            success_without = total_success - s
            rate_without = success_without / packs_without if packs_without > 0 else 0.0
            effect = rate_with - rate_without

            if abs(effect) < self._min_effect_size:
                continue

            confidence = self._compute_confidence(total, abs(effect))
            advisories.append(
                Advisory(
                    category=AdvisoryCategory.APPROACH,
                    confidence=confidence,
                    message=(
                        f"Packs using the '{strategy}' strategy"
                        f" succeeded {rate_with:.0%} of the time"
                        f" vs {rate_without:.0%} without"
                        f" (n={total}, effect={effect:+.0%})."
                    ),
                    evidence=AdvisoryEvidence(
                        sample_size=total,
                        success_rate_with=round(rate_with, 3),
                        success_rate_without=round(rate_without, 3),
                        effect_size=round(effect, 3),
                    ),
                    scope="global",
                    metadata={"strategy": strategy},
                )
            )

        return advisories

    def _scope_analysis(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Analyze whether narrower or broader packs correlate with success."""
        # Bin packs by item count: small, medium, large
        bins: dict[str, list[bool]] = {
            "small": [],
            "medium": [],
            "large": [],
        }
        for pack in packs:
            n_items = len(pack["item_ids"])
            if n_items <= _SCOPE_SMALL:
                bins["small"].append(pack["success"])
            elif n_items <= _SCOPE_LARGE:
                bins["medium"].append(pack["success"])
            else:
                bins["large"].append(pack["success"])

        advisories: list[Advisory] = []
        bin_rates: dict[str, float] = {}

        for bin_name, outcomes in bins.items():
            if len(outcomes) >= self._min_sample_size:
                bin_rates[bin_name] = sum(outcomes) / len(outcomes)

        # Compare best vs worst bin
        if len(bin_rates) < _MIN_BIN_COUNT:
            return advisories

        best_bin = max(bin_rates, key=lambda k: bin_rates[k])
        worst_bin = min(bin_rates, key=lambda k: bin_rates[k])
        effect = bin_rates[best_bin] - bin_rates[worst_bin]

        if effect < self._min_effect_size:
            return advisories

        best_n = len(bins[best_bin])
        worst_n = len(bins[worst_bin])
        confidence = self._compute_confidence(best_n + worst_n, effect)

        scope_hint = {
            "small": "<=5 items",
            "medium": "6-15 items",
            "large": ">15 items",
        }

        advisories.append(
            Advisory(
                category=AdvisoryCategory.SCOPE,
                confidence=confidence,
                message=(
                    f"Packs with {scope_hint[best_bin]} succeeded"
                    f" {bin_rates[best_bin]:.0%} vs"
                    f" {bin_rates[worst_bin]:.0%} for"
                    f" {scope_hint[worst_bin]}"
                    f" (effect=+{effect:.0%})."
                ),
                evidence=AdvisoryEvidence(
                    sample_size=best_n + worst_n,
                    success_rate_with=round(bin_rates[best_bin], 3),
                    success_rate_without=round(bin_rates[worst_bin], 3),
                    effect_size=round(effect, 3),
                ),
                scope="global",
                metadata={
                    "best_bin": best_bin,
                    "worst_bin": worst_bin,
                },
            )
        )
        return advisories

    def _anti_pattern_detection(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find entities disproportionately present in failed packs."""
        item_success: dict[str, int] = defaultdict(int)
        item_failure: dict[str, int] = defaultdict(int)

        for pack in packs:
            for item_id in pack["item_ids"]:
                if pack["success"]:
                    item_success[item_id] += 1
                else:
                    item_failure[item_id] += 1

        advisories: list[Advisory] = []
        for item_id in set(item_success) | set(item_failure):
            s = item_success.get(item_id, 0)
            f = item_failure.get(item_id, 0)
            total = s + f
            if total < self._min_sample_size:
                continue

            rate_with = s / total if total > 0 else 0.0
            packs_without = len(packs) - total
            success_without = sum(1 for p in packs if p["success"]) - s
            rate_without = success_without / packs_without if packs_without > 0 else 0.0
            effect = rate_with - rate_without

            # Anti-patterns have *negative* effect (presence hurts)
            if effect >= -self._min_effect_size:
                continue

            domains = {p["domain"] for p in packs if item_id in p["item_ids"]}
            scope = domains.pop() if len(domains) == 1 else "global"

            confidence = self._compute_confidence(total, abs(effect))
            failure_traces = [
                p["pack_id"]
                for p in packs
                if item_id in p["item_ids"] and not p["success"]
            ][:5]

            advisories.append(
                Advisory(
                    category=AdvisoryCategory.ANTI_PATTERN,
                    confidence=confidence,
                    message=(
                        f"Entity {item_id} correlates with failure:"
                        f" {rate_with:.0%} success when present vs"
                        f" {rate_without:.0%} without"
                        f" (n={total}, effect={effect:+.0%})."
                    ),
                    evidence=AdvisoryEvidence(
                        sample_size=total,
                        success_rate_with=round(rate_with, 3),
                        success_rate_without=round(rate_without, 3),
                        effect_size=round(effect, 3),
                        representative_trace_ids=failure_traces,
                    ),
                    scope=scope,
                    entity_id=item_id,
                )
            )

        return advisories

    def _query_improvement(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find intent keywords that correlate with successful packs."""
        # Tokenise intents and track per-word success rates
        word_success: dict[str, int] = defaultdict(int)
        word_failure: dict[str, int] = defaultdict(int)

        for pack in packs:
            intent = pack.get("intent", "")
            words = set(intent.lower().split())
            for word in words:
                if len(word) < _MIN_WORD_LENGTH:
                    continue
                if pack["success"]:
                    word_success[word] += 1
                else:
                    word_failure[word] += 1

        total_success = sum(1 for p in packs if p["success"])

        advisories: list[Advisory] = []
        for word in set(word_success) | set(word_failure):
            s = word_success.get(word, 0)
            f = word_failure.get(word, 0)
            total = s + f
            if total < self._min_sample_size:
                continue

            rate_with = s / total if total > 0 else 0.0
            packs_without = len(packs) - total
            success_without = total_success - s
            rate_without = success_without / packs_without if packs_without > 0 else 0.0
            effect = rate_with - rate_without

            if effect < self._min_effect_size:
                continue

            confidence = self._compute_confidence(total, effect)
            advisories.append(
                Advisory(
                    category=AdvisoryCategory.QUERY,
                    confidence=confidence,
                    message=(
                        f"Including '{word}' in your context query"
                        f" correlates with {rate_with:.0%} success"
                        f" (n={total}, effect=+{effect:.0%})."
                    ),
                    evidence=AdvisoryEvidence(
                        sample_size=total,
                        success_rate_with=round(rate_with, 3),
                        success_rate_without=round(rate_without, 3),
                        effect_size=round(effect, 3),
                    ),
                    scope="global",
                    metadata={"keyword": word},
                )
            )

        return advisories

    # --- Helpers ---

    @staticmethod
    def _compute_confidence(sample_size: int, effect_size: float) -> float:
        """Compute advisory confidence from sample size and effect size.

        Confidence scales linearly with both sample_size and effect_size,
        capped at 1.0.  Small samples or weak effects yield low confidence.
        """
        # sample component: scales from 0 at n=0 to 1.0 at n≥10
        sample_factor = min(1.0, sample_size * _CONFIDENCE_SCALE)
        # effect component: 0.0 at zero effect, 1.0 at effect ≥ 0.5
        effect_factor = min(1.0, abs(effect_size) / 0.5)
        return round(min(1.0, sample_factor * effect_factor), 3)
