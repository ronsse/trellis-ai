"""Shared helpers for Neo4j-backed stores.

Four concerns live here:

1. Detecting whether the optional ``neo4j`` driver is installed.
2. Holding the driver-construction kwargs that production deployments
   need (timeouts, pool sizing, keep-alive) in one place — :class:`DriverConfig`.
3. Building a ``Driver`` from a URI / user / password + a config — :func:`build_driver`.
4. The :class:`Neo4jSessionRunner` mixin that wraps the
   ``session.execute_{read,write}(lambda tx: ...)`` ceremony that
   otherwise repeats at every call site.

Driver sharing across stores (the ``Neo4jGraphStore`` + ``Neo4jVectorStore``
pair pointing at the same instance) is handled by :class:`StoreRegistry`,
which constructs a single driver per ``(uri, user)`` and injects it into
both stores. Stores own a driver when they build their own; they do *not*
own one that was injected. ``close()`` respects that distinction so the
registry's eventual shutdown sweep doesn't race with a store's individual
``close()`` call.
"""

from __future__ import annotations

from dataclasses import dataclass
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


# Sane production defaults. These match what the Neo4j Python driver
# documentation recommends for a long-lived service: a 30-second ceiling
# on the initial connect handshake, a generous-but-bounded pool so a
# misbehaving caller can't open unlimited sockets, retry that gives up
# faster than the default 30s minute so a stuck transaction surfaces as
# a clear error, and TCP keep-alive so AuraDB / load balancers don't
# silently kill idle connections.
_DEFAULT_USER_AGENT = "trellis-ai"


@dataclass(frozen=True)
class DriverConfig:
    """Driver construction kwargs with production-safe defaults.

    Pass an instance to :func:`build_driver` (or via the
    ``driver_config`` constructor kwarg on ``Neo4jGraphStore`` /
    ``Neo4jVectorStore``) to override individual fields. Frozen so
    instances are safe to share across stores.

    Attributes
    ----------
    connection_timeout
        Seconds the driver waits to establish a Bolt connection before
        raising. Default 30s — anything longer typically means DNS or
        TLS misconfiguration; surface fast. Today (without this kwarg)
        a misconfigured network hangs the call indefinitely.
    max_connection_pool_size
        Maximum concurrent Bolt connections per driver. Default 100 —
        matches AuraDB Pro's default per-instance limit and is
        comfortably above any single Trellis caller's needs.
    max_transaction_retry_time
        Seconds the driver retries a transient transaction failure
        (e.g. ``TransientError`` from concurrent writes) before
        re-raising. Default 30s.
    keep_alive
        Whether the driver enables TCP keep-alive on its connections.
        Default True — required for AuraDB and most cloud LBs that
        silently drop idle TCP sessions after a few minutes.
    user_agent
        Identifies Trellis in Neo4j's session monitoring / audit logs.
        Defaults to ``"trellis-ai"``.
    """

    connection_timeout: float = 30.0
    max_connection_pool_size: int = 100
    max_transaction_retry_time: float = 30.0
    keep_alive: bool = True
    user_agent: str = _DEFAULT_USER_AGENT


def build_driver(
    uri: str,
    user: str,
    password: str,
    *,
    config: DriverConfig | None = None,
) -> Driver:
    """Construct a Neo4j ``Driver`` with the given config (or defaults).

    Caller owns the returned driver's lifecycle — call ``driver.close()``
    when done. Use ``StoreRegistry`` to share one driver across stores
    pointing at the same instance; otherwise each ``Neo4jGraphStore`` /
    ``Neo4jVectorStore`` constructs its own pool.
    """
    cfg = config or DriverConfig()
    return GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=cfg.connection_timeout,
        max_connection_pool_size=cfg.max_connection_pool_size,
        max_transaction_retry_time=cfg.max_transaction_retry_time,
        keep_alive=cfg.keep_alive,
        user_agent=cfg.user_agent,
    )


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


def verify_connectivity(driver: Driver) -> None:
    """Perform a Bolt round-trip to confirm the driver can reach the server.

    Wraps ``Driver.verify_connectivity()`` — the official driver builtin
    that probes the server with a ``RESET`` and raises if the
    connection can't be established. Used by
    :meth:`StoreRegistry.validate` when ``check_connectivity=True`` so
    operators see "Neo4j unreachable" at startup rather than as an
    opaque Bolt error on the first request.

    Raises whatever the driver raises (``ServiceUnavailable``,
    ``AuthError``, etc.) — caller is expected to wrap the failure into
    a higher-level aggregate (typically
    :class:`RegistryValidationError`).
    """
    driver.verify_connectivity()


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
