"""Tests for trellis.ops.record_outcome."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.ops import record_outcome
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    yield s
    s.close()


def test_record_outcome_minimal(store: SQLiteOutcomeStore):
    event = record_outcome(
        store,
        component_id="x",
        success=True,
        latency_ms=1.5,
    )
    assert event.event_id
    assert event.component_id == "x"
    assert event.outcome.success is True
    assert event.outcome.latency_ms == 1.5
    assert store.count() == 1


def test_record_outcome_all_axes(store: SQLiteOutcomeStore):
    event = record_outcome(
        store,
        component_id="retrieve.pack_builder.PackBuilder",
        success=True,
        latency_ms=250.0,
        params_version="v42",
        domain="sportsbook",
        intent_family="plan",
        tool_name="get_task_context",
        phase="assemble",
        agent_role="claude-code",
        agent_id="a-1",
        run_id="r-1",
        session_id="s-1",
        pack_id="p-1",
        trace_id="t-1",
        items_served=12,
        items_referenced=4,
        metrics={"precision": 0.75, "tokens": 1500.0},
        metadata={"note": "hot path"},
        cohort="canary",
    )
    assert event.domain == "sportsbook"
    assert event.outcome.metrics["precision"] == 0.75
    assert event.cohort == "canary"

    results = store.query(domain="sportsbook", intent_family="plan")
    assert len(results) == 1


def test_record_outcome_failure(store: SQLiteOutcomeStore):
    event = record_outcome(
        store,
        component_id="c",
        success=False,
        latency_ms=10.0,
        error="timeout",
    )
    assert event.outcome.success is False
    assert event.outcome.error == "timeout"


def test_record_outcome_swallows_store_errors(tmp_path: Path):
    """Recording is advisory — a broken store must not raise."""

    class BrokenStore:
        def append(self, _outcome):
            msg = "boom"
            raise RuntimeError(msg)

        def append_many(self, _outcomes):
            return 0

        def query(self, **_kw):
            return []

        def count(self, **_kw):
            return 0

        def close(self):
            pass

    broken = BrokenStore()
    event = record_outcome(
        broken,  # type: ignore[arg-type]
        component_id="x",
        success=True,
        latency_ms=1.0,
    )
    # Recording returned an event even though append raised.
    assert event.component_id == "x"
