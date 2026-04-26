"""Shared helpers for Neo4j-backed stores.

Three concerns live here:

1. Detecting whether the optional ``neo4j`` driver is installed.
2. Holding the driver-construction kwargs that production deployments
   need (timeouts, pool sizing, keep-alive) in one place — :class:`DriverConfig`.
3. Building a ``Driver`` from a URI / user / password + a config — :func:`build_driver`.

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
from typing import TYPE_CHECKING

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
        TLS misconfiguration; surface fast.
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
