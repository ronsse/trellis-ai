"""A filter key must not become SQL in the SQLite graph and vector stores.

Both stores interpolate a caller's property or metadata key into SQL text as
a JSON path, ``json_extract(<column>, '$.<key>')``. A key carrying ``'``
closed that literal and the rest of the key ran as SQL: ``team') OR 1=1 OR
('`` matched every row, and ``te'am`` raised ``sqlite3.OperationalError``.
Every site now refuses a key outside ``[A-Za-z0-9_-]+`` with ``ValueError``
before a statement runs, and a plain key filters as it did before.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from trellis.stores.base.graph_query import EdgeQuery, FilterClause, NodeQuery
from trellis.stores.sqlite.base import SQLiteStoreBase
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

#: Closes the path literal and ORs in a tautology: it matched every row.
TAUTOLOGY_KEY = "team') OR 1=1 OR ('"
#: Closes the path literal and leaves a token SQLite cannot parse.
SYNTAX_ERROR_KEY = "te'am"

BAD_KEYS = pytest.mark.parametrize(
    "key", [TAUTOLOGY_KEY, SYNTAX_ERROR_KEY], ids=["tautology", "syntax-error"]
)
#: One key per accepted shape: the codebase's snake_case, plus ``-``, upper
#: case and digits. The fixtures store the same value under each.
PLAIN_KEYS = pytest.mark.parametrize("key", ["owner_team", "owner-team", "OwnerTeam2"])
REFUSAL = "not a plain JSON object key"


def _owner(team: str) -> dict[str, str]:
    return {"owner_team": team, "owner-team": team, "OwnerTeam2": team}


@contextmanager
def _statements(store: SQLiteStoreBase) -> Iterator[list[str]]:
    """Record every statement the store's connection runs inside the block."""
    conn = store._get_conn()
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        conn.set_trace_callback(None)


def _reached_sql(seen: list[str]) -> bool:
    """The recorder saw a filtered statement, so an empty one means something."""
    return any("json_extract" in statement for statement in seen)


@pytest.fixture
def graph_store(tmp_path: Path) -> Iterator[SQLiteGraphStore]:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    nodes: dict[str, dict[str, Any]] = {
        "node-a": {**_owner("platform"), "column_names": ["user_id", "email"]},
        "node-b": {**_owner("platform"), "column_names": ["order_id"]},
        "node-c": {**_owner("data"), "column_names": ["user_id"]},
        "node-d": {"column_names": []},
    }
    for node_id, props in nodes.items():
        store.upsert_node(node_id, "service", props)
    edges = [
        ("node-a", "node-b", {**_owner("platform"), "column_names": ["user_id"]}),
        ("node-b", "node-c", {**_owner("data"), "column_names": ["order_id"]}),
        ("node-c", "node-d", {**_owner("platform"), "column_names": ["email"]}),
    ]
    for source, target, props in edges:
        store.upsert_edge(source, target, "depends_on", properties=props)
    yield store
    store.close()


@pytest.fixture
def vector_store(tmp_path: Path) -> Iterator[SQLiteVectorStore]:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    teams = {"vec-a": "platform", "vec-b": "platform", "vec-c": "data"}
    for i, item_id in enumerate(["vec-a", "vec-b", "vec-c", "vec-d"]):
        meta = _owner(teams[item_id]) if item_id in teams else {}
        store.upsert(item_id, [1.0, float(i), 0.5], meta)
    yield store
    store.close()


def _node_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {row["node_id"] for row in rows}


def _edge_pairs(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(row["source_id"], row["target_id"]) for row in rows}


class TestGraphQuery:
    """``SQLiteGraphStore.query(properties=...)``."""

    @BAD_KEYS
    @pytest.mark.parametrize("value", ["platform", True, None])
    def test_bad_key_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore, key: str, value: object
    ) -> None:
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.query(properties={key: value})
        assert seen == []

    @pytest.mark.parametrize("key", ["a.b", "tags[0]", "", "two words"])
    def test_a_key_outside_the_plain_set_is_refused(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        """A nested path, an array index or an empty key is not one plain key."""
        with pytest.raises(ValueError, match=REFUSAL):
            graph_store.query(properties={key: "platform"})

    @PLAIN_KEYS
    def test_a_plain_key_filters_as_before(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.query(properties={key: "platform"})
        assert _node_ids(rows) == {"node-a", "node-b"}
        assert _reached_sql(seen)


class TestNodeQueryDsl:
    """``execute_node_query`` on ``properties.<key>`` (``_field_to_sql_expr``)."""

    @BAD_KEYS
    def test_bad_key_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        query = NodeQuery(
            filters=(FilterClause(f"properties.{key}", "eq", "platform"),)
        )
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_node_query(query)
        assert seen == []

    @PLAIN_KEYS
    def test_a_plain_key_filters_as_before(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        query = NodeQuery(
            filters=(FilterClause(f"properties.{key}", "eq", "platform"),)
        )
        with _statements(graph_store) as seen:
            rows = graph_store.execute_node_query(query)
        assert _node_ids(rows) == {"node-a", "node-b"}
        assert _reached_sql(seen)


class TestContainsDsl:
    """``contains`` on either side (``_render_contains_sqlite``)."""

    @BAD_KEYS
    def test_bad_key_is_refused_before_sql_on_nodes(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        query = NodeQuery(filters=(FilterClause(f"properties.{key}", "contains", 1),))
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_node_query(query)
        assert seen == []

    @BAD_KEYS
    def test_bad_key_is_refused_before_sql_on_edges(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        query = EdgeQuery(filters=(FilterClause(f"properties.{key}", "contains", 1),))
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_edge_query(query)
        assert seen == []

    def test_a_plain_key_filters_as_before(self, graph_store: SQLiteGraphStore) -> None:
        clause = FilterClause("properties.column_names", "contains", "user_id")
        with _statements(graph_store) as seen:
            nodes = graph_store.execute_node_query(NodeQuery(filters=(clause,)))
            edges = graph_store.execute_edge_query(EdgeQuery(filters=(clause,)))
        assert _node_ids(nodes) == {"node-a", "node-c"}
        assert _edge_pairs(edges) == {("node-a", "node-b")}
        assert _reached_sql(seen)


class TestEdgeQueryDsl:
    """``execute_edge_query`` on ``properties.<key>`` (``_edge_field_to_sql_expr``)."""

    @BAD_KEYS
    def test_bad_key_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        query = EdgeQuery(
            filters=(FilterClause(f"properties.{key}", "eq", "platform"),)
        )
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_edge_query(query)
        assert seen == []

    @PLAIN_KEYS
    def test_a_plain_key_filters_as_before(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        query = EdgeQuery(
            filters=(FilterClause(f"properties.{key}", "eq", "platform"),)
        )
        with _statements(graph_store) as seen:
            rows = graph_store.execute_edge_query(query)
        assert _edge_pairs(rows) == {("node-a", "node-b"), ("node-c", "node-d")}
        assert _reached_sql(seen)


class TestVectorQuery:
    """``SQLiteVectorStore.query(filters=...)``."""

    @BAD_KEYS
    def test_bad_key_is_refused_before_sql(
        self, vector_store: SQLiteVectorStore, key: str
    ) -> None:
        with (
            _statements(vector_store) as seen,
            pytest.raises(ValueError, match=REFUSAL),
        ):
            vector_store.query([1.0, 0.0, 0.0], top_k=10, filters={key: "platform"})
        assert seen == []

    @PLAIN_KEYS
    def test_a_plain_key_filters_as_before(
        self, vector_store: SQLiteVectorStore, key: str
    ) -> None:
        with _statements(vector_store) as seen:
            rows = vector_store.query(
                [1.0, 0.0, 0.0], top_k=10, filters={key: "platform"}
            )
        assert {row["item_id"] for row in rows} == {"vec-a", "vec-b"}
        assert _reached_sql(seen)
