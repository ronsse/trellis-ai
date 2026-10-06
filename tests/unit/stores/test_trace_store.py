"""Tests for the TraceStore."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.stores.sqlite_write_lock import committed_rows, write_at_once
from trellis.core.base import utc_now
from trellis.errors import StoreError
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep
from trellis.stores.sqlite.trace import SQLiteTraceStore


@pytest.fixture
def trace_store(tmp_path: Path):
    store = SQLiteTraceStore(tmp_path / "traces.db")
    yield store
    store.close()


def _make_trace(**kwargs):
    defaults = {
        "source": TraceSource.AGENT,
        "intent": "test task",
        "steps": [],
        "context": TraceContext(agent_id="agent-1", domain="platform"),
    }
    defaults.update(kwargs)
    return Trace(**defaults)


def test_append_and_get(trace_store):
    trace = _make_trace()
    tid = trace_store.append(trace)
    assert tid == trace.trace_id
    retrieved = trace_store.get(tid)
    assert retrieved is not None
    assert retrieved.intent == "test task"
    assert retrieved.source == TraceSource.AGENT


def test_append_duplicate_raises(trace_store):
    trace = _make_trace()
    trace_store.append(trace)
    with pytest.raises(StoreError):
        trace_store.append(trace)


def test_get_nonexistent(trace_store):
    assert trace_store.get("nope") is None


def test_query_by_source(trace_store):
    trace_store.append(_make_trace(source=TraceSource.AGENT))
    trace_store.append(_make_trace(source=TraceSource.HUMAN))
    trace_store.append(_make_trace(source=TraceSource.AGENT))
    results = trace_store.query(source="agent")
    assert len(results) == 2


def test_query_by_domain(trace_store):
    trace_store.append(_make_trace(context=TraceContext(domain="platform")))
    trace_store.append(_make_trace(context=TraceContext(domain="data")))
    results = trace_store.query(domain="platform")
    assert len(results) == 1


def test_query_by_agent_id(trace_store):
    trace_store.append(_make_trace(context=TraceContext(agent_id="a1")))
    trace_store.append(_make_trace(context=TraceContext(agent_id="a2")))
    results = trace_store.query(agent_id="a1")
    assert len(results) == 1


def test_query_by_time_range(trace_store):
    now = utc_now()
    trace_store.append(_make_trace())
    results = trace_store.query(
        since=now - timedelta(hours=1), until=now + timedelta(hours=1)
    )
    assert len(results) == 1
    results = trace_store.query(since=now + timedelta(hours=1))
    assert len(results) == 0


def test_query_limit(trace_store):
    for _ in range(10):
        trace_store.append(_make_trace())
    results = trace_store.query(limit=3)
    assert len(results) == 3


def test_count(trace_store):
    assert trace_store.count() == 0
    trace_store.append(_make_trace())
    trace_store.append(_make_trace(source=TraceSource.HUMAN))
    assert trace_store.count() == 2
    assert trace_store.count(source="agent") == 1
    assert trace_store.count(domain="platform") == 2


def test_trace_with_outcome(trace_store):
    trace = _make_trace(
        outcome=Outcome(status=OutcomeStatus.SUCCESS, metrics={"time_s": 42}),
    )
    trace_store.append(trace)
    retrieved = trace_store.get(trace.trace_id)
    assert retrieved is not None
    assert retrieved.outcome is not None
    assert retrieved.outcome.status == OutcomeStatus.SUCCESS
    assert retrieved.outcome.metrics["time_s"] == 42


def test_trace_with_steps(trace_store):
    trace = _make_trace(
        steps=[
            TraceStep(
                step_type="tool_call",
                name="kubectl apply",
                args={"f": "deploy.yaml"},
            )
        ],
    )
    trace_store.append(trace)
    retrieved = trace_store.get(trace.trace_id)
    assert retrieved is not None
    assert len(retrieved.steps) == 1
    assert retrieved.steps[0].name == "kubectl apply"


def test_an_append_commits_at_once(trace_store, tmp_path: Path):
    trace_store.append(_make_trace(trace_id="syn-trace-1"))

    rows = committed_rows(tmp_path / "traces.db", "SELECT trace_id FROM traces")
    assert rows == [("syn-trace-1",)]


def test_a_failed_append_holds_no_write_lock(trace_store, tmp_path: Path):
    """A duplicate append is rolled back, not left holding the write lock."""
    first = _make_trace(trace_id="syn-trace-1")
    trace_store.append(first)

    with pytest.raises(StoreError, match=r"^Trace syn-trace-1 already exists$") as err:
        trace_store.append(first)

    assert isinstance(err.value.__cause__, sqlite3.IntegrityError)
    assert trace_store._conn.in_transaction is False
    write_at_once(
        tmp_path / "traces.db",
        "INSERT INTO traces (trace_id, source, intent, trace_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("syn-trace-2", "agent", "syn-other", "{}", utc_now().isoformat()),
    )
    rows = committed_rows(
        tmp_path / "traces.db", "SELECT trace_id FROM traces ORDER BY trace_id"
    )
    assert rows == [("syn-trace-1",), ("syn-trace-2",)]
