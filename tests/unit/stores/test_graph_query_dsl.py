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
