"""A raw-NUL id against a Postgres-backed store reads as not found.

PostgreSQL ``text`` columns can never hold a NUL (0x00) byte: binding one
as a query parameter raises ``psycopg.errors.DataError`` client-side,
before any SQL reaches the server. ``RedactionApplyHandler`` lets a NUL
``target_id`` reach the store layer (Stage 1 exempts it for
``redaction.apply``), so every store method it calls with that id must
answer its ordinary not-found value instead of letting the driver raise.

No database is needed: each store is constructed without running
``__init__`` (which would open a real connection pool and run schema DDL),
and its ``_pool`` is set to a double that fails the test if the guarded
method ever reaches it — proving the NUL check is a true short-circuit,
not just a value that happens to come back empty.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg_pool")

from trellis.stores.pgvector.store import PgVectorStore
from trellis.stores.postgres import graph as pg_graph

NUL_ID = "x\x00y"


class _UnreachablePool:
    """A pool double that fails the test if a guarded method reaches it."""

    def connection(self) -> object:
        msg = "a NUL-bearing id must not open a connection"
        raise AssertionError(msg)


def _graph_store() -> pg_graph.PostgresGraphStore:
    store = object.__new__(pg_graph.PostgresGraphStore)
    store._pool = _UnreachablePool()  # type: ignore[attr-defined]
    return store


def _vector_store() -> PgVectorStore:
    store = object.__new__(PgVectorStore)
    store._pool = _UnreachablePool()  # type: ignore[attr-defined]
    return store


class TestPostgresGraphStoreNulGuard:
    def test_get_node_reads_as_not_found(self) -> None:
        assert _graph_store().get_node(NUL_ID) is None

    def test_get_node_history_reads_as_empty(self) -> None:
        assert _graph_store().get_node_history(NUL_ID) == []

    def test_get_edges_reads_as_empty(self) -> None:
        assert _graph_store().get_edges(NUL_ID) == []

    def test_get_aliases_reads_as_empty(self) -> None:
        assert _graph_store().get_aliases(NUL_ID) == []

    def test_delete_node_reads_as_already_gone(self) -> None:
        assert _graph_store().delete_node(NUL_ID) is False


class TestPgVectorStoreNulGuard:
    def test_delete_has_nothing_to_delete(self) -> None:
        assert _vector_store().delete(NUL_ID) is False
