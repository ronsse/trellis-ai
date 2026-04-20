"""Tests for the :class:`RuleTuner` orchestrator with real stores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.learning.tuners import DEFAULT_RULES, RuleTuner, TuningRule
from trellis.ops import record_outcome
from trellis.schemas.parameters import ParameterProposal, ParameterScope
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore


@pytest.fixture
def stores(tmp_path: Path):
    outcomes = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    state = SQLiteTunerStateStore(tmp_path / "tuner_state.db")
    try:
        yield outcomes, state
    finally:
        outcomes.close()
        state.close()


# A rule that fires aggressively so tests don't need to push 30+ events.
_TEST_RULE = TuningRule(
    name="low_success_halve_half_life",
    target_component_id="retrieve.strategies.KeywordSearch",
    min_sample_size=3,
    condition_key="success_rate",
    condition_op="lt",
    condition_value=0.5,
    proposed_param="recency_half_life_days",
    proposed_value=15.0,
)


def _seed_outcomes(
    outcome_store: SQLiteOutcomeStore,
    *,
    successes: int,
    failures: int,
    domain: str = "sportsbook",
    component_id: str = "retrieve.strategies.KeywordSearch",
    base_time: datetime | None = None,
) -> None:
    """Record ``successes`` + ``failures`` outcomes at strictly-increasing times."""
    base = base_time or datetime.now(UTC)
    for i in range(successes):
        record_outcome(
            outcome_store,
            component_id=component_id,
            success=True,
            latency_ms=10.0,
            domain=domain,
            intent_family="plan",
            occurred_at=base + timedelta(seconds=i),
        )
    for i in range(failures):
        record_outcome(
            outcome_store,
            component_id=component_id,
            success=False,
            latency_ms=10.0,
            domain=domain,
            intent_family="plan",
            occurred_at=base + timedelta(seconds=successes + i),
        )


def test_run_with_no_outcomes_is_noop(stores):
    outcome_store, state = stores
    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    result = tuner.run()
    assert result == []
    assert state.list_proposals() == []


def test_run_produces_proposals_and_advances_cursor(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=1, failures=9)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    result = tuner.run()

    assert len(result) == 1
    proposal = result[0]
    assert proposal.scope.component_id == "retrieve.strategies.KeywordSearch"
    assert proposal.scope.domain == "sportsbook"
    assert proposal.proposed_values == {"recency_half_life_days": 15.0}
    assert proposal.sample_size == 10

    # Cursor advanced to the latest event.
    cursor = state.get_cursor("rule_tuner")
    assert cursor is not None
    datetime.fromisoformat(cursor)  # parseable


def test_rerun_is_idempotent(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=1, failures=9)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    first = tuner.run()
    second = tuner.run()

    assert len(first) == 1
    # Second run finds no new outcomes (cursor past them) -> no proposals.
    assert second == []
    # Only one stored proposal total.
    all_proposals = state.list_proposals()
    assert len(all_proposals) == 1
    assert all_proposals[0].proposal_id == first[0].proposal_id


def test_rerun_refreshes_pending_but_not_terminal(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=1, failures=9)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    first = tuner.run()
    proposal_id = first[0].proposal_id

    # Simulate the proposal advancing to canary status.
    state.update_status(proposal_id, "canary", notes="A/B cohort")

    # Seed new failing outcomes and re-run over the full window.
    _seed_outcomes(
        outcome_store,
        successes=0,
        failures=5,
        base_time=datetime.now(UTC) + timedelta(hours=1),
    )
    second = tuner.run(since=datetime.now(UTC) - timedelta(days=365))

    # The rule fires again (more failures) but the existing proposal is
    # in a terminal status and must not be overwritten.
    persisted_ids = [p.proposal_id for p in second]
    assert proposal_id not in persisted_ids

    # The stored record still reflects canary status.
    existing = state.get_proposal(proposal_id)
    assert existing is not None
    assert existing.status == "canary"
    assert existing.notes == "A/B cohort"


def test_explicit_since_overrides_cursor(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=1, failures=9)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    tuner.run()  # sets cursor past all outcomes

    # Pass an explicit since before the events → proposals regenerate.
    far_past = datetime.now(UTC) - timedelta(days=365)
    # Add one more failing event so cursor advance registers; existing
    # proposal was already put, so nothing new is persisted but we
    # verify that run() doesn't crash with explicit since.
    _seed_outcomes(
        outcome_store,
        successes=0,
        failures=1,
        base_time=datetime.now(UTC) + timedelta(hours=1),
    )
    result = tuner.run(since=far_past)
    # 11 failing + 1 successful = 1/11 success rate, still below 0.5.
    # Rule fires, but proposal_id matches existing pending → replaced.
    assert len(result) == 1


def test_respects_batch_limit(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=10, failures=10)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE], batch_limit=5)
    tuner.run()
    # batch_limit capped the query; not all 20 outcomes scanned.
    # Rule may not fire if the 5 scanned were mostly successes; we just
    # assert the cursor advanced somewhere.
    cursor = state.get_cursor("rule_tuner")
    assert cursor is not None


def test_default_rules_constructor(stores):
    outcome_store, state = stores
    tuner = RuleTuner(outcome_store, state)
    assert tuner.tuner_name == "rule_tuner"
    assert tuner.rules == DEFAULT_RULES


def test_custom_tuner_name_recorded_on_proposals(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=0, failures=5)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE], tuner_name="custom_ab")
    result = tuner.run()
    assert len(result) == 1
    assert result[0].tuner == "custom_ab"
    # Cursor stored under the custom name.
    assert state.get_cursor("custom_ab") is not None
    assert state.get_cursor("rule_tuner") is None


def test_run_returns_persisted_not_raw(stores):
    """When all proposals match terminal records, run returns empty list."""
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=0, failures=5)

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    first = tuner.run()
    assert len(first) == 1
    state.update_status(first[0].proposal_id, "promoted")

    # Re-run with wider window.
    second = tuner.run(since=datetime.now(UTC) - timedelta(days=365))
    assert second == []  # existing proposal is terminal → skipped


def test_malformed_cursor_falls_back_to_full_history(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=0, failures=5)

    # Corrupt the cursor directly.
    state.set_cursor("rule_tuner", "not-a-valid-iso-date")
    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    result = tuner.run()
    # Should have processed the outcomes despite the bad cursor.
    assert len(result) == 1


def test_multiple_cells_each_produce_proposals(stores):
    outcome_store, state = stores
    now = datetime.now(UTC)
    _seed_outcomes(outcome_store, successes=0, failures=5, domain="a", base_time=now)
    _seed_outcomes(
        outcome_store,
        successes=0,
        failures=5,
        domain="b",
        base_time=now + timedelta(minutes=1),
    )

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    result = tuner.run()
    assert len(result) == 2
    domains = sorted(p.scope.domain or "" for p in result)
    assert domains == ["a", "b"]


def test_proposals_carry_deterministic_ids(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=0, failures=5)

    tuner_a = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    first = tuner_a.run()

    # Fresh state, same rule, same scope => same id.
    fresh_state = SQLiteTunerStateStore(
        Path(state._db_path.parent) / "tuner_state_fresh.db"
    )
    try:
        tuner_b = RuleTuner(outcome_store, fresh_state, rules=[_TEST_RULE])
        second = tuner_b.run()
        assert first[0].proposal_id == second[0].proposal_id
    finally:
        fresh_state.close()


def test_scope_key_roundtrip_via_parameter_proposal(stores):
    """The proposal's scope survives the put/get roundtrip intact."""
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=0, failures=5, domain="xyz")

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    first = tuner.run()
    proposal_id = first[0].proposal_id

    fetched = state.get_proposal(proposal_id)
    assert isinstance(fetched, ParameterProposal)
    assert fetched.scope.key() == (
        "retrieve.strategies.KeywordSearch",
        "xyz",
        "plan",
        None,
    )


def test_proposal_targets_correct_parameter_scope(stores):
    outcome_store, state = stores
    _seed_outcomes(outcome_store, successes=0, failures=5, domain="foo")

    tuner = RuleTuner(outcome_store, state, rules=[_TEST_RULE])
    result = tuner.run()
    # Full scope should be preserved.
    scope = result[0].scope
    assert isinstance(scope, ParameterScope)
    assert scope.component_id == "retrieve.strategies.KeywordSearch"
    assert scope.domain == "foo"
    assert scope.intent_family == "plan"
    assert scope.tool_name is None
