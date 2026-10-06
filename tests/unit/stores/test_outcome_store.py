"""Tests for SQLiteOutcomeStore."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.stores.sqlite_write_lock import committed_rows, write_at_once
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
    event = _make(domain="orders", intent_family="plan")
    store.append(event)

    results = store.query()
    assert len(results) == 1
    assert results[0].event_id == event.event_id
    assert results[0].domain == "orders"
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


_DUPLICATE_EVENT_ID = r"^UNIQUE constraint failed: outcomes\.event_id$"
_EVENT_IDS = "SELECT event_id FROM outcomes ORDER BY event_id"


def _write_at_once(db_path: Path, event_id: str) -> None:
    stamp = utc_now().isoformat()
    write_at_once(
        db_path,
        "INSERT INTO outcomes (event_id, component_id, occurred_at, recorded_at,"
        " success, latency_ms, outcome_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, "syn-component", stamp, stamp, 1, 1.0, "{}"),
    )


def test_an_append_commits_at_once(store: SQLiteOutcomeStore, tmp_path: Path):
    store.append(_make(event_id="syn-outcome-1"))

    rows = committed_rows(tmp_path / "outcomes.db", _EVENT_IDS)
    assert rows == [("syn-outcome-1",)]


def test_a_failed_append_holds_no_write_lock(store: SQLiteOutcomeStore, tmp_path: Path):
    """A duplicate append is rolled back, not left holding the write lock."""
    store.append(_make(event_id="syn-outcome-1"))

    with pytest.raises(sqlite3.IntegrityError, match=_DUPLICATE_EVENT_ID):
        store.append(_make(event_id="syn-outcome-1"))

    assert store._conn.in_transaction is False
    _write_at_once(tmp_path / "outcomes.db", "syn-outcome-2")
    rows = committed_rows(tmp_path / "outcomes.db", _EVENT_IDS)
    assert rows == [("syn-outcome-1",), ("syn-outcome-2",)]


def test_an_append_many_commits_at_once(store: SQLiteOutcomeStore, tmp_path: Path):
    store.append_many([_make(event_id=f"syn-outcome-{n}") for n in (1, 2, 3)])

    rows = committed_rows(tmp_path / "outcomes.db", _EVENT_IDS)
    assert rows == [("syn-outcome-1",), ("syn-outcome-2",), ("syn-outcome-3",)]


def test_a_failed_append_many_holds_no_write_lock(
    store: SQLiteOutcomeStore, tmp_path: Path
):
    """A batch that fails on a duplicate is rolled back, not left holding the lock."""
    store.append(_make(event_id="syn-outcome-0"))

    with pytest.raises(sqlite3.IntegrityError, match=_DUPLICATE_EVENT_ID):
        store.append_many(
            [_make(event_id="syn-outcome-1"), _make(event_id="syn-outcome-0")]
        )

    assert store._conn.in_transaction is False
    _write_at_once(tmp_path / "outcomes.db", "syn-outcome-9")
    rows = committed_rows(tmp_path / "outcomes.db", _EVENT_IDS)
    assert rows == [("syn-outcome-0",), ("syn-outcome-9",)]


def test_a_duplicate_mid_batch_writes_none_of_the_batch(
    store: SQLiteOutcomeStore, tmp_path: Path
):
    """``append_many`` is one transaction: a failed row writes none of its batch.

    Rows the batch inserted before the duplicate would otherwise stay pending
    on the store's connection, for its next commit to write.
    """
    store.append(_make(event_id="syn-outcome-0"))
    batch = [
        _make(event_id=event_id)
        for event_id in (
            "syn-outcome-1",
            "syn-outcome-2",
            "syn-outcome-0",
            "syn-outcome-3",
        )
    ]

    with pytest.raises(sqlite3.IntegrityError, match=_DUPLICATE_EVENT_ID):
        store.append_many(batch)

    db_path = tmp_path / "outcomes.db"
    assert committed_rows(db_path, _EVENT_IDS) == [("syn-outcome-0",)]
    store.append(_make(event_id="syn-outcome-9"))
    assert committed_rows(db_path, _EVENT_IDS) == [
        ("syn-outcome-0",),
        ("syn-outcome-9",),
    ]
