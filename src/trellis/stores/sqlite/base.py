"""Base class for SQLite store implementations."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from abc import abstractmethod
from pathlib import Path

import structlog

_WAL_RETRY_ATTEMPTS = 10
_WAL_RETRY_SLEEP_S = 0.05


def _ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """Best-effort transition of ``conn``'s database file to WAL mode.

    journal_mode is a per-file setting persisted in the file header,
    so the first connection that opens a fresh database does the
    transition and every subsequent opener sees ``mode=wal`` and
    returns instantly.

    The rollback→WAL transition needs an exclusive lock and can fail
    with ``database is locked`` if another connection is mid-
    transaction. ``busy_timeout`` does not cover this case in all
    SQLite builds. We:

    1. Check the current mode — if already ``wal``, return.
    2. Try to switch; on ``OperationalError`` re-check the mode in
       case a concurrent opener won the race for us.
    3. If still not WAL and retries remain, sleep briefly and loop.

    A persistent failure to enter WAL is logged but does not raise:
    the database remains usable in rollback-journal mode (which is
    what existed before this change), and ``busy_timeout`` still
    serializes writers safely. Concurrent-Connection commit-race
    safety comes from Part B (per-thread Connections), not WAL.
    """

    def _current_mode() -> str:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        if row is None:
            return ""
        # row_factory=sqlite3.Row may not be set yet on the calling
        # connection — handle both tuple and Row shapes.
        try:
            value = row[0]
        # GUARD: row may be a tuple or sqlite3.Row depending on whether
        # row_factory was set before this helper ran — accept either
        # shape (see comment above).
        except (IndexError, KeyError):
            return ""
        return str(value).lower()

    if _current_mode() == "wal":
        return

    for _ in range(_WAL_RETRY_ATTEMPTS):
        # Another concurrent opener may have just done the transition;
        # swallow the OperationalError and re-check the mode below.
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA journal_mode=WAL")
        if _current_mode() == "wal":
            return
        time.sleep(_WAL_RETRY_SLEEP_S)

    structlog.get_logger(__name__).warning(
        "sqlite_wal_mode_unavailable",
        message=(
            "Could not switch journal_mode to WAL after retries; "
            "falling back to rollback-journal mode."
        ),
    )


class _ConnectionRegistry:
    """Tracks every per-thread ``sqlite3.Connection`` a store has opened.

    ``threading.local`` only exposes data for the calling thread, so a
    naive ``close()`` from the main thread cannot reach connections
    opened by worker threads. We keep an explicit list (guarded by a
    lock) so :meth:`SQLiteStoreBase.close` can drain everything on
    teardown — important under FastAPI + ``StoreRegistry.close()``
    where the request thread that opened a connection has already
    exited by the time shutdown runs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []

    def add(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._connections.append(conn)

    def drain(self) -> list[sqlite3.Connection]:
        with self._lock:
            conns = list(self._connections)
            self._connections.clear()
            return conns


class SQLiteStoreBase:
    """Common init pattern for all SQLite-backed stores.

    Connection model: each thread that touches the store gets its own
    ``sqlite3.Connection`` via :meth:`_get_conn`. Sharing a single
    Connection across threads (the previous design) was sufficient at
    the SQLite-file level — WAL + ``busy_timeout`` serializes writers
    safely — but caused a Python-level race in ``sqlite3.Connection``'s
    transaction state. Two FastAPI worker threads could both observe
    "in transaction = True", both call ``commit()``, and the second one
    raised ``sqlite3.ProgrammingError: cannot commit - no transaction
    is active`` on Windows. Per-thread Connections eliminate that race.

    WAL mode (``journal_mode=WAL``) and ``synchronous=NORMAL`` allow
    concurrent readers + one writer at the DB-file level; ``busy_timeout``
    (10s in ms, superset of the ``timeout=`` constructor arg) queues
    blocked writers instead of failing immediately. WAL is skipped on
    ``:memory:`` databases where it is a no-op or warns.

    Subclasses inherit ``SCHEMA_VERSION = "1"``; a subclass that ships
    a schema-changing migration must override the constant on itself
    (NOT mutate the base) so :class:`StoreRegistry` can fail-fast on
    a stale on-disk shape via the fingerprint check (Logic Gap 4.5).

    Subclasses must access the connection via ``self._get_conn()``;
    direct access to the legacy ``self._conn`` attribute is no longer
    supported.
    """

    SCHEMA_VERSION: str = "1"

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._registry = _ConnectionRegistry()
        self._logger = structlog.get_logger(type(self).__name__)
        # Eagerly open the main-thread connection so a bad path fails
        # fast at construction time and the schema-init runs once on a
        # known thread.
        self._get_conn()
        self._init_schema()
        self._logger.info("store_initialized", db_path=str(self._db_path))

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the calling thread's ``sqlite3.Connection``.

        Lazily opens a new connection on first access from each thread
        and applies the WAL + busy_timeout pragmas. Subsequent calls
        from the same thread return the cached Connection.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(
            str(self._db_path),
            # Kept for belt-and-suspenders compat: a few async-shim code
            # paths surface a Connection to a hand-off thread. With per-
            # thread Connections this is no longer required for the
            # common case, but flipping it to True is a behaviour change
            # that's out of scope for this fix.
            check_same_thread=False,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row

        # busy_timeout MUST be set before any other PRAGMA that may
        # contend for the database lock. With busy_timeout in place,
        # ordinary read/write contention waits up to 10s for the lock
        # instead of raising ``database is locked``.
        conn.execute("PRAGMA busy_timeout=10000")

        # WAL is a no-op (or a warning) on in-memory databases — no
        # journal file exists. journal_mode is sticky on disk: once the
        # database file is in WAL mode, every connection that opens it
        # sees mode=wal and the SET below is a fast no-op.
        #
        # The first connection that converts a rollback-journal file
        # into WAL needs an exclusive lock, and that conversion CAN
        # fail with ``database is locked`` if another connection is
        # mid-transaction — ``busy_timeout`` does not cover the
        # rollback-journal exclusive-lock contention because SQLite
        # treats it as a fatal lock conflict, not a busy wait. We
        # check the current mode first and only attempt the SET if
        # the file is not yet in WAL; if the SET fails because
        # another concurrent opener won the race, that's fine —
        # subsequent reads will see ``mode=wal`` from the winner.
        if str(self._db_path) != ":memory:":
            _ensure_wal_mode(conn)
            conn.execute("PRAGMA synchronous=NORMAL")

        self._local.conn = conn
        self._registry.add(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        """Backwards-compat shim — forwards to :meth:`_get_conn`.

        Subclasses still reference ``self._conn`` throughout. Keeping
        this as a property avoids a 120-site rewrite and centralizes
        the lookup in :meth:`_get_conn`. Treat it as read-only — never
        rebind ``self._conn = ...``.
        """
        return self._get_conn()

    @abstractmethod
    def _init_schema(self) -> None:
        """Create tables and indexes. Implemented by each store."""

    def close(self) -> None:
        """Close all open per-thread connections.

        Drains the connection registry; safe to call from any thread
        even if worker threads that opened their own Connections have
        already exited. Idempotent — second call finds an empty
        registry and no-ops.
        """
        connections = self._registry.drain()
        for conn in connections:
            try:
                conn.close()
            # GRACEFUL-DEGRADATION: a single misbehaving connection
            # should not block the rest of the close pass; matches
            # StoreRegistry.close()'s log-and-continue contract.
            except Exception as exc:
                self._logger.warning(
                    "store_close_connection_failed",
                    db_path=str(self._db_path),
                    error=str(exc),
                )
        # Drop the calling thread's local reference so a subsequent
        # access in the same thread reopens cleanly.
        if hasattr(self._local, "conn"):
            del self._local.conn
        self._logger.info(
            "store_closed",
            db_path=str(self._db_path),
            connections_closed=len(connections),
        )
