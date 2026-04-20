"""Verify search strategies honour ParameterRegistry overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.ops import ParameterRegistry
from trellis.retrieve.strategies import (
    GRAPH_CURATED_BOOST,
    GRAPH_DESCRIPTION_BOOST,
    GRAPH_DOMAIN_MATCH_BOOST,
    GRAPH_POSITION_DECAY_STEP,
    GraphSearch,
    KeywordSearch,
)
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.parameter import SQLiteParameterStore


@pytest.fixture
def param_store(tmp_path: Path):
    s = SQLiteParameterStore(tmp_path / "parameters.db")
    yield s
    s.close()


class _FakeDocStore:
    """Minimal stand-in that returns a fixed result set with a timestamp."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def search(
        self, query: str, *, limit: int = 20, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        del query, limit, filters
        return list(self._docs)


class _FakeGraphStore:
    """Minimal stand-in for GraphStore.query."""

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes

    def query(
        self,
        *,
        node_type: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        del node_type, properties, limit
        return list(self._nodes)


def test_keyword_search_defaults_unchanged_without_registry():
    doc = {
        "doc_id": "d1",
        "rank": 1.0,
        "content": "hello",
        "metadata": {},
        "updated_at": "2026-04-20T00:00:00+00:00",
    }
    strategy = KeywordSearch(_FakeDocStore([doc]))
    items = strategy.search("hello")
    assert len(items) == 1
    assert items[0].item_id == "d1"
    # Recency decay applied with default half-life; score < base
    assert items[0].relevance_score <= 1.0


def test_keyword_search_honours_registry_half_life(
    param_store: SQLiteParameterStore,
):
    reg = ParameterRegistry(param_store)
    param_store.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch",
                domain="sportsbook",
            ),
            # Setting a very short half-life causes steep decay for an
            # old document — score drops far below the no-override case.
            values={"recency_half_life_days": 0.001, "recency_floor": 0.0},
        )
    )
    doc = {
        "doc_id": "d1",
        "rank": 1.0,
        "content": "hello",
        "metadata": {},
        "updated_at": "2000-01-01T00:00:00+00:00",
    }
    baseline = KeywordSearch(_FakeDocStore([doc])).search(
        "hello", filters={"domain": "sportsbook"}
    )
    tuned = KeywordSearch(_FakeDocStore([doc]), registry=reg).search(
        "hello", filters={"domain": "sportsbook"}
    )
    assert tuned[0].relevance_score < baseline[0].relevance_score


def test_graph_search_domain_match_boost_overridable(
    param_store: SQLiteParameterStore,
):
    reg = ParameterRegistry(param_store)
    nodes = [
        {
            "node_id": "n1",
            "node_type": "entity",
            "node_role": "semantic",
            "properties": {"domain": "sportsbook", "name": "match-a"},
        },
    ]
    # Baseline uses hardcoded GRAPH_DOMAIN_MATCH_BOOST (1.3).
    baseline = GraphSearch(_FakeGraphStore(nodes)).search(
        "irrelevant", filters={"domain": "sportsbook"}
    )
    # Override the domain-match boost down to 1.0 (neutral).
    param_store.put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.GraphSearch",
                domain="sportsbook",
            ),
            values={"domain_match_boost": 1.0},
        )
    )
    tuned = GraphSearch(_FakeGraphStore(nodes), registry=reg).search(
        "irrelevant", filters={"domain": "sportsbook"}
    )
    # Same node, lower boost → lower score.
    assert tuned[0].relevance_score < baseline[0].relevance_score


def test_graph_search_defaults_unchanged_without_registry():
    nodes = [
        {
            "node_id": "n1",
            "node_type": "entity",
            "node_role": "curated",
            "properties": {"name": "x", "description": "hello"},
        },
    ]
    items = GraphSearch(_FakeGraphStore(nodes)).search("q")
    assert len(items) == 1


def test_graph_constants_exported_and_unchanged():
    # The module-level constants are the fallback defaults — callers
    # inspecting them should see the historical values unchanged.
    assert GRAPH_DOMAIN_MATCH_BOOST == 1.3
    assert GRAPH_CURATED_BOOST == 1.3
    assert GRAPH_DESCRIPTION_BOOST == 1.2
    assert GRAPH_POSITION_DECAY_STEP == 0.05
