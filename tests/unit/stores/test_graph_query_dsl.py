"""Unit tests for the canonical graph query DSL.

The DSL itself (FilterClause / NodeQuery / SubgraphQuery / SubgraphResult)
is pure value objects — no I/O. These tests pin the validation
contract; backend integration tests (the contract suites) cover
execution behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trellis.stores.base.graph_query import (
    EdgeQuery,
    FilterClause,
    NodeQuery,
    SubgraphQuery,
    SubgraphResult,
)


class TestFilterClauseValidation:
    def test_eq_with_scalar_ok(self) -> None:
        clause = FilterClause("properties.team", "eq", "platform")
        assert clause.value == "platform"

    def test_eq_with_tuple_rejected(self) -> None:
        with pytest.raises(TypeError, match="scalar"):
            FilterClause("properties.team", "eq", ("a", "b"))

    def test_in_with_tuple_ok(self) -> None:
        clause = FilterClause("node_type", "in", ("service", "person"))
        assert clause.value == ("service", "person")

    def test_in_with_scalar_rejected(self) -> None:
        with pytest.raises(TypeError, match="tuple"):
            FilterClause("node_type", "in", "service")

    def test_exists_with_none_value_ok(self) -> None:
        clause = FilterClause("properties.deprecated_at", "exists")
        assert clause.value is None

    def test_exists_with_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="value=None"):
            FilterClause("properties.team", "exists", "x")

    def test_frozen_dataclass(self) -> None:
        clause = FilterClause("node_type", "eq", "service")
        with pytest.raises((AttributeError, TypeError)):
            clause.field = "other"  # type: ignore[misc]


class TestNodeQuery:
    def test_default_empty_filters(self) -> None:
        q = NodeQuery()
        assert q.filters == ()
        assert q.limit == 50
        assert q.as_of is None

    def test_construction_with_filters(self) -> None:
        clauses = (
            FilterClause("node_type", "eq", "service"),
            FilterClause("properties.tier", "in", (1, 2)),
        )
        q = NodeQuery(filters=clauses, limit=10)
        assert q.filters == clauses
        assert q.limit == 10

    def test_with_as_of(self) -> None:
        ts = datetime.now(UTC)
        q = NodeQuery(as_of=ts)
        assert q.as_of == ts

    def test_frozen(self) -> None:
        q = NodeQuery()
        with pytest.raises((AttributeError, TypeError)):
            q.limit = 100  # type: ignore[misc]


class TestSubgraphQuery:
    def test_required_seed_ids(self) -> None:
        q = SubgraphQuery(seed_ids=("a", "b"))
        assert q.seed_ids == ("a", "b")
        assert q.depth == 2
        assert q.edge_type_filter is None

    def test_with_filters(self) -> None:
        q = SubgraphQuery(
            seed_ids=("a",),
            depth=1,
            edge_type_filter=("depends_on", "calls"),
        )
        assert q.depth == 1
        assert q.edge_type_filter == ("depends_on", "calls")

    def test_frozen(self) -> None:
        q = SubgraphQuery(seed_ids=("a",))
        with pytest.raises((AttributeError, TypeError)):
            q.depth = 5  # type: ignore[misc]


class TestSubgraphResult:
    def test_default_empty(self) -> None:
        r = SubgraphResult()
        assert r.nodes == []
        assert r.edges == []

    def test_with_data(self) -> None:
        r = SubgraphResult(
            nodes=[{"node_id": "a"}],
            edges=[{"edge_id": "e1", "source_id": "a", "target_id": "b"}],
        )
        assert len(r.nodes) == 1
        assert len(r.edges) == 1


class TestFilterClauseContainsOp:
    """Phase 3 ``contains`` operator — list-membership.

    The DSL validation is the only thing the pure-value-object can pin;
    behaviour against an actual list-typed property lives in the
    backend contract suite.
    """

    def test_contains_with_string_scalar_ok(self) -> None:
        clause = FilterClause("properties.column_names", "contains", "user_id")
        assert clause.value == "user_id"
        assert clause.op == "contains"

    def test_contains_with_int_scalar_ok(self) -> None:
        clause = FilterClause("properties.ids", "contains", 42)
        assert clause.value == 42

    def test_contains_with_float_scalar_ok(self) -> None:
        clause = FilterClause("properties.thresholds", "contains", 0.5)
        assert clause.value == 0.5

    def test_contains_with_bool_scalar_ok(self) -> None:
        """``bool`` is a legitimate list element type, unlike for range ops."""
        clause = FilterClause("properties.flags", "contains", True)
        assert clause.value is True

    def test_contains_rejects_tuple_value(self) -> None:
        """A tuple of values is the ``in`` shape; ``contains`` takes one."""
        with pytest.raises(TypeError, match="scalar"):
            FilterClause("properties.column_names", "contains", ("a", "b"))

    def test_contains_rejects_none_value(self) -> None:
        with pytest.raises(ValueError, match="scalar"):
            FilterClause("properties.column_names", "contains", None)

    def test_contains_rejects_list_value(self) -> None:
        """List is not in the FilterClause value union; mypy + runtime reject."""
        with pytest.raises(TypeError, match="scalar"):
            FilterClause(
                "properties.column_names",
                "contains",
                ["a", "b"],  # type: ignore[arg-type]
            )

    def test_contains_rejects_dict_value(self) -> None:
        with pytest.raises(TypeError, match="scalar"):
            FilterClause(
                "properties.column_names",
                "contains",
                {"k": "v"},  # type: ignore[arg-type]
            )


class TestFilterClauseRangeOps:
    """Phase 2 range operators — added for provenance filtering."""

    @pytest.mark.parametrize("op", ["lt", "lte", "gt", "gte"])
    def test_range_op_with_numeric_value_ok(self, op: str) -> None:
        clause = FilterClause("confidence", op, 0.7)
        assert clause.value == 0.7
        assert clause.op == op

    @pytest.mark.parametrize("op", ["lt", "lte", "gt", "gte"])
    def test_range_op_with_string_value_ok(self, op: str) -> None:
        """Range ops on strings are permitted — the DSL is dtype-agnostic."""
        clause = FilterClause("source_trace_id", op, "trace-z")
        assert clause.value == "trace-z"

    @pytest.mark.parametrize("op", ["lt", "lte", "gt", "gte"])
    def test_range_op_rejects_tuple_value(self, op: str) -> None:
        with pytest.raises(TypeError, match="scalar"):
            FilterClause("confidence", op, (0.5, 0.9))

    @pytest.mark.parametrize("op", ["lt", "lte", "gt", "gte"])
    def test_range_op_rejects_none_value(self, op: str) -> None:
        with pytest.raises(ValueError, match="scalar"):
            FilterClause("confidence", op, None)

    @pytest.mark.parametrize("op", ["lt", "lte", "gt", "gte"])
    def test_range_op_rejects_bool_value(self, op: str) -> None:
        """``bool`` is an ``int`` subclass — reject it explicitly so
        ``confidence < True`` doesn't silently land as ``< 1``."""
        with pytest.raises(TypeError, match="numeric"):
            FilterClause("confidence", op, True)

    def test_out_of_range_value_passes_through_dsl(self) -> None:
        """The DSL deliberately does not range-check filter values.

        Even ``confidence < 2.0`` is allowed (caller may be sweeping a
        nonsense filter intentionally — it just matches every row).
        """
        clause = FilterClause("confidence", "lt", 2.0)
        assert clause.value == 2.0


class TestEdgeQuery:
    """Edge-side typed query value object — Phase 2 of Item 2."""

    def test_default_empty_filters(self) -> None:
        q = EdgeQuery()
        assert q.filters == ()
        assert q.limit == 50
        assert q.as_of is None

    def test_construction_with_provenance_filters(self) -> None:
        clauses = (
            FilterClause("confidence", "lt", 0.7),
            FilterClause("extractor_tier", "in", ("DETERMINISTIC", "HYBRID")),
        )
        q = EdgeQuery(filters=clauses, limit=25)
        assert q.filters == clauses
        assert q.limit == 25

    def test_with_as_of(self) -> None:
        ts = datetime.now(UTC)
        q = EdgeQuery(as_of=ts)
        assert q.as_of == ts

    def test_frozen(self) -> None:
        q = EdgeQuery()
        with pytest.raises((AttributeError, TypeError)):
            q.limit = 100  # type: ignore[misc]


class TestSQLiteEdgeCompiler:
    """Pure-compile tests for the SQLite edge-DSL compiler.

    No I/O — verifies the SQL fragments and parameter shapes.  Live
    SQLite execution lives in the contract suite.
    """

    def test_eq_provenance_column(self) -> None:
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        clause = FilterClause("source_trace_id", "eq", "trace-1")
        sql, params = SQLiteGraphStore._render_clause_sqlite(
            SQLiteGraphStore._edge_field_to_sql_expr(clause.field), clause
        )
        assert sql == "source_trace_id = ?"
        assert params == ["trace-1"]

    @pytest.mark.parametrize(
        ("op", "expected"),
        [("lt", "<"), ("lte", "<="), ("gt", ">"), ("gte", ">=")],
    )
    def test_range_op_compiles_to_sql_operator(
        self, op: str, expected: str
    ) -> None:
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        clause = FilterClause("confidence", op, 0.5)
        sql, params = SQLiteGraphStore._render_clause_sqlite(
            SQLiteGraphStore._edge_field_to_sql_expr(clause.field), clause
        )
        assert sql == f"confidence {expected} ?"
        assert params == [0.5]

    def test_in_with_extractor_tier(self) -> None:
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        clause = FilterClause(
            "extractor_tier", "in", ("DETERMINISTIC", "HYBRID")
        )
        sql, params = SQLiteGraphStore._render_clause_sqlite(
            SQLiteGraphStore._edge_field_to_sql_expr(clause.field), clause
        )
        assert sql == "extractor_tier IN (?, ?)"
        assert params == ["DETERMINISTIC", "HYBRID"]

    def test_unsupported_field_raises(self) -> None:
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        with pytest.raises(ValueError, match="Unsupported DSL edge field"):
            SQLiteGraphStore._edge_field_to_sql_expr("not_a_real_field")


class TestSQLiteContainsCompiler:
    """Pure-compile tests for the SQLite ``contains`` compiler path.

    Verifies the ``json_each`` EXISTS shape and parameter binding.
    Behavioural coverage lives in the contract suite.
    """

    def test_contains_compiles_to_json_each_exists(self) -> None:
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        clause = FilterClause("properties.column_names", "contains", "user_id")
        sql, params = SQLiteGraphStore._render_contains_sqlite(
            clause.field, clause
        )
        # The ``CASE WHEN json_type(...) = 'array' THEN ... ELSE '[]' END``
        # guard coerces non-array values to an empty array so
        # ``json_each`` never raises on scalar properties.
        assert "json_type" in sql
        assert "= 'array'" in sql
        assert "ELSE '[]'" in sql
        assert "json_each" in sql
        assert "EXISTS" in sql
        assert "json_extract(properties_json, '$.column_names')" in sql
        assert params == ["user_id"]

    def test_contains_rejects_top_level_field(self) -> None:
        """SQLite top-level columns are TEXT — ``contains`` is nonsense there."""
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        clause = FilterClause("node_type", "contains", "service")
        with pytest.raises(ValueError, match=r"properties\.<key>"):
            SQLiteGraphStore._render_contains_sqlite(clause.field, clause)

    def test_contains_with_int_value_binds_correctly(self) -> None:
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        clause = FilterClause("properties.ids", "contains", 42)
        sql, params = SQLiteGraphStore._render_contains_sqlite(
            clause.field, clause
        )
        assert params == [42]
        # The scalar value lands as a parameter binding on
        # ``json_each.value = ?`` inside the EXISTS subquery.
        assert "json_each.value = ?" in sql


class TestPostgresContainsCompiler:
    """Pure-compile tests for the Postgres ``contains`` compiler path.

    Verifies the ``jsonb_typeof`` guard + ``@>`` containment shape.
    Behavioural coverage lives in the contract suite (env-gated).

    ``PostgresGraphStore`` pulls ``psycopg_pool`` at import; tests skip
    cleanly when the ``[postgres]`` optional extra isn't installed,
    mirroring the contract-suite gating pattern.
    """

    def test_contains_compiles_to_jsonb_typeof_plus_containment(self) -> None:
        pytest.importorskip(
            "psycopg_pool", reason="Postgres optional extras not installed"
        )
        import json as _json

        from trellis.stores.postgres.graph import PostgresGraphStore

        clause = FilterClause("properties.column_names", "contains", "user_id")
        sql, params = PostgresGraphStore._compile_properties_clause(clause)
        # The typeof guard rules out scalar-valued properties; the @>
        # containment carries the array-special-case.
        assert "jsonb_typeof(properties->'column_names') = 'array'" in sql
        assert "properties @> %s::jsonb" in sql
        # Nested-level @> requires the scalar wrapped in an array —
        # '{"a": ["x"]}' @> '{"a": "x"}' is FALSE in PostgreSQL.
        assert params == [_json.dumps({"column_names": ["user_id"]})]

    def test_contains_top_level_field_rejected(self) -> None:
        pytest.importorskip(
            "psycopg_pool", reason="Postgres optional extras not installed"
        )
        from trellis.stores.postgres.graph import PostgresGraphStore

        clause = FilterClause("node_type", "contains", "service")
        with pytest.raises(ValueError, match=r"properties\.<key>"):
            PostgresGraphStore._render_top_level_clause("node_type", clause)


class TestBoltOpenCypherContainsCompiler:
    """Pure-compile tests for the BoltOpenCypher ``contains`` predicate.

    Verifies the Python-side predicate behaviour without a live driver.
    """

    def test_contains_list_property_matches(self) -> None:
        from trellis.stores.bolt_opencypher.graph import (
            BoltOpenCypherGraphStore,
        )

        clause = FilterClause("properties.column_names", "contains", "user_id")
        pred = BoltOpenCypherGraphStore._compile_property_predicate(clause)
        assert pred({"properties": {"column_names": ["user_id", "email"]}}) is True
        assert pred({"properties": {"column_names": ["email"]}}) is False

    def test_contains_scalar_property_skipped(self) -> None:
        """Scalar property at the path must NOT match — list-only contract."""
        from trellis.stores.bolt_opencypher.graph import (
            BoltOpenCypherGraphStore,
        )

        clause = FilterClause("properties.team", "contains", "platform")
        pred = BoltOpenCypherGraphStore._compile_property_predicate(clause)
        assert pred({"properties": {"team": "platform"}}) is False

    def test_contains_missing_property_skipped(self) -> None:
        from trellis.stores.bolt_opencypher.graph import (
            BoltOpenCypherGraphStore,
        )

        clause = FilterClause("properties.column_names", "contains", "user_id")
        pred = BoltOpenCypherGraphStore._compile_property_predicate(clause)
        assert pred({"properties": {}}) is False
        assert pred({"properties": {"other_key": ["x"]}}) is False

    def test_contains_top_level_field_rejected(self) -> None:
        from trellis.stores.bolt_opencypher.graph import (
            BoltOpenCypherGraphStore,
        )

        clause = FilterClause("node_type", "contains", "service")
        with pytest.raises(ValueError, match=r"properties\.<key>"):
            BoltOpenCypherGraphStore._compile_native_cypher_clause(
                "n", "node_type", clause, 0
            )
