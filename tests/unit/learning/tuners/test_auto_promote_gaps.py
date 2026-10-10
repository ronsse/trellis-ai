"""Gaps in the auto-promotion dry-run gate.

:mod:`trellis.learning.tuners.auto_promote`

``_evaluate`` is the dry-run qualification check ``run_auto_promotion``
calls per proposal before promoting. Unlike :func:`promote_proposal`, it
never called :func:`reachability_reasons` -- harmless today only because
``RuleTuner.run()`` screens every proposal it generates for reachability
before ``_evaluate`` ever sees it (#622), and the live promotion inside
``_promote_and_monitor`` re-checks independently via ``promote_proposal``.
A proposal already sitting in the store before that screen existed, or
written by a tuner that skips it, reached ``_evaluate`` with no
reachability opinion at all, so a dry run's reported ``reason`` could
disagree with what a live run of the exact same proposal would actually
do. These pin the re-check (#823's gate, probe 2's "Latent" finding).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from trellis.learning.tuners.auto_promote import AutoPromotePolicy, _evaluate
from trellis.schemas.outcome import GRAPH_SEARCH_COMPONENT_ID
from trellis.schemas.parameters import ParameterProposal, ParameterScope
from trellis.stores.base.parameter import ParameterStore


def _prod_shape_proposal(**kw) -> ParameterProposal:
    """``intent_family`` set, ``domain`` unset: the exact shape of the
    2026-10-03 incident, where both ``unsupplied_axis`` and ``gated_key``
    reasons fire at once (see ``test_promotion_reachability.py``)."""
    defaults: dict = {
        "proposal_id": "prop_auto_unreachable",
        "scope": ParameterScope(
            component_id=GRAPH_SEARCH_COMPONENT_ID, intent_family="plan"
        ),
        "tuner": "rule_tuner",
        "proposed_values": {"domain_match_boost": 1.4},
        "sample_size": 100,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


def test_evaluate_disqualifies_an_unreachable_scope() -> None:
    proposal = _prod_shape_proposal()
    parameter_store = MagicMock(spec=ParameterStore)

    qualifies, reason, effect_size = _evaluate(
        proposal,
        parameter_store=parameter_store,
        policy=AutoPromotePolicy(),
    )

    assert qualifies is False
    assert reason.startswith("unreachable: ")
    assert effect_size is None
    # The early return is a short-circuit, not just a check that happens
    # to also reject: the store is never consulted for a scope no in-tree
    # reader can resolve, mirroring promote_proposal's own gate ordering
    # (reachability runs above the baseline resolve there too).
    parameter_store.resolve.assert_not_called()


def test_evaluate_still_runs_the_policy_gate_for_a_reachable_scope() -> None:
    """Regression guard: adding the reachability check must not have
    short-circuited the pre-existing policy-gate path for a normal,
    reachable scope with no baseline yet."""
    proposal = _prod_shape_proposal(
        scope=ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, domain="orders"),
    )
    parameter_store = MagicMock(spec=ParameterStore)
    parameter_store.resolve.return_value = None

    qualifies, reason, effect_size = _evaluate(
        proposal,
        parameter_store=parameter_store,
        policy=AutoPromotePolicy(),
    )

    parameter_store.resolve.assert_called_once()
    # AutoPromotePolicy.require_baseline defaults True =>
    # to_promotion_policy().allow_no_baseline is False, so a scope with
    # no snapshot yet is refused here exactly as it always was.
    assert qualifies is False
    assert reason == "no_baseline_snapshot_for_scope"
    assert effect_size is None
