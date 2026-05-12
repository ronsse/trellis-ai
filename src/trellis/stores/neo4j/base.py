"""Neo4j-specific helpers — built on the shared Bolt base.

The driver, session, and connection-verification machinery is shared
across all Bolt-speaking backends and lives in
:mod:`trellis.stores.bolt_opencypher.base`. This module:

1. Re-exports the shared types under their historical Neo4j-prefixed
   names (``DriverConfig`` = ``BoltDriverConfig``,
   ``Neo4jSessionRunner`` = ``BoltSessionRunner``) for backward
   compatibility with existing imports from
   ``trellis.stores.neo4j.base``.
2. Owns :func:`build_driver` — Neo4j-specific because it uses basic
   authentication. ArcadeDB does the same; managed-Neptune would wrap
   it with SigV4.
3. Owns :func:`wait_for_vector_index_online` and
   :class:`VectorIndexNotOnlineError` — Neo4j vector-index specific
   (the SHOW VECTOR INDEXES surface is Neo4j-only today).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.stores.bolt_opencypher.base import (
    HAS_NEO4J,
    BoltDriverConfig,
    BoltSessionRunner,
    check_driver_installed,
    verify_connectivity,
)

# Backward-compatible aliases. Existing code imports ``DriverConfig``
# and ``Neo4jSessionRunner`` from ``trellis.stores.neo4j.base``; keep
# those names resolvable.
DriverConfig = BoltDriverConfig
Neo4jSessionRunner = BoltSessionRunner

__all__ = [
    "HAS_NEO4J",
    "DriverConfig",
    "Neo4jSessionRunner",
    "VectorIndexNotOnlineError",
    "build_driver",
    "check_driver_installed",
    "verify_connectivity",
    "wait_for_vector_index_online",
]

if TYPE_CHECKING:
    from neo4j import Driver

try:
    from neo4j import GraphDatabase

    _HAS_NEO4J_LOCAL = True
except ImportError:
    _HAS_NEO4J_LOCAL = False


def build_driver(
    uri: str,
    user: str,
    password: str,
    *,
    config: DriverConfig | None = None,
) -> Driver:
    """Construct a Neo4j ``Driver`` with basic auth + the given config.

    Caller owns the returned driver's lifecycle — call ``driver.close()``
    when done. Use ``StoreRegistry`` to share one driver across stores
    pointing at the same instance; otherwise each store constructs its
    own pool.
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
    completes. The first ``SEARCH ... IN VECTOR INDEX`` (or the legacy
    ``db.index.vector.queryNodes``) call against an unfinished index
    fails with "no such vector schema index" — the same race the A.1
    e2e suite already worked around by reusing a persistent index.

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
