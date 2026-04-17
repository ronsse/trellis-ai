"""Tests for GraphStore ABC and SQLiteGraphStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.stores.graph import SQLiteGraphStore


@pytest.fixture
def graph_store(tmp_path: Path):
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


def test_upsert_and_get_node(graph_store):
    nid = graph_store.upsert_node(None, "service", {"name": "auth"})
    node = graph_store.get_node(nid)
    assert node is not None
    assert node["node_type"] == "service"
    assert node["properties"]["name"] == "auth"


def test_upsert_node_with_explicit_id(graph_store):
    graph_store.upsert_node("n1", "person", {"name": "Alice"})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["name"] == "Alice"


def test_update_node(graph_store):
    graph_store.upsert_node("n1", "service", {"v": 1})
    graph_store.upsert_node("n1", "service", {"v": 2})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["v"] == 2


def test_upsert_and_get_edge(graph_store):
    graph_store.upsert_node("a", "service", {})
    graph_store.upsert_node("b", "service", {})
    eid = graph_store.upsert_edge("a", "b", "depends_on", {"weight": 1.0})
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "depends_on"
    assert edges[0]["edge_id"] == eid


def test_get_edges_incoming(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    edges = graph_store.get_edges("b", direction="incoming")
    assert len(edges) == 1


def test_get_edges_both(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("c", "b", "links_to")
    edges = graph_store.get_edges("b", direction="both")
    assert len(edges) == 2


def test_get_subgraph(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("b", "c", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=2)
    assert len(sg["nodes"]) == 3
    assert len(sg["edges"]) == 2


def test_query_by_type(graph_store):
    graph_store.upsert_node(None, "service", {"name": "a"})
    graph_store.upsert_node(None, "person", {"name": "b"})
    results = graph_store.query(node_type="service")
    assert len(results) == 1


def test_query_by_properties(graph_store):
    graph_store.upsert_node(None, "service", {"team": "platform"})
    graph_store.upsert_node(None, "service", {"team": "data"})
    results = graph_store.query(properties={"team": "platform"})
    assert len(results) == 1


def test_delete_node_cascades(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_node("a") is True
    assert graph_store.get_node("a") is None
    assert graph_store.get_edges("b") == []


def test_delete_edge(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    eid = graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_edge(eid) is True
    assert graph_store.get_edges("a") == []


def test_get_nodes_bulk(graph_store):
    graph_store.upsert_node("a", "s", {"n": 1})
    graph_store.upsert_node("b", "s", {"n": 2})
    graph_store.upsert_node("c", "s", {"n": 3})
    nodes = graph_store.get_nodes_bulk(["a", "c"])
    assert len(nodes) == 2


def test_count(graph_store):
    assert graph_store.count_nodes() == 0
    graph_store.upsert_node(None, "s", {})
    assert graph_store.count_nodes() == 1
    assert graph_store.count_edges() == 0


def test_upsert_and_resolve_alias(graph_store):
    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})
    alias_id = graph_store.upsert_alias(
        "orders_entity",
        "unity_catalog",
        "main.analytics.orders",
        raw_name="orders",
        match_confidence=0.95,
        is_primary=True,
    )
    assert alias_id is not None

    alias = graph_store.resolve_alias("unity_catalog", "main.analytics.orders")
    assert alias is not None
    assert alias["entity_id"] == "orders_entity"
    assert alias["raw_name"] == "orders"
    assert alias["match_confidence"] == 0.95
    assert alias["is_primary"] is True


def test_get_aliases_for_entity(graph_store):
    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})
    graph_store.upsert_alias("orders_entity", "unity_catalog", "main.analytics.orders")
    graph_store.upsert_alias("orders_entity", "dbt", "model.project.orders")

    aliases = graph_store.get_aliases("orders_entity")
    assert len(aliases) == 2
    assert {(alias["source_system"], alias["raw_id"]) for alias in aliases} == {
        ("unity_catalog", "main.analytics.orders"),
        ("dbt", "model.project.orders"),
    }


def test_get_nonexistent(graph_store):
    assert graph_store.get_node("nope") is None


def test_delete_nonexistent(graph_store):
    assert graph_store.delete_node("nope") is False
    assert graph_store.delete_edge("nope") is False


# ---------------------------------------------------------------------------
# node_role / generation_spec (v3 additive schema)
# ---------------------------------------------------------------------------


class TestNodeRole:
    """node_role / generation_spec round-trip and validation."""

    def test_default_node_role_is_semantic(self, graph_store):
        graph_store.upsert_node("n1", "service", {"name": "auth"})
        node = graph_store.get_node("n1")
        assert node is not None
        assert node["node_role"] == "semantic"
        assert node["generation_spec"] is None

    def test_structural_node_round_trip(self, graph_store):
        graph_store.upsert_node(
            "col1",
            "uc_column",
            {"table": "orders", "dtype": "int"},
            node_role="structural",
        )
        node = graph_store.get_node("col1")
        assert node is not None
        assert node["node_role"] == "structural"
        assert node["generation_spec"] is None

    def test_curated_node_round_trip(self, graph_store):
        spec = {
            "generator_name": "community_detection_louvain",
            "generator_version": "1.0.0",
            "generated_at": "2026-04-11T00:00:00+00:00",
            "source_node_ids": ["a", "b"],
            "source_trace_ids": [],
            "parameters": {"resolution": 1.2},
        }
        graph_store.upsert_node(
            "cluster1",
            "domain",
            {"name": "payments"},
            node_role="curated",
            generation_spec=spec,
        )
        node = graph_store.get_node("cluster1")
        assert node is not None
        assert node["node_role"] == "curated"
        assert node["generation_spec"] is not None
        gen = node["generation_spec"]
        assert gen["generator_name"] == "community_detection_louvain"
        assert gen["parameters"] == {"resolution": 1.2}

    def test_curated_without_spec_is_rejected(self, graph_store):
        with pytest.raises(ValueError, match="generation_spec is required"):
            graph_store.upsert_node(
                "c1",
                "domain",
                {"name": "x"},
                node_role="curated",
            )

    def test_spec_on_non_curated_is_rejected(self, graph_store):
        spec = {"generator_name": "x", "generator_version": "1"}
        with pytest.raises(ValueError, match="generation_spec must be None"):
            graph_store.upsert_node(
                "n1",
                "service",
                {},
                generation_spec=spec,
            )

    def test_invalid_role_is_rejected(self, graph_store):
        with pytest.raises(ValueError, match="Invalid node_role"):
            graph_store.upsert_node(
                "n1",
                "service",
                {},
                node_role="bogus",
            )

    def test_node_role_is_immutable_across_versions(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        # Same role — OK
        graph_store.upsert_node("n1", "service", {"v": 2})
        # Changing role — rejected
        with pytest.raises(ValueError, match="Cannot change node_role"):
            graph_store.upsert_node(
                "n1",
                "service",
                {"v": 3},
                node_role="structural",
            )

    def test_structural_history_preserves_role(self, graph_store):
        graph_store.upsert_node(
            "s1",
            "uc_column",
            {"dtype": "int"},
            node_role="structural",
        )
        graph_store.upsert_node(
            "s1",
            "uc_column",
            {"dtype": "bigint"},
            node_role="structural",
        )
        history = graph_store.get_node_history("s1")
        assert len(history) == 2
        assert all(v["node_role"] == "structural" for v in history)

    def test_query_returns_node_role(self, graph_store):
        graph_store.upsert_node("s1", "uc_column", {"x": 1}, node_role="structural")
        graph_store.upsert_node("s2", "service", {"x": 1})
        results = graph_store.query(properties={"x": 1})
        roles = {r["node_id"]: r["node_role"] for r in results}
        assert roles["s1"] == "structural"
        assert roles["s2"] == "semantic"

    def test_subgraph_includes_node_role(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("col", "uc_column", {}, node_role="structural")
        graph_store.upsert_edge("a", "col", "has_column")
        sg = graph_store.get_subgraph(["a"], depth=1)
        roles = {n["node_id"]: n["node_role"] for n in sg["nodes"]}
        assert roles["a"] == "semantic"
        assert roles["col"] == "structural"
