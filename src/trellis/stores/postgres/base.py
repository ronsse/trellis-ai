"""Base class for Postgres store implementations."""

from __future__ import annotations

import os
from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    import psycopg


def _env_int(name: str, default: int) -> int:
    """Read a positive int from ``os.environ`` or return the default.

    Garbage / non-numeric / non-positive values fall back so a malformed
    env var can never produce a pool with min_size > max_size or zero
    capacity.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class PostgresStoreBase:
    """Common init pattern for all Postgres-backed stores.

    Each store owns a :class:`psycopg_pool.ConnectionPool` so concurrent
    request handlers (FastAPI thread-pool workers, multiple agent
    sessions) don't serialise through a single TCP connection. Pool
    sizing reads ``TRELLIS_PG_POOL_MIN_SIZE`` /
    ``TRELLIS_PG_POOL_MAX_SIZE`` from env so deployments can tune
    without code changes; the defaults (2 / 20) are sized for a single
    uvicorn worker handling moderate agent traffic on AuraDB-Free-class
    Postgres.

    Callers acquire a connection via the :meth:`_conn` context manager
    rather than reaching into ``self._pool`` directly — the helper
    handles commit-on-success and rollback-on-exception so the dozens
    of call sites in subclasses don't have to.
    """

    DEFAULT_POOL_MIN_SIZE: int = 2
    DEFAULT_POOL_MAX_SIZE: int = 20

    def __init__(
        self,
        dsn: str,
        *,
        pool: ConnectionPool | None = None,
        on_connect: Callable[[psycopg.Connection], None] | None = None,
    ) -> None:
        """Initialize the store.

        ``on_connect`` runs against each newly-opened pooled connection
        (used by ``PgVectorStore`` to register the ``pgvector`` type
        adapter on every connection — the per-connection setup that the
        single-conn era did once on ``__init__``). Ignored when ``pool``
        is supplied; the caller owns connection setup in that case.
        """
        self._dsn = dsn
        self._logger = structlog.get_logger(type(self).__name__)
        self._owns_pool = pool is None
        if pool is None:
            min_size = _env_int("TRELLIS_PG_POOL_MIN_SIZE", self.DEFAULT_POOL_MIN_SIZE)
            max_size = _env_int("TRELLIS_PG_POOL_MAX_SIZE", self.DEFAULT_POOL_MAX_SIZE)
            min_size = min(min_size, max_size)
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=min_size,
                max_size=max_size,
                kwargs={"autocommit": False},
                configure=on_connect,
                open=True,
            )
            pool.wait()
            self._logger.info("pg_pool_opened", min_size=min_size, max_size=max_size)
        self._pool = pool
        self._init_schema()
        self._logger.info("store_initialized")

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        """Yield a pooled connection; commit on success, rollback on error.

        Subclasses use this everywhere they previously reached for
        ``self.conn``::

            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(SQL, params)
                # commit is automatic on with-block exit

        Multi-statement transactions wrap the whole block in one
        ``_conn()`` so all statements run on the same connection::

            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(SQL_A)
                with conn.cursor() as cur:
                    cur.execute(SQL_B)
                # both committed atomically on exit
        """
        with self._pool.connection() as conn:
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            conn.commit()

    @abstractmethod
    def _init_schema(self) -> None:
        """Create tables and indexes. Implemented by each store."""

    def close(self) -> None:
        """Close the connection pool (if owned)."""
        if self._owns_pool:
            self._pool.close()
        self._logger.info("store_closed")
