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
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from neo4j.exceptions import DriverError, Neo4jError


@dataclass(frozen=True)
class AtCommit:
    """The purge removes one ``Node`` row, then its commit raises ``error``."""

    error: Exception


Outcome = Exception | int | AtCommit


class FakeBoltDriver:
    """Open one :class:`FakeSession` per ``session()``."""

    def __init__(self, *outcomes: Outcome, attempts: int = 3) -> None:
        if not outcomes:
            msg = "script at least one purge outcome"
            raise ValueError(msg)
        self._outcomes = list(outcomes)
        self.attempts = attempts
        self.transactions: list[FakeTransaction] = []

    @contextmanager
    def session(self, **_config: Any) -> Iterator[FakeSession]:
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


class FakeTransaction:
    """One transaction, and how it ended."""

    def __init__(self, outcome: Outcome) -> None:
        self._outcome = outcome
        self._node_rows = outcome if isinstance(outcome, int) else 1
        self.ended: str | None = None

    def run(self, cypher: str, **_params: Any) -> FakeResult:
        if not cypher.startswith("MATCH (n:Node") or "DETACH DELETE" not in cypher:
            return FakeResult(0)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        removed, self._node_rows = self._node_rows, 0
        return FakeResult(removed)

    def commit(self) -> None:
        if isinstance(self._outcome, AtCommit):
            self.ended = "commit raised"
            raise self._outcome.error
        self.ended = "commit"


class FakeResult:
    """A statement's result: ``single()`` carries the rows it removed."""

    def __init__(self, deleted: int) -> None:
        self._deleted = deleted

    def single(self) -> dict[str, int]:
        return {"deleted": self._deleted}

    def consume(self) -> None:
        return None
