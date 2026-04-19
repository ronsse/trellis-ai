"""Tests for SQLiteOutcomeStore."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from trellis.core.base import utc_now
from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    yield s
    s.close()


def _make(**overrides) -> OutcomeEvent:
    kwargs: dict = {
        "component_id": "retrieve.strategies.KeywordSearch",
        "outcome": ComponentOutcome(success=True, latency_ms=5.0),
    }
    kwargs.update(overrides)
    return OutcomeEvent(**kwargs)


def test_append_and_query(store: SQLiteOutcomeStore):
    event = _make(domain="sportsbook", intent_family="plan")
    store.append(event)

    results = store.query()
    assert len(results) == 1
    assert results[0].event_id == event.event_id
    assert results[0].domain == "sportsbook"
    assert results[0].outcome.success is True


def test_append_many(store: SQLiteOutcomeStore):
    events = [_make(domain=f"d{i}") for i in range(5)]
    n = store.append_many(events)
    assert n == 5
    assert store.count() == 5


def test_filter_by_component(store: SQLiteOutcomeStore):
    store.append(_make(component_id="a"))
    store.append(_make(component_id="b"))
    store.append(_make(component_id="a"))
    assert store.count(component_id="a") == 2
    assert store.count(component_id="b") == 1


def test_filter_by_learning_axes(store: SQLiteOutcomeStore):
    store.append(_make(domain="d1", intent_family="plan", tool_name="t1"))
    store.append(_make(domain="d1", intent_family="plan", tool_name="t2"))
    store.append(_make(domain="d2", intent_family="plan", tool_name="t1"))
    assert store.count(domain="d1") == 2
    assert store.count(intent_family="plan") == 3
    assert store.count(tool_name="t1") == 2
    assert store.count(domain="d1", tool_name="t1") == 1


def test_filter_by_phase(store: SQLiteOutcomeStore):
    store.append(_make(phase="retrieve"))
    store.append(_make(phase="assemble"))
    assert store.count(phase="retrieve") == 1


def test_filter_by_params_version(store: SQLiteOutcomeStore):
    store.append(_make(params_version="v1"))
    store.append(_make(params_version="v2"))
    assert store.count(params_version="v1") == 1


def test_filter_by_time(store: SQLiteOutcomeStore):
    now = utc_now()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    store.append(_make())
    assert store.count(since=past, until=future) == 1
    assert store.count(since=future) == 0


def test_limit(store: SQLiteOutcomeStore):
    for _ in range(10):
        store.append(_make())
    results = store.query(limit=3)
    assert len(results) == 3


def test_outcome_payload_roundtrip(store: SQLiteOutcomeStore):
    event = _make(
        outcome=ComponentOutcome(
            success=False,
            latency_ms=99.9,
            items_served=5,
            items_referenced=2,
            metrics={"precision": 0.4, "tokens": 500.0},
            error="partial match",
        ),
        metadata={"experiment": "A"},
    )
    store.append(event)

    results = store.query()
    assert len(results) == 1
    got = results[0]
    assert got.outcome.success is False
    assert got.outcome.items_served == 5
    assert got.outcome.items_referenced == 2
    assert got.outcome.metrics["precision"] == 0.4
    assert got.outcome.error == "partial match"
    assert got.metadata == {"experiment": "A"}


def test_empty_append_many(store: SQLiteOutcomeStore):
    assert store.append_many([]) == 0


def test_run_filter(store: SQLiteOutcomeStore):
    store.append(_make(run_id="r1"))
    store.append(_make(run_id="r2"))
    results = store.query(run_id="r1")
    assert len(results) == 1
    assert results[0].run_id == "r1"
