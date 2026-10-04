"""A stand-in for ``psycopg_pool.ConnectionPool`` that scripts each purge.

``PostgresGraphStore("postgresql://unused", pool=FakePool(...))`` builds a
store that never opens a socket: the schema DDL runs against nothing, and
each ``delete_node`` transaction takes the next scripted outcome at its
first ``DELETE FROM nodes``. An outcome is an exception that statement
raises, such as ``psycopg.errors.DeadlockDetected``, or the number of node
rows it removes. The last outcome repeats for every later purge, so
``FakePool(deadlock)`` is a database where every purge deadlocks.

Every connection the store takes is kept on :attr:`FakePool.transactions`
with the statements it ran and how it ended, so a test can count the purge
attempts and see which were rolled back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class FakePool:
    """Hand the store one scripted transaction per ``connection()``."""

    def __init__(self, *outcomes: BaseException | int) -> None:
        if not outcomes:
            msg = "script at least one purge outcome"
            raise ValueError(msg)
        self._outcomes = list(outcomes)
        self.transactions: list[FakeConnection] = []

    @contextmanager
    def connection(self) -> Iterator[FakeConnection]:
        conn = FakeConnection(self)
        self.transactions.append(conn)
        yield conn

    def next_outcome(self) -> BaseException | int:
        if len(self._outcomes) > 1:
            return self._outcomes.pop(0)
        return self._outcomes[0]

    def purges(self) -> list[FakeConnection]:
        """The transactions that reached a ``DELETE FROM nodes``."""
        return [t for t in self.transactions if t.reached_nodes_delete]


class FakeConnection:
    """One transaction: the statements it ran and how it ended."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool
        self._nodes_left: int | None = None
        self.statements: list[str] = []
        self.ended: str | None = None

    @property
    def reached_nodes_delete(self) -> bool:
        return self._nodes_left is not None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.ended = "commit"

    def rollback(self) -> None:
        self.ended = "rollback"

    def run(self, sql: str) -> int:
        self.statements.append(sql)
        if not sql.startswith("DELETE FROM nodes"):
            return 0
        if self._nodes_left is None:
            self._nodes_left = 0
            outcome = self._pool.next_outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            self._nodes_left = outcome
        removed, self._nodes_left = self._nodes_left, 0
        return removed


class FakeCursor:
    """Report each statement's rowcount the way a psycopg cursor does."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.rowcount = -1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: Any, params: Any = None) -> None:
        self.rowcount = self._conn.run(str(sql))
