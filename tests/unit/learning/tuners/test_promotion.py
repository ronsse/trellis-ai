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
    # Terminal: a sample-size refusal has no recovery path, unlike the two
    # no_baseline_ reasons (see the bootstrap tests below, which assert
    # the opposite, ``terminal is False``).
    assert rejections[0].payload["terminal"] is True


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


def test_promote_no_baseline_refusal_is_not_terminal_and_can_bootstrap_later(stores):
    """A no-baseline refusal leaves the proposal promotable.

    Unlike every other refusal from the three gates, this one does not
    call ``tuner_state.update_status(..., "rejected", ...)`` — the
    store's state is *why* it refused, and ``allow_no_baseline=True`` is
    the documented remedy, so the proposal must still be reachable by a
    second ``promote_proposal`` call with that flag. Before this fix,
    ``update_status`` ran unconditionally and the second call hit
    ``promote_proposal``'s own ``status in {"promoted", "rejected"}``
    short-circuit and returned ``skipped proposal_already_rejected``
    instead — unrecoverable from every surface (#823's gate finding,
    reproduced against the Review-queue REST routes).
    """
    params, state, events = stores
    p = _proposal()
    state.put_proposal(p)

    refused = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_no_baseline=False),
    )
    assert refused.status == "rejected"
    assert refused.reason == "no_baseline_snapshot_for_scope"

    # The stored proposal was NOT moved to a terminal status.
    stored = state.get_proposal(p.proposal_id)
    assert stored.status not in {"promoted", "rejected"}

    rejected_events = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert len(rejected_events) == 1
    assert rejected_events[0].payload["terminal"] is False

    bootstrapped = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_no_baseline=True),
    )
    assert bootstrapped.status == "promoted"
    assert bootstrapped.params_version is not None
    assert state.get_proposal(p.proposal_id).status == "promoted"


def test_promote_no_baseline_for_proposed_keys_refusal_is_recoverable(
    stores,
):
    """The second bootstrap reason (a scope *has* a baseline, but it
    doesn't carry the proposed key) is non-terminal too.

    ``promote_proposal``'s ``terminal=`` classification matches on the
    ``no_baseline_`` prefix, which both ``_apply_policy`` bootstrap
    reasons share (``no_baseline_snapshot_for_scope`` — no snapshot at
    all — and ``no_baseline_for_proposed_keys=...`` — a snapshot that
    omits the proposed key). Only the first reason has a test exercising
    it through ``promote_proposal``; this pins the second so a future
    classification that special-cases one reason string, instead of the
    shared prefix, cannot pass unnoticed.
    """
    params, state, events = stores
    # A baseline exists for the scope, but it doesn't carry the key this
    # proposal proposes — the per-key bootstrap case.
    params.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"unrelated_key": 1.0},
            source="test",
        )
    )
    p = _proposal()  # proposes "recency_half_life_days", absent from baseline
    state.put_proposal(p)

    refused = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_no_baseline=False),
    )
    assert refused.status == "rejected"
    assert refused.reason.startswith("no_baseline_for_proposed_keys=")

    # Non-terminal: the stored proposal stays pending, not rejected.
    stored = state.get_proposal(p.proposal_id)
    assert stored.status not in {"promoted", "rejected"}

    rejected_events = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert len(rejected_events) == 1
    assert rejected_events[0].payload["terminal"] is False

    bootstrapped = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_no_baseline=True),
    )
    assert bootstrapped.status == "promoted"
    assert bootstrapped.params_version is not None
    assert state.get_proposal(p.proposal_id).status == "promoted"


@pytest.mark.parametrize(
    ("baseline_values", "proposed_values", "sample_size", "policy", "reason_prefix"),
    [
        pytest.param(
            None,
            {"recency_half_life_days": 15.0},
            3,
            PromotionPolicy(min_sample_size=10),
            "sample_size=",
            id="sample_size",
        ),
        pytest.param(
            {"recency_half_life_days": 30.0},
            {"recency_half_life_days": 29.0},
            30,
            PromotionPolicy(min_effect_size=0.15),
            "effect_size=",
            id="effect_size",
        ),
        pytest.param(
            {"mode": "hybrid"},
            {"mode": "hybrid"},
            30,
            PromotionPolicy(),
            "zero_effect_proposed_equals_baseline",
            id="zero_effect",
        ),
        pytest.param(
            {"mode": "standard"},
            {"mode": "aggressive"},
            30,
            PromotionPolicy(allow_non_numeric=False),
            "non_numeric_change_disallowed",
            id="non_numeric",
        ),
    ],
)
def test_terminal_refusals_mark_payload_terminal_true_and_store_rejected(
    stores, baseline_values, proposed_values, sample_size, policy, reason_prefix
):
    """Every non-bootstrap refusal is terminal end to end.

    Only the two ``no_baseline_`` reasons are non-terminal (see the
    bootstrap tests above); every other reason ``_apply_policy`` returns
    — low sample size, insufficient effect size, a proposal that is a
    no-op against its baseline, and a disallowed non-numeric change —
    must mark the emitted event's ``terminal`` payload key ``True`` and
    the stored proposal status ``"rejected"``. Before this test, only
    ``test_promote_rejected_on_low_sample_size`` (via its own assertions
    on ``result.status``, not on ``terminal``) touched this arm, and
    mutant G5 (``"terminal": False`` hard-coded) passed the whole suite.
    """
    params, state, events = stores
    scope = ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a")
    if baseline_values is not None:
        params.put(ParameterSet(scope=scope, values=baseline_values, source="test"))

    p = _proposal(proposed_values=proposed_values, sample_size=sample_size)
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=policy,
    )
    assert result.status == "rejected"
    assert result.reason.startswith(reason_prefix)

    assert state.get_proposal(p.proposal_id).status == "rejected"

    rejected_events = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert len(rejected_events) == 1
    assert rejected_events[0].payload["terminal"] is True


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
