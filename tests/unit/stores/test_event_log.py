"""Tests for the event log store."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from trellis.core.base import utc_now
from trellis.stores.event_log import Event, EventType, SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def test_append_and_query(event_log: SQLiteEventLog):
    event = Event(
        event_type=EventType.TRACE_INGESTED,
        source="ingest",
        entity_id="trace_123",
        entity_type="trace",
        payload={"intent": "deploy"},
    )
    event_log.append(event)
    events = event_log.get_events()
    assert len(events) == 1
    assert events[0].event_type == EventType.TRACE_INGESTED
    assert events[0].entity_id == "trace_123"


def test_emit_convenience(event_log: SQLiteEventLog):
    event = event_log.emit(
        EventType.ENTITY_CREATED,
        "curate",
        entity_id="ent_456",
        entity_type="entity",
        payload={"name": "auth-service"},
    )
    assert event.event_id
    events = event_log.get_events(event_type=EventType.ENTITY_CREATED)
    assert len(events) == 1


def test_filter_by_type(event_log: SQLiteEventLog):
    event_log.emit(EventType.TRACE_INGESTED, "a")
    event_log.emit(EventType.ENTITY_CREATED, "b")
    event_log.emit(EventType.TRACE_INGESTED, "c")
    traces = event_log.get_events(event_type=EventType.TRACE_INGESTED)
    assert len(traces) == 2


def test_filter_by_entity_id(event_log: SQLiteEventLog):
    event_log.emit(EventType.TRACE_INGESTED, "a", entity_id="t1")
    event_log.emit(EventType.TRACE_INGESTED, "a", entity_id="t2")
    events = event_log.get_events(entity_id="t1")
    assert len(events) == 1


def test_filter_by_source(event_log: SQLiteEventLog):
    event_log.emit(EventType.TRACE_INGESTED, "ingest")
    event_log.emit(EventType.ENTITY_CREATED, "curate")
    events = event_log.get_events(source="ingest")
    assert len(events) == 1


def test_filter_by_time_range(event_log: SQLiteEventLog):
    now = utc_now()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    event_log.emit(EventType.TRACE_INGESTED, "a")
    events = event_log.get_events(since=past, until=future)
    assert len(events) == 1
    events = event_log.get_events(since=future)
    assert len(events) == 0


def test_count(event_log: SQLiteEventLog):
    assert event_log.count() == 0
    event_log.emit(EventType.TRACE_INGESTED, "a")
    event_log.emit(EventType.ENTITY_CREATED, "b")
    assert event_log.count() == 2
    assert event_log.count(event_type=EventType.TRACE_INGESTED) == 1


def test_limit(event_log: SQLiteEventLog):
    for i in range(10):
        event_log.emit(EventType.TRACE_INGESTED, f"s{i}")
    events = event_log.get_events(limit=3)
    assert len(events) == 3


def test_order_asc_returns_oldest_first(event_log: SQLiteEventLog):
    """Default order is ASC — chronological consumption for analytics."""
    for i in range(5):
        event_log.emit(EventType.TRACE_INGESTED, f"src-{i}")
    events = event_log.get_events()
    assert [e.source for e in events] == [f"src-{i}" for i in range(5)]


def test_order_desc_returns_most_recent_first(event_log: SQLiteEventLog):
    """``order='desc'`` flips ordering so duplicate-checks see recent rows."""
    for i in range(5):
        event_log.emit(EventType.TRACE_INGESTED, f"src-{i}")
    events = event_log.get_events(order="desc")
    assert [e.source for e in events] == [f"src-{i}" for i in reversed(range(5))]


def test_order_desc_truncation_keeps_recent_end(event_log: SQLiteEventLog):
    """With ``limit`` smaller than the row count, ``order='desc'`` keeps
    the most recent N rows; ``order='asc'`` keeps the oldest N rows."""
    for i in range(10):
        event_log.emit(EventType.TRACE_INGESTED, f"src-{i}")

    asc_truncated = event_log.get_events(limit=3)
    assert [e.source for e in asc_truncated] == ["src-0", "src-1", "src-2"]

    desc_truncated = event_log.get_events(limit=3, order="desc")
    assert [e.source for e in desc_truncated] == ["src-9", "src-8", "src-7"]


def test_payload_preserved(event_log: SQLiteEventLog):
    event_log.emit(
        EventType.MUTATION_EXECUTED,
        "pipeline",
        payload={"command_id": "cmd_1", "operation": "entity.create"},
    )
    events = event_log.get_events()
    assert events[0].payload["command_id"] == "cmd_1"
