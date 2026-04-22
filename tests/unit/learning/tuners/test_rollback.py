"""Tests for :mod:`trellis.learning.tuners.rollback` — Gap 2.2."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trellis.learning.tuners import (
    PostPromotionPolicy,
    PostPromotionReport,
    monitor_post_promotion,
    run_post_promotion_sweep,
)
from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore
from trellis.stores.sqlite.parameter import SQLiteParameterStore

SCOPE = ParameterScope(
    component_id="retrieve.strategies.KeywordSearch", domain="platform"
)


@pytest.fixture
def stores(tmp_path: Path):
    params = SQLiteParameterStore(tmp_path / "parameters.db")
    outcomes = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    events = SQLiteEventLog(tmp_path / "events.db")
    try:
        yield params, outcomes, events
    finally:
        params.close()
        outcomes.close()
        events.close()


def _make_params(values: dict, scope: ParameterScope = SCOPE) -> ParameterSet:
    return ParameterSet(scope=scope, values=values, source="test")


def _emit_promotion(
    events: SQLiteEventLog,
    *,
    params_version: str,
    baseline_version: str | None,
    scope: ParameterScope = SCOPE,
) -> datetime:
    """Record a PARAMS_UPDATED event, returning its occurred_at."""
    event = events.emit(
        EventType.PARAMS_UPDATED,
        source="tuner.promotion",
        entity_id=params_version,
        entity_type="parameter_set",
        payload={
            "params_version": params_version,
            "baseline_version": baseline_version,
            "scope": list(scope.key()),
        },
    )
    return event.occurred_at


def _record_outcomes(
    outcomes: SQLiteOutcomeStore,
    *,
    params_version: str,
    success_count: int,
    failure_count: int,
    at: datetime,
) -> None:
    def _event(success: bool) -> OutcomeEvent:
        return OutcomeEvent(
            component_id=SCOPE.component_id,
            params_version=params_version,
            domain=SCOPE.domain,
            occurred_at=at,
            outcome=ComponentOutcome(success=success, latency_ms=10.0),
        )

    batch = [_event(True) for _ in range(success_count)]
    batch.extend(_event(False) for _ in range(failure_count))
    outcomes.append_many(batch)


# ---------------------------------------------------------------------------
# Edge-case verdicts — no version, no promotion event, no baseline
# ---------------------------------------------------------------------------


def test_unknown_params_version(stores):
    params, outcomes, events = stores
    report = monitor_post_promotion(
        "does-not-exist",
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
    )
    assert report.verdict == "unknown_version"
    assert report.action == "none"


def test_no_promotion_event(stores):
    params, outcomes, events = stores
    stored = params.put(_make_params({"k": 1.0}))
    report = monitor_post_promotion(
        stored.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
    )
    assert report.verdict == "no_promotion_event"


def test_no_baseline_on_promotion_event(stores):
    params, outcomes, events = stores
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events, params_version=promoted.params_version, baseline_version=None
    )
    # Enough post-promotion outcomes to pass the sample gate.
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=20,
        failure_count=5,
        at=promoted_at + timedelta(hours=1),
    )
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "no_baseline"
    assert report.post_samples == 25
    assert report.action == "none"


# ---------------------------------------------------------------------------
# Insufficient samples
# ---------------------------------------------------------------------------


def test_insufficient_post_samples(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    # Only 3 outcomes — default min is 20.
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=1,
        failure_count=2,
        at=promoted_at + timedelta(hours=1),
    )
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "insufficient_samples"
    assert report.post_samples == 3


# ---------------------------------------------------------------------------
# OK vs degraded verdicts
# ---------------------------------------------------------------------------


def test_ok_when_no_regression(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    # Baseline: 80% success (before promotion)
    _record_outcomes(
        outcomes,
        params_version=baseline.params_version,
        success_count=80,
        failure_count=20,
        at=promoted_at - timedelta(days=2),
    )
    # Post: 85% success (improvement!)
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=85,
        failure_count=15,
        at=promoted_at + timedelta(hours=1),
    )
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "ok"
    assert report.degradation is not None
    assert report.degradation < 0
    assert events.count(event_type=EventType.PARAMETERS_DEGRADED) == 0


def test_degraded_signal_only_by_default(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    # Baseline outcomes: 90 successes out of 100.
    _record_outcomes(
        outcomes,
        params_version=baseline.params_version,
        success_count=90,
        failure_count=10,
        at=promoted_at - timedelta(days=2),
    )
    # Post: 70% success — 20pt drop, above the 10pt default threshold.
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=70,
        failure_count=30,
        at=promoted_at + timedelta(hours=1),
    )
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "degraded"
    assert report.action == "event_only"
    assert report.demoted_version is None
    # Event emitted for drift dashboard.
    degraded_events = events.get_events(event_type=EventType.PARAMETERS_DEGRADED)
    assert len(degraded_events) == 1
    payload = degraded_events[0].payload
    assert payload["params_version"] == promoted.params_version
    assert payload["baseline_version"] == baseline.params_version
    assert payload["auto_demote"] is False
    assert payload["degradation"] == pytest.approx(0.20, abs=0.001)


def test_auto_demote_writes_rollback(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    _record_outcomes(
        outcomes,
        params_version=baseline.params_version,
        success_count=90,
        failure_count=10,
        at=promoted_at - timedelta(days=2),
    )
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=65,
        failure_count=35,
        at=promoted_at + timedelta(hours=1),
    )
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        policy=PostPromotionPolicy(auto_demote=True),
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "degraded"
    assert report.action == "demoted"
    assert report.demoted_version is not None

    # Rollback snapshot restores baseline values.
    rollback = params.get(report.demoted_version)
    assert rollback is not None
    assert rollback.values == baseline.values
    assert rollback.source == "tuner:rollback"
    assert rollback.metadata["reverted_from"] == promoted.params_version

    # Audit: PARAMETERS_DEGRADED + a rollback PARAMS_UPDATED.
    assert events.count(event_type=EventType.PARAMETERS_DEGRADED) == 1
    rollback_events = [
        e
        for e in events.get_events(event_type=EventType.PARAMS_UPDATED)
        if e.payload.get("reverted_from") == promoted.params_version
    ]
    assert len(rollback_events) == 1
    assert rollback_events[0].payload["tuner"] == "rollback"


def test_missing_baseline_snapshot_falls_back_to_event_only(stores):
    params, outcomes, events = stores
    # Emit a promotion event with a baseline_version pointing at a
    # nonexistent ParameterSet (e.g. the record was pruned).
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version="does-not-exist",
    )
    # Need baseline outcomes or we'd hit "no_baseline" earlier.
    _record_outcomes(
        outcomes,
        params_version="does-not-exist",
        success_count=90,
        failure_count=10,
        at=promoted_at - timedelta(days=2),
    )
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=60,
        failure_count=40,
        at=promoted_at + timedelta(hours=1),
    )
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        policy=PostPromotionPolicy(auto_demote=True),
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "degraded"
    # auto_demote requested but baseline missing → event only.
    assert report.action == "event_only"
    assert report.demoted_version is None


def test_no_baseline_outcomes_in_window(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=20,
        failure_count=5,
        at=promoted_at + timedelta(hours=1),
    )
    # No baseline outcomes at all.
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    assert report.verdict == "no_baseline"
    assert report.baseline_samples == 0


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def test_sweep_runs_every_promotion_and_skips_rollbacks(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))

    # Original promotion.
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    # A rollback PARAMS_UPDATED event for a different version — the sweep
    # should skip it so a rollback isn't itself candidate for demotion.
    events.emit(
        EventType.PARAMS_UPDATED,
        source="tuner.rollback",
        entity_id="rollback-v",
        entity_type="parameter_set",
        payload={
            "params_version": "rollback-v",
            "baseline_version": promoted.params_version,
            "reverted_from": promoted.params_version,
            "tuner": "rollback",
        },
    )

    _record_outcomes(
        outcomes,
        params_version=baseline.params_version,
        success_count=50,
        failure_count=50,
        at=promoted_at - timedelta(days=2),
    )
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=50,
        failure_count=50,
        at=promoted_at + timedelta(hours=1),
    )

    reports = run_post_promotion_sweep(
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    # Only the original (non-rollback) promotion yields a report.
    assert len(reports) == 1
    assert reports[0].params_version == promoted.params_version


def test_sweep_deduplicates_same_version(stores):
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    # Emit the same promotion event twice (shouldn't happen in practice
    # but the sweep must be idempotent).
    _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    reports = run_post_promotion_sweep(
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
        now=promoted_at + timedelta(days=1),
    )
    assert len(reports) == 1


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_is_frozen_dataclass():
    # Ensures the dataclass stays immutable — guards against accidental
    # in-place mutation by consumers treating it like a dict.
    r = PostPromotionReport(
        params_version="v1",
        baseline_version=None,
        scope=SCOPE,
        post_samples=0,
        baseline_samples=0,
        post_success_rate=None,
        baseline_success_rate=None,
        degradation=None,
        verdict="ok",
        action="none",
    )
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        r.verdict = "degraded"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Default policy timestamp freshness (utc_now default)
# ---------------------------------------------------------------------------


def test_monitor_uses_utc_now_when_no_override(stores):
    """Regression guard — the ``now`` kwarg defaults to utc_now(). A bug
    where the default fired at module import would make the function
    forever compare against an ancient ``now``. The easiest way to prove
    the default is live is to run without an override and confirm the
    report is well-formed for a just-emitted promotion.
    """
    params, outcomes, events = stores
    baseline = params.put(_make_params({"k": 1.0}))
    promoted = params.put(_make_params({"k": 2.0}))
    promoted_at = _emit_promotion(
        events,
        params_version=promoted.params_version,
        baseline_version=baseline.params_version,
    )
    # Record outcomes stamped exactly at the promotion instant — the
    # post window uses ``>=`` on the since bound, so these qualify. Using
    # ``promoted_at`` (not ``+1s``) keeps them inside the ``<= now``
    # bound that defaults to ``utc_now()``.
    _record_outcomes(
        outcomes,
        params_version=promoted.params_version,
        success_count=5,
        failure_count=5,
        at=promoted_at,
    )
    # Default `now` — not overridden. utc_now() is strictly after
    # promoted_at since the event was emitted this call-stack.
    report = monitor_post_promotion(
        promoted.params_version,
        parameter_store=params,
        outcome_store=outcomes,
        event_log=events,
    )
    # Only 10 post samples vs. default min of 20 → insufficient_samples.
    assert report.verdict == "insufficient_samples"
    assert report.post_samples == 10
