"""Promotion refuses a proposal no in-tree reader can ever resolve.

The one proposal ever promoted to prod (2026-10-03, through the Review
queue) set ``intent_family``, an axis ``GraphSearch`` never supplies, and
carried ``baseline_values: {}`` — unreachable *and* unbaselined, to a
scope ``ParameterStore.resolve`` can never match. ``RuleTuner.run()``
screens for this at generation time (#622), but a proposal already
sitting in the store reaches ``promote_proposal``/``preview_promotion``
unchecked unless it is re-checked there too — these pin that re-check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.learning.tuners import (
    PromotionPreview,
    PromotionResult,
    preview_promotion,
    promote_proposal,
)
from trellis.ops.parameter_reachability import reachability_reasons
from trellis.schemas.outcome import GRAPH_SEARCH_COMPONENT_ID
from trellis.schemas.parameters import ParameterProposal, ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore


@pytest.fixture
def stores(tmp_path: Path):
    params = SQLiteParameterStore(tmp_path / "parameters.db")
    state = SQLiteTunerStateStore(tmp_path / "tuner_state.db")
    events = SQLiteEventLog(tmp_path / "events.db")
    try:
        yield params, state, events
    finally:
        params.close()
        state.close()
        events.close()


def _unsupplied_axis_proposal(**kw) -> ParameterProposal:
    """``intent_family`` set with ``domain`` set: only the axis reason fires.

    The prod incident left ``domain`` unset as well, which trips both
    reasons at once; ``test_promote_refuses_the_prod_shape_with_both_reasons``
    pins that shape.
    """
    defaults: dict = {
        "proposal_id": "prop_unreachable_axis",
        "scope": ParameterScope(
            component_id=GRAPH_SEARCH_COMPONENT_ID,
            domain="orders",
            intent_family="plan",
        ),
        "tuner": "rule_tuner",
        "proposed_values": {"domain_match_boost": 1.4},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


def _gated_key_proposal(**kw) -> ParameterProposal:
    """Resolves fine, but ``domain_match_boost``'s use site is domain-gated."""
    defaults: dict = {
        "proposal_id": "prop_gated_key",
        "scope": ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID),
        "tuner": "rule_tuner",
        "proposed_values": {"domain_match_boost": 1.4},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


# ---------------------------------------------------------------------------
# promote_proposal refuses, for both unreachable shapes
# ---------------------------------------------------------------------------


def test_promote_refuses_the_prod_shape_with_both_reasons(stores):
    """``intent_family`` set and ``domain`` unset, as on 2026-10-03.

    That scope fails both checks, so the refusal names both reasons and
    leaves exactly one ``TUNER_PROPOSAL_REJECTED`` row carrying them.
    """
    params, state, events = stores
    p = _unsupplied_axis_proposal(
        scope=ParameterScope(
            component_id=GRAPH_SEARCH_COMPONENT_ID, intent_family="plan"
        )
    )
    state.put_proposal(p)
    expected = reachability_reasons(p.scope, tuple(p.proposed_values))
    assert {r.kind for r in expected} == {"unsupplied_axis", "gated_key"}

    result = promote_proposal(
        p.proposal_id, tuner_state=state, parameter_store=params, event_log=events
    )

    assert result.status == "rejected"
    assert all(r.detail in result.reason for r in expected)
    rejected = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert [e.payload["reason"] for e in rejected] == [result.reason]
    assert events.get_events(event_type=EventType.PARAMS_UPDATED) == []


def test_promote_refuses_unsupplied_axis_scope(stores):
    params, state, events = stores
    p = _unsupplied_axis_proposal()
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id, tuner_state=state, parameter_store=params, event_log=events
    )

    assert isinstance(result, PromotionResult)
    assert result.status == "rejected"
    assert "unreachable" in result.reason
    assert "intent_family" in result.reason
    assert params.resolve(p.scope) is None
    assert events.get_events(event_type=EventType.PARAMS_UPDATED) == []
    assert state.get_proposal(p.proposal_id).status == "rejected"


def test_promote_refuses_gated_key_scope(stores):
    params, state, events = stores
    p = _gated_key_proposal()
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id, tuner_state=state, parameter_store=params, event_log=events
    )

    assert result.status == "rejected"
    assert "unreachable" in result.reason
    assert "domain_match_boost" in result.reason
    assert events.get_events(event_type=EventType.PARAMS_UPDATED) == []


def test_promote_refuses_unreachable_scope_even_with_force(stores):
    """``force`` skips the policy gate, not the reachability check.

    Mirrors the immutable-core refusal's own force-does-not-unlock
    guarantee (see ``test_immutable_core.py``): a promotion no reader
    will ever resolve is not a risk a caller can opt into.
    """
    params, state, events = stores
    p = _unsupplied_axis_proposal()
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        force=True,
    )

    assert result.status == "rejected"
    assert "unreachable" in result.reason
    assert events.get_events(event_type=EventType.PARAMS_UPDATED) == []


# ---------------------------------------------------------------------------
# preview_promotion mirrors the same refusal (preview/commit parity)
# ---------------------------------------------------------------------------


def test_preview_predicts_rejection_for_unreachable_scope(stores):
    params, state, _events = stores
    p = _unsupplied_axis_proposal()
    state.put_proposal(p)

    preview = preview_promotion(
        p.proposal_id, tuner_state=state, parameter_store=params
    )

    assert isinstance(preview, PromotionPreview)
    assert preview.status == "rejected"
    assert "unreachable" in preview.reason


def test_preview_predicts_rejection_for_unreachable_scope_even_with_force(stores):
    params, state, _events = stores
    p = _gated_key_proposal()
    state.put_proposal(p)

    preview = preview_promotion(
        p.proposal_id, tuner_state=state, parameter_store=params, force=True
    )

    assert preview.status == "rejected"
    assert "unreachable" in preview.reason


# ---------------------------------------------------------------------------
# Regression guard: a reachable, baselined, passing proposal still promotes
# ---------------------------------------------------------------------------


def test_promote_still_promotes_a_reachable_baselined_passing_proposal(stores):
    params, state, events = stores
    scope = ParameterScope(component_id=GRAPH_SEARCH_COMPONENT_ID, domain="orders")
    params.put(
        ParameterSet(scope=scope, values={"domain_match_boost": 1.0}, source="op")
    )
    p = ParameterProposal(
        proposal_id="prop_reachable_ok",
        scope=scope,
        tuner="rule_tuner",
        proposed_values={"domain_match_boost": 1.4},
        sample_size=30,
    )
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id, tuner_state=state, parameter_store=params, event_log=events
    )

    assert result.status == "promoted"
    assert result.params_version is not None
    assert len(events.get_events(event_type=EventType.PARAMS_UPDATED)) == 1
