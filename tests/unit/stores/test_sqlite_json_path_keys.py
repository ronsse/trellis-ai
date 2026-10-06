"""A filter key must not become SQL in the SQLite graph and vector stores.

Both stores splice a caller's property or metadata key into SQL text as a JSON
path, ``json_extract(<column>, '$.<key>')``, so every site passes the key
through ``json_key_path`` first, which raises ``ValueError`` for a key outside
``[A-Za-z0-9_-]+`` before any statement runs. The check is one function, so
its refused and accepted keys are pinned once, on ``query(properties=...)``;
each other site gets one refused key and one plain key.
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

#: Closes the path literal and ORs in a tautology: spliced raw, it matches
#: every row.
TAUTOLOGY_KEY = "team') OR 1=1 OR ('"
#: Closes the path literal and leaves a token SQLite cannot parse.
SYNTAX_ERROR_KEY = "te'am"
REFUSAL = "not a plain JSON object key"


def _owner(team: str) -> dict[str, str]:
    """The same value under one key of each accepted shape."""
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


def _eq(key: str) -> tuple[FilterClause, ...]:
    return (FilterClause(f"properties.{key}", "eq", "platform"),)


class TestGraphQuery:
    """``SQLiteGraphStore.query(properties=...)``, and the check's two sets."""

    @pytest.mark.parametrize(
        "key",
        [TAUTOLOGY_KEY, SYNTAX_ERROR_KEY, "a.b", "tags[0]", "", "two words", "clé"],
        ids=["tautology", "syntax", "dot", "index", "empty", "space", "non-ascii"],
    )
    def test_a_key_outside_the_plain_set_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.query(properties={key: "platform"})
        assert seen == []

    @pytest.mark.parametrize("value", [True, None])
    def test_the_bool_and_none_branches_check_the_key_too(
        self, graph_store: SQLiteGraphStore, value: object
    ) -> None:
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.query(properties={TAUTOLOGY_KEY: value})
        assert seen == []

    @pytest.mark.parametrize("key", ["owner_team", "owner-team", "OwnerTeam2"])
    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore, key: str) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.query(properties={key: "platform"})
        assert _node_ids(rows) == {"node-a", "node-b"}
        assert _reached_sql(seen)


class TestNodeQueryDsl:
    """``execute_node_query`` on ``properties.<key>`` (``_field_to_sql_expr``)."""

    def test_a_bad_key_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        query = NodeQuery(filters=_eq(TAUTOLOGY_KEY))
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_node_query(query)
        assert seen == []

    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.execute_node_query(NodeQuery(filters=_eq("owner_team")))
        assert _node_ids(rows) == {"node-a", "node-b"}
        assert _reached_sql(seen)


class TestContainsDsl:
    """``contains`` on nodes and edges (both use ``_render_contains_sqlite``)."""

    def test_a_bad_key_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        clause = FilterClause(f"properties.{TAUTOLOGY_KEY}", "contains", 1)
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_node_query(NodeQuery(filters=(clause,)))
        assert seen == []

    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore) -> None:
        clause = FilterClause("properties.column_names", "contains", "user_id")
        with _statements(graph_store) as seen:
            nodes = graph_store.execute_node_query(NodeQuery(filters=(clause,)))
            edges = graph_store.execute_edge_query(EdgeQuery(filters=(clause,)))
        assert _node_ids(nodes) == {"node-a", "node-c"}
        assert _edge_pairs(edges) == {("node-a", "node-b")}
        assert _reached_sql(seen)


class TestEdgeQueryDsl:
    """``execute_edge_query`` on ``properties.<key>`` (``_edge_field_to_sql_expr``)."""

    def test_a_bad_key_is_refused_before_sql(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        query = EdgeQuery(filters=_eq(TAUTOLOGY_KEY))
        with _statements(graph_store) as seen, pytest.raises(ValueError, match=REFUSAL):
            graph_store.execute_edge_query(query)
        assert seen == []

    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.execute_edge_query(EdgeQuery(filters=_eq("owner_team")))
        assert _edge_pairs(rows) == {("node-a", "node-b"), ("node-c", "node-d")}
        assert _reached_sql(seen)


class TestVectorQuery:
    """``SQLiteVectorStore.query(filters=...)``."""

    def test_a_bad_key_is_refused_before_sql(
        self, vector_store: SQLiteVectorStore
    ) -> None:
        with (
            _statements(vector_store) as seen,
            pytest.raises(ValueError, match=REFUSAL),
        ):
            vector_store.query(
                [1.0, 0.0, 0.0], top_k=10, filters={TAUTOLOGY_KEY: "platform"}
            )
        assert seen == []

    def test_a_plain_key_filters(self, vector_store: SQLiteVectorStore) -> None:
        with _statements(vector_store) as seen:
            rows = vector_store.query(
                [1.0, 0.0, 0.0], top_k=10, filters={"owner_team": "platform"}
            )
        assert {row["item_id"] for row in rows} == {"vec-a", "vec-b"}
        assert _reached_sql(seen)
