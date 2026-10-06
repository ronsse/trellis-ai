"""A filter key reaches the SQLite graph and vector stores only as a bound value.

Both stores filter on a caller's property or metadata key through
``json_extract(<column>, ?)``, binding the JSON path ``json_key_path`` spells,
``$."<key>"``. The statement text is then the same whatever the key holds, and
the key names one flat object member, as it does on every other backend.

#729 refused every key outside ``[A-Za-z0-9_-]+`` here, because the path was
spliced into the statement text then. Its injection keys are kept, and each now
filters as an ordinary key. The spelling is one function, so the keys it must
spell are pinned once, on ``query(properties=...)``. Each call site is then
pinned on the statement it sends, recorded before sqlite3 binds anything: no
part of the key in the text, its spelled path among the parameters, and exactly
the rows that carry it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from trellis.stores.base.graph_query import EdgeQuery, FilterClause, NodeQuery
from trellis.stores.sqlite.base import SQLiteStoreBase, json_key_path
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

#: Spliced raw into ``'$.<key>'``, this closes the string literal and ORs in a
#: tautology that matches every row.
TAUTOLOGY_KEY = "team') OR 1=1 OR ('"
#: Spliced raw, this closes the literal and leaves a token SQLite cannot parse.
SYNTAX_ERROR_KEY = "te'am"

#: Keys #729 refused, the characters a quoted JSON path label decodes, and a
#: lone surrogate, which sqlite3 can bind only escaped. Each is one flat key,
#: carried by its own node in ``key_store``.
FLAT_KEYS = {
    "tautology": TAUTOLOGY_KEY,
    "syntax": SYNTAX_ERROR_KEY,
    "dot": "a.b",
    "index": "tags[0]",
    "empty": "",
    "space": "two words",
    "non-ascii": "clé",
    "double-quote": 'say "hi"',
    "backslash": "back\\slash",
    "escape-text": "\\u00e9",
    "backslash-quote": 'a\\"b',
    "lone-surrogate": "a\ud800b",
}

#: The key every call site filters on: the tautology above, a ``.``, both
#: characters a quoted label escapes, a space and a non-ASCII letter.
KEY = "team') OR 1=1 OR ('a.b \"c\" \\ é"
#: Pieces of ``KEY`` a statement's text could carry; none of them is SQL.
KEY_PARTS = ("team", "1=1", "a.b", "é")
#: ``KEY`` as bound: as ``json.dumps`` writes it, each ``\"`` then ``\u0022``.
KEY_PATH = "$.\"team') OR 1=1 OR ('a.b \\u0022c\\u0022 \\\\ \\u00e9\""

Statements = list[tuple[str, tuple[Any, ...]]]


class _Recorder:
    """Stands in for a store's connection and keeps each statement unexpanded.

    ``set_trace_callback`` cannot tell a bound key from a spliced one: from
    Python 3.11 it is handed the statement with its values substituted.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements: Statements = []

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        self.statements.append((sql, tuple(params)))
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@contextmanager
def _statements(store: SQLiteStoreBase) -> Iterator[Statements]:
    """Record every statement the store sends inside the block."""
    recorder = _Recorder(store._get_conn())
    # An instance attribute shadows the method ``_conn`` forwards to.
    store._get_conn = lambda: recorder  # type: ignore[method-assign]
    try:
        yield recorder.statements
    finally:
        del store._get_conn


def _assert_bound(seen: Statements, *, times: int = 1) -> None:
    """``KEY`` reached SQLite as a bound path, and no statement's text holds it."""
    assert [sql for sql, _ in seen if any(part in sql for part in KEY_PARTS)] == []
    assert [value for _, params in seen for value in params].count(KEY_PATH) == times


def _owner(team: str) -> dict[str, str]:
    """The same value under one plain key of each shape #729 accepted."""
    return {"owner_team": team, "owner-team": team, "OwnerTeam2": team}


@pytest.fixture
def graph_store(tmp_path: Path) -> Iterator[SQLiteGraphStore]:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    nodes: dict[str, dict[str, Any]] = {
        "node-a": {
            **_owner("platform"),
            KEY: "platform",
            "column_names": ["user_id", "email"],
        },
        "node-b": {**_owner("platform"), KEY: True, "column_names": ["order_id"]},
        "node-c": {**_owner("data"), KEY: ["user_id"], "column_names": ["user_id"]},
        "node-d": {"column_names": []},
    }
    for node_id, props in nodes.items():
        store.upsert_node(node_id, "service", props)
    edges = [
        (
            "node-a",
            "node-b",
            {**_owner("platform"), KEY: "platform", "column_names": ["user_id"]},
        ),
        (
            "node-b",
            "node-c",
            {**_owner("data"), KEY: ["user_id"], "column_names": ["order_id"]},
        ),
        (
            "node-c",
            "node-d",
            {**_owner("platform"), KEY: "data", "column_names": ["email"]},
        ),
    ]
    for source, target, props in edges:
        store.upsert_edge(source, target, "depends_on", properties=props)
    yield store
    store.close()


@pytest.fixture
def key_store(tmp_path: Path) -> Iterator[SQLiteGraphStore]:
    """One node per ``FLAT_KEYS`` entry, a control and a decoy."""
    store = SQLiteGraphStore(tmp_path / "keys.db")
    for key_id, key in FLAT_KEYS.items():
        store.upsert_node(f"kp-{key_id}", "service", {key: "platform"})
    store.upsert_node(
        "kp-control", "service", dict.fromkeys(FLAT_KEYS.values(), "data")
    )
    # The value where a misspelled path lands: ``b`` inside ``a``, element 0
    # of ``tags``, and the ``é`` the text ``\u00e9`` decodes to unescaped.
    store.upsert_node(
        "kp-decoy",
        "service",
        {"a": {"b": "platform"}, "tags": ["platform"], "é": "platform"},
    )
    yield store
    store.close()


@pytest.fixture
def vector_store(tmp_path: Path) -> Iterator[SQLiteVectorStore]:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    metas: dict[str, dict[str, Any]] = {
        "vec-a": {**_owner("platform"), KEY: "platform"},
        "vec-b": {**_owner("platform"), KEY: "data"},
        "vec-c": _owner("data"),
        "vec-d": {},
    }
    for i, (item_id, meta) in enumerate(metas.items()):
        store.upsert(item_id, [1.0, float(i), 0.5], meta)
    yield store
    store.close()


def _node_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {row["node_id"] for row in rows}


def _edge_pairs(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(row["source_id"], row["target_id"]) for row in rows}


def _eq(key: str) -> tuple[FilterClause, ...]:
    return (FilterClause(f"properties.{key}", "eq", "platform"),)


class TestTheSpelling:
    """``json_key_path``: each key names its own member on the SQLite in use."""

    @pytest.mark.parametrize("key_id", list(FLAT_KEYS))
    def test_a_key_filters_as_one_flat_member(
        self, key_store: SQLiteGraphStore, key_id: str
    ) -> None:
        rows = key_store.query(properties={FLAT_KEYS[key_id]: "platform"})
        assert _node_ids(rows) == {f"kp-{key_id}"}

    def test_a_double_quote_is_written_as_a_unicode_escape(self) -> None:
        """``\\"`` matches on SQLite 3.53 and silently misses on 3.45 and 3.46."""
        assert json_key_path('say "hi"') == '$."say \\u0022hi\\u0022"'

    def test_the_label_is_spelled_as_the_stores_write_the_key(self) -> None:
        """SQLite 3.40 compares the label with the stored text as written.

        Both stores write that text with ``json.dumps`` defaults, which store
        ``é`` as ``\\u00e9`` and a newline as ``\\n``. SQLite 3.45 and later
        decode either spelling of those two, so no filter tells them apart.
        """
        assert json_key_path("clé\n") == '$."cl\\u00e9\\n"'

    def test_a_nul_is_refused_before_sql(self, key_store: SQLiteGraphStore) -> None:
        """No spelling names it.

        A raw NUL ends the path where SQLite reads it, and ``\\u0000``
        decodes to the same terminator and matches the key's prefix.
        """
        with _statements(key_store) as seen, pytest.raises(ValueError, match="NUL"):
            key_store.query(properties={"a\x00b": "platform"})
        assert seen == []

    @pytest.mark.parametrize("key", ["owner_team", "owner-team", "OwnerTeam2"])
    def test_a_plain_key_filters_as_before(
        self, graph_store: SQLiteGraphStore, key: str
    ) -> None:
        rows = graph_store.query(properties={key: "platform"})
        assert _node_ids(rows) == {"node-a", "node-b"}


class TestGraphQuery:
    """``query(properties=...)``: the scalar, bool and ``None`` branches."""

    def test_the_scalar_branch_binds_the_key(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.query(properties={KEY: "platform"})
        assert _node_ids(rows) == {"node-a"}
        _assert_bound(seen)

    def test_the_bool_branch_binds_the_key(self, graph_store: SQLiteGraphStore) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.query(properties={KEY: True})
        assert _node_ids(rows) == {"node-b"}
        _assert_bound(seen)

    def test_the_none_branch_binds_the_key(self, graph_store: SQLiteGraphStore) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.query(properties={KEY: None})
        assert _node_ids(rows) == {"node-d"}
        _assert_bound(seen)


class TestNodeQueryDsl:
    """``execute_node_query`` on ``properties.<key>`` (``_field_to_sql_expr``)."""

    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            ("eq", "platform", {"node-a"}),
            ("in", ("platform", "data"), {"node-a"}),
            ("exists", None, {"node-a", "node-b", "node-c"}),
            ("gte", "platform", {"node-a"}),
        ],
        ids=["eq", "in", "exists", "range"],
    )
    def test_every_op_binds_the_key(
        self,
        graph_store: SQLiteGraphStore,
        op: str,
        value: object,
        expected: set[str],
    ) -> None:
        clause = FilterClause(f"properties.{KEY}", op, value)  # type: ignore[arg-type]
        with _statements(graph_store) as seen:
            rows = graph_store.execute_node_query(NodeQuery(filters=(clause,)))
        assert _node_ids(rows) == expected
        _assert_bound(seen)

    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore) -> None:
        rows = graph_store.execute_node_query(NodeQuery(filters=_eq("owner_team")))
        assert _node_ids(rows) == {"node-a", "node-b"}


class TestContainsDsl:
    """``contains`` on nodes and edges (both use ``_render_contains_sqlite``)."""

    def test_the_key_is_bound_for_both_its_uses(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        clause = FilterClause(f"properties.{KEY}", "contains", "user_id")
        with _statements(graph_store) as node_seen:
            nodes = graph_store.execute_node_query(NodeQuery(filters=(clause,)))
        with _statements(graph_store) as edge_seen:
            edges = graph_store.execute_edge_query(EdgeQuery(filters=(clause,)))
        assert _node_ids(nodes) == {"node-c"}
        assert _edge_pairs(edges) == {("node-b", "node-c")}
        _assert_bound(node_seen, times=2)
        _assert_bound(edge_seen, times=2)

    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore) -> None:
        clause = FilterClause("properties.column_names", "contains", "user_id")
        nodes = graph_store.execute_node_query(NodeQuery(filters=(clause,)))
        edges = graph_store.execute_edge_query(EdgeQuery(filters=(clause,)))
        assert _node_ids(nodes) == {"node-a", "node-c"}
        assert _edge_pairs(edges) == {("node-a", "node-b")}


class TestEdgeQueryDsl:
    """``execute_edge_query`` on ``properties.<key>`` (``_edge_field_to_sql_expr``)."""

    def test_the_key_is_bound(self, graph_store: SQLiteGraphStore) -> None:
        with _statements(graph_store) as seen:
            rows = graph_store.execute_edge_query(EdgeQuery(filters=_eq(KEY)))
        assert _edge_pairs(rows) == {("node-a", "node-b")}
        _assert_bound(seen)

    def test_a_plain_key_filters(self, graph_store: SQLiteGraphStore) -> None:
        rows = graph_store.execute_edge_query(EdgeQuery(filters=_eq("owner_team")))
        assert _edge_pairs(rows) == {("node-a", "node-b"), ("node-c", "node-d")}


class TestVectorQuery:
    """``SQLiteVectorStore.query(filters=...)``."""

    def test_the_key_is_bound(self, vector_store: SQLiteVectorStore) -> None:
        with _statements(vector_store) as seen:
            rows = vector_store.query(
                [1.0, 0.0, 0.0], top_k=10, filters={KEY: "platform"}
            )
        assert {row["item_id"] for row in rows} == {"vec-a"}
        _assert_bound(seen)

    def test_a_plain_key_filters(self, vector_store: SQLiteVectorStore) -> None:
        rows = vector_store.query(
            [1.0, 0.0, 0.0], top_k=10, filters={"owner_team": "platform"}
        )
        assert {row["item_id"] for row in rows} == {"vec-a", "vec-b"}
