"""Read and write a SQLite store's database from connections of its own.

Shared by the SQLite store suites that pin a failed write's rollback. A
write that fails after taking the database's write lock (a duplicate
primary key, say) must not leave its transaction open: an open transaction
keeps the lock, and every other connection's write waits out its timeout
and then fails with ``database is locked``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def committed_rows(db_path: Path, sql: str) -> list[tuple[Any, ...]]:
    """Return the rows *sql* reads at a fresh connection.

    A fresh connection sees only committed writes, so a row the store's own
    connection still holds in an open transaction is absent.
    """
    conn = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def write_at_once(db_path: Path, sql: str, params: Sequence[Any]) -> None:
    """Run the write *sql* at a second connection that never waits for a lock.

    With a zero timeout, ``BEGIN IMMEDIATE`` takes the database's write lock
    at once or raises ``sqlite3.OperationalError: database is locked``.
    """
    conn = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, params)
        conn.execute("COMMIT")
    finally:
        conn.close()
