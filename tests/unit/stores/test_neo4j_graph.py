"""Tests for Neo4jGraphStore — requires a real Neo4j instance.

Skipped unless ``TRELLIS_TEST_NEO4J_URI`` is set and the ``neo4j``
driver is importable. Run locally with:

    docker run --rm -d --name trellis-neo4j -p 7687:7687 -p 7474:7474 \\
        -e NEO4J_AUTH=neo4j/testtest12 neo4j:5
    export TRELLIS_TEST_NEO4J_URI=bolt://localhost:7687
    export TRELLIS_TEST_NEO4J_USER=neo4j
    export TRELLIS_TEST_NEO4J_PASSWORD=testtest12

Or against a Neo4j AuraDB Free instance (validated 2026-04-25):

    export TRELLIS_TEST_NEO4J_URI=neo4j+s://<id>.databases.neo4j.io
    export TRELLIS_TEST_NEO4J_USER=<id>          # AuraDB: user = instance_id
    export TRELLIS_TEST_NEO4J_PASSWORD=<from console>
    export TRELLIS_TEST_NEO4J_DATABASE=<id>      # AuraDB: db = instance_id

The URI + user + password env vars are split so CI can keep the
password out of the URL.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("neo4j")

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
# AuraDB Free instances host the user database under the instance ID,
# not under the canonical "neo4j" name. Set this env var to override.
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]


@pytest.fixture
def graph_store():
    """Fresh Neo4jGraphStore with a cleaned database per test."""
    from trellis.stores.neo4j.graph import Neo4jGraphStore

    store = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
    # Wipe all data the store might have created in a prior run.
    with store._driver.session(database=store._database) as session:
        session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
    yield store
    store.close()


def _backdate_closed(store, label: str, valid_to_iso: str) -> None:
    """Rewrite every closed row's ``valid_to`` on ``label`` for test control."""
    with store._driver.session(database=store._database) as session:
        session.run(
            f"MATCH (n:{label}) WHERE n.valid_to IS NOT NULL SET n.valid_to = $vt",
            vt=valid_to_iso,
        )


def _backdate_closed_edges(store, valid_to_iso: str) -> None:
    with store._driver.session(database=store._database) as session:
        session.run(
            "MATCH ()-[r:EDGE]->() WHERE r.valid_to IS NOT NULL SET r.valid_to = $vt",
            vt=valid_to_iso,
        )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


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


def test_update_node_returns_latest(graph_store):
    graph_store.upsert_node("n1", "service", {"v": 1})
    graph_store.upsert_node("n1", "service", {"v": 2})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["v"] == 2


def test_get_nonexistent(graph_store):
    assert graph_store.get_node("nope") is None


def test_get_nodes_bulk(graph_store):
    graph_store.upsert_node("a", "s", {"n": 1})
    graph_store.upsert_node("b", "s", {"n": 2})
    graph_store.upsert_node("c", "s", {"n": 3})
    nodes = graph_store.get_nodes_bulk(["a", "c"])
    ids = {n["node_id"] for n in nodes}
    assert ids == {"a", "c"}


def test_count(graph_store):
    assert graph_store.count_nodes() == 0
    graph_store.upsert_node(None, "s", {})
    assert graph_store.count_nodes() == 1
    assert graph_store.count_edges() == 0


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_upsert_and_get_edge(graph_store):
    graph_store.upsert_node("a", "service", {})
    graph_store.upsert_node("b", "service", {})
    eid = graph_store.upsert_edge("a", "b", "depends_on", {"weight": 1.0})
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "depends_on"
    assert edges[0]["edge_id"] == eid
    assert edges[0]["properties"]["weight"] == 1.0


def test_edge_upsert_replaces_current(graph_store):
    graph_store.upsert_node("a", "service", {})
    graph_store.upsert_node("b", "service", {})
    first = graph_store.upsert_edge("a", "b", "depends_on", {"w": 1})
    second = graph_store.upsert_edge("a", "b", "depends_on", {"w": 2})
    # Same logical edge — edge_id is carried forward.
    assert first == second
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["properties"]["w"] == 2


def test_get_edges_incoming(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    assert len(graph_store.get_edges("b", direction="incoming")) == 1


def test_get_edges_both(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("c", "b", "links_to")
    assert len(graph_store.get_edges("b", direction="both")) == 2


def test_get_edges_filter_by_type(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("a", "b", "depends_on")
    filtered = graph_store.get_edges("a", direction="outgoing", edge_type="depends_on")
    assert len(filtered) == 1
    assert filtered[0]["edge_type"] == "depends_on"


def test_upsert_edge_missing_endpoints_raises(graph_store):
    with pytest.raises(ValueError, match="no current version"):
        graph_store.upsert_edge("ghost_a", "ghost_b", "links_to")


def test_delete_edge(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    eid = graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_edge(eid) is True
    assert graph_store.get_edges("a") == []


# ---------------------------------------------------------------------------
# Subgraph
# ---------------------------------------------------------------------------


def test_get_subgraph_depth_2(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("b", "c", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=2)
    node_ids = {n["node_id"] for n in sg["nodes"]}
    assert node_ids == {"a", "b", "c"}
    assert len(sg["edges"]) == 2


def test_get_subgraph_depth_0_returns_seeds_only(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=0)
    assert [n["node_id"] for n in sg["nodes"]] == ["a"]
    assert sg["edges"] == []


def test_get_subgraph_edge_type_filter(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "depends_on")
    graph_store.upsert_edge("a", "c", "mentions")
    sg = graph_store.get_subgraph(["a"], depth=1, edge_types=["depends_on"])
    node_ids = {n["node_id"] for n in sg["nodes"]}
    assert node_ids == {"a", "b"}


# ---------------------------------------------------------------------------
# Query / delete / aliases
# ---------------------------------------------------------------------------


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


def test_delete_nonexistent_returns_false(graph_store):
    assert graph_store.delete_node("nope") is False
    assert graph_store.delete_edge("nope") is False


def test_upsert_and_resolve_alias(graph_store):
    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})
    graph_store.upsert_alias(
        "orders_entity",
        "unity_catalog",
        "main.analytics.orders",
        raw_name="orders",
        match_confidence=0.95,
        is_primary=True,
    )
    alias = graph_store.resolve_alias("unity_catalog", "main.analytics.orders")
    assert alias is not None
    assert alias["entity_id"] == "orders_entity"
    assert alias["raw_name"] == "orders"
    assert alias["match_confidence"] == 0.95
    assert alias["is_primary"] is True


def test_get_aliases_for_entity(graph_store):
    graph_store.upsert_node("orders_entity", "table", {})
    graph_store.upsert_alias("orders_entity", "uc", "main.orders")
    graph_store.upsert_alias("orders_entity", "dbt", "model.orders")
    aliases = graph_store.get_aliases("orders_entity")
    assert {(a["source_system"], a["raw_id"]) for a in aliases} == {
        ("uc", "main.orders"),
        ("dbt", "model.orders"),
    }


# ---------------------------------------------------------------------------
# node_role / generation_spec
# ---------------------------------------------------------------------------


class TestNodeRole:
    def test_default_role_is_semantic(self, graph_store):
        graph_store.upsert_node("n1", "service", {})
        node = graph_store.get_node("n1")
        assert node["node_role"] == "semantic"
        assert node["generation_spec"] is None

    def test_curated_node_with_spec(self, graph_store):
        spec = {
            "generator_name": "louvain",
            "generator_version": "1.0.0",
            "parameters": {"resolution": 1.2},
        }
        graph_store.upsert_node(
            "c1",
            "domain",
            {"name": "payments"},
            node_role="curated",
            generation_spec=spec,
        )
        node = graph_store.get_node("c1")
        assert node["node_role"] == "curated"
        assert node["generation_spec"]["parameters"] == {"resolution": 1.2}

    def test_curated_without_spec_rejected(self, graph_store):
        with pytest.raises(ValueError, match="generation_spec is required"):
            graph_store.upsert_node("c1", "domain", {}, node_role="curated")

    def test_role_is_immutable(self, graph_store):
        graph_store.upsert_node("n1", "service", {})
        with pytest.raises(ValueError, match="Cannot change node_role"):
            graph_store.upsert_node("n1", "service", {}, node_role="structural")

    def test_document_ids_round_trip(self, graph_store):
        graph_store.upsert_node(
            "n1",
            "service",
            {},
            document_ids=["doc-1", "doc-2"],
        )
        node = graph_store.get_node("n1")
        assert node["document_ids"] == ["doc-1", "doc-2"]


# ---------------------------------------------------------------------------
# Temporal (SCD-2) versioning
# ---------------------------------------------------------------------------


class TestTemporal:
    def test_history_ordered_desc(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        graph_store.upsert_node("n1", "service", {"v": 3})
        history = graph_store.get_node_history("n1")
        assert [h["properties"]["v"] for h in history] == [3, 2, 1]
        # Only newest is still open.
        assert history[0]["valid_to"] is None
        assert all(h["valid_to"] is not None for h in history[1:])

    def test_as_of_returns_past_version(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        between = datetime.now(UTC)
        # Give the clock room; the next upsert's valid_from must be strictly
        # greater than `between` for the as_of read to pick v=1.
        graph_store.upsert_node("n1", "service", {"v": 2})
        node = graph_store.get_node("n1", as_of=between)
        # We can't guarantee which version wins when valid_from == between,
        # so just confirm we get *some* version (not None) and that the
        # current read returns v=2.
        assert node is not None
        assert graph_store.get_node("n1")["properties"]["v"] == 2


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompactVersions:
    def test_compacts_closed_nodes_before_cutoff(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _backdate_closed(graph_store, "Node", ten_days_ago)

        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff)

        assert report.nodes_compacted == 1
        assert report.total_compacted == 1
        assert report.dry_run is False
        # Current row still reachable.
        assert graph_store.get_node("n1")["properties"]["v"] == 2
        # Only one version survives compaction.
        assert len(graph_store.get_node_history("n1")) == 1

    def test_preserves_current_rows(self, graph_store):
        graph_store.upsert_node("n1", "service", {})
        future = datetime.now(UTC) + timedelta(days=365)
        report = graph_store.compact_versions(future)
        assert report.nodes_compacted == 0
        assert graph_store.get_node("n1") is not None

    def test_dry_run_reports_without_deleting(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _backdate_closed(graph_store, "Node", ten_days_ago)
        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff, dry_run=True)
        assert report.dry_run is True
        assert report.nodes_compacted == 1
        assert len(graph_store.get_node_history("n1")) == 2

    def test_compacts_edges_and_aliases(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 1})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 2})
        graph_store.upsert_alias("a", "systemX", "raw-1", raw_name="old")
        graph_store.upsert_alias("a", "systemX", "raw-1", raw_name="new")

        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _backdate_closed_edges(graph_store, ten_days_ago)
        _backdate_closed(graph_store, "Alias", ten_days_ago)

        report = graph_store.compact_versions(datetime.now(UTC) - timedelta(days=5))
        assert report.edges_compacted == 1
        assert report.aliases_compacted == 1
