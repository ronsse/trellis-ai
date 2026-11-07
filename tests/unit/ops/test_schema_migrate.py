"""``run_migrate``: report by default, stamp the baseline on ``--apply``.

Every store here is a real SQLite store opened by the registry, so the tests
exercise the shapes the stores actually create, not a hand-drawn imitation of
them. The migration chain that proves the framework runs steps is registered by
the test (:data:`SHIPPED_MIGRATIONS` is empty: this build has one version).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trellis.errors import ConfigError
from trellis.ops import schema_migrate
from trellis.ops.schema_migrate import (
    BASELINE_TABLES,
    BASELINE_VERSION,
    SHIPPED_MIGRATIONS,
    VERSION_TABLE,
    Migration,
    MigrationSet,
    detect_version,
    owned_tables,
    run_migrate,
)
from trellis.stores.registry import _PLANE_OF, StoreRegistry

SQLITE_STORES = sorted(t for t in _PLANE_OF if t != "blob")


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    reg = StoreRegistry(stores_dir=stores)
    yield reg
    reg.close()


def open_every_store(reg: StoreRegistry) -> None:
    for store_type in _PLANE_OF:
        reg._get(store_type)


def db_path(reg: StoreRegistry, store_type: str) -> Path:
    path = reg.configured_sqlite_path(store_type)
    assert path is not None
    return path


def tables_of(path: Path) -> set[str]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {r[0] for r in rows}
    finally:
        conn.close()


def recorded_rows(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            f"SELECT store, version, description FROM {VERSION_TABLE}"  # noqa: S608
            " ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def by_store(report: Any) -> dict[str, Any]:
    return {s.store: s for s in report.stores}


def marker_chain(calls: list[int], *, fail_at: int | None = None) -> MigrationSet:
    """Two steps for the document store; each leaves a table behind."""

    def step(version: int):
        def apply(conn: Any) -> None:
            conn.execute(f"CREATE TABLE step_{version} (x INTEGER)")
            calls.append(version)
            if version == fail_at:
                msg = "boom"
                raise RuntimeError(msg)

        return apply

    return MigrationSet(
        [Migration("document", "sqlite", v, f"add step {v}", step(v)) for v in (2, 3)]
    )


class TestBaselineShapes:
    def test_every_sqlite_store_opens_with_its_baseline_tables(
        self, registry: StoreRegistry
    ) -> None:
        """The roster in ``BASELINE_TABLES`` is read against the stores themselves,
        so a store that renames a table cannot leave it stale."""
        open_every_store(registry)
        for store_type in SQLITE_STORES:
            tables = frozenset(tables_of(db_path(registry, store_type)))
            assert BASELINE_TABLES[("sqlite", store_type)] <= tables, store_type
            assert detect_version(store_type, "sqlite", tables) == BASELINE_VERSION

    def test_every_table_a_fresh_sqlite_store_creates_is_one_it_is_known_to_own(
        self, registry: StoreRegistry
    ) -> None:
        """``absent`` means none of a store's own tables exist, so a table the store
        creates but the roster does not name is a leftover that would read absent."""
        open_every_store(registry)
        for store_type in SQLITE_STORES:
            created = tables_of(db_path(registry, store_type)) - {VERSION_TABLE}
            owned = owned_tables("sqlite", store_type)
            assert owned is not None, store_type
            assert created <= owned, (store_type, sorted(created - owned))

    def test_every_sqlite_store_type_has_a_baseline_entry(self) -> None:
        missing = [t for t in SQLITE_STORES if ("sqlite", t) not in BASELINE_TABLES]
        assert not missing, f"add BASELINE_TABLES entries for {missing}"

    def test_an_unknown_shape_has_no_version(self) -> None:
        assert detect_version("document", "sqlite", frozenset({"unrelated"})) is None

    def test_the_build_ships_the_baseline_and_nothing_pending(self) -> None:
        for store_type in _PLANE_OF:
            assert SHIPPED_MIGRATIONS.target(store_type, "sqlite") == BASELINE_VERSION
            assert not SHIPPED_MIGRATIONS.after(store_type, "sqlite", BASELINE_VERSION)


class TestReport:
    def test_a_fresh_deployment_reports_absent_and_creates_nothing(
        self, registry: StoreRegistry
    ) -> None:
        before = sorted(p.name for p in registry.stores_dir.iterdir())

        report = run_migrate(registry, apply=False)

        assert report.status == "ok"
        states = by_store(report)
        for store_type in SQLITE_STORES:
            assert states[store_type].status == "absent", store_type
        assert sorted(p.name for p in registry.stores_dir.iterdir()) == before

    def test_a_main_shaped_store_reads_as_the_baseline_without_a_record(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        paths = {t: db_path(registry, t) for t in SQLITE_STORES}
        mtimes = {t: p.stat().st_mtime_ns for t, p in paths.items()}

        report = run_migrate(registry, apply=False)

        assert report.status == "ok"
        for store_type, state in by_store(report).items():
            if store_type == "blob":
                assert state.status == "unsupported"
                continue
            assert state.status == "current", store_type
            assert state.current_version == BASELINE_VERSION
            assert state.target_version == BASELINE_VERSION
            assert state.recorded is False
            assert state.pending == []
        for store_type, path in paths.items():
            assert VERSION_TABLE not in tables_of(path), store_type
            assert path.stat().st_mtime_ns == mtimes[store_type], store_type

    def test_pending_steps_are_listed_and_not_run(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        calls: list[int] = []

        report = run_migrate(registry, apply=False, migrations=marker_chain(calls))

        doc = by_store(report)["document"]
        assert doc.status == "pending"
        assert doc.current_version == BASELINE_VERSION
        assert doc.target_version == 3
        assert doc.pending == [2, 3]
        assert calls == []
        assert "step_2" not in tables_of(db_path(registry, "document"))


class TestApply:
    def test_apply_stamps_the_baseline_and_changes_no_other_table(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        before = {t: tables_of(db_path(registry, t)) for t in SQLITE_STORES}

        report = run_migrate(registry, apply=True)

        assert report.status == "ok"
        for store_type in SQLITE_STORES:
            state = by_store(report)[store_type]
            assert state.status == "stamped", store_type
            assert state.recorded is True
            assert state.applied == []
            path = db_path(registry, store_type)
            assert tables_of(path) - {VERSION_TABLE} == before[store_type]
            assert recorded_rows(path) == [(store_type, BASELINE_VERSION, "baseline")]

    def test_a_second_apply_is_a_no_op(self, registry: StoreRegistry) -> None:
        open_every_store(registry)
        run_migrate(registry, apply=True)
        snapshot = {t: recorded_rows(db_path(registry, t)) for t in SQLITE_STORES}

        again = run_migrate(registry, apply=True)

        assert again.status == "ok"
        for store_type in SQLITE_STORES:
            assert by_store(again)[store_type].status == "current", store_type
            assert recorded_rows(db_path(registry, store_type)) == snapshot[store_type]

    def test_a_report_after_apply_reads_the_record(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        run_migrate(registry, apply=True)

        report = run_migrate(registry, apply=False)

        doc = by_store(report)["document"]
        assert (doc.status, doc.recorded, doc.current_version) == (
            "current",
            True,
            BASELINE_VERSION,
        )

    def test_apply_on_a_fresh_deployment_creates_no_file(
        self, registry: StoreRegistry
    ) -> None:
        report = run_migrate(registry, apply=True)

        assert report.status == "ok"
        assert not list(registry.stores_dir.iterdir())
        assert by_store(report)["document"].status == "absent"

    def test_a_partial_store_is_refused_and_not_stamped(
        self, registry: StoreRegistry
    ) -> None:
        """Some of a store's tables but not all: a shape no build created."""
        foreign = db_path(registry, "graph").with_name("other.db")
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE nodes (x INTEGER)")  # the graph owns nodes AND edges
        conn.commit()
        conn.close()
        reg = StoreRegistry(
            config={
                "knowledge": {"graph": {"backend": "sqlite", "db_path": str(foreign)}}
            },
            stores_dir=registry.stores_dir,
        )

        report = run_migrate(reg, apply=True)

        graph = by_store(report)["graph"]
        assert graph.status == "unrecognized"
        assert report.status == "error"
        assert VERSION_TABLE not in tables_of(foreign)

    def test_a_graph_file_whose_only_leftover_is_entity_aliases_is_unrecognized(
        self, registry: StoreRegistry
    ) -> None:
        """``entity_aliases`` alone is not an empty graph database: some build made it
        and the nodes and edges are gone. Reading it ``absent`` hid that."""
        foreign = db_path(registry, "graph").with_name("leftover.db")
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE entity_aliases (x INTEGER)")
        conn.commit()
        conn.close()
        reg = StoreRegistry(
            config={
                "knowledge": {"graph": {"backend": "sqlite", "db_path": str(foreign)}}
            },
            stores_dir=registry.stores_dir,
        )

        report = run_migrate(reg, apply=True)

        assert by_store(report)["graph"].status == "unrecognized"
        assert report.status == "error"
        assert VERSION_TABLE not in tables_of(foreign), "never stamped"

    def test_a_file_holding_only_another_stores_tables_reads_absent_not_unrecognized(
        self, registry: StoreRegistry
    ) -> None:
        shared = db_path(registry, "document").with_name("shared.db")
        conn = sqlite3.connect(shared)
        conn.execute("CREATE TABLE events (x INTEGER)")  # the event log's table
        conn.commit()
        conn.close()
        reg = StoreRegistry(
            config={
                "operational": {
                    "trace": {"backend": "sqlite", "db_path": str(shared)},
                    "event_log": {"backend": "sqlite", "db_path": str(shared)},
                }
            },
            stores_dir=registry.stores_dir,
        )

        report = run_migrate(reg, apply=True)

        states = by_store(report)
        assert states["trace"].status == "absent"
        assert states["event_log"].status == "stamped"
        assert report.status == "ok"
        assert recorded_rows(shared) == [("event_log", 1, "baseline")]


class TestStepsRunInOrderAndOnce:
    def test_steps_apply_once_in_version_order_with_a_row_each(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        calls: list[int] = []
        chain = marker_chain(calls)

        first = run_migrate(registry, apply=True, migrations=chain)

        doc = by_store(first)["document"]
        assert doc.status == "applied"
        assert doc.applied == [2, 3]
        assert doc.current_version == 3
        assert doc.pending == []
        assert calls == [2, 3]
        path = db_path(registry, "document")
        assert {"step_2", "step_3"} <= tables_of(path)
        assert recorded_rows(path) == [
            ("document", 1, "baseline"),
            ("document", 2, "add step 2"),
            ("document", 3, "add step 3"),
        ]

        second = run_migrate(registry, apply=True, migrations=chain)

        assert by_store(second)["document"].status == "current"
        assert calls == [2, 3], "an applied step must not run again"
        # A store with no step of its own still stamps the baseline only.
        assert recorded_rows(db_path(registry, "trace")) == [("trace", 1, "baseline")]

    def test_a_failed_step_leaves_the_version_unstamped_and_the_schema_unchanged(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        calls: list[int] = []

        report = run_migrate(
            registry, apply=True, migrations=marker_chain(calls, fail_at=3)
        )

        doc = by_store(report)["document"]
        assert report.status == "error"
        assert doc.status == "failed"
        assert doc.applied == [2]
        assert doc.current_version == 2
        assert "step to version 3 failed" in (doc.reason or "")
        assert "boom" in (doc.reason or "")
        path = db_path(registry, "document")
        tables = tables_of(path)
        assert "step_2" in tables, "the step before the failure stays applied"
        assert "step_3" not in tables, "the failed step's DDL rolled back"
        assert [r[1] for r in recorded_rows(path)] == [1, 2]
        # The other stores were not stopped by it.
        assert by_store(report)["trace"].status == "stamped"

    def test_a_failed_first_step_still_leaves_the_store_at_the_baseline(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)

        report = run_migrate(
            registry, apply=True, migrations=marker_chain([], fail_at=2)
        )

        assert by_store(report)["document"].status == "failed"
        path = db_path(registry, "document")
        assert "step_2" not in tables_of(path)
        assert [r[1] for r in recorded_rows(path)] == [1]


class TestMigrationSetValidation:
    @staticmethod
    def step(version: int) -> Migration:
        return Migration("document", "sqlite", version, "x", lambda conn: None)

    def test_a_gap_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no gap or repeat"):
            MigrationSet([self.step(2), self.step(4)])

    def test_a_repeat_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no gap or repeat"):
            MigrationSet([self.step(2), self.step(2)])

    def test_a_chain_that_skips_the_first_step_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no gap or repeat"):
            MigrationSet([self.step(3)])

    def test_chains_are_kept_per_store_and_backend(self) -> None:
        other = Migration("graph", "sqlite", 2, "x", lambda conn: None)
        chain = MigrationSet([self.step(2), other])
        assert chain.target("document", "sqlite") == 2
        assert chain.target("graph", "sqlite") == 2
        assert chain.target("document", "postgres") == BASELINE_VERSION


class FakePostgres(schema_migrate._Db):
    """A shared Postgres database: it holds ``tables`` and notes every stamp."""

    def __init__(self, tables: set[str]) -> None:
        self.tables = frozenset(tables)
        self.stamped: list[tuple[str, int]] = []
        self.opened_writable: list[bool] = []

    def inspect(self, store: str) -> tuple[frozenset[str], int | None]:
        return self.tables, None

    def record(self, store, version, description, step, now) -> None:
        self.stamped.append((store, version))

    def close(self) -> None:
        pass


def postgres_registry() -> StoreRegistry:
    return StoreRegistry(
        config={
            "knowledge": {
                "document": {"backend": "postgres"},
                "graph": {"backend": "postgres"},
                "vector": {"backend": "pgvector"},
            },
            "operational": {
                "trace": {"backend": "postgres"},
                "event_log": {"backend": "postgres"},
                "api_key": {"backend": "postgres"},
            },
        },
        stores_dir=None,
    )


def run_against(
    monkeypatch: pytest.MonkeyPatch, planes: dict[str, FakePostgres], *, apply: bool
) -> Any:
    """Run ``run_migrate`` with each plane's stores sharing that plane's database."""

    def fake_open(reg, store_type, dialect, *, writable):
        return planes[schema_migrate._PLANE_OF[store_type]]

    monkeypatch.setattr(schema_migrate, "_open", fake_open)
    return run_migrate(postgres_registry(), apply=apply)


class TestAbsentIsPerStoreOnASharedDatabase:
    """Postgres stores share one database per plane, so a neighbour's tables
    must not make a store that was never opened look unrecognized."""

    def test_an_operational_database_with_only_api_keys_reads_trace_and_events_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = FakePostgres(set())
        operational = FakePostgres({"trellis_api_keys"})

        report = run_against(
            monkeypatch,
            {"knowledge": knowledge, "operational": operational},
            apply=False,
        )

        states = by_store(report)
        assert states["trace"].status == "absent"
        assert states["event_log"].status == "absent"
        assert states["api_key"].status == "current"
        assert report.status == "ok"

    def test_a_knowledge_database_with_only_documents_reads_graph_and_vectors_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = FakePostgres({"documents"})
        operational = FakePostgres(set())

        report = run_against(
            monkeypatch,
            {"knowledge": knowledge, "operational": operational},
            apply=True,
        )

        states = by_store(report)
        assert states["graph"].status == "absent"
        assert states["vector"].status == "absent"
        assert states["document"].status == "stamped"
        assert report.status == "ok"
        # Only the store that exists is stamped.
        assert knowledge.stamped == [("document", 1)]

    def test_a_partial_store_on_a_shared_database_is_still_unrecognized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = FakePostgres({"documents", "nodes"})  # graph without its edges
        operational = FakePostgres(set())

        report = run_against(
            monkeypatch,
            {"knowledge": knowledge, "operational": operational},
            apply=True,
        )

        states = by_store(report)
        assert states["graph"].status == "unrecognized"
        assert states["vector"].status == "absent"
        assert report.status == "error"
        assert ("graph", 1) not in knowledge.stamped

    def test_a_knowledge_database_with_only_entity_aliases_reads_graph_unrecognized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = FakePostgres({"entity_aliases"})  # the graph's, and not a marker
        operational = FakePostgres(set())

        report = run_against(
            monkeypatch,
            {"knowledge": knowledge, "operational": operational},
            apply=True,
        )

        states = by_store(report)
        assert states["graph"].status == "unrecognized"
        assert states["document"].status == "absent"
        assert states["vector"].status == "absent"
        assert report.status == "error"
        assert knowledge.stamped == []


class FakeCatalogConnection:
    """A psycopg connection that answers the two catalog queries the way Postgres does.

    The database holds one base table the role cannot read (``edges``), one it can
    (``nodes``) and a view (``documents``). ``information_schema.tables`` lists the
    view and leaves out what the role holds no privilege on; ``pg_class`` (read as
    ``pg_tables`` reads it, ordinary and partitioned tables) lists base tables
    only, whatever the role may do with them.
    """

    BASE_TABLES = ("nodes", "edges")
    GRANTED = ("nodes",)
    VIEWS = ("documents",)

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.rolled_back = False
        self._rows: list[tuple[str]] = []

    def cursor(self) -> FakeCatalogConnection:
        return self

    def __enter__(self) -> FakeCatalogConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, _params: Any = None) -> None:
        self.queries.append(sql)
        if "information_schema.tables" in sql:
            self._rows = [(n,) for n in (*self.GRANTED, *self.VIEWS)]
        elif "pg_catalog.pg_class" in sql:
            self._rows = [(n,) for n in self.BASE_TABLES]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[str]]:
        return self._rows

    def rollback(self) -> None:
        self.rolled_back = True


class TestPostgresInspectReadsBaseTablesOnly:
    """The inspect asks ``pg_catalog.pg_class`` for the tables the search path reaches.

    The fake models the difference between the catalogs; the same two cases run
    against a real server in ``test_postgres_ops_live.py``, which CI's live-infra
    leg runs.
    """

    @staticmethod
    def inspect() -> tuple[FakeCatalogConnection, frozenset[str]]:
        conn = FakeCatalogConnection()
        db = object.__new__(schema_migrate._PostgresDb)
        db._conn = conn
        tables, version = db.inspect("graph")
        assert version is None
        return conn, tables

    def test_it_reads_pg_class_and_not_the_information_schema(self) -> None:
        conn, _tables = self.inspect()

        assert len(conn.queries) == 1
        sql = conn.queries[0]
        assert "pg_catalog.pg_class" in sql
        assert "information_schema.tables" not in sql
        assert "relkind IN ('r', 'p')" in sql, "ordinary and partitioned tables only"
        assert conn.rolled_back, "the read transaction is ended"

    def test_it_follows_the_whole_search_path_and_not_the_first_schema(self) -> None:
        """``current_schema()`` is the first existing schema on the path, so a
        store living in a later one read ``absent``. ``pg_table_is_visible`` is
        what the store's own unqualified table names resolve by.

        The live leg that creates a store behind a second schema was not run
        here; this pins the query's text, and the SQL was run by hand against a
        throwaway Postgres 16 (views excluded, partitioned tables included,
        shadowed names once, ``pg_catalog`` left out).
        """
        conn, _tables = self.inspect()

        sql = conn.queries[0]
        assert "pg_catalog.pg_table_is_visible(c.oid)" in sql
        assert "current_schema" not in sql
        assert "schemaname" not in sql

    def test_the_system_schemas_are_left_out(self) -> None:
        """``pg_catalog`` is always on the path, so without this its tables
        would be listed and a store with no table list would read as present."""
        conn, _tables = self.inspect()

        sql = conn.queries[0]
        assert "'pg_catalog'::regnamespace" in sql
        assert "'information_schema'::regnamespace" in sql
        assert "NOT IN" in sql

    def test_a_view_is_not_a_table_and_an_unreadable_table_is_still_one(self) -> None:
        _conn, tables = self.inspect()

        assert tables == frozenset({"nodes", "edges"})
        assert "documents" not in tables, "a view must not make a store look present"


class TestPostgresReportIsReadOnly:
    @staticmethod
    def fake_psycopg(monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
        conns: list[SimpleNamespace] = []

        def connect(dsn: str, **_kw: Any) -> SimpleNamespace:
            conn = SimpleNamespace(read_only=False)
            conns.append(conn)
            return conn

        monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
        return conns

    def test_a_report_opens_its_transactions_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = self.fake_psycopg(monkeypatch)

        schema_migrate._PostgresDb("postgresql://u@h/db", writable=False)

        assert conns[0].read_only is True

    def test_apply_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conns = self.fake_psycopg(monkeypatch)

        schema_migrate._PostgresDb("postgresql://u@h/db", writable=True)

        assert conns[0].read_only is False

    def test_the_runner_asks_for_a_read_only_connection_in_report_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[bool] = []

        def fake_pg(dsn: str, *, writable: bool) -> FakePostgres:
            asked.append(writable)
            return FakePostgres(set())

        monkeypatch.setattr(schema_migrate, "_PostgresDb", fake_pg)
        reg = StoreRegistry(
            config={"operational": {"api_key": {"backend": "postgres", "dsn": "x"}}},
            stores_dir=None,
        )

        schema_migrate._open(reg, "api_key", "postgres", writable=False)
        schema_migrate._open(reg, "api_key", "postgres", writable=True)

        assert asked == [False, True]
