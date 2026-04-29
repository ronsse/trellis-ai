"""Wipe persistent state in live backends used by an eval registry.

Multi-backend eval scenarios all share the same hygiene problem: Neon
Postgres and AuraDB persist between runs and across scenario boundaries,
so leftover rows from a prior run leak into the current one and break
cross-backend equivalence comparisons. SQLite handles use a fresh
``stores_dir`` per registry and need no wipe.

The :func:`wipe_live_state` orchestrator inspects each store the
registry actually constructs and dispatches to the right wipe helper
by type. No handle-name coupling — scenarios can call this on any
registry shape.

This module is private to ``eval/`` (leading underscore in the
filename). Production code does not need a wipe helper; eval-only
infrastructure does.
"""

from __future__ import annotations

import structlog

from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


def wipe_live_state(registry: StoreRegistry) -> None:
    """Truncate any non-SQLite knowledge + operational tables this registry uses.

    Calls ``_init_schema`` on every store first (via property access),
    because TRUNCATE errors against a missing table — registry stores
    are lazy and only construct on first access.

    Dispatch is by ``type(store).__name__`` rather than ``isinstance``
    so this module never imports the optional ``[postgres]`` /
    ``[neo4j]`` extras — eval's import graph stays minimal regardless
    of which backends the runtime has installed. Same helper covers
    5.1's ``"neo4j"`` handle and 5.5's ``"neo4j_op_postgres"`` handle
    without per-scenario branching.
    """
    knowledge = registry.knowledge
    operational = registry.operational

    # Force schema init on every store the wipe might touch — store
    # registry is lazy and these property accesses are the public API
    # that triggers ``_init_schema``.
    knowledge.graph_store  # noqa: B018
    knowledge.vector_store  # noqa: B018
    knowledge.document_store  # noqa: B018
    operational.trace_store  # noqa: B018
    operational.event_log  # noqa: B018

    _wipe_postgres_event_log(operational.event_log)
    _wipe_postgres_trace_store(operational.trace_store)
    _wipe_postgres_graph_store(knowledge.graph_store)
    _wipe_postgres_vector_store(knowledge.vector_store)
    _wipe_neo4j_graph_store(knowledge.graph_store)


# ---------------------------------------------------------------------------
# Per-store wipe helpers
# ---------------------------------------------------------------------------


def _wipe_postgres_event_log(store: object) -> None:
    if type(store).__name__ != "PostgresEventLog":
        return
    _truncate_postgres(store, ["events"], cascade=False)


def _wipe_postgres_trace_store(store: object) -> None:
    if type(store).__name__ != "PostgresTraceStore":
        return
    _truncate_postgres(store, ["traces"], cascade=False)


def _wipe_postgres_graph_store(store: object) -> None:
    if type(store).__name__ != "PostgresGraphStore":
        return
    _truncate_postgres(store, ["nodes", "edges", "entity_aliases"], cascade=True)


def _wipe_postgres_vector_store(store: object) -> None:
    """Truncate ``vectors`` rows, leaving the column dimension intact.

    TRUNCATE preserves the column-level pgvector dimension constraint,
    so a wipe + reuse round trip with the same configured dim is safe.
    """
    if type(store).__name__ != "PgVectorStore":
        return
    _truncate_postgres(store, ["vectors"], cascade=False)


def _wipe_neo4j_graph_store(store: object) -> None:
    if type(store).__name__ != "Neo4jGraphStore":
        return
    driver = getattr(store, "_driver", None)
    if driver is None:
        return
    database = getattr(store, "_database", "neo4j")
    with driver.session(database=database) as session:
        session.run("MATCH (n:Node) DETACH DELETE n")
    logger.debug("eval.neo4j_wiped", database=database)


# ---------------------------------------------------------------------------
# Internal — Postgres helper
# ---------------------------------------------------------------------------


def _truncate_postgres(store: object, tables: list[str], *, cascade: bool) -> None:
    """TRUNCATE the given tables via the store's existing ``_conn``.

    Reaches into the store's private ``_conn`` attribute on purpose —
    the registry doesn't expose a public truncate-tables method, and
    eval is the only consumer that needs one. Defensive ``getattr``
    fallback in case a future refactor changes the connection layout
    so the failure surfaces as a no-op + warning rather than a crash.
    """
    conn = getattr(store, "_conn", None)
    if conn is None:
        logger.warning(
            "eval.wipe_unavailable",
            store=type(store).__name__,
            reason="no _conn attribute",
        )
        return
    suffix = " CASCADE" if cascade else ""
    sql = f"TRUNCATE {', '.join(tables)} RESTART IDENTITY{suffix}"
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.debug("eval.postgres_wiped", store=type(store).__name__, tables=tables)
