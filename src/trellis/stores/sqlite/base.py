"""Base class for SQLite store implementations."""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from pathlib import Path

import structlog


class SQLiteStoreBase:
    """Common init pattern for all SQLite-backed stores.

    Handles: Path resolution, parent directory creation, connection
    with ``check_same_thread=False``, ``row_factory``, and schema init.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._logger = structlog.get_logger(type(self).__name__)
        self._init_schema()
        self._logger.info("store_initialized", db_path=str(self._db_path))

    @abstractmethod
    def _init_schema(self) -> None:
        """Create tables and indexes. Implemented by each store."""

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
        self._logger.info("store_closed", db_path=str(self._db_path))
