"""A stand-in for ``neo4j.Driver`` that scripts each purge transaction.

``BoltOpenCypherGraphStore(driver=FakeBoltDriver(...), database="neo4j",
owns_driver=False, init_schema=False)`` builds a store that never opens a
socket. Each transaction ``delete_node`` runs takes the next scripted outcome
and plays it at its ``DETACH DELETE`` of the ``Node`` rows. An outcome is the
number of rows that statement removes, an exception it raises, or
:class:`AtCommit`, whose error the commit raises after the statements ran.
The last outcome repeats for every later transaction, so
``FakeBoltDriver(error)`` is a database where every purge raises ``error``.

``execute_write`` runs the transaction function as
``neo4j.Session.execute_write`` does: a ``DriverError`` or ``Neo4jError``
whose ``is_retryable()`` is true runs it again in a new transaction, and when
no attempt is left the last such error is raised. Any other exception, and
an error the driver does not retry, ends the call at once. The driver bounds
the retries by ``max_transaction_retry_time``; the fake by ``attempts``.

Every transaction is kept on :attr:`FakeBoltDriver.transactions` with how it
ended.

``rows_left`` scripts the read ``delete_node`` makes after a commit whose
outcome the driver cannot know: the number of ``Node`` rows of the node that
read finds, or an exception it raises. The parameters of every such read are
kept on :attr:`FakeBoltDriver.reads`, and a read the test did not script
fails it. The keyword arguments of every ``session()`` are kept on
:attr:`FakeBoltDriver.sessions`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from neo4j.exceptions import DriverError, Neo4jError


@dataclass(frozen=True)
class AtCommit:
    """The purge removes ``rows`` ``Node`` rows, then its commit raises ``error``."""

    error: Exception
    rows: int = 1


Outcome = Exception | int | AtCommit


class FakeBoltDriver:
    """Open one :class:`FakeSession` per ``session()``."""

    def __init__(
        self,
        *outcomes: Outcome,
        attempts: int = 3,
        rows_left: int | Exception | None = None,
    ) -> None:
        if not outcomes:
            msg = "script at least one purge outcome"
            raise ValueError(msg)
        self._outcomes = list(outcomes)
        self.attempts = attempts
        self.rows_left = rows_left
        self.transactions: list[FakeTransaction] = []
        self.reads: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []

    @contextmanager
    def session(self, **config: Any) -> Iterator[FakeSession]:
        self.sessions.append(config)
        yield FakeSession(self)

    def next_outcome(self) -> Outcome:
        if len(self._outcomes) > 1:
            return self._outcomes.pop(0)
        return self._outcomes[0]


class FakeSession:
    """Run a transaction function with the driver's retry rule."""

    def __init__(self, driver: FakeBoltDriver) -> None:
        self._driver = driver

    def execute_write(
        self, transaction_function: Callable[[FakeTransaction], Any]
    ) -> Any:
        errors: list[DriverError | Neo4jError] = []
        for _ in range(self._driver.attempts):
            tx = FakeTransaction(self._driver.next_outcome())
            self._driver.transactions.append(tx)
            try:
                try:
                    result = transaction_function(tx)
                except Exception:
                    tx.ended = "rollback"
                    raise
                tx.commit()
            except (DriverError, Neo4jError) as error:
                if not error.is_retryable():
                    raise
                errors.append(error)
            else:
                return result
        raise errors[-1]

    def run(self, cypher: str, **params: Any) -> FakeResult:
        """Answer an auto-commit query with the scripted ``rows_left``."""
        self._driver.reads.append(params)
        rows_left = self._driver.rows_left
        if rows_left is None:
            msg = f"no read is scripted: {cypher}"
            raise AssertionError(msg)
        if isinstance(rows_left, Exception):
            raise rows_left
        return FakeResult({"remaining": rows_left})


class FakeTransaction:
    """One transaction, and how it ended."""

    def __init__(self, outcome: Outcome) -> None:
        self._outcome = outcome
        if isinstance(outcome, AtCommit):
            self._node_rows = outcome.rows
        elif isinstance(outcome, int):
            self._node_rows = outcome
        else:
            self._node_rows = 1
        self.ended: str | None = None

    def run(self, cypher: str, **_params: Any) -> FakeResult:
        if not cypher.startswith("MATCH (n:Node") or "DETACH DELETE" not in cypher:
            return FakeResult({"deleted": 0})
        if isinstance(self._outcome, Exception):
            raise self._outcome
        removed, self._node_rows = self._node_rows, 0
        return FakeResult({"deleted": removed})

    def commit(self) -> None:
        if isinstance(self._outcome, AtCommit):
            self.ended = "commit raised"
            raise self._outcome.error
        self.ended = "commit"


class FakeResult:
    """A statement's result: ``single()`` is its one row."""

    def __init__(self, row: dict[str, int]) -> None:
        self._row = row

    def single(self, strict: bool = False) -> dict[str, int]:
        return self._row

    def consume(self) -> None:
        return None
