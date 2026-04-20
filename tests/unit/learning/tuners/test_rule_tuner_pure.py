"""Tests for the pure aggregation + rule-application layer of RuleTuner."""

from __future__ import annotations

import pytest

from trellis.learning.tuners import (
    DEFAULT_RULES,
    AggregatedOutcomes,
    TuningRule,
    aggregate_outcomes,
    apply_rules,
)
from trellis.learning.tuners.rule_tuner import _deterministic_proposal_id
from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.schemas.parameters import ParameterScope


def _event(
    component_id: str = "retrieve.strategies.KeywordSearch",
    *,
    success: bool = True,
    latency_ms: float = 10.0,
    domain: str | None = "sportsbook",
    intent_family: str | None = "plan",
    tool_name: str | None = None,
    items_served: int | None = None,
    items_referenced: int | None = None,
    metrics: dict[str, float] | None = None,
) -> OutcomeEvent:
    return OutcomeEvent(
        component_id=component_id,
        domain=domain,
        intent_family=intent_family,
        tool_name=tool_name,
        outcome=ComponentOutcome(
            success=success,
            latency_ms=latency_ms,
            items_served=items_served,
            items_referenced=items_referenced,
            metrics=metrics or {},
        ),
    )


# ---------------------------------------------------------------------------
# aggregate_outcomes
# ---------------------------------------------------------------------------


def test_aggregate_empty():
    assert aggregate_outcomes([]) == []


def test_aggregate_groups_by_full_scope():
    outcomes = [
        _event(domain="a", intent_family="plan"),
        _event(domain="a", intent_family="plan"),
        _event(domain="a", intent_family="diagnose"),
        _event(domain="b", intent_family="plan"),
    ]
    aggs = aggregate_outcomes(outcomes)
    assert len(aggs) == 3

    keyed = {a.scope.key(): a for a in aggs}
    assert keyed[("retrieve.strategies.KeywordSearch", "a", "plan", None)].count == 2
    assert (
        keyed[("retrieve.strategies.KeywordSearch", "a", "diagnose", None)].count == 1
    )
    assert keyed[("retrieve.strategies.KeywordSearch", "b", "plan", None)].count == 1


def test_aggregate_counts_success_and_failure():
    outcomes = [
        _event(success=True),
        _event(success=True),
        _event(success=False),
        _event(success=False),
        _event(success=False),
    ]
    aggs = aggregate_outcomes(outcomes)
    assert len(aggs) == 1
    agg = aggs[0]
    assert agg.count == 5
    assert agg.success_count == 2
    assert agg.success_rate == pytest.approx(0.4)


def test_aggregate_mean_latency():
    outcomes = [
        _event(latency_ms=10.0),
        _event(latency_ms=20.0),
        _event(latency_ms=30.0),
    ]
    agg = aggregate_outcomes(outcomes)[0]
    assert agg.mean_latency_ms == pytest.approx(20.0)


def test_aggregate_reference_rate():
    outcomes = [
        _event(items_served=10, items_referenced=3),
        _event(items_served=5, items_referenced=2),
    ]
    agg = aggregate_outcomes(outcomes)[0]
    assert agg.items_served_total == 15
    assert agg.items_referenced_total == 5
    assert agg.reference_rate == pytest.approx(5 / 15)


def test_aggregate_reference_rate_with_no_items_served():
    outcomes = [_event()]  # items_served=None
    agg = aggregate_outcomes(outcomes)[0]
    assert agg.reference_rate == 0.0


def test_aggregate_metrics_accumulate():
    outcomes = [
        _event(metrics={"precision": 0.6, "recall": 0.4}),
        _event(metrics={"precision": 0.8}),
        _event(metrics={}),
    ]
    agg = aggregate_outcomes(outcomes)[0]
    assert agg.mean_metric("precision") == pytest.approx(0.7)
    assert agg.mean_metric("recall") == pytest.approx(0.4)
    assert agg.mean_metric("never_reported") is None


def test_aggregate_none_axes_are_their_own_cells():
    outcomes = [
        _event(domain=None),
        _event(domain="a"),
    ]
    aggs = aggregate_outcomes(outcomes)
    assert len(aggs) == 2


# ---------------------------------------------------------------------------
# TuningRule validation
# ---------------------------------------------------------------------------


def test_rule_rejects_unknown_op():
    with pytest.raises(ValueError, match="Unknown condition_op"):
        TuningRule(
            name="bad",
            target_component_id="x",
            min_sample_size=10,
            condition_key="success_rate",
            condition_op="approximately",
            condition_value=0.5,
            proposed_param="k",
            proposed_value=1,
        )


def test_rule_rejects_zero_sample_size():
    with pytest.raises(ValueError, match="min_sample_size"):
        TuningRule(
            name="bad",
            target_component_id="x",
            min_sample_size=0,
            condition_key="success_rate",
            condition_op="lt",
            condition_value=0.5,
            proposed_param="k",
            proposed_value=1,
        )


# ---------------------------------------------------------------------------
# TuningRule.applies
# ---------------------------------------------------------------------------


def _agg(**kw) -> AggregatedOutcomes:
    scope = kw.pop(
        "scope",
        ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a"),
    )
    agg = AggregatedOutcomes(scope=scope)
    for key, val in kw.items():
        setattr(agg, key, val)
    return agg


def test_rule_skips_other_components():
    rule = TuningRule(
        name="r",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=1,
        condition_key="success_rate",
        condition_op="lt",
        condition_value=1.0,
        proposed_param="k",
        proposed_value=1,
    )
    wrong_scope = ParameterScope(component_id="retrieve.strategies.GraphSearch")
    assert rule.applies(_agg(scope=wrong_scope, count=100, success_count=50)) is False


def test_rule_skips_insufficient_samples():
    rule = TuningRule(
        name="r",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=50,
        condition_key="success_rate",
        condition_op="lt",
        condition_value=1.0,
        proposed_param="k",
        proposed_value=1,
    )
    assert rule.applies(_agg(count=10, success_count=0)) is False


def test_rule_fires_on_lt():
    rule = TuningRule(
        name="r",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=1,
        condition_key="success_rate",
        condition_op="lt",
        condition_value=0.5,
        proposed_param="k",
        proposed_value=1,
    )
    assert rule.applies(_agg(count=10, success_count=3)) is True  # 0.3 < 0.5
    assert rule.applies(_agg(count=10, success_count=5)) is False  # 0.5 not < 0.5
    assert rule.applies(_agg(count=10, success_count=8)) is False


def test_rule_fires_on_gt():
    rule = TuningRule(
        name="r",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=1,
        condition_key="mean_latency_ms",
        condition_op="gt",
        condition_value=100.0,
        proposed_param="k",
        proposed_value=1,
    )
    assert rule.applies(_agg(count=10, total_latency_ms=2000.0)) is True  # 200 > 100
    assert rule.applies(_agg(count=10, total_latency_ms=500.0)) is False


def test_rule_metric_key_prefix():
    rule = TuningRule(
        name="r",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=1,
        condition_key="metric:precision",
        condition_op="lt",
        condition_value=0.5,
        proposed_param="k",
        proposed_value=1,
    )
    low = _agg(count=1, metric_sums={"precision": 0.3}, metric_counts={"precision": 1})
    high = _agg(count=1, metric_sums={"precision": 0.9}, metric_counts={"precision": 1})
    missing = _agg(count=1)
    assert rule.applies(low) is True
    assert rule.applies(high) is False
    assert rule.applies(missing) is False  # no signal


# ---------------------------------------------------------------------------
# apply_rules + proposal_id determinism
# ---------------------------------------------------------------------------


def test_apply_rules_produces_proposals_per_firing():
    rule = TuningRule(
        name="r",
        target_component_id="retrieve.strategies.KeywordSearch",
        min_sample_size=5,
        condition_key="success_rate",
        condition_op="lt",
        condition_value=0.5,
        proposed_param="recency_half_life_days",
        proposed_value=15.0,
    )
    aggs = [
        _agg(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            count=10,
            success_count=2,
        ),
        _agg(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="b"
            ),
            count=10,
            success_count=8,
        ),
    ]
    proposals = apply_rules(aggs, [rule])
    assert len(proposals) == 1
    assert proposals[0].scope.domain == "a"
    assert proposals[0].proposed_values == {"recency_half_life_days": 15.0}
    assert proposals[0].sample_size == 10
    assert proposals[0].metadata["rule_name"] == "r"


def test_proposal_id_is_deterministic():
    scope = ParameterScope(
        component_id="retrieve.strategies.KeywordSearch", domain="sportsbook"
    )
    rule = DEFAULT_RULES[0]
    id_a = _deterministic_proposal_id(rule, scope)
    id_b = _deterministic_proposal_id(rule, scope)
    assert id_a == id_b
    assert id_a.startswith("prop_")


def test_apply_rules_idempotent_on_rerun():
    aggs = [
        _agg(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            count=50,
            success_count=15,  # 0.3 success rate
        ),
    ]
    rules = DEFAULT_RULES
    first = apply_rules(aggs, rules)
    second = apply_rules(aggs, rules)
    assert [p.proposal_id for p in first] == [p.proposal_id for p in second]


# ---------------------------------------------------------------------------
# DEFAULT_RULES smoke
# ---------------------------------------------------------------------------


def test_default_rules_fire_on_representative_cell():
    # Cell: keyword search, 50 calls, 12 successes -> success_rate=0.24 < 0.4
    agg = _agg(
        scope=ParameterScope(
            component_id="retrieve.strategies.KeywordSearch", domain="a"
        ),
        count=50,
        success_count=12,
    )
    proposals = apply_rules([agg], DEFAULT_RULES)
    assert len(proposals) == 1
    assert proposals[0].proposed_values == {"recency_half_life_days": 15.0}


def test_default_rules_skip_healthy_cells():
    agg = _agg(
        scope=ParameterScope(
            component_id="retrieve.strategies.KeywordSearch", domain="a"
        ),
        count=50,
        success_count=40,  # 0.8 success rate — above threshold
    )
    proposals = apply_rules([agg], DEFAULT_RULES)
    assert proposals == []
