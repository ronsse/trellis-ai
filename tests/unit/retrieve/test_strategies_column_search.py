"""Contract tests for the ``properties.column_names`` searchability recipe.

These tests pin the canonical DSL behaviour against a *real* SQLiteGraphStore
(not a MagicMock) — the rest of ``test_strategies.py`` exercises scoring math
and code paths with mocks, but the question this file answers is whether the
canonical graph layer can actually serve the search shape the Track G recipe
relies on:

    NodeQuery(filters=[
        FilterClause("properties.column_names", "in", ("user_id",))
    ])

Reading the SQLite compile path (``SQLiteGraphStore._compile_clause_sqlite``)
this *should* compile to::

    json_extract(properties_json, '$.column_names') IN (?)

with the bind parameter ``"user_id"``. ``json_extract`` on an array returns
the *whole array as JSON text*, so the IN compares ``"[\"user_id\",...]"``
against the scalar ``"user_id"`` — never matches.

The two tests here pin both sides of that observation:

* :meth:`test_dsl_in_against_list_property_finds_no_rows` is the **xfail
  trip-wire** — once the DSL grows a JSON-array-aware compile branch or a
  new ``contains`` operator, this test will start passing (``strict=True``)
  and force a code-review of the fix.
* :meth:`test_raw_json_each_finds_the_table` is the **working baseline** —
  a hand-rolled SQL fragment using ``json_each(...) WHERE value = ?`` does
  find the row, proving SQLite *can* do this query; the gap is purely on
  the DSL compiler side.

See ``docs/agent-guide/source-modeling-cookbook.md`` (the "Searchable
columns without column nodes" section) and Track G's ADR for the design
discussion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trellis.stores.base.graph_query import FilterClause, NodeQuery
from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def graph_store(tmp_path: Path):
    """A SQLite graph store seeded with two ``unity_catalog.table`` rows.

    Both nodes carry the dual-key column shape from the searchability
    recipe:

    * ``properties.columns`` — the structured metadata (list of dicts)
    * ``properties.column_names`` — the flat denormalised list used for
      exact-match search.

    ``fct_orders`` has a ``user_id`` column; ``dim_customers`` does not.
    """
    store = SQLiteGraphStore(tmp_path / "graph.db")
    store.upsert_node(
        "table:fct_orders",
        "unity_catalog.table",
        {
            "columns": [
                {"name": "user_id", "data_type": "BIGINT", "nullable": False},
                {"name": "email", "data_type": "STRING", "nullable": True},
                {"name": "order_total", "data_type": "DECIMAL(18,2)"},
            ],
            "column_names": ["user_id", "email", "order_total"],
        },
    )
    store.upsert_node(
        "table:dim_customers",
        "unity_catalog.table",
        {
            "columns": [
                {"name": "id", "data_type": "BIGINT", "nullable": False},
                {"name": "name", "data_type": "STRING"},
            ],
            "column_names": ["id", "name"],
        },
    )
    yield store
    store.close()


class TestColumnNamesSearch:
    """The Track G searchability recipe — pin behaviour and gap."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Canonical DSL `in` over a JSON-array property is broken on "
            "SQLite — `_compile_clause_sqlite` emits "
            "`json_extract(...) IN (?)` which compares the array-as-JSON-text "
            "against a scalar. See cookbook 'Searchable columns without "
            "column nodes' for the design gap. Strict xfail: when this "
            "starts passing, the fix has landed and the cookbook can drop "
            "the SQLite caveat."
        ),
    )
    def test_dsl_in_against_list_property_finds_no_rows(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        """The recipe's promised query path — currently broken on SQLite.

        Pins the canonical-DSL behaviour: ``FilterClause(..., "in",
        ("user_id",))`` should return ``fct_orders``. It does not, because
        SQLite's ``json_extract`` flattens the array to JSON text and ``IN``
        compares that text to the scalar bind.
        """
        results = graph_store.execute_node_query(
            NodeQuery(
                filters=(
                    FilterClause(
                        "properties.column_names", "in", ("user_id",)
                    ),
                ),
            )
        )
        assert {r["node_id"] for r in results} == {"table:fct_orders"}

    def test_raw_json_each_finds_the_table(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        """SQLite *can* do this — the gap is the DSL compiler, not the engine.

        Uses ``json_each`` against the underlying connection to confirm a
        working query plan exists. A future DSL extension (e.g. a
        ``contains`` operator, or an array-aware branch in the existing
        ``in`` compiler) would compile to roughly this SQL.
        """
        # ``_conn`` is private but stable — every backend in this repo
        # exposes the underlying connection on the store instance, and
        # the rest of the test suite reaches into it freely for
        # invariant checks (see e.g. test_sqlite_graph_bulk_upsert.py).
        sql = (
            "SELECT node_id FROM nodes WHERE valid_to IS NULL AND EXISTS ("
            " SELECT 1 FROM json_each("
            "  json_extract(properties_json, '$.column_names')"
            " ) WHERE json_each.value = ?"
            ")"
        )
        cursor = graph_store._conn.execute(sql, ("user_id",))
        rows = [row["node_id"] for row in cursor.fetchall()]
        assert rows == ["table:fct_orders"]

    def test_columns_round_trip_preserves_structured_shape(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        """Sibling check: the structured ``columns`` list survives a round-trip.

        The dual-key recipe leans on ``properties.columns`` being the
        canonical metadata and ``properties.column_names`` being a
        denormalised search-index — this test pins that the structured
        side reads back as a list of dicts, not a stringified blob, so
        agents that retrieve a table node get the full column metadata.
        """
        node = graph_store.get_node("table:fct_orders")
        assert node is not None
        cols: Any = node["properties"]["columns"]
        assert isinstance(cols, list)
        assert cols[0]["name"] == "user_id"
        assert cols[0]["data_type"] == "BIGINT"

        names = node["properties"]["column_names"]
        # Denormalised key must be a flat list of the same names, in the
        # same order — extractors that diverge break the search recipe
        # silently. The cookbook calls this contract out explicitly.
        assert names == [c["name"] for c in cols]

    def test_column_names_list_is_json_serialisable(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        """``column_names`` survives the wire as plain JSON.

        Not a search test — a wire-shape check. Useful as a regression net
        against an extractor that accidentally emits a tuple, ``set``, or
        custom object that would break JSON serialisation downstream.
        """
        node = graph_store.get_node("table:fct_orders")
        assert node is not None
        roundtrip = json.loads(json.dumps(node["properties"]["column_names"]))
        assert roundtrip == ["user_id", "email", "order_total"]
