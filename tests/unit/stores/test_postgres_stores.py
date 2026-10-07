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


def _wait_for_insert_lock_wait(dsn: str, table: str) -> bool:
    """Poll until an ``INSERT INTO <table>`` waits on a lock (20 s cap).

    ``%%`` on both sides: the store's INSERT text starts with a newline, and
    a newline follows the table name.
    """
    sql = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        " AND wait_event_type = 'Lock'"
        " AND query LIKE '%%INSERT INTO ' || %s || '%%'"
    )
    deadline = time.monotonic() + 20
    with psycopg.connect(dsn, autocommit=True) as mon:
        while time.monotonic() < deadline:
            row = mon.execute(sql, (table,)).fetchone()
            if row and row[0]:
                return True
            time.sleep(0.005)
    return False


def _wait_for_select_for_update_lock_wait(dsn: str, table: str) -> bool:
    """Poll until a ``SELECT ... FOR UPDATE`` on ``table`` waits on a lock
    (20 s cap).
    """
    sql = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        " AND wait_event_type = 'Lock'"
        " AND query LIKE '%%FROM ' || %s || '%%FOR UPDATE%%'"
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


def _current_edge_rows(
    dsn: str, source_id: str, target_id: str, edge_type: str
) -> list[dict[str, Any]]:
    """Every ``valid_to IS NULL`` row for one logical edge key."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT edge_id, properties FROM edges"
            " WHERE source_id = %s AND target_id = %s AND edge_type = %s"
            " AND valid_to IS NULL",
            (source_id, target_id, edge_type),
        ).fetchall()
    return [{"edge_id": r[0], "properties": r[1]} for r in rows]


def test_edge_lock_key_keeps_distinct_triples_from_colliding() -> None:
    """``_edge_lock_key`` must not let two distinct triples collide.

    JSON-encoding (rather than bare concatenation) keeps a boundary-shifted
    pair of triples distinct, and keeping ``edge_type`` in the key keeps two
    logical edges that share endpoints but differ only in ``edge_type`` from
    locking each other out: each needs its own key, or a writer racing one
    could be serialized against the other's unrelated lock instead of its
    own.
    """
    from trellis.stores.postgres.graph import _edge_lock_key

    assert _edge_lock_key("ab", "c", "knows") != _edge_lock_key("a", "bc", "knows")
    assert _edge_lock_key("s", "t", "knows") != _edge_lock_key("s", "t", "related_to")


def _write_after_a_freed_page(store: Any, dsn: str, node_id: str) -> None:
    """Write ``node_id`` on a page after one VACUUM has emptied.

    Padded fillers fill the first pages and the node lands after them. The
    fillers on page 0 are then deleted and vacuumed, so the free space map
    sends a new connection's next insert to page 0.
    """
    for i in range(150):
        store.upsert_node(f"filler-{i}", "person", {"pad": "p" * 200})
    store.upsert_node(node_id, "person", {"phase": "1"})
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM nodes WHERE node_id LIKE %s AND (ctid::text::point)[0] = 0",
            ("filler-%",),
        )
        conn.execute("VACUUM nodes")


def _version_ctids(dsn: str, node_id: str) -> dict[bool, list[tuple[int, int]]]:
    """Each version's ctid as ``(page, item)``, keyed by whether it is current."""
    ctids: dict[bool, list[tuple[int, int]]] = {True: [], False: []}
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT ctid::text, valid_to IS NULL FROM nodes WHERE node_id = %s",
            (node_id,),
        ).fetchall()
    for ctid, is_current in rows:
        page, item = ctid.strip("()").split(",")
        ctids[is_current].append((int(page), int(item)))
    return ctids


class _OppositeOrderPurges:
    """Hold two purges of one node at the points where their locks cross.

    :meth:`execute` stands in for ``psycopg.Cursor.execute``. When the
    thread that built this object gets the result of its ``FOR UPDATE``
    read, it starts the first purge and waits until that purge's ``DELETE``
    waits on its lock. The first purge is held before its second
    ``DELETE FROM nodes`` until another ``DELETE FROM nodes`` waits on a
    lock. Each ``DeadlockDetected`` is recorded under the thread's name.
    """

    def __init__(self, store: Any, dsn: str, node_id: str) -> None:
        self._store = store
        self._dsn = dsn
        self._node_id = node_id
        self._original = psycopg.Cursor.execute
        self._writer = threading.get_ident()
        self._first_deletes = 0
        self.first: threading.Thread | None = None
        self.first_waited = False
        self.second_waited = False
        self.outcome: dict[str, Any] = {}
        self.deadlocks: list[str] = []

    def purge(self) -> None:
        name = threading.current_thread().name
        try:
            self.outcome[name] = self._store.delete_node(self._node_id)
        except Exception as exc:
            self.outcome[name] = exc

    def execute(self, cur: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
        thread = threading.current_thread().name
        sql = str(query)
        if thread == "purge-1" and sql.startswith("DELETE FROM nodes"):
            self._first_deletes += 1
            if self._first_deletes == 2:
                self.second_waited = _wait_for_lock_wait(self._dsn, "nodes")
        try:
            result = self._original(cur, query, params, **kwargs)
        except psycopg.errors.DeadlockDetected:
            self.deadlocks.append(thread)
            raise
        if (
            threading.get_ident() == self._writer
            and self.first is None
            and "FOR UPDATE" in sql
        ):
            self.first = threading.Thread(target=self.purge, name="purge-1")
            self.first.start()
            self.first_waited = _wait_for_lock_wait(self._dsn, "nodes")
        return result


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

    def test_upsert_node_create_race_becomes_new_version(self, store) -> None:
        """Two writers creating the same new node_id race on
        ``idx_nodes_current``. The loser retries and writes a new version
        over the winner's row, as SQLite's single writer would.
        """
        node_id = "race-node-synthetic-create"

        # The "other writer": insert the target's current row directly and
        # hold the transaction open, so the store's own FOR UPDATE read
        # (which only locks an *existing* row) finds nothing and the store
        # proceeds to the bare INSERT that then races this row.
        other = psycopg.connect(PG_DSN)
        other.execute(
            "INSERT INTO nodes (version_id, node_id, node_type, created_at,"
            " updated_at, valid_from)"
            " VALUES ('race-other-writer-v1', %s, 'synthetic_type', now(),"
            " now(), now())",
            (node_id,),
        )

        outcome: dict[str, Any] = {}

        def call_upsert() -> None:
            try:
                outcome["node_id"] = store.upsert_node(
                    node_id, "synthetic_type", {"writer": "a"}
                )
            except Exception as exc:
                outcome["error"] = exc

        writer_a = threading.Thread(target=call_upsert)
        writer_a.start()

        try:
            waited = _wait_for_insert_lock_wait(PG_DSN, "nodes")
            other.commit()
            writer_a.join(timeout=20)
        finally:
            other.close()

        assert waited, "writer A never blocked on writer B's held insert"
        assert not writer_a.is_alive()
        assert "error" not in outcome, outcome.get("error")
        assert outcome["node_id"] == node_id

        history = store.get_node_history(node_id)
        assert len(history) == 2
        current = [v for v in history if v["valid_to"] is None]
        assert len(current) == 1
        assert current[0]["properties"] == {"writer": "a"}

    def test_upsert_node_update_race_becomes_new_version(self, store) -> None:
        """Two writers updating the same *existing* node race on
        ``idx_nodes_current`` too, not only the create case: the
        ``FOR UPDATE`` waiter re-checks the specific row it blocked on,
        finds it closed, and cannot see the row inserted while it waited --
        so it INSERTs its own, collides with that row on
        ``idx_nodes_current``, and retries.
        """
        node_id = "race-node-synthetic-update"
        store.upsert_node(node_id, "synthetic_type", {"writer": "seed"})

        # The "other writer": take the row lock, close it and insert its
        # own new current version, all uncommitted -- so the store's own
        # ``FOR UPDATE`` read blocks on this held lock instead of finding an
        # already-closed row outright.
        other = psycopg.connect(PG_DSN)
        other.execute(
            "SELECT 1 FROM nodes WHERE node_id = %s AND valid_to IS NULL FOR UPDATE",
            (node_id,),
        )
        (now_other,) = other.execute("SELECT now()").fetchone()
        other.execute(
            "UPDATE nodes SET valid_to = %s WHERE node_id = %s AND valid_to IS NULL",
            (now_other, node_id),
        )
        other.execute(
            "INSERT INTO nodes (version_id, node_id, node_type, created_at,"
            " updated_at, valid_from)"
            " VALUES ('race-other-writer-update-v1', %s, 'synthetic_type',"
            " %s, %s, %s)",
            (node_id, now_other, now_other, now_other),
        )

        outcome: dict[str, Any] = {}

        def call_upsert() -> None:
            try:
                outcome["node_id"] = store.upsert_node(
                    node_id, "synthetic_type", {"writer": "a"}
                )
            except Exception as exc:
                outcome["error"] = exc

        writer_a = threading.Thread(target=call_upsert)
        writer_a.start()

        try:
            waited = _wait_for_select_for_update_lock_wait(PG_DSN, "nodes")
            other.commit()
            writer_a.join(timeout=20)
        finally:
            other.close()

        assert waited, "writer A never blocked on writer B's held row lock"
        assert not writer_a.is_alive()
        assert "error" not in outcome, outcome.get("error")
        assert outcome["node_id"] == node_id

        history = store.get_node_history(node_id)
        assert len(history) == 3
        current = [v for v in history if v["valid_to"] is None]
        assert len(current) == 1
        assert current[0]["properties"] == {"writer": "a"}

    def test_upsert_node_other_unique_violation_is_not_retried(
        self, store, monkeypatch
    ) -> None:
        """A ``UniqueViolation`` on a *different* constraint must propagate
        unchanged, never be mistaken for the create race (mutant: widening
        the constraint-name check to match any ``UniqueViolation``).

        Forced by making every create reuse the same ``version_id`` (the
        table's real primary key), which is a genuine, unrelated
        ``UniqueViolation`` from Postgres rather than a fabricated one.
        """
        import trellis.stores.postgres.graph as graph_mod

        monkeypatch.setattr(
            graph_mod, "generate_ulid", lambda: "fixed-version-id-synthetic"
        )

        store.upsert_node("race-node-synthetic-pk-1", "t", {})
        with pytest.raises(psycopg.errors.UniqueViolation) as exc_info:
            store.upsert_node("race-node-synthetic-pk-2", "t", {})
        assert exc_info.value.diag.constraint_name == "nodes_pkey"

    def test_upsert_node_retry_exhausted_raises_store_error(
        self, store, monkeypatch
    ) -> None:
        """When the retry also conflicts, ``upsert_node`` gives up after one
        retry and raises a :class:`StoreError` naming only the exception
        type, never the node_id.
        """
        from trellis.errors import StoreError
        from trellis.stores.postgres.graph import PostgresGraphStore

        node_id = "race-node-synthetic-exhausted"
        store.upsert_node(node_id, "t", {})

        # Force every attempt down the "no existing row" branch, so its
        # bare INSERT conflicts with the real current row above on both
        # the first attempt and the retry.
        reads: list[str] = []

        def no_current_row(_self: object, _conn: object, nid: str) -> None:
            reads.append(nid)

        monkeypatch.setattr(
            PostgresGraphStore, "_fetch_current_node_for_update", no_current_row
        )

        with pytest.raises(StoreError) as exc_info:
            store.upsert_node(node_id, "t", {})

        assert len(reads) == 2
        assert node_id not in str(exc_info.value)

    def test_upsert_nodes_bulk_create_race_raises_store_error(
        self, store, monkeypatch
    ) -> None:
        """``upsert_nodes_bulk`` does not retry: a racing create rolls the
        batch back and raises a type-only :class:`StoreError` instead of a
        raw ``UniqueViolation``.
        """
        from trellis.errors import StoreError
        from trellis.stores.postgres.graph import PostgresGraphStore

        node_id = "race-node-synthetic-bulk"
        store.upsert_node(node_id, "t", {})

        # Make the bulk path blind to the row that already exists, so its
        # INSERT (no preceding UPDATE) races the real current row.
        monkeypatch.setattr(
            PostgresGraphStore,
            "get_nodes_bulk",
            lambda self, node_ids, as_of=None: [],
        )

        with pytest.raises(StoreError) as exc_info:
            store.upsert_nodes_bulk(
                [{"node_id": node_id, "node_type": "t", "properties": {}}]
            )
        assert node_id not in str(exc_info.value)

    def test_upsert_nodes_bulk_other_unique_violation_is_not_caught(
        self, store, monkeypatch
    ) -> None:
        """A bulk ``UniqueViolation`` on a constraint *other* than
        ``idx_nodes_current`` must propagate unchanged, never be mistaken
        for the bulk create race.

        Forced by making both rows in one batch reuse the same
        ``version_id`` (the table's real primary key) -- a genuine,
        unrelated ``UniqueViolation`` from Postgres rather than a
        fabricated one.
        """
        import trellis.stores.postgres.graph as graph_mod

        monkeypatch.setattr(
            graph_mod, "generate_ulid", lambda: "fixed-bulk-version-id-synthetic"
        )

        with pytest.raises(psycopg.errors.UniqueViolation) as exc_info:
            store.upsert_nodes_bulk(
                [
                    {
                        "node_id": "race-node-synthetic-bulk-pk-1",
                        "node_type": "t",
                        "properties": {},
                    },
                    {
                        "node_id": "race-node-synthetic-bulk-pk-2",
                        "node_type": "t",
                        "properties": {},
                    },
                ]
            )
        assert exc_info.value.diag.constraint_name == "nodes_pkey"

    def test_upsert_and_get_edge(self, store) -> None:
        store.upsert_node("n1", "person", {})
        store.upsert_node("n2", "person", {})
        eid = store.upsert_edge("n1", "n2", "knows", {"since": "2024"})
        assert eid is not None

        edges = store.get_edges("n1", direction="outgoing")
        assert len(edges) == 1
        assert edges[0]["edge_type"] == "knows"

    def test_upsert_edge_create_race_leaves_one_current_row(self, store) -> None:
        """Two writers creating the same new logical edge must leave exactly
        one current row.

        ``idx_edges_current`` is unique on the random ``edge_id``, not on
        the logical key, so before the fix neither writer's INSERT ever
        collides with the other's: both read "no current row", both mint
        their own ``edge_id``, and both commit (confirmed 30/30 by the
        #762 gate's probe). A ``threading.Barrier`` starts both real
        ``upsert_edge`` calls together so the race window is exercised the
        same way on every run, matching the gate's own reproduction.
        """
        source_id, target_id, edge_type = (
            "race-edge-synthetic-create-src",
            "race-edge-synthetic-create-tgt",
            "synthetic_rel",
        )
        store.upsert_node(source_id, "synthetic_type", {})
        store.upsert_node(target_id, "synthetic_type", {})

        barrier = threading.Barrier(2)
        outcome: dict[str, Any] = {}

        def call_upsert(name: str, writer: str) -> None:
            try:
                barrier.wait(10)
                outcome[name] = store.upsert_edge(
                    source_id, target_id, edge_type, {"writer": writer}
                )
            except Exception as exc:
                outcome[name] = exc

        writer_a = threading.Thread(target=call_upsert, args=("a", "a"))
        writer_b = threading.Thread(target=call_upsert, args=("b", "b"))
        writer_a.start()
        writer_b.start()
        writer_a.join(timeout=20)
        writer_b.join(timeout=20)

        assert not writer_a.is_alive()
        assert not writer_b.is_alive()
        for name in ("a", "b"):
            assert not isinstance(outcome[name], Exception), outcome[name]

        current = _current_edge_rows(PG_DSN, source_id, target_id, edge_type)
        assert len(current) == 1, current
        assert current[0]["edge_id"] in (outcome["a"], outcome["b"])

    def test_upsert_edge_update_race_leaves_one_current_row(self, store) -> None:
        """Two writers updating the same *existing* logical edge must leave
        exactly one current row.

        Before the fix, the loser's ``FOR UPDATE`` read blocks on the
        winner's row lock, then re-checks the row once unblocked, finds it
        already closed, and -- unable to see the winner's new row -- mints
        its own ``edge_id`` instead of reusing one, so both commit a
        current row (confirmed 30/30 by the #762 gate's probe).
        """
        source_id, target_id, edge_type = (
            "race-edge-synthetic-update-src",
            "race-edge-synthetic-update-tgt",
            "synthetic_rel",
        )
        store.upsert_node(source_id, "synthetic_type", {})
        store.upsert_node(target_id, "synthetic_type", {})
        store.upsert_edge(source_id, target_id, edge_type, {"writer": "seed"})

        barrier = threading.Barrier(2)
        outcome: dict[str, Any] = {}

        def call_upsert(name: str, writer: str) -> None:
            try:
                barrier.wait(10)
                outcome[name] = store.upsert_edge(
                    source_id, target_id, edge_type, {"writer": writer}
                )
            except Exception as exc:
                outcome[name] = exc

        writer_a = threading.Thread(target=call_upsert, args=("a", "a"))
        writer_b = threading.Thread(target=call_upsert, args=("b", "b"))
        writer_a.start()
        writer_b.start()
        writer_a.join(timeout=20)
        writer_b.join(timeout=20)

        assert not writer_a.is_alive()
        assert not writer_b.is_alive()
        for name in ("a", "b"):
            assert not isinstance(outcome[name], Exception), outcome[name]

        current = _current_edge_rows(PG_DSN, source_id, target_id, edge_type)
        assert len(current) == 1, current
        assert current[0]["properties"]["writer"] in ("a", "b")

    def test_upsert_edges_bulk_overlapping_batches_do_not_deadlock(self, store) -> None:
        """Two batches that share two logical edges, listed in opposite
        order, must not deadlock and must leave exactly one current row per
        key.

        Before the fix neither batch takes any lock, so this is just the
        create race again, twice over. After the fix each batch locks its
        triplets in one sorted order, so two batches touching the same
        keys in different orders can't form a circular wait -- the second
        batch serializes behind the first instead of deadlocking with it.
        """
        store.upsert_node("race-bulk-n1", "synthetic_type", {})
        store.upsert_node("race-bulk-n2", "synthetic_type", {})
        store.upsert_node("race-bulk-n3", "synthetic_type", {})
        store.upsert_node("race-bulk-n4", "synthetic_type", {})

        edge_x = ("race-bulk-n1", "race-bulk-n2", "synthetic_rel")
        edge_y = ("race-bulk-n3", "race-bulk-n4", "synthetic_rel")

        def spec(key: tuple[str, str, str], writer: str) -> dict[str, Any]:
            return {
                "source_id": key[0],
                "target_id": key[1],
                "edge_type": key[2],
                "properties": {"writer": writer},
            }

        batch1 = [spec(edge_x, "p"), spec(edge_y, "p")]
        batch2 = [spec(edge_y, "q"), spec(edge_x, "q")]

        barrier = threading.Barrier(2)
        outcome: dict[str, Any] = {}

        def call_bulk(name: str, batch: list[dict[str, Any]]) -> None:
            try:
                barrier.wait(10)
                outcome[name] = store.upsert_edges_bulk(batch)
            except Exception as exc:
                outcome[name] = exc

        writer_1 = threading.Thread(target=call_bulk, args=("1", batch1))
        writer_2 = threading.Thread(target=call_bulk, args=("2", batch2))
        writer_1.start()
        writer_2.start()
        writer_1.join(timeout=20)
        writer_2.join(timeout=20)

        assert not writer_1.is_alive()
        assert not writer_2.is_alive()
        for name in ("1", "2"):
            assert not isinstance(outcome[name], Exception), outcome[name]

        for key in (edge_x, edge_y):
            current = _current_edge_rows(PG_DSN, *key)
            assert len(current) == 1, (key, current)

    def test_lock_edge_keys_acquires_locks_in_one_sorted_order(
        self, store, monkeypatch
    ) -> None:
        """``_lock_edge_keys`` must issue its ``pg_advisory_xact_lock`` calls
        in one sorted, de-duplicated order regardless of the caller's input
        order.

        That is what lets two ``upsert_edges_bulk`` batches whose triplets
        overlap in different orders converge on the same lock sequence
        instead of forming a wait cycle. This inspects the actual SQL call
        order directly rather than relying on a real deadlock to manifest,
        which is timing-dependent and -- for a small key set -- not
        guaranteed even without the ``sorted()`` call, since a plain
        ``set``'s iteration order for a few non-colliding elements can
        coincide with sorted order by chance; nine keys makes that
        coincidence negligible (1-in-362880) while still being a direct,
        deterministic check of the ordering contract itself.
        """
        from trellis.stores.postgres.graph import _edge_lock_key, _lock_edge_keys

        keys = [
            (f"race-lk-s{i}", f"race-lk-t{i}", "knows")
            for i in (3, 1, 4, 0, 5, 2, 8, 6, 7)
        ]
        expected_order = sorted({_edge_lock_key(*key) for key in keys})

        seen: list[str] = []
        original_execute = psycopg.Cursor.execute

        def recording_execute(cur, query, params=None, **kwargs):  # type: ignore[no-untyped-def]
            sql = str(query)
            if "pg_advisory_xact_lock" in sql and params:
                seen.append(params[0])
            return original_execute(cur, query, params, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", recording_execute)

        for ordering in (keys, list(reversed(keys))):
            seen.clear()
            with store._conn() as conn:
                _lock_edge_keys(conn, ordering)
            assert seen == expected_order

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

    def test_delete_node_removes_the_versions_two_writes_made_in_turn(
        self, store, monkeypatch
    ) -> None:
        # A first write takes its row lock and commits once the purge's first
        # nodes DELETE waits on it. The purge pauses before its second nodes
        # DELETE until a second write has locked the version the first
        # inserted, and that write commits once the second DELETE waits on
        # it. The version the second write inserts is invisible to that
        # DELETE, so only a third statement removes it.
        assert PG_DSN is not None
        store.upsert_node("n1", "person", {"phase": "1"})
        writer = threading.get_ident()
        original = psycopg.Cursor.execute
        paused, resume = threading.Event(), threading.Event()
        state: dict[str, Any] = {"node_deletes": 0}

        def purge() -> None:
            state["deleted"] = store.delete_node("n1")

        def start_purge() -> None:
            state["purge"] = threading.Thread(target=purge)
            state["purge"].start()
            state["first_waited"] = _wait_for_lock_wait(PG_DSN, "nodes")

        def resume_purge() -> None:
            resume.set()
            state["second_waited"] = _wait_for_lock_wait(PG_DSN, "nodes")

        def gated(cur: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
            mine = threading.get_ident() == writer
            if not mine and str(query).startswith("DELETE FROM nodes"):
                state["node_deletes"] += 1
                if state["node_deletes"] == 2:
                    paused.set()
                    resume.wait(20)
            result = original(cur, query, params, **kwargs)
            if mine and "FOR UPDATE" in str(query):
                state.pop("hold", lambda: None)()
            return result

        monkeypatch.setattr(psycopg.Cursor, "execute", gated)
        state["hold"] = start_purge
        store.upsert_node("n1", "person", {"phase": "2"})
        assert paused.wait(20), "the purge never reached its second nodes DELETE"
        state["hold"] = resume_purge
        store.upsert_node("n1", "person", {"phase": "3"})
        state["purge"].join(timeout=30)

        assert state["first_waited"], "the first DELETE never waited on the first write"
        assert state["second_waited"], "the second DELETE never waited on the second"
        assert not state["purge"].is_alive()
        assert state["deleted"] is True
        assert _rows_left(PG_DSN, "n1") == {"nodes": 0, "edges": 0, "entity_aliases": 0}

    def test_a_purge_aborted_as_a_deadlock_victim_runs_again(
        self, store, monkeypatch
    ) -> None:
        # Two purges lock the node's versions in opposite orders. A writer
        # closes the version the first purge waits on and inserts the new
        # one on a page VACUUM freed, at a lower ctid than the closed one.
        # The second purge's scan locks the new version first, then waits
        # on the closed one, which the first purge holds; the first purge,
        # held before its second DELETE until the second waits, then waits
        # on the new version. PostgreSQL aborts one purge as the deadlock
        # victim, and the store runs it again: it finds the node purged.
        from trellis.stores.postgres.graph import PostgresGraphStore

        assert PG_DSN is not None
        _write_after_a_freed_page(store, PG_DSN, "n1")
        race = _OppositeOrderPurges(store, PG_DSN, "n1")
        # A new pool's connections have no cached insert block, so the
        # write's new version goes to the freed page.
        fresh = PostgresGraphStore(PG_DSN)

        def execute(cur: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
            return race.execute(cur, query, params, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", execute)
        try:
            fresh.upsert_node("n1", "person", {"phase": "2"})
        finally:
            fresh.close()
        assert race.first is not None, "the write never took a FOR UPDATE row lock"
        # Read while the first purge holds the closed version: both show.
        ctids = _version_ctids(PG_DSN, "n1")
        second = threading.Thread(target=race.purge, name="purge-2")
        second.start()
        race.first.join(timeout=60)
        second.join(timeout=60)

        assert race.first_waited, "the first purge never waited on the write"
        assert ctids[True], ctids
        assert ctids[False], ctids
        assert min(ctids[True]) < min(ctids[False]), f"not inverted: {ctids}"
        assert race.second_waited, "the second purge never waited on the first"
        assert not race.first.is_alive()
        assert not second.is_alive()
        assert race.deadlocks in (["purge-1"], ["purge-2"]), race.deadlocks
        raw = {
            name: repr(result)
            for name, result in race.outcome.items()
            if isinstance(result, psycopg.Error)
        }
        assert raw == {}, f"a psycopg error escaped delete_node: {raw}"
        # The victim's next run finds the node purged by the other.
        (winner,) = {"purge-1", "purge-2"} - set(race.deadlocks)
        assert race.outcome == {winner: True, race.deadlocks[0]: False}
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

    # A ``properties.<key>`` DSL filter takes its key as a bound parameter,
    # because psycopg reads a ``%`` in statement text as placeholder syntax.
    # Each key is paired with the sibling key a misread of it would land on,
    # and the sibling holds the value the filter matches, so a misread
    # returns the sibling's row rather than nothing.
    _DSL_KEY_CASES = pytest.mark.parametrize(
        ("key", "sibling"),
        [
            ("owner_team", "owner_teams"),
            ("a%b", "a%%b"),
            ("a%%b", "a%b"),
            ("it's", "it''s"),
        ],
        ids=["plain", "percent", "doubled-percent", "quote"],
    )

    @staticmethod
    def _dsl_ids(store, key: str, op: str, value: Any) -> list[str]:
        from trellis.stores.base.graph_query import FilterClause, NodeQuery

        rows = store.execute_node_query(
            NodeQuery(filters=(FilterClause(f"properties.{key}", op, value),))
        )
        return sorted(row["node_id"] for row in rows)

    @_DSL_KEY_CASES
    def test_contains_filters_on_exactly_the_named_key(
        self, store, key: str, sibling: str
    ) -> None:
        store.upsert_node("hit", "service", {key: ["m", "z"]})
        store.upsert_node("miss", "service", {key: ["z"]})
        store.upsert_node("sibling", "service", {sibling: ["m"]})

        assert self._dsl_ids(store, key, "contains", "m") == ["hit"]

    @_DSL_KEY_CASES
    def test_a_range_op_filters_on_exactly_the_named_key(
        self, store, key: str, sibling: str
    ) -> None:
        store.upsert_node("low", "service", {key: 1})
        store.upsert_node("boundary", "service", {key: 5})
        store.upsert_node("sibling", "service", {sibling: 1})

        assert self._dsl_ids(store, key, "lt", 5) == ["low"]

    def test_an_edge_range_op_filters_on_exactly_the_named_key(self, store) -> None:
        from trellis.stores.base.graph_query import EdgeQuery, FilterClause

        # The edge DSL binds the key too: a misread of ``a%%b`` would
        # return the ``a%b`` edge.
        for node_id in ("source", "low", "sibling"):
            store.upsert_node(node_id, "service", {})
        store.upsert_edge("source", "low", "calls", {"a%%b": 1})
        store.upsert_edge("source", "sibling", "calls", {"a%b": 1})

        rows = store.execute_edge_query(
            EdgeQuery(filters=(FilterClause("properties.a%%b", "lt", 5),))
        )
        assert [row["target_id"] for row in rows] == ["low"]


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


def _drop_the_events_table(store: Any) -> type[Exception]:
    """Make every statement fail inside the cursor, as a server error does.

    The drop goes through ``_clean_tables`` and its scratch-database check.
    """
    _clean_tables(store._dsn)
    return psycopg.errors.UndefinedTable


def _close_the_pool(store: Any) -> type[Exception]:
    """Make every call fail on pool entry, where a ``PoolTimeout`` is raised."""
    from psycopg_pool import PoolClosed

    store._pool.close()
    return PoolClosed


_DRIVER_ERRORS = [
    *(
        pytest.param(_drop_the_events_table, operation, id=f"table-dropped-{operation}")
        for operation in ("append", "has_idempotency_key", "get_events", "count")
    ),
    # Every operation takes its connection through the same wrapper.
    pytest.param(
        _close_the_pool, "has_idempotency_key", id="pool-closed-has_idempotency_key"
    ),
]


class TestPostgresEventLogDriverErrors:
    """A psycopg error in the event log is raised as a type-only ``StoreError``.

    The executor's catches name ``StoreError``, and a ``psycopg.Error`` is
    outside them. The message names the operation and the error's type, not
    the server's text; the psycopg error stays on ``__cause__``.
    """

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

    @pytest.mark.parametrize(("break_log", "operation"), _DRIVER_ERRORS)
    def test_a_driver_error_is_raised_as_a_type_only_store_error(
        self, store, break_log, operation: str
    ) -> None:
        from trellis.errors import StoreError
        from trellis.stores.base.event_log import Event, EventType

        event = Event(
            event_type=EventType.ENTITY_CREATED,
            source="syn-source",
            entity_id="syn-entity-1",
        )
        calls = {
            "append": lambda: store.append(event),
            "has_idempotency_key": lambda: store.has_idempotency_key("syn-key-1"),
            "get_events": store.get_events,
            "count": store.count,
        }
        cause_type = break_log(store)

        with pytest.raises(StoreError) as caught:
            calls[operation]()

        expected = f"Event log {operation} failed: {cause_type.__name__}"
        assert str(caught.value) == expected
        assert caught.value.store == "event_log"
        assert type(caught.value.__cause__) is cause_type

    def test_an_error_that_is_not_the_driver_s_keeps_its_type(self, store) -> None:
        """A payload that cannot be serialized is the caller's bug, not the store's.

        The executor logs a handler's untyped failure with its traceback and
        a ``StoreError`` without one, so relabelling it would hide the bug.
        """
        from trellis.stores.base.event_log import Event, EventType

        event = Event(
            event_type=EventType.ENTITY_CREATED,
            source="syn-source",
            payload={"syn-field": object()},
        )

        with pytest.raises(TypeError):
            store.append(event)


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
