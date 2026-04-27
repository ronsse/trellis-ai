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

    def _run_read_list(self, cypher: str, **params: Any) -> Any:
        """Run a read transaction and return all rows as a list.

        Returns ``Any`` rather than ``list[Any]`` because the ``neo4j``
        package is an optional extra — without it installed, mypy types
        ``Driver`` and ``Session`` as ``Any`` and ``warn_return_any``
        flags ``list[Any]`` annotations on values that flow through
        them. Callers iterate the result, so the precise type is moot.
        """
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


# Default poll cadence for :func:`wait_for_vector_index_online`. 0.5s is
# fast enough that a healthy ONLINE transition (typically <2s on AuraDB)
# stays bounded, slow enough that the 30s default ceiling doesn't burn
# 60 round-trips on a server that legitimately needs the time.
_VECTOR_INDEX_POLL_INTERVAL = 0.5
_VECTOR_INDEX_DEFAULT_TIMEOUT = 30.0


class VectorIndexNotOnlineError(RuntimeError):
    """Raised when a vector index never reached ``ONLINE`` state in time.

    Carries the last observed state + (when available) the population
    percentage so an operator can distinguish "still populating, just
    slow" from "stuck in FAILED" from "never appeared". ``index_name``
    echoes the index that timed out.
    """

    def __init__(
        self,
        index_name: str,
        state: str | None,
        population_percent: float | None,
        timeout: float,
    ) -> None:
        self.index_name = index_name
        self.state = state
        self.population_percent = population_percent
        self.timeout = timeout
        bits = [f"vector index {index_name!r} did not reach ONLINE in {timeout}s"]
        if state is not None:
            bits.append(f"last state: {state}")
        if population_percent is not None:
            bits.append(f"populationPercent: {population_percent:.1f}")
        super().__init__("; ".join(bits))


def wait_for_vector_index_online(
    driver: Driver,
    *,
    database: str,
    index_name: str,
    timeout: float = _VECTOR_INDEX_DEFAULT_TIMEOUT,
    poll_interval: float = _VECTOR_INDEX_POLL_INTERVAL,
) -> None:
    """Block until ``index_name`` reaches the ``ONLINE`` state.

    AuraDB (and self-hosted Neo4j on slow disks) provisions vector
    indexes asynchronously: ``CREATE VECTOR INDEX`` returns immediately,
    but the index isn't queryable until its background population
    completes. The first ``db.index.vector.queryNodes`` call against an
    unfinished index fails with "no such vector schema index" — the
    same race the A.1 e2e suite already worked around by reusing a
    persistent index.

    Call this after ``CREATE VECTOR INDEX`` to surface the transition
    cleanly. Raises :class:`VectorIndexNotOnlineError` on timeout, with
    the last observed state attached.

    Treats ``FAILED`` as a fast-fail — the index won't recover on its
    own, so we raise immediately rather than burning the full timeout.
    """
    import time  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    last_state: str | None = None
    last_pct: float | None = None

    cypher = (
        "SHOW VECTOR INDEXES YIELD name, state, populationPercent "
        "WHERE name = $index_name "
        "RETURN state, populationPercent"
    )
    while time.monotonic() < deadline:
        with driver.session(database=database) as session:
            record = session.run(cypher, index_name=index_name).single()
        if record is not None:
            last_state = record["state"]
            last_pct = record.get("populationPercent")
            if last_state == "ONLINE":
                return
            if last_state == "FAILED":
                raise VectorIndexNotOnlineError(
                    index_name, last_state, last_pct, timeout
                )
        # ``record is None`` means the index isn't visible yet (very
        # early after CREATE on AuraDB). Treat as POPULATING and keep
        # polling.
        time.sleep(poll_interval)

    raise VectorIndexNotOnlineError(index_name, last_state, last_pct, timeout)
