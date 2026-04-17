"""Base class for Postgres store implementations."""

from __future__ import annotations

from abc import abstractmethod

import psycopg
import structlog


class PostgresStoreBase:
    """Common init pattern for all Postgres-backed stores.

    Handles: psycopg connection, schema init, logging, close, and reconnection.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=False)
        self._logger = structlog.get_logger(type(self).__name__)
        self._init_schema()
        self._logger.info("store_initialized")

    @property
    def conn(self) -> psycopg.Connection:
        """Return the connection, reconnecting if it was closed."""
        if self._conn.closed:
            self._logger.warning("reconnecting_closed_connection")
            self._conn = psycopg.connect(self._dsn, autocommit=False)
        return self._conn

    @abstractmethod
    def _init_schema(self) -> None:
        """Create tables and indexes. Implemented by each store."""

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
        self._logger.info("store_closed")
