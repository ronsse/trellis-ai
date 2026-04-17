"""Tests for SCD Type 2 temporal graph features."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.stores.graph import SQLiteGraphStore


@pytest.fixture
def graph_store(tmp_path: Path):
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


# ------------------------------------------------------------------
# Basic backward-compat (no as_of)
# ------------------------------------------------------------------


def test_basic_upsert_and_get(graph_store: SQLiteGraphStore):
    """Basic operations still work without as_of."""
    nid = graph_store.upsert_node(None, "service", {"name": "auth"})
    node = graph_store.get_node(nid)
    assert node is not None
    assert node["node_type"] == "service"
    assert node["properties"]["name"] == "auth"
    assert node["valid_from"] is not None
    assert node["valid_to"] is None


def test_basic_update_node(graph_store: SQLiteGraphStore):
    """Updating a node still returns the latest version by default."""
    graph_store.upsert_node("n1", "service", {"v": 1})
    graph_store.upsert_node("n1", "service", {"v": 2})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["v"] == 2
    assert node["valid_to"] is None


def test_basic_edges(graph_store: SQLiteGraphStore):
    """Edge operations still work without as_of."""
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    eid = graph_store.upsert_edge("a", "b", "depends_on", {"weight": 1.0})
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["edge_id"] == eid
    assert edges[0]["valid_from"] is not None
    assert edges[0]["valid_to"] is None


def test_basic_query(graph_store: SQLiteGraphStore):
    """Query still works without as_of."""
    graph_store.upsert_node(None, "service", {"name": "a"})
    graph_store.upsert_node(None, "person", {"name": "b"})
    results = graph_store.query(node_type="service")
    assert len(results) == 1


def test_basic_subgraph(graph_store: SQLiteGraphStore):
    """Subgraph traversal works without as_of."""
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("b", "c", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=2)
    assert len(sg["nodes"]) == 3
    assert len(sg["edges"]) == 2


def test_basic_counts(graph_store: SQLiteGraphStore):
    """Counts only include current versions."""
    graph_store.upsert_node("n1", "s", {"v": 1})
    assert graph_store.count_nodes() == 1
    # Update creates a new version, but count should still be 1
    graph_store.upsert_node("n1", "s", {"v": 2})
    assert graph_store.count_nodes() == 1


def test_basic_delete(graph_store: SQLiteGraphStore):
    """Delete removes all versions."""
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_node("a") is True
    assert graph_store.get_node("a") is None
    assert graph_store.get_edges("b") == []


def test_basic_get_nodes_bulk(graph_store: SQLiteGraphStore):
    """Bulk get returns current versions."""
    graph_store.upsert_node("a", "s", {"n": 1})
    graph_store.upsert_node("b", "s", {"n": 2})
    nodes = graph_store.get_nodes_bulk(["a", "b"])
    assert len(nodes) == 2


# ------------------------------------------------------------------
# Version creation
# ------------------------------------------------------------------


def test_upsert_creates_initial_version(graph_store: SQLiteGraphStore):
    """First upsert creates a version with valid_from set and valid_to=None."""
    nid = graph_store.upsert_node("n1", "concept", {"title": "hello"})
    node = graph_store.get_node(nid)
    assert node is not None
    assert node["valid_from"] is not None
    assert node["valid_to"] is None

    history = graph_store.get_node_history(nid)
    assert len(history) == 1
    assert history[0]["valid_to"] is None


def test_upsert_caps_old_version_creates_new(graph_store: SQLiteGraphStore):
    """Second upsert caps the old version and creates a new one."""
    graph_store.upsert_node("n1", "concept", {"v": 1})
    graph_store.upsert_node("n1", "concept", {"v": 2})

    history = graph_store.get_node_history("n1")
    assert len(history) == 2

    # History is newest first
    newest = history[0]
    oldest = history[1]

    assert newest["properties"]["v"] == 2
    assert newest["valid_to"] is None

    assert oldest["properties"]["v"] == 1
    assert oldest["valid_to"] is not None

    # The old version's valid_to should equal the new version's valid_from
    assert oldest["valid_to"] == newest["valid_from"]


def test_multiple_updates_create_history(graph_store: SQLiteGraphStore):
    """Multiple upserts build a full version chain."""
    for i in range(5):
        graph_store.upsert_node("n1", "concept", {"v": i})

    history = graph_store.get_node_history("n1")
    assert len(history) == 5
    # Only the newest should have valid_to=None
    assert history[0]["valid_to"] is None
    for old in history[1:]:
        assert old["valid_to"] is not None


# ------------------------------------------------------------------
# Time-travel queries (as_of)
# ------------------------------------------------------------------


def test_get_node_as_of(graph_store: SQLiteGraphStore):
    """get_node with as_of returns the version valid at that time."""
    # Create version 1 at a known time
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_node("n1", "concept", {"v": 1})

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_node("n1", "concept", {"v": 2})

    # Query at a time between t1 and t2 → should get v1
    mid = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    node = graph_store.get_node("n1", as_of=mid)
    assert node is not None
    assert node["properties"]["v"] == 1

    # Query at t2 or after → should get v2
    after = datetime(2025, 7, 1, 0, 0, 0, tzinfo=UTC)
    node = graph_store.get_node("n1", as_of=after)
    assert node is not None
    assert node["properties"]["v"] == 2

    # Query before t1 → should get None
    before = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    node = graph_store.get_node("n1", as_of=before)
    assert node is None


def test_get_nodes_bulk_as_of(graph_store: SQLiteGraphStore):
    """get_nodes_bulk respects as_of."""
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_node("a", "s", {"v": 1})

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_node("a", "s", {"v": 2})
        graph_store.upsert_node("b", "s", {"v": 10})

    mid = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    nodes = graph_store.get_nodes_bulk(["a", "b"], as_of=mid)
    # Only 'a' existed at mid; 'b' didn't yet
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "a"
    assert nodes[0]["properties"]["v"] == 1


def test_get_node_history_all_versions(graph_store: SQLiteGraphStore):
    """get_node_history returns all versions in newest-first order."""
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
    t3 = datetime(2025, 12, 1, 0, 0, 0, tzinfo=UTC)

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_node("n1", "concept", {"v": 1})
    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_node("n1", "concept", {"v": 2})
    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t3):
        graph_store.upsert_node("n1", "concept", {"v": 3})

    history = graph_store.get_node_history("n1")
    assert len(history) == 3
    assert [h["properties"]["v"] for h in history] == [3, 2, 1]


def test_get_edges_as_of(graph_store: SQLiteGraphStore):
    """get_edges with as_of returns edges valid at that time."""
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        graph_store.upsert_edge("a", "b", "links", {"w": 1})

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_edge("a", "b", "links", {"w": 2})

    mid = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    edges = graph_store.get_edges("a", direction="outgoing", as_of=mid)
    assert len(edges) == 1
    assert edges[0]["properties"]["w"] == 1

    after = datetime(2025, 7, 1, 0, 0, 0, tzinfo=UTC)
    edges = graph_store.get_edges("a", direction="outgoing", as_of=after)
    assert len(edges) == 1
    assert edges[0]["properties"]["w"] == 2


def test_get_subgraph_as_of(graph_store: SQLiteGraphStore):
    """get_subgraph with as_of only returns nodes/edges valid at that time."""
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        graph_store.upsert_edge("a", "b", "links")

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_node("c", "s", {})
        graph_store.upsert_edge("b", "c", "links")

    # Before t2, only a and b exist
    mid = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    sg = graph_store.get_subgraph(["a"], depth=3, as_of=mid)
    node_ids = {n["node_id"] for n in sg["nodes"]}
    assert node_ids == {"a", "b"}
    assert len(sg["edges"]) == 1

    # After t2, all three exist
    after = datetime(2025, 7, 1, 0, 0, 0, tzinfo=UTC)
    sg = graph_store.get_subgraph(["a"], depth=3, as_of=after)
    node_ids = {n["node_id"] for n in sg["nodes"]}
    assert node_ids == {"a", "b", "c"}
    assert len(sg["edges"]) == 2


def test_query_as_of(graph_store: SQLiteGraphStore):
    """query with as_of only returns nodes valid at that time."""
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_node("a", "service", {"v": 1})

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_node("a", "service", {"v": 2})
        graph_store.upsert_node("b", "service", {"v": 10})

    mid = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    results = graph_store.query(node_type="service", as_of=mid)
    assert len(results) == 1
    assert results[0]["node_id"] == "a"
    assert results[0]["properties"]["v"] == 1


def test_node_history_nonexistent(graph_store: SQLiteGraphStore):
    """get_node_history for non-existent node returns empty list."""
    history = graph_store.get_node_history("nonexistent")
    assert history == []


def test_resolve_alias_as_of(graph_store: SQLiteGraphStore):
    """Alias resolution respects temporal versions."""
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t1):
        graph_store.upsert_alias(
            "orders_entity",
            "unity_catalog",
            "main.analytics.orders",
            raw_name="orders_v1",
            match_confidence=0.7,
        )

    with patch("trellis.stores.sqlite.graph.utc_now", return_value=t2):
        graph_store.upsert_alias(
            "orders_entity",
            "unity_catalog",
            "main.analytics.orders",
            raw_name="orders_v2",
            match_confidence=0.9,
        )

    mid = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    alias = graph_store.resolve_alias(
        "unity_catalog", "main.analytics.orders", as_of=mid
    )
    assert alias is not None
    assert alias["raw_name"] == "orders_v1"
    assert alias["match_confidence"] == 0.7

    after = datetime(2025, 7, 1, 0, 0, 0, tzinfo=UTC)
    alias = graph_store.resolve_alias(
        "unity_catalog", "main.analytics.orders", as_of=after
    )
    assert alias is not None
    assert alias["raw_name"] == "orders_v2"
    assert alias["match_confidence"] == 0.9


# ------------------------------------------------------------------
# Migration from v1 schema
# ------------------------------------------------------------------


def test_migration_from_v1(tmp_path: Path):
    """A v1 database is migrated to v2 on init."""
    db_path = tmp_path / "v1.db"

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            properties_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            properties_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        INSERT INTO nodes VALUES ('n1', 'concept', '{"title":"hello"}',
                                  '2025-01-01T00:00:00+00:00',
                                  '2025-01-01T00:00:00+00:00');
        INSERT INTO edges VALUES ('e1', 'n1', 'n1', 'self_ref', '{}',
                                  '2025-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    # Open with SQLiteGraphStore — should trigger migration
    store = SQLiteGraphStore(db_path)

    node = store.get_node("n1")
    assert node is not None
    assert node["properties"]["title"] == "hello"
    assert node["valid_from"] == "2025-01-01T00:00:00+00:00"
    assert node["valid_to"] is None

    edges = store.get_edges("n1")
    assert len(edges) == 1
    assert edges[0]["valid_from"] == "2025-01-01T00:00:00+00:00"
    assert edges[0]["valid_to"] is None

    store.close()
