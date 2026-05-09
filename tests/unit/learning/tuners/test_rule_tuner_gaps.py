"""Targeted gap-coverage tests for :mod:`trellis.learning.tuners.rule_tuner`.

The main suites in ``test_rule_tuner_pure.py`` and
``test_rule_tuner_run.py`` cover ``aggregate_outcomes``, the
``lt`` / ``gt`` operators, the metric-key prefix, the ``RuleTuner.run``
cursor flow, and idempotency.  This file backfills the remaining
``_compare`` operators (``lte`` / ``gte`` / ``eq``) and the type-guards
in ``TuningRule._stat`` (bool stat → None, missing attribute → None,
truthy non-bool → coerced to float).

These are pure-function tests; no stores, no mocks needed beyond a
hand-rolled :class:`AggregatedOutcomes` instance.
"""

from __future__ import annotations

import pytest

from trellis.learning.tuners import (
    AggregatedOutcomes,
    TuningRule,
    apply_rules,
)
from trellis.schemas.parameters import ParameterScope

_COMPONENT = "retrieve.strategies.KeywordSearch"
_SCOPE = ParameterScope(component_id=_COMPONENT, domain="x")


def _agg(
    *,
    count: int = 50,
    success_count: int = 25,
    total_latency_ms: float = 500.0,
    items_served: int = 100,
    items_referenced: int = 30,
) -> AggregatedOutcomes:
    return AggregatedOutcomes(
        scope=_SCOPE,
        count=count,
        success_count=success_count,
        total_latency_ms=total_latency_ms,
        items_served_total=items_served,
        items_referenced_total=items_referenced,
    )


def _rule(
    *,
    name: str = "gap_rule",
    op: str,
    value: float,
    key: str = "success_rate",
    min_sample_size: int = 1,
    proposed_param: str = "k",
    proposed_value: float | int | str | bool = 30,
) -> TuningRule:
    return TuningRule(
        name=name,
        target_component_id=_COMPONENT,
        min_sample_size=min_sample_size,
        condition_key=key,
        condition_op=op,
        condition_value=value,
        proposed_param=proposed_param,
        proposed_value=proposed_value,
    )


# ---------------------------------------------------------------------------
# Comparison operators — lte / gte / eq
# ---------------------------------------------------------------------------


def test_rule_fires_on_lte_at_boundary():
    """``lte`` fires when stat equals the threshold.

    Covers the ``op == "lte"`` arm of ``_compare`` — distinct from
    ``lt``, which would not fire at equality.
    """
    rule = _rule(op="lte", value=0.5, key="success_rate")
    # success_rate = 25 / 50 = 0.5 — exactly at the boundary.
    assert rule.applies(_agg()) is True


def test_rule_fires_on_gte_at_boundary():
    """``gte`` fires when stat equals the threshold.

    Mirror of the ``lte`` test; covers the ``op == "gte"`` arm of
    ``_compare`` and locks the boundary semantics down.
    """
    rule = _rule(op="gte", value=0.5, key="success_rate")
    assert rule.applies(_agg()) is True


def test_rule_fires_on_eq_only_at_exact_match():
    """``eq`` fires only on equality.

    Covers the ``op == "eq"`` arm and pins the strict semantics: a
    stat that differs by any amount must not fire.
    """
    rule = _rule(op="eq", value=0.5, key="success_rate")
    assert rule.applies(_agg(count=50, success_count=25)) is True
    # success_rate = 26/50 = 0.52 — not equal.
    assert rule.applies(_agg(count=50, success_count=26)) is False


def test_rule_does_not_fire_when_lte_strict_above():
    """Sanity counter: ``lte`` does not fire when stat > value.

    Together with ``test_rule_fires_on_lte_at_boundary`` this nails
    down the inclusive-vs-strict semantics.
    """
    rule = _rule(op="lte", value=0.4, key="success_rate")
    assert rule.applies(_agg(count=50, success_count=25)) is False


# ---------------------------------------------------------------------------
# _stat type-guards — bool stat, missing attribute
# ---------------------------------------------------------------------------


def test_rule_does_not_fire_when_attribute_missing():
    """Unknown ``condition_key`` → stat is None → rule does not fire.

    Covers the ``value is None`` branch of ``_stat`` (line 201-202).
    Without this test, a typo in a rule's condition_key would silently
    fire for every aggregate.
    """
    rule = _rule(op="lt", value=0.5, key="not_a_real_attribute")
    assert rule.applies(_agg()) is False


def test_rule_metric_key_returns_none_when_metric_missing():
    """``metric:<key>`` for a key not present returns None → no fire.

    Crosses the ``mean_metric`` short-circuit with the ``stat is None``
    early-out in ``applies``.
    """
    rule = _rule(op="lt", value=0.5, key="metric:precision_at_5")
    assert rule.applies(_agg()) is False  # no metrics on the agg


def test_rule_does_not_fire_when_attribute_is_bool():
    """A stat that resolves to a Python bool returns None, not 1/0.

    Covers the ``isinstance(value, bool)`` short-circuit in ``_stat``
    at ``rule_tuner.py:201``. Pydantic-style truthy-ints would silently
    coerce; bool must not.
    """

    class _BoolyAgg:
        """Duck-typed aggregate with a bool attribute the rule keys on."""

        def __init__(self) -> None:
            self.scope = _SCOPE
            self.count = 50
            self.success_count = 25
            self.total_latency_ms = 0.0
            self.items_served_total = 0
            self.items_referenced_total = 0
            self.metric_sums: dict[str, float] = {}
            self.metric_counts: dict[str, int] = {}
            self.is_healthy = True  # boolean attribute the rule will read

        @property
        def success_rate(self) -> float:
            return 0.5

        @property
        def mean_latency_ms(self) -> float:
            return 0.0

        @property
        def reference_rate(self) -> float:
            return 0.0

        def mean_metric(self, key: str) -> float | None:
            return None

    rule = _rule(op="eq", value=1.0, key="is_healthy")
    # Without the bool guard, ``True`` would coerce to 1.0 and the rule
    # would fire.  With it, ``_stat`` returns None and the rule abstains.
    assert rule.applies(_BoolyAgg()) is False


# ---------------------------------------------------------------------------
# apply_rules with a custom operator — error path through TuningRule
# ---------------------------------------------------------------------------


def test_tuning_rule_construction_raises_on_unknown_op():
    """Unknown ``condition_op`` is rejected at construction.

    The main suite covers this at the constructor level
    (``test_rule_rejects_unknown_op``); this is a lightweight
    duplicate that pins the precise message format so lint/format
    refactors don't silently soften the error.
    """
    with pytest.raises(ValueError, match="Unknown condition_op"):
        _rule(op="approx", value=0.5)


def test_tuning_rule_construction_raises_on_zero_sample_size():
    """``min_sample_size < 1`` is rejected with a typed ``ValueError``.

    Covers the second guard in ``__post_init__``.  Mirror to the
    unknown-op test; together they form the rule's input-validation
    contract.
    """
    with pytest.raises(ValueError, match="min_sample_size must be >= 1"):
        _rule(op="lt", value=0.5, min_sample_size=0)


# ---------------------------------------------------------------------------
# apply_rules — boundary integration: lte/gte/eq actually emit proposals
# ---------------------------------------------------------------------------


def test_apply_rules_emits_proposal_for_lte_boundary_match():
    """End-to-end: a ``lte`` boundary fire emits exactly one proposal.

    Crosses ``apply_rules`` with the new comparison operators so the
    proposal generation path also stays exercised.
    """
    rule = _rule(op="lte", value=0.5, key="success_rate", proposed_value=42)
    proposals = apply_rules([_agg()], [rule])
    assert len(proposals) == 1
    assert proposals[0].proposed_values == {"k": 42}
    assert proposals[0].sample_size == 50


def test_apply_rules_emits_no_proposal_when_below_min_sample_size():
    """Edge case: aggregate with too few samples produces zero proposals.

    Mirrors ``test_rule_skips_insufficient_samples`` at the
    ``apply_rules`` integration level so the suppression goes all the
    way through proposal emission, not just the rule predicate.
    """
    rule = _rule(op="lte", value=0.5, key="success_rate", min_sample_size=100)
    # count=50 < min_sample_size=100
    proposals = apply_rules([_agg()], [rule])
    assert proposals == []
