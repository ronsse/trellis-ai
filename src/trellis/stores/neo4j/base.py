"""Shared helpers for Neo4j-backed stores.

Only two concerns live here: detecting whether the optional ``neo4j``
driver is installed, and building a ``Driver`` instance. Each store
owns its own driver — the official driver pools connections
internally so there's no benefit to a module-level cache.
"""

from __future__ import annotations

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


def build_driver(uri: str, user: str, password: str) -> Driver:
    """Construct a Neo4j ``Driver``. Caller owns its lifecycle."""
    return GraphDatabase.driver(uri, auth=(user, password))
