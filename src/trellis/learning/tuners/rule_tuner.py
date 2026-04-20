"""Rule-based tuner — deterministic parameter proposals from OutcomeEvents.

Split into three layers so each is testable in isolation:

1. :func:`aggregate_outcomes` — pure: ``list[OutcomeEvent]`` →
   ``list[AggregatedOutcomes]``, grouped by learning-axis cell.
2. :class:`TuningRule` + :func:`apply_rules` — pure: aggregates +
   rules → ``list[ParameterProposal]``, each with a deterministic
   ``proposal_id`` derived from rule name + scope.
3. :class:`RuleTuner` (next commit) — orchestrator: cursor-driven
   read from :class:`OutcomeStore`, aggregation, rule application,
   and proposal persistence to :class:`TunerStateStore`.

Rules are data, not code. The built-in :data:`DEFAULT_RULES` covers the
most common retrieval-tuning triggers; callers can extend with custom
:class:`TuningRule` instances. This mirrors the
``ExtractionDispatcher``'s pattern — the dispatcher is generic; tier
definitions are data.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from trellis.schemas.outcome import OutcomeEvent
from trellis.schemas.parameters import ParameterProposal, ParameterScope

# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AggregatedOutcomes:
    """Per-cell aggregate computed from a batch of :class:`OutcomeEvent`.

    ``scope`` identifies the learning-axis cell
    ``(component_id, domain, intent_family, tool_name)`` — rules key
    off this. Counters are totals; :attr:`success_rate` and
    :attr:`mean_latency_ms` are derived.

    ``metric_sums`` / ``metric_counts`` track the freeform
    ``OutcomeEvent.outcome.metrics`` dict so rules can consult
    component-specific numeric signals (e.g. ``precision``, ``recall``)
    without forcing every rule to crunch raw events.
    """

    scope: ParameterScope
    count: int = 0
    success_count: int = 0
    total_latency_ms: float = 0.0
    items_served_total: int = 0
    items_referenced_total: int = 0
    metric_sums: dict[str, float] = field(default_factory=dict)
    metric_counts: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.count if self.count else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.count if self.count else 0.0

    @property
    def reference_rate(self) -> float:
        """Ratio of items_referenced to items_served across the cell."""
        if self.items_served_total <= 0:
            return 0.0
        return self.items_referenced_total / self.items_served_total

    def mean_metric(self, key: str) -> float | None:
        """Return the mean of a metric key across samples that reported it.

        Returns ``None`` when no samples reported the key so rules can
        distinguish "no signal" from "signal = 0.0".
        """
        n = self.metric_counts.get(key, 0)
        if n <= 0:
            return None
        return self.metric_sums.get(key, 0.0) / n


def aggregate_outcomes(
    outcomes: Sequence[OutcomeEvent],
) -> list[AggregatedOutcomes]:
    """Group outcomes by ``(component_id, domain, intent_family, tool_name)``.

    Unknown axes (``None``) are preserved — they form their own cells.
    Rules that want to back off to wider scopes re-aggregate with the
    narrowed axes set to ``None``.
    """
    buckets: dict[
        tuple[str, str | None, str | None, str | None], AggregatedOutcomes
    ] = {}

    for event in outcomes:
        key = (
            event.component_id,
            event.domain,
            event.intent_family,
            event.tool_name,
        )
        agg = buckets.get(key)
        if agg is None:
            agg = AggregatedOutcomes(
                scope=ParameterScope(
                    component_id=event.component_id,
                    domain=event.domain,
                    intent_family=event.intent_family,
                    tool_name=event.tool_name,
                ),
            )
            buckets[key] = agg

        agg.count += 1
        if event.outcome.success:
            agg.success_count += 1
        agg.total_latency_ms += event.outcome.latency_ms
        if event.outcome.items_served is not None:
            agg.items_served_total += event.outcome.items_served
        if event.outcome.items_referenced is not None:
            agg.items_referenced_total += event.outcome.items_referenced
        for metric_key, metric_value in event.outcome.metrics.items():
            agg.metric_sums[metric_key] = (
                agg.metric_sums.get(metric_key, 0.0) + metric_value
            )
            agg.metric_counts[metric_key] = agg.metric_counts.get(metric_key, 0) + 1

    return list(buckets.values())


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


_CONDITION_OPS: Final = {"lt", "lte", "gt", "gte", "eq"}


@dataclass(frozen=True, slots=True)
class TuningRule:
    """A deterministic rule mapping aggregate stats to a parameter proposal.

    A rule fires for a cell when:

    * ``agg.scope.component_id == target_component_id``
    * ``agg.count >= min_sample_size``
    * ``getattr(agg, condition_key) <condition_op> condition_value``

    When a rule fires it emits a proposal setting ``proposed_param``
    to ``proposed_value`` for the cell's full scope.  Rules do not
    know the current value — the promotion step reads the active
    snapshot (if any) and records the delta as ``effect_size`` for the
    policy gate.

    ``condition_key`` may reference any property on
    :class:`AggregatedOutcomes` (including computed properties like
    ``success_rate`` and ``reference_rate``) or a metric key via the
    ``metric:<key>`` prefix (e.g. ``metric:precision``).
    """

    name: str
    target_component_id: str
    min_sample_size: int
    condition_key: str
    condition_op: str
    condition_value: float
    proposed_param: str
    proposed_value: float | int | str | bool
    description: str = ""

    def __post_init__(self) -> None:
        if self.condition_op not in _CONDITION_OPS:
            msg = (
                f"Unknown condition_op {self.condition_op!r}; "
                f"must be one of {sorted(_CONDITION_OPS)}"
            )
            raise ValueError(msg)
        if self.min_sample_size < 1:
            msg = f"min_sample_size must be >= 1, got {self.min_sample_size}"
            raise ValueError(msg)

    def _stat(self, agg: AggregatedOutcomes) -> float | None:
        if self.condition_key.startswith("metric:"):
            metric_key = self.condition_key.removeprefix("metric:")
            return agg.mean_metric(metric_key)
        value = getattr(agg, self.condition_key, None)
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def applies(self, agg: AggregatedOutcomes) -> bool:
        """Does this rule fire for the given aggregate?"""
        if agg.scope.component_id != self.target_component_id:
            return False
        if agg.count < self.min_sample_size:
            return False
        stat = self._stat(agg)
        if stat is None:
            return False
        return _compare(stat, self.condition_op, self.condition_value)


def _compare(stat: float, op: str, value: float) -> bool:
    if op == "lt":
        return stat < value
    if op == "lte":
        return stat <= value
    if op == "gt":
        return stat > value
    if op == "gte":
        return stat >= value
    if op == "eq":
        return stat == value
    return False  # unreachable; guarded by TuningRule.__post_init__


def _deterministic_proposal_id(rule: TuningRule, scope: ParameterScope) -> str:
    """Build a deterministic proposal_id from rule + scope.

    Same rule + same scope → same id, regardless of when the tuner
    runs.  This is the idempotency key: re-running the tuner on the
    same window never produces duplicate proposals.
    """
    payload = "|".join(
        [
            rule.name,
            rule.target_component_id,
            rule.condition_key,
            rule.condition_op,
            str(rule.condition_value),
            rule.proposed_param,
            repr(rule.proposed_value),
            scope.component_id,
            scope.domain or "",
            scope.intent_family or "",
            scope.tool_name or "",
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:26]
    return f"prop_{digest}"


def apply_rules(
    aggregates: Sequence[AggregatedOutcomes],
    rules: Sequence[TuningRule],
    *,
    tuner: str = "rule_tuner",
) -> list[ParameterProposal]:
    """Apply ``rules`` to every aggregate, return one proposal per firing.

    Each proposal carries:

    * ``tuner`` — caller-supplied name (default ``"rule_tuner"``).
    * ``scope`` — full scope from the aggregate.
    * ``proposed_values`` — single-key dict ``{rule.proposed_param:
      rule.proposed_value}``.
    * ``sample_size`` — ``agg.count`` at proposal time.
    * ``proposal_id`` — deterministic (see
      :func:`_deterministic_proposal_id`); re-running the tuner over
      the same data gives the same id.

    ``effect_size`` is left unset here; the promotion step computes it
    against the active snapshot.
    """
    proposals: list[ParameterProposal] = []
    for agg in aggregates:
        for rule in rules:
            if not rule.applies(agg):
                continue
            proposals.append(
                ParameterProposal(
                    proposal_id=_deterministic_proposal_id(rule, agg.scope),
                    scope=agg.scope,
                    proposed_values={rule.proposed_param: rule.proposed_value},
                    tuner=tuner,
                    sample_size=agg.count,
                    notes=rule.description or rule.name,
                    metadata={"rule_name": rule.name},
                )
            )
    return proposals


# ---------------------------------------------------------------------------
# Default rule set
# ---------------------------------------------------------------------------


#: Conservative starter rules. Triggered only with ample samples
#: (``min_sample_size=30``) and only set values that were previously
#: the module-level defaults — never introduce novel thresholds a
#: human hasn't reviewed. Replace / extend via the ``rules`` argument
#: on :class:`RuleTuner`.
DEFAULT_RULES: Final[tuple[TuningRule, ...]] = (
    TuningRule(
        name="keyword_low_success_halve_half_life",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=30,
        condition_key="success_rate",
        condition_op="lt",
        condition_value=0.4,
        proposed_param="recency_half_life_days",
        proposed_value=15.0,
        description=(
            "Cell shows persistent low success — halve the recency "
            "half-life so older items decay faster."
        ),
    ),
    TuningRule(
        name="graph_low_reference_rate_tighten_domain_boost",
        target_component_id="retrieve.strategies.GraphSearch",
        min_sample_size=30,
        condition_key="reference_rate",
        condition_op="lt",
        condition_value=0.2,
        proposed_param="domain_match_boost",
        proposed_value=1.15,
        description=(
            "Graph results rarely referenced — pull domain_match_boost "
            "back toward neutral so domain-matched noise stops crowding "
            "the pack."
        ),
    ),
    TuningRule(
        name="rrf_low_success_reduce_smoothing",
        target_component_id="retrieve.rerankers.RRFReranker",
        min_sample_size=30,
        condition_key="success_rate",
        condition_op="lt",
        condition_value=0.4,
        proposed_param="k",
        proposed_value=30,
        description=(
            "RRF smoothing at default (60) too forgiving for this cell — "
            "reduce k so top-ranked items dominate the fusion."
        ),
    ),
)
