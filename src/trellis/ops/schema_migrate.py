"""Versioned schema migration for the SQL stores: explicit only, never on open.

Every SQL store already creates its own tables, and adds a column it later
needs, when it is opened (``_init_schema``). That is how a fresh install works
and it is unchanged. What was missing is a place to say *which version of the
schema a store is at* and a way to apply a change that cannot be an idempotent
"add it if absent" (a column rewrite, a backfill, a split table).

This module is that place. Its one hard rule is the reason it is a separate
module and not a method on the store base classes: **nothing here runs when a
store is opened.** No store class, the registry, ``admin init``, the API
lifespan or the MCP server imports it
(``tests/unit/ops/test_schema_migrate_not_on_open.py``). The version record is
created by the first ``trellis admin migrate --apply`` and not before, so a
``git pull`` followed by an ordinary command cannot change a deployed store's
schema through this path.

The model, kept small on purpose:

* A store's *version* is an integer. A store with no record reads as the
  baseline, :data:`BASELINE_VERSION`, *derived from its shape*
  (:func:`detect_version`): the tables the baseline owns are there. A store
  none of whose tables exist is ``absent`` (Postgres stores share a database,
  so a neighbour's tables do not count), and one with some of its tables but not
  all the baseline ones, or a shape that matches nothing known, is
  ``unrecognized`` and never stamped.
* A :class:`Migration` moves one ``(store, backend)`` from ``version - 1`` to
  ``version``. A :class:`MigrationSet` holds the steps and refuses a chain with
  a gap or a repeat. The shipped set is empty: this build has no migration,
  so ``--apply`` on today's stores only stamps the baseline.
* A step and the row that records it commit in one transaction (SQLite and
  Postgres DDL are both transactional, with the exceptions listed on
  :class:`Migration`), so a failed step leaves the version where it was and the
  schema as it was.

Adding the first real migration also means teaching :func:`detect_version` the
new shape: a store that a newer build created fresh has no record yet, and its
shape, not the absence of a record, is what says it is already at the new
version (``tests/unit/ops/test_schema_migrate.py`` pins that a store opened by
this build is detected at the target).

This is not ``trellis admin migrate-graph`` (copies graph rows between
backends) or ``migrate-provenance`` (backfills edge provenance in place). Those
move *data*; this versions the *schema*.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.path_presence import path_is_present
from trellis.errors import ConfigError
from trellis.stores.registry import _PLANE_OF, StoreRegistry

logger = structlog.get_logger(__name__)

#: The version a store is at when nothing was ever applied to it.
BASELINE_VERSION = 1

#: The table that records applied versions. It does not exist until the first
#: ``--apply``; reading never creates it.
VERSION_TABLE = "trellis_schema_migrations"

SQLITE = "sqlite"
POSTGRES = "postgres"

# Per-store outcomes.
CURRENT = "current"  # at the target version
PENDING = "pending"  # report mode: steps would be applied
STAMPED = "stamped"  # apply mode: baseline recorded, nothing else to do
APPLIED = "applied"  # apply mode: one or more steps ran
ABSENT = "absent"  # no schema yet; the store creates it at first open
UNSUPPORTED = "unsupported"  # a backend this framework does not version
UNRECOGNIZED = "unrecognized"  # a shape that matches no known version
FAILED = "failed"  # could not be read, or a step failed

STATUS_OK = "ok"
STATUS_ERROR = "error"

_FAILURE_STATUSES = frozenset({FAILED, UNRECOGNIZED})

#: The tables a baseline-shaped store owns. Both are small hand-read sets, one
#: marker or two per store; ``tests/unit/ops/test_schema_migrate.py`` opens every
#: SQLite store fresh and requires them to be there, so a store that renames its
#: table cannot leave this stale.
BASELINE_TABLES: dict[tuple[str, str], frozenset[str]] = {
    (SQLITE, "trace"): frozenset({"traces"}),
    (SQLITE, "document"): frozenset({"documents"}),
    (SQLITE, "graph"): frozenset({"nodes", "edges"}),
    (SQLITE, "vector"): frozenset({"vectors"}),
    (SQLITE, "event_log"): frozenset({"events"}),
    (SQLITE, "outcome"): frozenset({"outcomes"}),
    (SQLITE, "parameter"): frozenset({"parameter_snapshots"}),
    (SQLITE, "tuner_state"): frozenset({"proposals", "tuner_cursors"}),
    (SQLITE, "api_key"): frozenset({"trellis_api_keys"}),
    (POSTGRES, "trace"): frozenset({"traces"}),
    (POSTGRES, "document"): frozenset({"documents"}),
    (POSTGRES, "graph"): frozenset({"nodes", "edges"}),
    (POSTGRES, "vector"): frozenset({"vectors"}),
    (POSTGRES, "event_log"): frozenset({"events"}),
    (POSTGRES, "api_key"): frozenset({"trellis_api_keys"}),
}

#: Tables a store creates *beyond* its baseline markers. They are not what makes a
#: store baseline-shaped, but they are its own: a database holding one of them and
#: not the markers is a leftover of this store, not an empty database, so it reads
#: ``unrecognized`` rather than ``absent``. ``tests/unit/ops/test_schema_migrate.py``
#: opens every SQLite store fresh and requires each table it creates to be in this
#: set or in :data:`BASELINE_TABLES`; the Postgres entries are read against the
#: store's DDL by hand (and run in the live-infra leg).
EXTRA_TABLES: dict[tuple[str, str], frozenset[str]] = {
    (SQLITE, "document"): frozenset(
        {
            "documents_fts",
            "documents_fts_config",
            "documents_fts_content",
            "documents_fts_data",
            "documents_fts_docsize",
            "documents_fts_idx",
        }
    ),
    (SQLITE, "graph"): frozenset({"entity_aliases"}),
    (POSTGRES, "graph"): frozenset({"entity_aliases"}),
}


def owned_tables(backend: str, store_type: str) -> frozenset[str] | None:
    """Every table the store is known to create, or ``None`` for an unknown store."""
    baseline = BASELINE_TABLES.get((backend, store_type))
    if baseline is None:
        return None
    return baseline | EXTRA_TABLES.get((backend, store_type), frozenset())


@dataclass(frozen=True)
class Migration:
    """One schema step for one store on one backend.

    ``apply`` receives the open DB-API connection (``sqlite3.Connection`` or
    ``psycopg.Connection``) inside the step's transaction. It must not commit,
    roll back or open its own connection: the runner commits the step and its
    version row together, or neither.

    What a step must not do, because it would break that:

    * **SQLite:** call ``executescript``. It issues a ``COMMIT`` first, which ends
      the runner's transaction and applies the step's earlier statements for good.
      Use ``execute`` one statement at a time.
    * **Postgres:** use ``CREATE INDEX CONCURRENTLY``, ``VACUUM`` or any other
      statement that cannot run inside a transaction block. Build the index
      without ``CONCURRENTLY`` (it takes a lock), or do that part by hand.
    * **Assume a column that ``_init_schema`` adds at open.** The baseline is
      judged by *table names* only (:data:`BASELINE_TABLES`), so a store created
      by an older build and never reopened since reads as the baseline while
      missing a column a newer build adds when it opens the store. A step that
      reads or alters such a column must add it first if absent.
    """

    store_type: str
    backend: str
    version: int
    description: str
    apply: Callable[[Any], None]


class MigrationSet:
    """The steps a build ships, validated as one chain per ``(backend, store)``."""

    def __init__(self, migrations: Iterable[Migration] = ()) -> None:
        chains: dict[tuple[str, str], list[Migration]] = {}
        for step in migrations:
            chains.setdefault((step.backend, step.store_type), []).append(step)
        self._chains: dict[tuple[str, str], list[Migration]] = {}
        for key, steps in chains.items():
            ordered = sorted(steps, key=lambda s: s.version)
            expected = list(
                range(BASELINE_VERSION + 1, BASELINE_VERSION + 1 + len(ordered))
            )
            if [s.version for s in ordered] != expected:
                msg = (
                    f"migrations for {key[1]!r} on {key[0]!r} must be versions"
                    f" {expected[0]}..{expected[-1]} with no gap or repeat; got"
                    f" {[s.version for s in ordered]}"
                )
                raise ConfigError(msg, setting="migrations")
            self._chains[key] = ordered

    def target(self, store_type: str, backend: str) -> int:
        """The version this set brings the store to (the baseline if no step)."""
        steps = self._chains.get((backend, store_type), [])
        return steps[-1].version if steps else BASELINE_VERSION

    def after(self, store_type: str, backend: str, current: int) -> list[Migration]:
        """The steps still to run for a store at ``current``, in order."""
        return [
            s
            for s in self._chains.get((backend, store_type), [])
            if s.version > current
        ]


#: What this build ships. Empty: the baseline is the only version.
SHIPPED_MIGRATIONS = MigrationSet()


def detect_version(store_type: str, backend: str, tables: frozenset[str]) -> int | None:
    """The version a store's *shape* says it is at, or ``None`` if it matches none."""
    owned = BASELINE_TABLES.get((backend, store_type))
    if owned is not None and owned <= tables:
        return BASELINE_VERSION
    return None


@dataclass
class StoreState:
    """One store's row in the report."""

    store: str
    status: str
    backend: str | None = None
    current_version: int | None = None
    recorded: bool = False
    target_version: int | None = None
    pending: list[int] = field(default_factory=list)
    applied: list[int] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class MigrateReport:
    """Everything one ``admin migrate`` run saw and did."""

    apply: bool
    run_at: datetime
    stores: list[StoreState] = field(default_factory=list)

    @property
    def status(self) -> str:
        """``ok`` | ``error``; the exit code reads this alone."""
        failed = any(s.status in _FAILURE_STATUSES for s in self.stores)
        return STATUS_ERROR if failed else STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": "apply" if self.apply else "report",
            "baseline_version": BASELINE_VERSION,
            "run_at": self.run_at.isoformat(),
            "stores": [s.as_dict() for s in self.stores],
        }


class _StepError(Exception):
    """A migration step raised; carries the version that did not apply."""

    def __init__(self, version: int, cause: BaseException) -> None:
        super().__init__(f"step to version {version} failed")
        self.version = version
        self.cause = cause


class _Db:
    """The two operations a runner needs from a database, per dialect."""

    def inspect(self, store: str) -> tuple[frozenset[str], int | None]:
        """Table names, and the highest version recorded for ``store``.

        Read-only: never creates the version table.
        """
        raise NotImplementedError

    def record(
        self,
        store: str,
        version: int,
        description: str,
        step: Migration | None,
        now: datetime,
    ) -> None:
        """Run ``step`` (if any) and record ``version`` in one transaction."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


_CREATE_VERSION_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
    "store TEXT NOT NULL, version INTEGER NOT NULL,"
    " description TEXT NOT NULL, applied_at TEXT NOT NULL,"
    " PRIMARY KEY (store, version))"
)


class _SqliteDb(_Db):
    def __init__(self, path: Path, *, writable: bool) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        if writable:
            self._conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        else:
            uri = f"{path.resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30.0)

    def inspect(self, store: str) -> tuple[frozenset[str], int | None]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = frozenset(r[0] for r in rows)
        if VERSION_TABLE not in tables:
            return tables - {VERSION_TABLE}, None
        (version,) = self._conn.execute(
            f"SELECT MAX(version) FROM {VERSION_TABLE} WHERE store = ?",  # noqa: S608
            (store,),
        ).fetchone()
        return tables - {VERSION_TABLE}, version

    def record(
        self,
        store: str,
        version: int,
        description: str,
        step: Migration | None,
        now: datetime,
    ) -> None:
        conn = self._conn
        assert conn is not None
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_CREATE_VERSION_TABLE)
            if step is not None:
                step.apply(conn)
            conn.execute(
                f"INSERT INTO {VERSION_TABLE} (store, version, description,"  # noqa: S608
                " applied_at) VALUES (?, ?, ?, ?)",
                (store, version, description, now.isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


#: The base tables the connection's ``search_path`` reaches, by name.
#: ``relkind`` ``r`` is an ordinary table and ``p`` a partitioned one, the two
#: kinds ``pg_tables`` lists.
_VISIBLE_BASE_TABLES_SQL = (
    "SELECT c.relname FROM pg_catalog.pg_class c"
    " WHERE c.relkind IN ('r', 'p')"
    " AND pg_catalog.pg_table_is_visible(c.oid)"
    " AND c.relnamespace NOT IN"
    " ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)"
)


class _PostgresDb(_Db):
    def __init__(self, dsn: str, *, writable: bool) -> None:
        try:
            import psycopg  # noqa: PLC0415
        except ImportError as exc:
            msg = "psycopg is not installed; install trellis-ai[cloud] for Postgres"
            raise ConfigError(msg, setting="postgres") from exc
        self._conn = psycopg.connect(dsn, connect_timeout=30)
        if not writable:
            # Every transaction this connection opens is ``BEGIN READ ONLY``, so
            # a report cannot write even by mistake; the server refuses it.
            self._conn.read_only = True

    def inspect(self, store: str) -> tuple[frozenset[str], int | None]:
        with self._conn.cursor() as cur:
            # ``pg_class`` lists the base tables (ordinary and partitioned) whatever
            # the role may do with them. ``information_schema.tables`` lists views
            # as well (a view called ``documents`` would read as the store) and
            # only what the role holds a privilege on (an under-granted role would
            # read ``absent`` for a store that is there).
            #
            # ``pg_table_is_visible`` is true for a table the ``search_path``
            # reaches and no earlier schema shadows, which is how the unqualified
            # names in the store's own SQL and in ``VERSION_TABLE`` below resolve.
            # ``pg_tables`` filtered on ``current_schema()`` saw only the first
            # existing schema on the path, so a store living in a later one read
            # ``absent``. ``pg_catalog`` is always on the path, so it is left out
            # here, and so is ``information_schema``.
            cur.execute(_VISIBLE_BASE_TABLES_SQL)
            tables = frozenset(r[0] for r in cur.fetchall())
            version = None
            if VERSION_TABLE in tables:
                cur.execute(
                    f"SELECT MAX(version) FROM {VERSION_TABLE} WHERE store = %s",  # noqa: S608
                    (store,),
                )
                version = cur.fetchone()[0]
        self._conn.rollback()
        return tables - {VERSION_TABLE}, version

    def record(
        self,
        store: str,
        version: int,
        description: str,
        step: Migration | None,
        now: datetime,
    ) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_CREATE_VERSION_TABLE)
            if step is not None:
                step.apply(self._conn)
            cur.execute(
                f"INSERT INTO {VERSION_TABLE} (store, version, description,"  # noqa: S608
                " applied_at) VALUES (%s, %s, %s, %s)",
                (store, version, description, now.isoformat()),
            )

    def close(self) -> None:
        self._conn.close()


def _open(
    registry: StoreRegistry, store_type: str, dialect: str, *, writable: bool
) -> _Db | None:
    """The database a configured store lives in, or ``None`` if it has no schema yet."""
    if dialect == SQLITE:
        path = registry.configured_sqlite_path(store_type)
        if path is None or not path_is_present(path):
            return None
        return _SqliteDb(path, writable=writable)
    dsn = registry.configured_postgres_dsn(store_type)
    if not dsn:
        msg = f"no DSN configured for the {store_type} store"
        raise ConfigError(msg, setting=store_type)
    return _PostgresDb(dsn, writable=writable)


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {sanitize_error_message(str(exc))}"


#: The dialect each configured backend name speaks. ``pgvector`` is the vector
#: store on Postgres.
_DIALECT_OF = {"postgres": POSTGRES, "pgvector": POSTGRES, "sqlite": SQLITE}


def _owns_any_table(store_type: str, dialect: str, tables: frozenset[str]) -> bool:
    """Does the database hold any table this store owns?

    Postgres stores share one database per plane (and a SQLite ``db_path`` can
    be shared), so "the database has no tables" is the wrong test for a store
    that has not been opened yet: its neighbours' tables are there. A store is
    absent when *none of its own* tables exist, and unrecognized when some do and
    its baseline tables are not all there. "Its own" is every table the store
    creates (:func:`owned_tables`), not only the baseline markers: a graph database
    holding ``entity_aliases`` alone is a leftover of the graph store.
    """
    owned = owned_tables(dialect, store_type)
    if owned is None:
        return bool(tables)
    return not owned.isdisjoint(tables)


def _read_state(
    db: _Db, state: StoreState, dialect: str, migrations: MigrationSet
) -> tuple[list[Migration], int | None] | None:
    """Fill ``state`` from the database; return ``(pending steps, recorded)``.

    ``None`` when the store has nothing to migrate (none of its own tables) or
    its shape is unknown; ``state`` then carries the status and reason.
    """
    store_type = state.store
    tables, recorded = db.inspect(store_type)
    state.target_version = migrations.target(store_type, dialect)
    state.recorded = recorded is not None
    if recorded is None:
        if not _owns_any_table(store_type, dialect, tables):
            state.status = ABSENT
            state.reason = (
                "none of this store's tables exist yet: a store creates its"
                " schema when first opened"
            )
            return None
        current = detect_version(store_type, dialect, tables)
        if current is None:
            state.status = UNRECOGNIZED
            state.reason = "tables match no known schema version; not stamped"
            return None
    else:
        current = recorded
    state.current_version = current
    steps = migrations.after(store_type, dialect, current)
    state.pending = [s.version for s in steps]
    return steps, recorded


def _apply_steps(
    db: _Db,
    state: StoreState,
    steps: list[Migration],
    recorded: int | None,
    now: datetime,
) -> None:
    """Record the baseline if unrecorded, then run ``steps`` one transaction each."""
    store_type = state.store
    if recorded is None:
        db.record(
            store_type, state.current_version or BASELINE_VERSION, "baseline", None, now
        )
    for step in steps:
        try:
            db.record(store_type, step.version, step.description, step, now)
        except Exception as exc:
            raise _StepError(step.version, exc) from exc
        state.applied.append(step.version)
        state.current_version = step.version
        state.pending.remove(step.version)
    state.recorded = True
    if state.applied:
        state.status = APPLIED
    else:
        state.status = STAMPED if recorded is None else CURRENT


def _migrate_store(
    registry: StoreRegistry,
    store_type: str,
    migrations: MigrationSet,
    *,
    apply: bool,
    now: datetime,
) -> StoreState:
    raw = registry.configured_backend(store_type)
    if raw is None:
        return StoreState(
            store_type,
            FAILED,
            reason="configured backend is not a registered one; fix the config",
        )
    dialect = _DIALECT_OF.get(raw)
    if dialect is None:
        return StoreState(
            store_type,
            UNSUPPORTED,
            backend=raw,
            reason="schema is not versioned for this backend by this command",
        )
    state = StoreState(store_type, CURRENT, backend=raw)
    db: _Db | None = None
    try:
        db = _open(registry, store_type, dialect, writable=apply)
        if db is None:
            state.status = ABSENT
            state.reason = "no file yet: a store creates its schema when first opened"
            return state
        planned = _read_state(db, state, dialect, migrations)
        if planned is None:
            return state
        steps, recorded = planned
        if apply:
            _apply_steps(db, state, steps, recorded, now)
        else:
            state.status = PENDING if steps else CURRENT
    except _StepError as err:
        logger.warning(
            "schema_migrate_step_failed", store=store_type, version=err.version
        )
        state.status = FAILED
        state.reason = f"step to version {err.version} failed: {_failure(err.cause)}"
    except Exception as exc:
        logger.warning(
            "schema_migrate_store_failed", store=store_type, error=type(exc).__name__
        )
        state.status = FAILED
        state.reason = _failure(exc)
    finally:
        if db is not None:
            db.close()
    return state


def run_migrate(
    registry: StoreRegistry,
    *,
    apply: bool,
    migrations: MigrationSet = SHIPPED_MIGRATIONS,
) -> MigrateReport:
    """Report each store's schema version, and with ``apply`` bring it to the target.

    A report (``apply=False``) opens every database read-only and writes
    nothing. ``apply=True`` records the baseline for a store that has no record
    and runs its pending steps in order, one transaction per step. A store that
    fails does not stop the others; each outcome is in the report.
    """
    report = MigrateReport(apply=apply, run_at=utc_now())
    for store_type in _PLANE_OF:
        report.stores.append(
            _migrate_store(
                registry, store_type, migrations, apply=apply, now=report.run_at
            )
        )
    logger.info("schema_migrate_completed", status=report.status, apply=apply)
    return report


__all__ = [
    "BASELINE_TABLES",
    "BASELINE_VERSION",
    "SHIPPED_MIGRATIONS",
    "VERSION_TABLE",
    "MigrateReport",
    "Migration",
    "MigrationSet",
    "StoreState",
    "detect_version",
    "run_migrate",
]
