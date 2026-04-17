"""Tests for Postgres store backends.

Requires:
- psycopg v3 installed
- A running Postgres instance with DSN in TRELLIS_TEST_PG_DSN env var

All tests are marked with ``@pytest.mark.postgres`` for easy selection.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

PG_DSN = os.environ.get("TRELLIS_TEST_PG_DSN")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(PG_DSN is None, reason="TRELLIS_TEST_PG_DSN not set"),
]


def _clean_tables(dsn: str) -> None:
    """Drop test tables so each test starts fresh."""
    conn = psycopg.connect(dsn, autocommit=True)
    with conn.cursor() as cur:
        for table in (
            "traces",
            "documents",
            "nodes",
            "edges",
            "entity_aliases",
            "events",
        ):
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    conn.close()


# ======================================================================
# TraceStore
# ======================================================================


class TestPostgresTraceStore:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        assert PG_DSN is not None
        _clean_tables(PG_DSN)

    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.trace import PostgresTraceStore

        assert PG_DSN is not None
        s = PostgresTraceStore(PG_DSN)
        yield s
        s.close()

    def _make_trace(self) -> Trace:  # noqa: F821
        from trellis.schemas.trace import Trace

        return Trace(
            source="human",
            intent="test intent",
            steps=[],
        )

    def test_append_and_get(self, store) -> None:
        trace = self._make_trace()
        tid = store.append(trace)
        assert tid == trace.trace_id

        retrieved = store.get(tid)
        assert retrieved is not None
        assert retrieved.trace_id == tid
        assert retrieved.intent == "test intent"

    def test_append_duplicate_raises(self, store) -> None:
        from trellis.errors import StoreError

        trace = self._make_trace()
        store.append(trace)
        with pytest.raises(StoreError):
            store.append(trace)

    def test_get_missing_returns_none(self, store) -> None:
        assert store.get("nonexistent") is None

    def test_query(self, store) -> None:
        t1 = self._make_trace()
        t2 = self._make_trace()
        store.append(t1)
        store.append(t2)

        results = store.query(limit=10)
        assert len(results) == 2

    def test_count(self, store) -> None:
        assert store.count() == 0
        store.append(self._make_trace())
        assert store.count() == 1


# ======================================================================
# DocumentStore
# ======================================================================


class TestPostgresDocumentStore:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        assert PG_DSN is not None
        _clean_tables(PG_DSN)

    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.document import PostgresDocumentStore

        assert PG_DSN is not None
        s = PostgresDocumentStore(PG_DSN)
        yield s
        s.close()

    def test_put_and_get(self, store) -> None:
        doc_id = store.put("doc-1", "hello world", {"tag": "test"})
        assert doc_id == "doc-1"

        doc = store.get("doc-1")
        assert doc is not None
        assert doc["content"] == "hello world"
        assert doc["metadata"]["tag"] == "test"

    def test_put_auto_id(self, store) -> None:
        doc_id = store.put(None, "content")
        assert doc_id is not None
        assert len(doc_id) > 0

    def test_put_upsert(self, store) -> None:
        store.put("doc-1", "version 1")
        store.put("doc-1", "version 2")
        doc = store.get("doc-1")
        assert doc is not None
        assert doc["content"] == "version 2"

    def test_delete(self, store) -> None:
        store.put("doc-1", "content")
        assert store.delete("doc-1") is True
        assert store.get("doc-1") is None
        assert store.delete("doc-1") is False

    def test_search(self, store) -> None:
        store.put("doc-1", "the quick brown fox jumps over the lazy dog")
        store.put("doc-2", "postgres database management system")

        results = store.search("fox")
        assert len(results) >= 1
        assert any(r["doc_id"] == "doc-1" for r in results)

    def test_list_documents(self, store) -> None:
        store.put("doc-1", "first")
        store.put("doc-2", "second")

        docs = store.list_documents(limit=10)
        assert len(docs) == 2

    def test_count(self, store) -> None:
        assert store.count() == 0
        store.put("doc-1", "content")
        assert store.count() == 1

    def test_get_by_hash(self, store) -> None:
        store.put("doc-1", "unique content")
        doc = store.get("doc-1")
        assert doc is not None

        found = store.get_by_hash(doc["content_hash"])
        assert found is not None
        assert found["doc_id"] == "doc-1"


# ======================================================================
# GraphStore
# ======================================================================


class TestPostgresGraphStore:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        assert PG_DSN is not None
        _clean_tables(PG_DSN)

    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.graph import PostgresGraphStore

        assert PG_DSN is not None
        s = PostgresGraphStore(PG_DSN)
        yield s
        s.close()

    def test_upsert_and_get_node(self, store) -> None:
        nid = store.upsert_node("n1", "person", {"name": "Alice"})
        assert nid == "n1"

        node = store.get_node("n1")
        assert node is not None
        assert node["node_type"] == "person"
        assert node["properties"]["name"] == "Alice"

    def test_upsert_node_creates_new_version(self, store) -> None:
        store.upsert_node("n1", "person", {"name": "Alice"})
        store.upsert_node("n1", "person", {"name": "Alice Updated"})

        node = store.get_node("n1")
        assert node is not None
        assert node["properties"]["name"] == "Alice Updated"

        history = store.get_node_history("n1")
        assert len(history) == 2

    def test_upsert_and_get_edge(self, store) -> None:
        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "person", {})
        eid = store.upsert_edge("n1", "n2", "knows", {"since": "2024"})
        assert eid is not None

        edges = store.get_edges("n1", direction="outgoing")
        assert len(edges) == 1
        assert edges[0]["edge_type"] == "knows"

    def test_delete_node(self, store) -> None:
        store.upsert_node("n1", "person", {})
        assert store.delete_node("n1") is True
        assert store.get_node("n1") is None
        assert store.delete_node("n1") is False

    def test_delete_edge(self, store) -> None:
        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "person", {})
        eid = store.upsert_edge("n1", "n2", "knows")

        assert store.delete_edge(eid) is True
        assert store.delete_edge(eid) is False

    def test_count_nodes_and_edges(self, store) -> None:
        assert store.count_nodes() == 0
        assert store.count_edges() == 0

        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "person", {})
        store.upsert_edge("n1", "n2", "knows")

        assert store.count_nodes() == 2
        assert store.count_edges() == 1

    def test_get_nodes_bulk(self, store) -> None:
        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "person", {})
        store.upsert_node("n3", "person", {})

        nodes = store.get_nodes_bulk(["n1", "n3"])
        assert len(nodes) == 2

    def test_query_by_type(self, store) -> None:
        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "org", {})

        results = store.query(node_type="person")
        assert len(results) == 1
        assert results[0]["node_id"] == "n1"

    def test_get_subgraph(self, store) -> None:
        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "person", {})
        store.upsert_node("n3", "person", {})
        store.upsert_edge("n1", "n2", "knows")
        store.upsert_edge("n2", "n3", "knows")

        sg = store.get_subgraph(["n1"], depth=2)
        assert len(sg["nodes"]) == 3
        assert len(sg["edges"]) == 2

    def test_upsert_and_resolve_alias(self, store) -> None:
        store.upsert_node("orders_entity", "table", {"name": "orders"})
        alias_id = store.upsert_alias(
            "orders_entity",
            "unity_catalog",
            "main.analytics.orders",
            raw_name="orders",
            match_confidence=0.93,
            is_primary=True,
        )
        assert alias_id is not None

        alias = store.resolve_alias("unity_catalog", "main.analytics.orders")
        assert alias is not None
        assert alias["alias_id"] == alias_id
        assert alias["entity_id"] == "orders_entity"
        assert alias["raw_name"] == "orders"
        assert alias["match_confidence"] == 0.93
        assert alias["is_primary"] is True

    def test_get_aliases_for_entity(self, store) -> None:
        store.upsert_node("orders_entity", "table", {"name": "orders"})
        store.upsert_alias("orders_entity", "unity_catalog", "main.analytics.orders")
        store.upsert_alias("orders_entity", "dbt", "model.project.orders")

        aliases = store.get_aliases("orders_entity")
        assert len(aliases) == 2
        assert {(alias["source_system"], alias["raw_id"]) for alias in aliases} == {
            ("unity_catalog", "main.analytics.orders"),
            ("dbt", "model.project.orders"),
        }


# ======================================================================
# EventLog
# ======================================================================


class TestPostgresEventLog:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        assert PG_DSN is not None
        _clean_tables(PG_DSN)

    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.event_log import PostgresEventLog

        assert PG_DSN is not None
        s = PostgresEventLog(PG_DSN)
        yield s
        s.close()

    def _make_event(self) -> Event:  # noqa: F821
        from trellis.stores.base.event_log import Event, EventType

        return Event(
            event_type=EventType.TRACE_INGESTED,
            source="test",
            entity_id="t-123",
            entity_type="trace",
            payload={"key": "value"},
        )

    def test_append_and_get(self, store) -> None:
        event = self._make_event()
        store.append(event)

        events = store.get_events(entity_id="t-123")
        assert len(events) == 1
        assert events[0].event_id == event.event_id
        assert events[0].payload == {"key": "value"}

    def test_count(self, store) -> None:
        assert store.count() == 0
        store.append(self._make_event())
        assert store.count() == 1

    def test_get_events_with_type_filter(self, store) -> None:
        from trellis.stores.base.event_log import EventType

        store.append(self._make_event())

        events = store.get_events(event_type=EventType.TRACE_INGESTED)
        assert len(events) == 1

        events = store.get_events(event_type=EventType.ENTITY_CREATED)
        assert len(events) == 0

    def test_count_with_type_filter(self, store) -> None:
        from trellis.stores.base.event_log import EventType

        store.append(self._make_event())

        assert store.count(event_type=EventType.TRACE_INGESTED) == 1
        assert store.count(event_type=EventType.ENTITY_CREATED) == 0

    def test_emit_convenience(self, store) -> None:
        from trellis.stores.base.event_log import EventType

        event = store.emit(
            EventType.SYSTEM_INITIALIZED,
            source="test",
            payload={"version": "1.0"},
        )
        assert event.event_id is not None

        events = store.get_events(event_type=EventType.SYSTEM_INITIALIZED)
        assert len(events) == 1
