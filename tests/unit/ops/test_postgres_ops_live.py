"""``admin backup`` and ``admin migrate`` against a real Postgres.

Marked ``postgres``: deselected by default and run by ``live-infra.yml``, which
supplies a throwaway server and ``pg_dump``. The unit-level behaviour (argv
shape, sentinel password, ``tool_missing``) is in ``test_backup_postgres.py``
with the subprocess replaced; this file is the part that needs a database.

Fixtures drop tables, so they go through ``tests.pg_scratch``, which refuses a
database that holds data it did not create.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration._live_server import repo_src_pythonpath
from tests.pg_scratch import configured_dsn, scratch_dsn
from trellis.ops import schema_migrate
from trellis.ops.backup_postgres import DUMPED, run_pg_dump
from trellis.ops.schema_migrate import (
    VERSION_TABLE,
    Migration,
    MigrationSet,
    run_migrate,
)
from trellis.stores.registry import StoreRegistry

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not configured_dsn(), reason="TRELLIS_TEST_PG_DSN not set"),
]

_TABLES = (
    "documents",
    "trellis_api_keys",
    VERSION_TABLE,
    "step_2",
    "step_3",
    "trellis_backup_probe",
)


@pytest.fixture
def dsn() -> str:
    psycopg = pytest.importorskip("psycopg")
    value = scratch_dsn()
    with psycopg.connect(value, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {', '.join(_TABLES)} CASCADE")
    return value


def _tables(dsn: str) -> set[str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def _need_pg_dump() -> None:
    if shutil.which("pg_dump") is not None:
        return
    if os.environ.get("GITHUB_ACTIONS"):
        pytest.fail("pg_dump is not on PATH in CI; install postgresql-client")
    pytest.skip("pg_dump is not installed")


class TestRealDump:
    def test_the_dump_is_a_custom_format_archive_holding_the_data(
        self, dsn: str, tmp_path: Path
    ) -> None:
        import psycopg

        _need_pg_dump()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE trellis_backup_probe (x INTEGER)")
            conn.execute("INSERT INTO trellis_backup_probe VALUES (41), (42)")
        dest = tmp_path / "postgres" / "probe.dump"

        result = run_pg_dump(dsn, dest)

        assert result.status == DUMPED, result.reason
        assert dest.stat().st_size == result.size_bytes > 0
        assert dest.read_bytes()[:5] == b"PGDMP"
        restore = shutil.which("pg_restore")
        assert restore is not None
        listing = subprocess.run(  # noqa: S603 - fixed argv, no DSN
            [restore, "--list", str(dest)],
            env={**os.environ, "PYTHONPATH": repo_src_pythonpath()},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "trellis_backup_probe" in listing

    def test_an_unreachable_server_fails_without_leaving_a_file(
        self, dsn: str, tmp_path: Path
    ) -> None:
        _need_pg_dump()
        dest = tmp_path / "dead.dump"

        result = run_pg_dump(
            "postgresql://nobody:never-used@127.0.0.1:1/none?connect_timeout=2", dest
        )

        assert result.status == "failed"
        assert "never-used" not in (result.reason or "")
        assert not dest.exists()


def _registry(tmp_path: Path, dsn: str) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir(exist_ok=True)
    return StoreRegistry(
        config={
            "knowledge": {"document": {"backend": "postgres", "dsn": dsn}},
            "operational": {"api_key": {"backend": "postgres", "dsn": dsn}},
        },
        stores_dir=stores,
    )


def _by_store(report: Any) -> dict[str, Any]:
    return {s.store: s for s in report.stores}


def _chain(*, fail_at: int | None = None) -> MigrationSet:
    def step(version: int):
        def apply(conn: Any) -> None:
            conn.execute(f"CREATE TABLE step_{version} (x INTEGER)")
            if version == fail_at:
                msg = "boom"
                raise RuntimeError(msg)

        return apply

    return MigrationSet(
        [Migration("document", "postgres", v, f"add step {v}", step(v)) for v in (2, 3)]
    )


@contextlib.contextmanager
def _relation(dsn: str, create: str, drop: str) -> Iterator[None]:
    """Create a relation for one test and drop it after, whatever the test does."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(create)
    try:
        yield
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(drop)


class TestPostgresMigrateSeesOnlyBaseTables:
    """``pg_class`` lists base tables the search path reaches; the information schema
    lists views too, and only what the role holds a privilege on."""

    def test_a_view_named_like_a_store_table_does_not_make_the_store_current(
        self, dsn: str, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path, dsn)

        with _relation(
            dsn,
            "CREATE VIEW documents AS SELECT 1 AS x",
            "DROP VIEW IF EXISTS documents",
        ):
            report = run_migrate(registry, apply=False)

        doc = _by_store(report)["document"]
        assert doc.status == "absent", "a view is not the document store's table"
        registry.close()

    def test_a_graph_database_holding_only_entity_aliases_reads_unrecognized(
        self, dsn: str, tmp_path: Path
    ) -> None:
        stores = tmp_path / "stores"
        stores.mkdir(exist_ok=True)
        registry = StoreRegistry(
            config={"knowledge": {"graph": {"backend": "postgres", "dsn": dsn}}},
            stores_dir=stores,
        )

        with _relation(
            dsn,
            "CREATE TABLE entity_aliases (x INTEGER)",
            "DROP TABLE IF EXISTS entity_aliases",
        ):
            report = run_migrate(registry, apply=False)

        graph = _by_store(report)["graph"]
        assert graph.status == "unrecognized"
        assert report.status == "error"
        registry.close()


def _on_search_path(dsn: str, *schemas: str) -> str:
    """The DSN with the session's ``search_path`` set, as a deployment sets it."""
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, options=f"-c search_path={','.join(schemas)}")


@pytest.fixture
def three_schemas(dsn: str) -> Iterator[tuple[str, str, str]]:
    """``(first, second, off)``: three schemas made here and dropped here.

    The names carry a random suffix, so nothing the database already holds can
    be touched, and only these three are dropped.
    """
    import psycopg

    suffix = uuid.uuid4().hex[:8]
    names = (
        f"trellis_sp_first_{suffix}",
        f"trellis_sp_second_{suffix}",
        f"trellis_sp_off_{suffix}",
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name in names:
            conn.execute(f"CREATE SCHEMA {name}")
    try:
        yield names
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for name in names:
                conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _execute(dsn: str, *statements: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _current_schema(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT current_schema()").fetchone()[0]


class TestPostgresInspectFollowsSearchPath:
    """The inspect reads the tables the connection's ``search_path`` reaches.

    ``current_schema()`` is the first existing schema on the path, so reading
    ``pg_tables`` through it saw nothing of a later one. The stores' own SQL
    names its tables unqualified and resolves them through the whole path, so
    the inspect has to resolve them the same way. The query's text is pinned in
    ``test_schema_migrate.py``; these cases run it.
    """

    def test_a_store_in_a_later_schema_on_the_path_reads_present(
        self, dsn: str, three_schemas: tuple[str, str, str], tmp_path: Path
    ) -> None:
        first, second, _off = three_schemas
        on_path = _on_search_path(dsn, first, second)
        _execute(dsn, f"CREATE TABLE {second}.documents (x INTEGER)")
        assert _current_schema(on_path) == first, "the first schema is empty"
        registry = _registry(tmp_path, on_path)

        report = run_migrate(registry, apply=False)

        doc = _by_store(report)["document"]
        assert (doc.status, doc.current_version) == ("current", 1)
        registry.close()

    def test_a_store_in_a_schema_off_the_path_reads_absent(
        self, dsn: str, three_schemas: tuple[str, str, str], tmp_path: Path
    ) -> None:
        first, second, off = three_schemas
        _execute(dsn, f"CREATE TABLE {off}.documents (x INTEGER)")
        registry = _registry(tmp_path, _on_search_path(dsn, first, second))

        report = run_migrate(registry, apply=False)

        assert _by_store(report)["document"].status == "absent"
        registry.close()

    def test_a_view_earlier_on_the_path_shadows_the_stores_table(
        self, dsn: str, three_schemas: tuple[str, str, str], tmp_path: Path
    ) -> None:
        """The store's unqualified ``documents`` would resolve to the view, so
        the table behind it is not the store's and the store reads absent."""
        first, second, _off = three_schemas
        _execute(
            dsn,
            f"CREATE VIEW {first}.documents AS SELECT 1 AS x",
            f"CREATE TABLE {second}.documents (x INTEGER)",
        )
        registry = _registry(tmp_path, _on_search_path(dsn, first, second))

        report = run_migrate(registry, apply=False)

        assert _by_store(report)["document"].status == "absent"
        registry.close()

    def test_the_version_record_is_read_from_the_schema_the_path_reaches_first(
        self, dsn: str, three_schemas: tuple[str, str, str]
    ) -> None:
        """Two schemas each hold the store and a version record; the one that
        answers is the first on the path, and swapping the path swaps it."""
        first, second, _off = three_schemas
        for schema, version in ((first, 3), (second, 5)):
            _execute(
                dsn,
                f"CREATE TABLE {schema}.documents (x INTEGER)",
                f"CREATE TABLE {schema}.{VERSION_TABLE} (store TEXT NOT NULL,"
                " version INTEGER NOT NULL, description TEXT NOT NULL,"
                " applied_at TEXT NOT NULL, PRIMARY KEY (store, version))",
                f"INSERT INTO {schema}.{VERSION_TABLE} VALUES"  # noqa: S608
                f" ('document', {version}, 'seed', 'now')",
            )

        def inspect(*path: str) -> tuple[frozenset[str], int | None]:
            db = schema_migrate._PostgresDb(_on_search_path(dsn, *path), writable=False)
            try:
                return db.inspect("document")
            finally:
                db.close()

        assert inspect(first, second) == (frozenset({"documents"}), 3)
        assert inspect(second, first) == (frozenset({"documents"}), 5)


class TestPostgresMigrate:
    def test_a_report_reads_the_shape_and_creates_no_version_table(
        self, dsn: str, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path, dsn)
        registry._get("document")
        registry._get("api_key")

        report = run_migrate(registry, apply=False)

        doc = _by_store(report)["document"]
        assert (doc.status, doc.current_version, doc.recorded) == ("current", 1, False)
        assert _by_store(report)["api_key"].status == "current"
        assert VERSION_TABLE not in _tables(dsn)
        registry.close()

    def test_apply_stamps_once_and_a_second_apply_is_a_no_op(
        self, dsn: str, tmp_path: Path
    ) -> None:
        import psycopg

        registry = _registry(tmp_path, dsn)
        registry._get("document")

        first = run_migrate(registry, apply=True)
        second = run_migrate(registry, apply=True)

        assert _by_store(first)["document"].status == "stamped"
        assert _by_store(second)["document"].status == "current"
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                f"SELECT store, version FROM {VERSION_TABLE} ORDER BY store"  # noqa: S608
            ).fetchall()
        assert rows == [("document", 1)]
        registry.close()

    def test_steps_apply_in_order_and_a_failed_step_rolls_back_whole(
        self, dsn: str, tmp_path: Path
    ) -> None:
        import psycopg

        registry = _registry(tmp_path, dsn)
        registry._get("document")

        failed = run_migrate(registry, apply=True, migrations=_chain(fail_at=3))

        doc = _by_store(failed)["document"]
        assert doc.status == "failed"
        assert doc.applied == [2]
        tables = _tables(dsn)
        assert "step_2" in tables
        assert "step_3" not in tables, "the failed step's DDL must roll back"
        with psycopg.connect(dsn) as conn:
            versions = conn.execute(
                f"SELECT version FROM {VERSION_TABLE} ORDER BY version"  # noqa: S608
            ).fetchall()
        assert versions == [(1,), (2,)]
        registry.close()
