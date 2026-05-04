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


def test_payload_filters_single_key(event_log: SQLiteEventLog):
    """``payload_filters`` pushes a payload-key predicate into SQL."""
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "test",
        payload={"domain": "billing", "title": "match"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "test",
        payload={"domain": "shipping", "title": "skip"},
    )
    events = event_log.get_events(payload_filters={"domain": "billing"})
    assert len(events) == 1
    assert events[0].payload["title"] == "match"


def test_payload_filters_multiple_keys_are_anded(event_log: SQLiteEventLog):
    """Multiple ``payload_filters`` entries AND together."""
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "test",
        payload={"domain": "billing", "tier": "gold", "title": "match"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "test",
        payload={"domain": "billing", "tier": "silver", "title": "wrong-tier"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "test",
        payload={"domain": "shipping", "tier": "gold", "title": "wrong-domain"},
    )
    events = event_log.get_events(payload_filters={"domain": "billing", "tier": "gold"})
    assert len(events) == 1
    assert events[0].payload["title"] == "match"


def test_payload_filters_empty_or_none_is_noop(event_log: SQLiteEventLog):
    """``None`` and ``{}`` behave identically to the previous shape."""
    event_log.emit(EventType.PRECEDENT_PROMOTED, "a", payload={"domain": "x"})
    event_log.emit(EventType.PRECEDENT_PROMOTED, "b", payload={"domain": "y"})

    baseline = event_log.get_events(event_type=EventType.PRECEDENT_PROMOTED)
    none_filtered = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED, payload_filters=None
    )
    empty_filtered = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED, payload_filters={}
    )
    assert len(baseline) == 2
    assert [e.event_id for e in none_filtered] == [e.event_id for e in baseline]
    assert [e.event_id for e in empty_filtered] == [e.event_id for e in baseline]


def test_payload_filters_apply_after_other_predicates(event_log: SQLiteEventLog):
    """``payload_filters`` AND with type/source/time predicates."""
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "promoter",
        payload={"domain": "billing"},
    )
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        "feedback",
        payload={"domain": "billing"},
    )
    events = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED,
        payload_filters={"domain": "billing"},
    )
    assert len(events) == 1
    assert events[0].event_type == EventType.PRECEDENT_PROMOTED


def test_payload_filters_no_match_returns_empty(event_log: SQLiteEventLog):
    """Predicate that matches nothing returns an empty list."""
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "test",
        payload={"domain": "billing"},
    )
    events = event_log.get_events(payload_filters={"domain": "nonexistent"})
    assert events == []


def test_payload_filters_limit_applied_after_filter(event_log: SQLiteEventLog):
    """The ``limit`` cap applies AFTER the payload predicate.

    Regression guard for the silent-truncation bug the post-fetch
    Python filter caused in ``list_precedents``: with 5 matches and
    20 non-matches and ``limit=3``, we want 3 matching rows back, not
    fewer (which the old shape produced when non-matches sorted ahead
    of matches and consumed the limit).
    """
    for i in range(20):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "noise",
            payload={"domain": "shipping", "n": i},
        )
    for i in range(5):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "match",
            payload={"domain": "billing", "n": i},
        )
    events = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED,
        payload_filters={"domain": "billing"},
        limit=3,
        order="desc",
    )
    assert len(events) == 3
    assert all(e.payload["domain"] == "billing" for e in events)
