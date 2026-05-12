"""Shared driver + session plumbing for openCypher-over-Bolt backends.

Three concerns live here:

1. Detecting whether the optional ``neo4j`` Python driver is installed —
   the driver is the Bolt protocol implementation; both Neo4j and
   ArcadeDB (and any future Bolt-speaking backend) reuse it.
2. :class:`BoltDriverConfig` — production-safe defaults for driver
   construction (timeouts, pool sizing, keep-alive, user agent).
   Frozen so instances are safe to share across stores pointing at the
   same instance.
3. :class:`BoltSessionRunner` — mixin that wraps the
   ``session.execute_{read,write}(lambda tx: ...)`` ceremony that
   otherwise repeats at every call site. Subclasses (concrete graph
   stores) must expose ``self._driver`` (a Bolt ``Driver``) and
   ``self._database`` (target database name).

Driver lifecycle is owned by the subclass that constructed the driver.
``StoreRegistry`` is responsible for sharing one driver per ``(uri,
user)`` across the graph + vector store pair when both target the same
instance; stores own a driver only when they build their own (passed
``driver=None``) and do *not* own one that was injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver

try:
    from neo4j import GraphDatabase  # noqa: F401 — imported for HAS_NEO4J check

    HAS_NEO4J = True
except ImportError:
    HAS_NEO4J = False

_MISSING_MSG = (
    "The ``neo4j`` Python driver is required for any Bolt-speaking "
    "graph store (Neo4j, ArcadeDB, …). Install it with one of: "
    "pip install 'trellis-ai[neo4j]' (for Neo4j) or "
    "pip install 'trellis-ai[arcadedb]' (for ArcadeDB)."
)


def check_driver_installed() -> None:
    """Raise ``ImportError`` with install hint if ``neo4j`` is unavailable."""
    if not HAS_NEO4J:
        raise ImportError(_MISSING_MSG)


_DEFAULT_USER_AGENT = "trellis-ai"


@dataclass(frozen=True)
class BoltDriverConfig:
    """Driver construction kwargs with production-safe defaults.

    Pass an instance to a backend-specific ``build_*_driver`` helper to
    override individual fields. Frozen so instances are safe to share
    across stores.

    Attributes
    ----------
    connection_timeout
        Seconds the driver waits to establish a Bolt connection before
        raising. Default 30s — anything longer typically means DNS or
        TLS misconfiguration; surface fast.
    max_connection_pool_size
        Maximum concurrent Bolt connections per driver. Default 100 —
        comfortably above any single Trellis caller's needs and matches
        common managed-service defaults.
    max_transaction_retry_time
        Seconds the driver retries a transient transaction failure
        (e.g. ``TransientError`` from concurrent writes) before
        re-raising. Default 30s.
    keep_alive
        Whether the driver enables TCP keep-alive on its connections.
        Default True — required for managed services and most cloud
        load balancers that silently drop idle TCP sessions.
    user_agent
        Identifies Trellis in the backend's session monitoring / audit
        logs. Defaults to ``"trellis-ai"``.
    """

    connection_timeout: float = 30.0
    max_connection_pool_size: int = 100
    max_transaction_retry_time: float = 30.0
    keep_alive: bool = True
    user_agent: str = _DEFAULT_USER_AGENT


class BoltSessionRunner:
    """Mixin: thin wrappers over ``session.execute_{read,write}``.

    Subclasses must expose ``self._driver`` (a Bolt ``Driver``) and
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
    operators see "backend unreachable" at startup rather than as an
    opaque Bolt error on the first request.

    Raises whatever the driver raises (``ServiceUnavailable``,
    ``AuthError``, etc.) — caller is expected to wrap the failure into
    a higher-level aggregate (typically
    :class:`RegistryValidationError`).
    """
    driver.verify_connectivity()
