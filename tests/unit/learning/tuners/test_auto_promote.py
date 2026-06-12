"""Tests for Tier-1 auto-promotion (:mod:`trellis.learning.tuners.auto_promote`).

Covers the WP9 contract:

* disabled policy => zero behaviour change (no mutations, no events);
* below-threshold proposals stay ``pending`` (reported, never rejected);
* a qualifying proposal auto-promotes and emits ``PARAMS_AUTO_PROMOTED``;
* synthetic degradation triggers an auto-rollback and emits
  ``PARAMS_AUTO_ROLLED_BACK``;
* an integration-style promote -> degrade -> rollback walk asserting every
  event in the EventLog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.learning.tuners import (
    AutoPromotePolicy,
    PostPromotionPolicy,
    RuleTuner,
    TuningRule,
    run_auto_promotion,
)
from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore

COMPONENT = "retrieve.strategies.KeywordSearch"
SCOPE = ParameterScope(component_id=COMPONENT)

# A rule that fires on a low success rate and proposes a large half-life
# change — chosen so the effect size against the baseline clears the strict
# 0.25 auto threshold (baseline 30 -> proposed 15 is a 0.5 relative delta).
_LOW_SUCCESS_RULE = TuningRule(
    name="auto_test_halve_half_life",
    target_component_id=COMPONENT,
    min_sample_size=30,
    condition_key="success_rate",
    condition_op="lt",
    condition_value=0.5,
    proposed_param="recency_half_life_days",
    proposed_value=15.0,
    description="auto-promote test rule",
)


@pytest.fixture
def stores(tmp_path: Path):
    params = SQLiteParameterStore(tmp_path / "parameters.db")
    outcomes = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    events = SQLiteEventLog(tmp_path / "events.db")
    tuner_state = SQLiteTunerStateStore(tmp_path / "tuner_state.db")
    try:
        yield params, outcomes, events, tuner_state
    finally:
        params.close()
        outcomes.close()
        events.close()
        tuner_state.close()


def _seed_baseline(params: SQLiteParameterStore, value: float = 30.0) -> ParameterSet:
    """Put an active baseline snapshot so promotions have something to beat."""
    return params.put(
        ParameterSet(
            scope=SCOPE,
            values={"recency_half_life_days": value},
            source="test:baseline",
        )
    )


def _record_outcomes(
    outcomes: SQLiteOutcomeStore,
    *,
    params_version: str | None,
    success: int,
    failure: int,
    at: datetime,
    domain: str | None = None,
) -> None:
    """Append success/failure OutcomeEvents.

    ``domain`` lets a caller park baseline/post-promotion outcomes in a
    *separate* tuner aggregation cell (the tuner buckets by
    ``(component_id, domain, intent_family, tool_name)``) while the
    post-promotion monitor still finds them by ``params_version`` alone.
    """
    batch = [
        OutcomeEvent(
            component_id=COMPONENT,
            params_version=params_version,
            domain=domain,
            occurred_at=at,
            outcome=ComponentOutcome(success=True, latency_ms=5.0),
        )
        for _ in range(success)
    ]
    batch.extend(
        OutcomeEvent(
            component_id=COMPONENT,
            params_version=params_version,
            domain=domain,
            occurred_at=at,
            outcome=ComponentOutcome(success=False, latency_ms=5.0),
        )
        for _ in range(failure)
    )
    outcomes.append_many(batch)


def _make_tuner(
    outcomes: SQLiteOutcomeStore, tuner_state: SQLiteTunerStateStore
) -> RuleTuner:
    return RuleTuner(
        outcomes,
        tuner_state,
        tuner_name="rule_tuner",
        rules=(_LOW_SUCCESS_RULE,),
    )


def _event_types(events: SQLiteEventLog) -> list[str]:
    return [e.event_type.value for e in events.get_events(limit=1000)]


# ---------------------------------------------------------------------------
# Policy invariants
# ---------------------------------------------------------------------------


def test_policy_defaults_are_stricter_than_manual() -> None:
    from trellis.learning.tuners.promotion import (
        DEFAULT_MIN_EFFECT_SIZE,
        DEFAULT_MIN_SAMPLE_SIZE,
    )

    policy = AutoPromotePolicy()
    assert policy.enabled is False
    assert policy.min_sample_size > DEFAULT_MIN_SAMPLE_SIZE
    assert policy.min_effect_size > DEFAULT_MIN_EFFECT_SIZE
    assert policy.post_promotion.auto_demote is True


def test_policy_rejects_looser_than_manual() -> None:
    with pytest.raises(ValueError, match="looser than the manual"):
        AutoPromotePolicy(min_sample_size=4)
    with pytest.raises(ValueError, match="looser than the manual"):
        AutoPromotePolicy(min_effect_size=0.10)


def test_policy_rejects_disarmed_rollback() -> None:
    with pytest.raises(ValueError, match="auto_demote must be True"):
        AutoPromotePolicy(post_promotion=PostPromotionPolicy(auto_demote=False))


# ---------------------------------------------------------------------------
# Disabled => zero behaviour change
# ---------------------------------------------------------------------------


def test_disabled_policy_promotes_nothing_and_emits_nothing(stores) -> None:
    params, outcomes, events, tuner_state = stores
    at = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_baseline(params)
    # 40 outcomes at 25% success -> the rule fires (success_rate < 0.5, n>=30).
    _record_outcomes(outcomes, params_version=None, success=10, failure=30, at=at)

    report = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=AutoPromotePolicy(enabled=False),
        now=at + timedelta(days=1),
    )

    assert report.enabled is False
    assert report.auto_promoted == 0
    assert report.proposals_considered >= 1
    # Proposal still exists and stays pending — tuner ran, but no promotion.
    proposals = tuner_state.list_proposals()
    assert proposals
    assert all(p.status == "pending" for p in proposals)
    # No governance or tier-1 events emitted.
    assert _event_types(events) == []
    # Disabled outcomes report as "disabled" disposition (would-qualify subset).
    assert any(o.disposition == "disabled" for o in report.outcomes)


# ---------------------------------------------------------------------------
# Below-threshold proposals stay pending
# ---------------------------------------------------------------------------


def test_below_threshold_stays_pending(stores) -> None:
    params, outcomes, events, tuner_state = stores
    at = datetime(2026, 6, 1, tzinfo=UTC)
    # Baseline 16.0 -> proposed 15.0 is only a ~0.06 relative delta, below the
    # strict 0.25 auto floor, so the proposal must stay pending even enabled.
    _seed_baseline(params, value=16.0)
    _record_outcomes(outcomes, params_version=None, success=10, failure=30, at=at)

    report = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=AutoPromotePolicy(enabled=True),
        now=at + timedelta(days=1),
    )

    assert report.auto_promoted == 0
    assert report.pending_manual >= 1
    pending = [o for o in report.outcomes if o.disposition == "pending_manual"]
    assert pending
    assert "min_effect_size" in pending[0].reason
    # The proposal is left pending — NOT rejected — for manual review.
    proposals = tuner_state.list_proposals()
    assert all(p.status == "pending" for p in proposals)
    assert _event_types(events) == []


# ---------------------------------------------------------------------------
# Qualifying proposal auto-promotes + emits event
# ---------------------------------------------------------------------------


def test_qualifying_proposal_auto_promotes(stores) -> None:
    params, outcomes, events, tuner_state = stores
    at = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_baseline(params, value=30.0)  # 30 -> 15 is a 0.5 delta, clears 0.25.
    _record_outcomes(outcomes, params_version=None, success=10, failure=30, at=at)

    report = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=AutoPromotePolicy(enabled=True),
        now=at + timedelta(days=1),
    )

    assert report.auto_promoted == 1
    promoted = [o for o in report.outcomes if o.disposition == "auto_promoted"]
    assert len(promoted) == 1
    assert promoted[0].params_version is not None
    # Proposal moved to terminal "promoted".
    proposal = tuner_state.get_proposal(promoted[0].proposal_id)
    assert proposal is not None
    assert proposal.status == "promoted"
    # Both the governance event and the dedicated tier-1 event are present.
    types = _event_types(events)
    assert EventType.PARAMS_UPDATED.value in types
    assert EventType.PARAMS_AUTO_PROMOTED.value in types
    # No rollback (no post-promotion degradation seeded).
    assert EventType.PARAMS_AUTO_ROLLED_BACK.value not in types


# ---------------------------------------------------------------------------
# Degradation triggers auto-rollback + emits event
# ---------------------------------------------------------------------------


def test_degradation_triggers_auto_rollback(stores) -> None:
    """Promote on pass 1, degrade, then pass 2's sweep auto-rolls-back.

    The promotion event's ``occurred_at`` is real wall-clock time (the
    governance pipeline stamps it), so baseline and post-promotion outcomes
    are anchored to ``utc_now()`` — mirroring ``test_rollback.py``.
    """
    from trellis.core.base import utc_now

    params, outcomes, events, tuner_state = stores
    real_now = utc_now()
    baseline = _seed_baseline(params, value=30.0)

    # Baseline outcomes: 90% success in the window *before* promotion.
    _record_outcomes(
        outcomes,
        params_version=baseline.params_version,
        success=90,
        failure=10,
        domain="monitoring",
        at=real_now - timedelta(days=2),
    )
    # Firing outcomes (25% success, n=40) so the rule fires — any past time.
    _record_outcomes(
        outcomes,
        params_version=None,
        success=10,
        failure=30,
        at=real_now - timedelta(days=1),
    )

    policy = AutoPromotePolicy(enabled=True)

    # Pass 1 — promote. No post-promotion outcomes yet, so no rollback.
    report1 = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=policy,
    )
    assert report1.auto_promoted == 1
    assert report1.rolled_back == 0
    promoted_version = report1.outcomes[0].params_version
    assert promoted_version is not None

    # Degrade: seed 27% success for the promoted version, just after promotion.
    _record_outcomes(
        outcomes,
        params_version=promoted_version,
        success=8,
        failure=22,
        domain="monitoring",
        at=real_now + timedelta(minutes=1),
    )

    # Pass 2 — the prior-promotion sweep catches the degradation and rolls
    # back through the production code path. ``now`` is advanced so the
    # post-promotion outcomes fall inside the monitor's lookback window.
    report2 = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=policy,
        now=real_now + timedelta(days=1),
    )
    assert report2.rolled_back == 1
    types = _event_types(events)
    assert EventType.PARAMETERS_DEGRADED.value in types
    assert EventType.PARAMS_AUTO_ROLLED_BACK.value in types


# ---------------------------------------------------------------------------
# Integration walk: promote -> degrade -> rollback, all events asserted
# ---------------------------------------------------------------------------


def test_integration_promote_degrade_rollback_full_event_trail(stores) -> None:
    """End-to-end tier-1 walk through the public ``run_auto_promotion`` API.

    Seeded stores, ``tmp_path`` fixtures, two periodic passes (promote, then
    degrade-and-sweep-rollback), with every event asserted in the EventLog.
    No private helper drives the rollback — it goes through the same
    ``run_auto_promotion`` the ``trellis worker tune`` command calls.
    """
    from trellis.core.base import utc_now

    params, outcomes, events, tuner_state = stores
    real_now = utc_now()
    baseline = _seed_baseline(params, value=30.0)

    # Baseline window: 90% success before promotion.
    _record_outcomes(
        outcomes,
        params_version=baseline.params_version,
        success=90,
        failure=10,
        domain="monitoring",
        at=real_now - timedelta(days=2),
    )
    # Firing outcomes for the tuner (25% success, n=40).
    _record_outcomes(
        outcomes,
        params_version=None,
        success=10,
        failure=30,
        at=real_now - timedelta(days=1),
    )

    policy = AutoPromotePolicy(enabled=True)

    # Pass 1 — promote.
    report = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=policy,
    )
    assert report.auto_promoted == 1
    promoted_version = report.outcomes[0].params_version
    assert promoted_version is not None
    # After pass 1: promotion events present, no rollback yet.
    types_after_promote = _event_types(events)
    assert EventType.PARAMS_UPDATED.value in types_after_promote
    assert EventType.PARAMS_AUTO_PROMOTED.value in types_after_promote
    assert EventType.PARAMS_AUTO_ROLLED_BACK.value not in types_after_promote

    # Degrade: 27% success for the promoted version (a ~63pp drop).
    _record_outcomes(
        outcomes,
        params_version=promoted_version,
        success=8,
        failure=22,
        domain="monitoring",
        at=real_now + timedelta(minutes=1),
    )

    # Pass 2 — sweep catches the degradation and rolls back. ``now`` advanced
    # so the post-promotion outcomes fall inside the monitor's window.
    report2 = run_auto_promotion(
        tuner=_make_tuner(outcomes, tuner_state),
        parameter_store=params,
        tuner_state=tuner_state,
        outcome_store=outcomes,
        event_log=events,
        policy=policy,
        now=real_now + timedelta(days=1),
    )
    assert report2.rolled_back == 1
    rollback_outcome = next(o for o in report2.outcomes if o.rolled_back_to is not None)
    rollback_version = rollback_outcome.rolled_back_to
    assert rollback_version is not None

    # Full event trail in the log:
    types = _event_types(events)
    assert EventType.PARAMS_UPDATED.value in types  # promotion
    assert EventType.PARAMS_AUTO_PROMOTED.value in types
    assert EventType.PARAMETERS_DEGRADED.value in types
    assert EventType.PARAMS_AUTO_ROLLED_BACK.value in types
    # At least two PARAMS_UPDATED — the promotion and the rollback snapshot.
    assert types.count(EventType.PARAMS_UPDATED.value) >= 2

    # The active snapshot is now the rollback, restoring the baseline values.
    active = params.get_active(SCOPE)
    assert active is not None
    assert active.params_version == rollback_version
    assert active.values["recency_half_life_days"] == 30.0

    # Reusable policy logic lives in the library, not the CLI.
    from trellis.learning.tuners.auto_promote import _promote_and_monitor

    assert callable(_promote_and_monitor)
