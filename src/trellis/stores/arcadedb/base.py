"""ArcadeDB connection helpers — Bolt driver build + HTTP DDL/admin.

Two access paths to the same ArcadeDB instance, intentional:

1. **Bolt protocol on port 7687** (default), language=openCypher — used
   by :class:`~trellis.stores.arcadedb.graph.ArcadeDBGraphStore` for
   every graph operation. Speaks the standard ``neo4j`` Python driver
   so the existing Cypher payload in :class:`BoltOpenCypherGraphStore`
   works unchanged. Requires the ``BoltProtocolPlugin`` to be loaded on
   the server side (set via ``-Darcadedb.server.plugins=...``).
2. **HTTP REST on port 2480** — used for database
   creation/migration and (separately) by
   :class:`ArcadeDBVectorStore` to issue SQL DDL + vector queries that
   are not available via openCypher.

The Bolt driver is built by :func:`build_arcadedb_driver`, which reuses
:class:`trellis.stores.bolt_opencypher.base.BoltDriverConfig` for
production-safe defaults (timeouts, pool size, keep-alive). Auth is
plain basic auth ``(user, password)`` — no SigV4 or token refresh
needed because ArcadeDB is self-hosted, not cloud-managed.

:func:`ensure_database` is an idempotent helper that creates the target
ArcadeDB database via the HTTP server-admin endpoint if it doesn't
already exist. The Bolt driver assumes the database is already
present, so this is called once at registry construction time before
any Bolt sessions open.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import structlog

from trellis.stores.bolt_opencypher.base import (
    BoltDriverConfig,
    check_driver_installed,
)

if TYPE_CHECKING:
    from neo4j import Driver

logger = structlog.get_logger(__name__)


# Default ArcadeDB ports. The HTTP port serves the REST API and Studio
# UI; the Bolt port speaks the Neo4j Bolt wire protocol via the
# ``BoltProtocolPlugin``.
DEFAULT_HTTP_PORT = 2480
DEFAULT_BOLT_PORT = 7687

_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400


def derive_http_url_from_bolt(bolt_uri: str) -> str | None:
    """Derive the ArcadeDB HTTP base URL from a Bolt URI.

    Same host, ``http://`` scheme, default HTTP port. Returns ``None``
    when the Bolt URI has no parseable host. Used by the registry so a
    single ``arcadedb:`` config block can serve both planes without
    duplicating the host.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    parsed = urlparse(bolt_uri)
    if not parsed.hostname:
        return None
    return f"http://{parsed.hostname}:{DEFAULT_HTTP_PORT}"


def build_arcadedb_driver(
    uri: str,
    user: str,
    password: str,
    *,
    config: BoltDriverConfig | None = None,
) -> Driver:
    """Construct a Bolt ``Driver`` pointed at an ArcadeDB instance.

    Identical to :func:`trellis.stores.neo4j.base.build_driver` except
    the caller is responsible for ensuring the URI is reachable and the
    target ArcadeDB database exists (:func:`ensure_database`). The
    driver itself is the standard ``neo4j`` Python driver — ArcadeDB's
    Bolt plugin negotiates protocol versions transparently.
    """
    check_driver_installed()
    from neo4j import GraphDatabase  # noqa: PLC0415 — guarded by check_driver_installed

    cfg = config or BoltDriverConfig()
    return GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=cfg.connection_timeout,
        max_connection_pool_size=cfg.max_connection_pool_size,
        max_transaction_retry_time=cfg.max_transaction_retry_time,
        keep_alive=cfg.keep_alive,
        user_agent=cfg.user_agent,
    )


def _http_request(
    method: str,
    url: str,
    *,
    user: str,
    password: str,
    body: dict[str, object] | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    """Internal: issue an authenticated HTTP request and return ``(status, body_text)``.

    Used by :func:`ensure_database` and (via :class:`ArcadeDBVectorStore`)
    for SQL execution. Keeps the dependency footprint to the standard
    library — we don't pull ``httpx`` for two endpoints.
    """
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    # URLs are sourced from registry-resolved Trellis config (http_url
    # is supplied by the operator), not user input. urllib's open-any-
    # scheme audit warning (S310) is a false positive for this code
    # path.
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode() if exc.fp else ""
        return exc.code, body_text


def ensure_database(
    http_url: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    """Create the named database on the ArcadeDB server if it doesn't exist.

    Returns ``True`` if the database was newly created, ``False`` if it
    already existed. Idempotent — safe to call on every registry boot.

    ``http_url`` should be the base of the HTTP server, e.g.
    ``http://localhost:2480``. The ``/api/v1/server`` endpoint accepts a
    ``"create database <name>"`` command and replies ``200`` on success,
    ``400`` with a descriptive body when the database already exists.
    """
    status, body_text = _http_request(
        "POST",
        f"{http_url.rstrip('/')}/api/v1/server",
        user=user,
        password=password,
        body={"command": f"create database {database}"},
    )
    if status == _HTTP_OK:
        logger.info("arcadedb_database_created", database=database)
        return True
    if status == _HTTP_BAD_REQUEST and "already exists" in body_text.lower():
        return False
    msg = (
        f"ArcadeDB create-database returned {status}: {body_text[:200]}. "
        f"URL: {http_url}, database: {database!r}"
    )
    raise RuntimeError(msg)


def execute_sql(
    http_url: str,
    user: str,
    password: str,
    database: str,
    command: str,
    *,
    params: dict[str, object] | None = None,
    timeout: float = 30.0,
) -> list[dict[str, object]]:
    """Execute an ArcadeDB SQL command via the HTTP REST endpoint.

    Used by :class:`ArcadeDBVectorStore` for DDL (creating LSM_VECTOR
    indexes) and queries that openCypher doesn't expose
    (``vectorNeighbors`` etc.). Returns the parsed ``result`` array from
    the response, or raises :class:`RuntimeError` with the server's
    error body on failure.

    ``params`` are bound to ``:name`` placeholders in the SQL via
    ArcadeDB's parameter-binding protocol. Note: per-property type
    coercion still applies — vector values being assigned to a
    declared ``LIST OF FLOAT`` property must be inlined as a SQL list
    literal because parameter binding produces ``ARRAY_OF_FLOATS``
    type-mismatch errors. Reads via ``vectorNeighbors`` accept bound
    parameters normally.
    """
    body: dict[str, object] = {"language": "sql", "command": command}
    if params is not None:
        body["params"] = params
    status, body_text = _http_request(
        "POST",
        f"{http_url.rstrip('/')}/api/v1/command/{database}",
        user=user,
        password=password,
        body=body,
        timeout=timeout,
    )
    if status != _HTTP_OK:
        msg = (
            f"ArcadeDB SQL command failed with status {status}. "
            f"Command: {command[:120]!r}. Body: {body_text[:300]}"
        )
        raise RuntimeError(msg)
    payload = json.loads(body_text)
    result = payload.get("result", [])
    if not isinstance(result, list):
        msg = f"Unexpected ArcadeDB SQL response shape: {payload!r}"
        raise TypeError(msg)
    return result
