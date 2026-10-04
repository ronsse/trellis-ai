"""Tests for Postgres store backends.

Requires:
- psycopg v3 installed
- A running Postgres instance with DSN in TRELLIS_TEST_PG_DSN env var

All tests are marked with ``@pytest.mark.postgres`` for easy selection.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tests.pg_scratch import configured_dsn, require_scratch_database

psycopg = pytest.importorskip("psycopg")

PG_DSN = configured_dsn() or None
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(PG_DSN is None, reason="TRELLIS_TEST_PG_DSN not set"),
]


def _clean_tables(dsn: str) -> None:
    """Drop test tables so each test starts fresh.

    ``DROP ... CASCADE`` on six tables is the widest blast radius in the
    suite, so the scratch-database check comes first — before the
    connection, let alone the drop.
    """
    require_scratch_database(dsn)
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
        from trellis.schemas.trace import Trace, TraceContext

        return Trace(
            source="human",
            intent="test intent",
            steps=[],
            context=TraceContext(agent_id="agent-1", domain="platform"),
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


def _wait_for_lock_wait(dsn: str, table: str) -> bool:
    """Poll until a ``DELETE FROM <table>`` waits on a lock (20 s cap)."""
    sql = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        " AND wait_event_type = 'Lock' AND query LIKE 'DELETE FROM ' || %s || ' %%'"
    )
    deadline = time.monotonic() + 20
    with psycopg.connect(dsn, autocommit=True) as mon:
        while time.monotonic() < deadline:
            row = mon.execute(sql, (table,)).fetchone()
            if row and row[0]:
                return True
            time.sleep(0.005)
    return False


def _rows_left(dsn: str, node_id: str) -> dict[str, int]:
    """Count the raw rows, any version, each table still holds for a node."""
    queries = {
        "nodes": "SELECT count(*) FROM nodes WHERE node_id = %s",
        "edges": "SELECT count(*) FROM edges WHERE source_id = %s OR target_id = %s",
        "entity_aliases": "SELECT count(*) FROM entity_aliases WHERE entity_id = %s",
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        return {
            table: conn.execute(sql, (node_id,) * sql.count("%s")).fetchone()[0]
            for table, sql in queries.items()
        }


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

    @pytest.mark.parametrize(
        ("write", "table"),
        [
            ("upsert_node", "nodes"),
            ("update_node_if_current", "nodes"),
            ("upsert_edge", "edges"),
            ("upsert_alias", "entity_aliases"),
        ],
    )
    def test_delete_node_removes_a_version_written_while_it_waited(
        self, store, monkeypatch, write: str, table: str
    ) -> None:
        # The write takes its row lock (its ``... FOR UPDATE`` read returns),
        # the purge starts, and the write commits only once pg_stat_activity
        # shows the purge's DELETE waiting on that lock. The version the
        # write inserts is invisible to the DELETE that waited.
        assert PG_DSN is not None
        store.upsert_node("n1", "person", {"phase": "1"})
        store.upsert_node("n2", "person", {})
        store.upsert_edge("n1", "n2", "knows", {"phase": "1"})
        store.upsert_alias("n1", "sys", "raw-1", raw_name="One")
        v1 = store.get_node("n1")
        writes = {
            "upsert_node": lambda: store.upsert_node("n1", "person", {"phase": "2"}),
            "update_node_if_current": lambda: store.update_node_if_current(
                "n1", v1["valid_from"], "person", {"phase": "2"}, node_role="semantic"
            ),
            "upsert_edge": lambda: store.upsert_edge(
                "n1", "n2", "knows", {"phase": "2"}
            ),
            "upsert_alias": lambda: store.upsert_alias(
                "n1", "sys", "raw-1", raw_name="Two"
            ),
        }
        writer = threading.get_ident()
        original = psycopg.Cursor.execute
        state: dict[str, Any] = {}

        def purge() -> None:
            state["deleted"] = store.delete_node("n1")

        def gated(cur: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
            result = original(cur, query, params, **kwargs)
            if (
                threading.get_ident() == writer
                and "purge" not in state
                and "FOR UPDATE" in str(query)
            ):
                state["purge"] = threading.Thread(target=purge)
                state["purge"].start()
                state["waited"] = _wait_for_lock_wait(PG_DSN, table)
            return result

        monkeypatch.setattr(psycopg.Cursor, "execute", gated)
        writes[write]()
        assert "purge" in state, "the write never took a FOR UPDATE row lock"
        state["purge"].join(timeout=30)

        assert state["waited"], f"the purge never waited on the {table} lock"
        assert not state["purge"].is_alive()
        assert state["deleted"] is True
        assert _rows_left(PG_DSN, "n1") == {"nodes": 0, "edges": 0, "entity_aliases": 0}

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

    def test_compact_versions_drops_old_closed_rows(self, store) -> None:
        """Gap 4.2 — Postgres SCD2 retention."""
        from datetime import UTC, datetime, timedelta

        store.upsert_node("n1", "person", {"v": 1})
        store.upsert_node("n1", "person", {"v": 2})
        # Backdate the closed row's valid_to.
        ten_days_ago = datetime.now(UTC) - timedelta(days=10)
        with store._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE nodes SET valid_to = %s WHERE valid_to IS NOT NULL",
                (ten_days_ago,),
            )

        report = store.compact_versions(datetime.now(UTC) - timedelta(days=5))
        assert report.nodes_compacted == 1
        assert report.dry_run is False
        assert store.get_node("n1") is not None
        assert len(store.get_node_history("n1")) == 1

    def test_compact_versions_dry_run(self, store) -> None:
        from datetime import UTC, datetime, timedelta

        store.upsert_node("n1", "person", {"v": 1})
        store.upsert_node("n1", "person", {"v": 2})
        ten_days_ago = datetime.now(UTC) - timedelta(days=10)
        with store._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE nodes SET valid_to = %s WHERE valid_to IS NOT NULL",
                (ten_days_ago,),
            )

        report = store.compact_versions(
            datetime.now(UTC) - timedelta(days=5), dry_run=True
        )
        assert report.dry_run is True
        assert report.nodes_compacted == 1
        # Dry run did not delete the row.
        assert len(store.get_node_history("n1")) == 2

    # ------------------------------------------------------------------
    # Edge provenance (Phase 3 of adr-graph-ontology §6.4 / item 2 of
    # plan-self-improvement-program). The five columns + CHECK
    # constraint mirror the SQLite write path; tests track that here.
    # ------------------------------------------------------------------

    def test_edge_provenance_round_trips(self, store) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge(
            "a",
            "b",
            "depends_on",
            source_trace_id="tr_42",
            agent_id="agent-7",
            confidence=0.83,
            evidence_ref="doc-9",
            extractor_tier="HYBRID",
        )
        edges = store.get_edges("a", direction="outgoing")
        assert len(edges) == 1
        edge = edges[0]
        assert edge["source_trace_id"] == "tr_42"
        assert edge["agent_id"] == "agent-7"
        assert edge["confidence"] == pytest.approx(0.83)
        assert edge["evidence_ref"] == "doc-9"
        assert edge["extractor_tier"] == "HYBRID"

    def test_edge_without_provenance_reads_back_none(self, store) -> None:
        from trellis.stores.base.edge_provenance import EDGE_PROVENANCE_FIELDS

        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on", {"w": 1.0})
        edge = store.get_edges("a", direction="outgoing")[0]
        for field in EDGE_PROVENANCE_FIELDS:
            assert edge[field] is None

    def test_edge_bad_confidence_raises_before_write(self, store) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="confidence must be in"):
            store.upsert_edge("a", "b", "depends_on", confidence=1.5)
        assert store.get_edges("a", direction="outgoing") == []

    def test_edge_bad_extractor_tier_raises_before_write(self, store) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            store.upsert_edge("a", "b", "depends_on", extractor_tier="MAGIC")
        assert store.get_edges("a", direction="outgoing") == []

    def test_bulk_edges_provenance_round_trips(self, store) -> None:
        store.upsert_node("a", "s", {})
        store.upsert_node("b", "s", {})
        store.upsert_node("c", "s", {})
        store.upsert_edges_bulk(
            [
                {
                    "source_id": "a",
                    "target_id": "b",
                    "edge_type": "links_to",
                    "confidence": 0.5,
                    "extractor_tier": "DETERMINISTIC",
                },
                {
                    "source_id": "a",
                    "target_id": "c",
                    "edge_type": "links_to",
                },
            ]
        )
        edges = sorted(
            store.get_edges("a", direction="outgoing"),
            key=lambda e: e["target_id"],
        )
        assert edges[0]["confidence"] == pytest.approx(0.5)
        assert edges[0]["extractor_tier"] == "DETERMINISTIC"
        assert edges[1]["confidence"] is None
        assert edges[1]["extractor_tier"] is None

    def test_init_schema_idempotent_with_provenance(self, store) -> None:
        """Re-running ``_init_schema`` against an already-migrated DB is a no-op.

        ``_MIGRATE_ADD_EDGE_PROVENANCE`` uses ``ADD COLUMN IF NOT
        EXISTS`` for the columns and a DO block guarded by
        ``pg_constraint`` lookup for the CHECK — both must survive a
        second pass.
        """
        store._init_schema()
        store._init_schema()

    def test_a_schema_migrated_from_v0_3_reads_back_the_full_payload(self) -> None:
        """Every node read names its columns rather than trusting their order.

        ``ALTER TABLE ... ADD COLUMN`` appends, so a database created by
        v0.3.x (before ``document_ids``, added in v0.4.0) and migrated
        forward stores ``document_ids`` last, not between
        ``generation_spec`` and ``properties`` as ``_CREATE_NODES`` does.
        Mapped by position, a ``SELECT *`` row there comes back with empty
        properties and with the ``document_ids`` column as ``valid_to``.
        """
        from trellis.stores.base.graph_query import FilterClause, NodeQuery
        from trellis.stores.postgres.graph import PostgresGraphStore

        assert PG_DSN is not None
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(_NODES_DDL_V0_3)
        store = PostgresGraphStore(PG_DSN)
        try:
            with psycopg.connect(PG_DSN) as conn:
                columns = [
                    row[0]
                    for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'nodes' ORDER BY ordinal_position"
                    )
                ]
            # The precondition this test exists for: without it, every
            # read below passes whatever its SELECT list says.
            assert columns[-1] == "document_ids"

            store.upsert_node("n1", "service", {"name": "Alpha"}, document_ids=["d1"])
            reads = {
                "get_node": [store.get_node("n1")],
                "get_nodes_bulk": store.get_nodes_bulk(["n1"]),
                "get_node_history": store.get_node_history("n1"),
                "query": store.query(node_type="service"),
                "execute_node_query": store.execute_node_query(
                    NodeQuery(filters=(FilterClause("node_id", "eq", "n1"),))
                ),
                "get_subgraph": store.get_subgraph(["n1"], depth=0)["nodes"],
            }
            for read, nodes in reads.items():
                assert len(nodes) == 1, read
                node = nodes[0]
                assert node is not None, read
                assert node["properties"] == {"name": "Alpha"}, read
                assert node["document_ids"] == ["d1"], read
                assert node["valid_from"] is not None, read
                assert node["valid_to"] is None, read
        finally:
            store.close()

    def test_an_alias_seed_confirms_through_namespace_seed_extractor(
        self, store
    ) -> None:
        """Graph-axis seeding resolves an alias-bound entity on Postgres.

        ``NamespaceSeedExtractor`` confirms an alias-derived id through
        ``get_subgraph(depth=0)`` and keeps it only while the node's
        ``properties["name"]`` still normalizes to the alias key, so the
        subgraph payload decides whether the alias path seeds anything.
        """
        from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
        from trellis.retrieve.strategies import NamespaceSeedExtractor

        # An id no namespace candidate (``<ns>:orion``) can reach, so only
        # the alias path can seed it.
        entity_id = "entity-0001"
        store.upsert_node(entity_id, "service", {"name": "Orion"})
        store.upsert_alias(
            entity_id, NAME_ALIAS_SOURCE_SYSTEM, "orion", raw_name="Orion"
        )
        extractor = NamespaceSeedExtractor(store)
        assert extractor.extract("deploy orion") == [entity_id]

        # The binding is checked against the payload: rename the node and
        # the same alias no longer seeds it.
        store.upsert_node(entity_id, "service", {"name": "Vega"})
        assert extractor.extract("deploy orion") == []


# ``nodes`` exactly as v0.3.x created it (3b9cedbd), before v0.4.0 added
# ``document_ids``. ``PostgresGraphStore`` migrates it forward on open.
_NODES_DDL_V0_3 = """\
CREATE TABLE nodes (
    version_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    node_role TEXT NOT NULL DEFAULT 'semantic',
    generation_spec JSONB DEFAULT NULL,
    properties JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ DEFAULT NULL
)"""


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

    def test_init_schema_is_idempotent(self, store) -> None:
        """Re-running ``_init_schema`` must be a no-op: existing deployments
        pick up newly-added indices on next process start without a separate
        migration script.
        """
        store._init_schema()
        store._init_schema()

    def test_explain_uses_type_occurred_desc_index(self, store) -> None:
        """``get_events(event_type=X, order="desc", limit=N)`` must be served
        by the composite ``(event_type, occurred_at DESC)`` index.
        """
        with store._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "EXPLAIN SELECT * FROM events "
                "WHERE event_type = 'feedback.recorded' "
                "ORDER BY occurred_at DESC LIMIT 10"
            )
            plan_lines = cur.fetchall()
        plan_text = "\n".join(row[0] for row in plan_lines)
        assert "idx_events_type_occurred_desc" in plan_text, plan_text

    def test_explain_uses_idempotency_key_index(self, store) -> None:
        """``has_idempotency_key`` must be served by the partial JSON
        expression index on ``payload->>'idempotency_key'``.
        """
        with store._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "EXPLAIN SELECT 1 FROM events "
                "WHERE event_type = 'mutation.executed' "
                "AND payload->>'idempotency_key' = 'k1' LIMIT 1"
            )
            plan_lines = cur.fetchall()
        plan_text = "\n".join(row[0] for row in plan_lines)
        assert "idx_events_idempotency_key" in plan_text, plan_text

    def test_payload_filters_pushdown(self, store) -> None:
        """``payload_filters`` is rendered as ``payload->>'k' = 'v'``."""
        from trellis.stores.base.event_log import EventType

        store.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            payload={"domain": "billing", "title": "match"},
        )
        store.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            payload={"domain": "shipping", "title": "skip"},
        )
        events = store.get_events(payload_filters={"domain": "billing"})
        assert len(events) == 1
        assert events[0].payload["title"] == "match"

    def test_payload_filters_multiple_keys_anded(self, store) -> None:
        """Multiple payload-filter entries AND together in SQL."""
        from trellis.stores.base.event_log import EventType

        store.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            payload={"domain": "billing", "tier": "gold", "title": "match"},
        )
        store.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            payload={"domain": "billing", "tier": "silver"},
        )
        events = store.get_events(payload_filters={"domain": "billing", "tier": "gold"})
        assert len(events) == 1
        assert events[0].payload["title"] == "match"

    def test_payload_filters_empty_or_none_is_noop(self, store) -> None:
        from trellis.stores.base.event_log import EventType

        store.emit(EventType.PRECEDENT_PROMOTED, source="a", payload={"domain": "x"})
        store.emit(EventType.PRECEDENT_PROMOTED, source="b", payload={"domain": "y"})

        baseline = store.get_events(event_type=EventType.PRECEDENT_PROMOTED)
        none_filtered = store.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, payload_filters=None
        )
        empty_filtered = store.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, payload_filters={}
        )
        assert len(baseline) == 2
        assert [e.event_id for e in none_filtered] == [e.event_id for e in baseline]
        assert [e.event_id for e in empty_filtered] == [e.event_id for e in baseline]


# ======================================================================
# Connection pool — concurrent throughput
# ======================================================================


class TestPostgresConnectionPool:
    """Smoke tests for the ``PostgresStoreBase`` connection pool.

    The pre-pool implementation held one ``psycopg.Connection`` per
    store and serialised every query through it; FastAPI's thread-pool
    handlers blocked on each other under load. These tests prove the
    pool gives concurrent threads real parallelism without deadlocks
    or "another command is already in progress" errors.
    """

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        assert PG_DSN is not None
        _clean_tables(PG_DSN)

    def test_concurrent_writes_do_not_deadlock(self) -> None:
        """8 threads writing distinct events finish without errors."""
        import concurrent.futures

        from trellis.stores.base.event_log import EventType
        from trellis.stores.postgres.event_log import PostgresEventLog

        store = PostgresEventLog(PG_DSN)
        try:
            n_threads = 8
            writes_per_thread = 25

            def worker(worker_id: int) -> int:
                for i in range(writes_per_thread):
                    store.emit(
                        EventType.TRACE_INGESTED,
                        source="pool-smoke",
                        entity_id=f"w{worker_id}-i{i}",
                        payload={"worker": worker_id, "i": i},
                    )
                return writes_per_thread

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=n_threads
            ) as pool_executor:
                results = list(pool_executor.map(worker, range(n_threads)))

            assert sum(results) == n_threads * writes_per_thread
            assert store.count(event_type=EventType.TRACE_INGESTED) == (
                n_threads * writes_per_thread
            )
        finally:
            store.close()

    def test_pool_respects_max_size_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TRELLIS_PG_POOL_MAX_SIZE`` flows into the pool config."""
        from trellis.stores.postgres.event_log import PostgresEventLog

        monkeypatch.setenv("TRELLIS_PG_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("TRELLIS_PG_POOL_MAX_SIZE", "3")
        store = PostgresEventLog(PG_DSN)
        try:
            assert store._pool.max_size == 3
            assert store._pool.min_size == 1
        finally:
            store.close()
