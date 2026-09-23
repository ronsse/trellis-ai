"""Tests for the parameter reachability screen (#617).

These test the *model* — the two reasons a proposal can be inert, and the
three-way partition the screen reports.  The claim that the declared read
points match the tree lives in ``tests/unit/test_parameter_reachability_rule.py``,
which derives them from the AST rather than reading this file's roster.
"""

from __future__ import annotations

import pytest

from trellis.ops.parameter_reachability import (
    LEARNING_AXES,
    READ_POINTS,
    ReachabilityScreen,
    ReadPoint,
    reachability_reasons,
    screen_proposals,
)
from trellis.retrieve.strategies import GRAPH_SEARCH_COMPONENT_ID
from trellis.schemas.parameters import ParameterProposal, ParameterScope

_UNROSTERED = "tests.ops.unrostered_component"


def _proposal(scope: ParameterScope, **values: float) -> ParameterProposal:
    return ParameterProposal(
        scope=scope,
        proposed_values=dict(values) or {"recency_half_life_days": 15.0},
        tuner="test_tuner",
    )


# ---------------------------------------------------------------------------
# reachability_reasons — the two ways a proposal can be inert
# ---------------------------------------------------------------------------


def test_axis_no_reader_supplies_is_unreachable():
    """The resolver builds its chain from the *querying* scope.

    ``ParameterStore.resolve`` derives all five candidates from the axis
    values the reader supplies, and ``_exact_scope_clauses`` emits
    ``<axis> IS NULL`` for the rest — so a snapshot that *sets* an axis the
    reader leaves ``None`` can never match any candidate.
    """
    scope = ParameterScope(
        component_id=GRAPH_SEARCH_COMPONENT_ID,
        domain="d",
        intent_family="plan",
    )
    reasons = reachability_reasons(scope, ["recency_half_life_days"])

    assert [r.kind for r in reasons] == ["unsupplied_axis"]
    assert reasons[0].axis == "intent_family"
    assert "trellis.retrieve.strategies" in reasons[0].detail
    assert "intent_family" in reasons[0].detail


def test_axis_the_reader_does_supply_is_reachable():
    """The mirror case — ``domain`` *is* supplied, so setting it is fine."""
    scope = ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, domain="d")
    assert reachability_reasons(scope, ["recency_half_life_days"]) == []


def test_every_unsupplied_axis_gets_its_own_reason():
    """Two wrong axes are two findings, not one summary line."""
    scope = ParameterScope(
        component_id=GRAPH_SEARCH_COMPONENT_ID,
        intent_family="plan",
        tool_name="Edit",
    )
    axes = {r.axis for r in reachability_reasons(scope)}
    assert axes == {"intent_family", "tool_name"}


def test_gated_key_resolves_but_never_applies():
    """Resolution and use are separate steps, and a key can clear only the first.

    ``domain_match_boost`` resolves perfectly well at a domainless scope.
    Its use site multiplies nothing there, because it sits behind
    ``if request_domain and ...`` — which is exactly the proposal #617 was
    filed about.
    """
    scope = ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID)
    reasons = reachability_reasons(scope, ["domain_match_boost"])

    assert [r.kind for r in reasons] == ["gated_key"]
    assert reasons[0].key == "domain_match_boost"
    assert reasons[0].axis == "domain"


def test_gated_key_with_its_axis_set_is_reachable():
    scope = ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, domain="d")
    assert reachability_reasons(scope, ["domain_match_boost"]) == []


def test_only_the_proposed_keys_are_screened_for_gating():
    """A gated key the proposal does not touch is not a reason to refuse it."""
    scope = ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID)
    assert reachability_reasons(scope, ["recency_half_life_days"]) == []
    assert reachability_reasons(scope, []) == []


def test_unknown_component_yields_no_reasons():
    """Absence from the roster is not evidence of unreachability.

    An empty list here means *not checked*.  Telling that apart from
    *checked and fine* is what :func:`screen_proposals` is for.
    """
    scope = ParameterScope(component_id=_UNROSTERED, intent_family="plan")
    assert reachability_reasons(scope, ["anything"]) == []


# ---------------------------------------------------------------------------
# screen_proposals — the three-way partition
# ---------------------------------------------------------------------------


def test_screen_partitions_the_input():
    reachable = _proposal(
        ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, domain="d")
    )
    unreachable = _proposal(
        ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, intent_family="plan")
    )
    unknown = _proposal(
        ParameterScope(component_id=_UNROSTERED, intent_family="plan"),
    )

    screen = screen_proposals([reachable, unreachable, unknown])

    assert [p.proposal_id for p in screen.admitted] == [reachable.proposal_id]
    assert [r.proposal.proposal_id for r in screen.refused] == [unreachable.proposal_id]
    assert [p.proposal_id for p in screen.unchecked] == [unknown.proposal_id]
    # The three are a partition, not overlapping views.
    assert screen.total == 3


def test_unchecked_proposals_are_served():
    """No claim means serve it.

    Refusing an unrostered component would make this roster a gate on every
    out-of-tree one — suppressing a real tuning loop on the strength of a
    scan of ``src/``, which cannot see a ``ParameterRegistry`` caller in
    another package.
    """
    unknown = _proposal(ParameterScope(component_id=_UNROSTERED, tool_name="Edit"))
    screen = screen_proposals([unknown])

    assert screen.servable() == [unknown]
    assert screen.refused == ()


def test_refused_proposals_are_not_served():
    unreachable = _proposal(
        ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, tool_name="Edit")
    )
    screen = screen_proposals([unreachable])

    assert screen.servable() == []
    assert screen.refused[0].proposal is unreachable


def test_refusal_summary_joins_every_reason():
    unreachable = _proposal(
        ParameterScope(
            component_id=GRAPH_SEARCH_COMPONENT_ID,
            intent_family="plan",
            tool_name="Edit",
        )
    )
    refusal = screen_proposals([unreachable]).refused[0]

    assert len(refusal.reasons) == 2
    summary = refusal.summary()
    assert "intent_family" in summary
    assert "tool_name" in summary


def test_empty_input_screens_to_an_empty_verdict():
    screen = screen_proposals([])
    assert screen.total == 0
    assert screen.servable() == []


def test_screen_is_pure():
    """It reads no store and mutates nothing it is handed."""
    scope = ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, intent_family="x")
    proposal = _proposal(scope)
    before = proposal.model_dump(mode="json")

    screen_proposals([proposal])

    assert proposal.model_dump(mode="json") == before


def test_default_screen_is_empty():
    assert ReachabilityScreen().total == 0


# ---------------------------------------------------------------------------
# ReadPoint validation — a typo in the roster must not read as "no axes"
# ---------------------------------------------------------------------------


def test_read_point_rejects_an_unknown_resolvable_axis():
    with pytest.raises(ValueError, match="unknown resolvable axes"):
        ReadPoint(
            component_id="c",
            reader_module="m",
            resolvable_axes=frozenset({"domian"}),
        )


def test_read_point_rejects_an_unknown_gating_axis():
    with pytest.raises(ValueError, match="not a learning axis"):
        ReadPoint(
            component_id="c",
            reader_module="m",
            resolvable_axes=frozenset(),
            gated_keys={"k": "intent-family"},
        )


def test_read_point_accepts_every_learning_axis():
    point = ReadPoint(
        component_id="c",
        reader_module="m",
        resolvable_axes=frozenset(LEARNING_AXES),
        gated_keys=dict.fromkeys(["k"], LEARNING_AXES[0]),
    )
    assert point.resolvable_axes == frozenset(LEARNING_AXES)


def test_roster_is_keyed_by_component_id():
    """A duplicated ``component_id`` would silently drop a read point."""
    for component_id, point in READ_POINTS.items():
        assert point.component_id == component_id
