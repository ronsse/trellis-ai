"""Tests for the promote_proposal governance pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.learning.tuners import (
    PromotionPolicy,
    PromotionResult,
    promote_proposal,
)
from trellis.ops import ParameterRegistry
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


def _proposal(**kw) -> ParameterProposal:
    defaults: dict = {
        "proposal_id": "prop_test",
        "scope": ParameterScope(
            component_id="retrieve.strategies.KeywordSearch", domain="a"
        ),
        "tuner": "rule_tuner",
        "proposed_values": {"recency_half_life_days": 15.0},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


# ---------------------------------------------------------------------------
# Missing / terminal proposals
# ---------------------------------------------------------------------------


def test_promote_missing_proposal_is_skipped(stores):
    params, state, events = stores
    result = promote_proposal(
        "prop_does_not_exist",
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    assert isinstance(result, PromotionResult)
    assert result.status == "skipped"
    assert result.reason == "proposal_not_found"
    assert events.count() == 0


def test_promote_already_promoted_proposal_is_skipped(stores):
    params, state, events = stores
    p = _proposal()
    state.put_proposal(p)
    state.update_status(p.proposal_id, "promoted")

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    assert result.status == "skipped"
    assert result.reason == "proposal_already_promoted"


def test_promote_already_rejected_proposal_is_skipped(stores):
    params, state, events = stores
    p = _proposal()
    state.put_proposal(p)
    state.update_status(p.proposal_id, "rejected")

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------


def test_promote_rejected_on_low_sample_size(stores):
    params, state, events = stores
    p = _proposal(sample_size=3)
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(min_sample_size=10),
    )
    assert result.status == "rejected"
    assert "sample_size=3" in result.reason
    # Proposal record reflects rejection.
    assert state.get_proposal(p.proposal_id).status == "rejected"
    # Audit event emitted.
    rejections = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert len(rejections) == 1
    assert rejections[0].payload["reason"].startswith("sample_size=3")


def test_promote_rejected_on_insufficient_effect(stores):
    params, state, events = stores
    # Baseline: half-life 30.  Proposal: 29 (tiny change ~3 %).
    params.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"recency_half_life_days": 30.0},
        )
    )
    p = _proposal(proposed_values={"recency_half_life_days": 29.0})
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(min_effect_size=0.15),
    )
    assert result.status == "rejected"
    assert "effect_size" in result.reason


def test_promote_succeeds_with_sufficient_effect(stores):
    params, state, events = stores
    params.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"recency_half_life_days": 30.0},
        )
    )
    p = _proposal(proposed_values={"recency_half_life_days": 15.0})
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    assert result.status == "promoted"
    assert result.params_version is not None
    # Effect = |15 - 30| / 30 = 0.5
    assert result.effect_size == pytest.approx(0.5)

    # Proposal flipped to promoted.
    assert state.get_proposal(p.proposal_id).status == "promoted"

    # New snapshot is the active one.
    active = params.get_active(
        ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a")
    )
    assert active is not None
    assert active.values["recency_half_life_days"] == 15.0
    assert active.source == "tuner:rule_tuner"

    # Audit event.
    events_list = events.get_events(event_type=EventType.PARAMS_UPDATED)
    assert len(events_list) == 1
    assert events_list[0].payload["params_version"] == result.params_version
    assert events_list[0].payload["effect_size"] == pytest.approx(0.5)


def test_promote_with_no_baseline_bootstrap(stores):
    params, state, events = stores
    # No existing ParameterSet for the scope.
    p = _proposal()
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_no_baseline=True),
    )
    assert result.status == "promoted"
    assert result.effect_size == float("inf") or result.effect_size is None


def test_promote_no_baseline_disallowed(stores):
    params, state, events = stores
    p = _proposal()
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_no_baseline=False),
    )
    assert result.status == "rejected"
    assert result.reason == "no_baseline_snapshot_for_scope"


# ---------------------------------------------------------------------------
# Merging behaviour
# ---------------------------------------------------------------------------


def test_promote_merges_with_baseline(stores):
    """Proposal touching one key should preserve other baseline keys."""
    params, state, events = stores
    scope = ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a")
    params.put(
        ParameterSet(
            scope=scope,
            values={"recency_half_life_days": 30.0, "recency_floor": 0.3},
        )
    )
    # Proposal only touches recency_half_life_days.
    p = _proposal(proposed_values={"recency_half_life_days": 15.0})
    state.put_proposal(p)

    promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )

    active = params.get_active(scope)
    assert active is not None
    assert active.values == {"recency_half_life_days": 15.0, "recency_floor": 0.3}


# ---------------------------------------------------------------------------
# Registry invalidation
# ---------------------------------------------------------------------------


def test_promote_invalidates_registry_cache(stores):
    params, state, events = stores
    scope = ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a")
    params.put(ParameterSet(scope=scope, values={"recency_half_life_days": 30.0}))

    reg = ParameterRegistry(params)
    # Prime the cache.
    assert reg.get(scope, "recency_half_life_days", 0.0) == 30.0

    p = _proposal(proposed_values={"recency_half_life_days": 15.0})
    state.put_proposal(p)

    promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        parameter_registry=reg,
    )

    # Registry should see the new value after invalidation.
    assert reg.get(scope, "recency_half_life_days", 0.0) == 15.0


# ---------------------------------------------------------------------------
# Force flag
# ---------------------------------------------------------------------------


def test_force_bypasses_policy(stores):
    params, state, events = stores
    params.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"recency_half_life_days": 30.0},
        )
    )
    # Effect way below min_effect_size.
    p = _proposal(proposed_values={"recency_half_life_days": 29.9}, sample_size=2)
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        force=True,
    )
    assert result.status == "promoted"

    ev = events.get_events(event_type=EventType.PARAMS_UPDATED)[0]
    assert ev.payload["force"] is True


# ---------------------------------------------------------------------------
# Non-numeric proposals
# ---------------------------------------------------------------------------


def test_promote_string_value_bypasses_effect_size(stores):
    params, state, events = stores
    scope = ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a")
    params.put(ParameterSet(scope=scope, values={"mode": "standard"}))

    p = _proposal(proposed_values={"mode": "aggressive"})
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    assert result.status == "promoted"


# ---------------------------------------------------------------------------
# Event payload shape
# ---------------------------------------------------------------------------


def test_params_updated_event_payload(stores):
    params, state, events = stores
    params.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"recency_half_life_days": 30.0},
        )
    )
    p = _proposal(proposed_values={"recency_half_life_days": 15.0})
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    ev = events.get_events(event_type=EventType.PARAMS_UPDATED)[0]
    assert ev.entity_id == result.params_version
    assert ev.entity_type == "parameter_set"
    payload = ev.payload
    assert payload["proposal_id"] == p.proposal_id
    assert payload["scope"] == list(p.scope.key())
    assert payload["proposed_values"] == {"recency_half_life_days": 15.0}
    assert payload["baseline_values"] == {"recency_half_life_days": 30.0}
    assert payload["sample_size"] == p.sample_size
    assert payload["tuner"] == "rule_tuner"
    assert payload["force"] is False
