"""Shared helpers for Neo4j-backed stores.

Three concerns live here: detecting whether the optional ``neo4j``
driver is installed, building a ``Driver`` instance, and a small
mixin that wraps the ``session.execute_{read,write}(lambda tx: ...)``
ceremony that otherwise repeats at every call site. Each store owns
its own driver — the official driver pools connections internally so
there's no benefit to a module-level cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver

try:
    from neo4j import GraphDatabase

    HAS_NEO4J = True
except ImportError:
    HAS_NEO4J = False

_MISSING_MSG = (
    "neo4j driver is required for Neo4jGraphStore / Neo4jVectorStore. "
    "Install it with: pip install 'trellis-ai[neo4j]'"
)


def check_driver_installed() -> None:
    """Raise ``ImportError`` with install hint if ``neo4j`` is unavailable."""
    if not HAS_NEO4J:
        raise ImportError(_MISSING_MSG)


def build_driver(uri: str, user: str, password: str) -> Driver:
    """Construct a Neo4j ``Driver``. Caller owns its lifecycle."""
    return GraphDatabase.driver(uri, auth=(user, password))


class Neo4jSessionRunner:
    """Mixin: thin wrappers over ``session.execute_{read,write}``.

    Subclasses must expose ``self._driver`` (a Neo4j ``Driver``) and
    ``self._database`` (target database name). Each helper opens its
    own session — appropriate for one-shot operations, but bulk paths
    that issue many round trips back-to-back should manage a single
    session themselves to avoid per-call connection-acquisition cost.
    """

    _driver: Driver
    _database: str

    def _run_read_single(self, cypher: str, **params: Any) -> Any:
        """Run a read transaction and return the single row (or ``None``)."""
        with self._driver.session(database=self._database) as session:
            return session.execute_read(lambda tx: tx.run(cypher, **params).single())

    def _run_read_list(self, cypher: str, **params: Any) -> list[Any]:
        """Run a read transaction and return all rows as a list."""
        with self._driver.session(database=self._database) as session:
            return session.execute_read(lambda tx: list(tx.run(cypher, **params)))

    def _run_write(self, cypher: str, **params: Any) -> None:
        """Run a write transaction, discarding any result rows."""
        with self._driver.session(database=self._database) as session:
            session.execute_write(lambda tx: tx.run(cypher, **params).consume())

    def _run_write_single(self, cypher: str, **params: Any) -> Any:
        """Run a write transaction and return the single row (or ``None``)."""
        with self._driver.session(database=self._database) as session:
            return session.execute_write(lambda tx: tx.run(cypher, **params).single())
