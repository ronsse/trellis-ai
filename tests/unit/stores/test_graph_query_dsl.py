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
