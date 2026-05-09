"""Order-of-magnitude performance + SCD-2 invariants for the bulk upsert path.

The SQLite ``upsert_nodes_bulk`` / ``upsert_edges_bulk`` methods used to
loop over the single-row variants with ``commit=True`` per row. That
path measured at ~32 nodes/sec on default hardware, dominated by per-row
fsync. PR #62 rewrote both into ``executemany`` + a single commit,
analogous to the Neo4j ``UNWIND`` fast path.

These tests lock in the rewrite:

* The 500-node bulk insert finishes in well under a second on default
  hardware (rough order-of-magnitude check, not a strict SLO — pinned
  loose at 1s so noisy CI doesn't go red).
* SCD-2 versioning still works correctly under batch mode: re-upserting
  a node closes the prior current row (sets ``valid_to``) and inserts a
  new current row, exactly like the per-row path.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def graph_store(tmp_path: Path):
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


# ------------------------------------------------------------------
# Performance — order-of-magnitude check
# ------------------------------------------------------------------


@pytest.mark.slow
def test_bulk_upsert_500_nodes_under_one_second(graph_store: SQLiteGraphStore):
    """500-node bulk insert must complete in <1s on default hardware.

    Per-row commit path measured at ~32 nodes/sec → 500 nodes ≈ 15s.
    The ``executemany`` rewrite hits ~6000 nodes/sec on the same hw,
    bringing 500 nodes to ~80ms with comfortable headroom under the
    1s budget. The threshold is deliberately loose — this is a
    regression guard against re-introducing per-row commits, not a
    perf SLO.
    """
    nodes = [
        {"node_id": f"n{i}", "node_type": "service", "properties": {"i": i}}
        for i in range(500)
    ]
    start = time.perf_counter()
    ids = graph_store.upsert_nodes_bulk(nodes)
    elapsed = time.perf_counter() - start

    assert len(ids) == 500
    assert graph_store.count_nodes() == 500
    assert elapsed < 1.0, (
        f"500-node bulk insert took {elapsed:.3f}s; expected <1s. "
        "Did the per-row commit loop creep back in?"
    )


@pytest.mark.slow
def test_bulk_upsert_500_edges_under_one_second(graph_store: SQLiteGraphStore):
    """500-edge bulk insert must complete in <1s on default hardware.

    Same regression guard as nodes — locks in the ``executemany``
    rewrite for the edge path.
    """
    nodes = [
        {"node_id": f"n{i}", "node_type": "service", "properties": {}}
        for i in range(501)
    ]
    graph_store.upsert_nodes_bulk(nodes)
    edges = [
        {
            "source_id": f"n{i}",
            "target_id": f"n{i + 1}",
            "edge_type": "depends_on",
        }
        for i in range(500)
    ]

    start = time.perf_counter()
    ids = graph_store.upsert_edges_bulk(edges)
    elapsed = time.perf_counter() - start

    assert len(ids) == 500
    assert graph_store.count_edges() == 500
    assert elapsed < 1.0, (
        f"500-edge bulk insert took {elapsed:.3f}s; expected <1s. "
        "Did the per-row commit loop creep back in?"
    )


# ------------------------------------------------------------------
# SCD-2 invariants under batch mode
# ------------------------------------------------------------------


def test_bulk_node_upsert_preserves_scd2_versioning(graph_store: SQLiteGraphStore):
    """Re-upserting a node via the bulk path closes the prior version
    and inserts a new current row — matching the per-row contract."""
    # First insert
    graph_store.upsert_nodes_bulk(
        [{"node_id": "n1", "node_type": "service", "properties": {"v": 1}}]
    )

    # Second insert — should create a new version, close the first
    graph_store.upsert_nodes_bulk(
        [{"node_id": "n1", "node_type": "service", "properties": {"v": 2}}]
    )

    history = graph_store.get_node_history("n1")
    assert len(history) == 2

    # Newest first
    newest, oldest = history[0], history[1]
    assert newest["properties"]["v"] == 2
    assert newest["valid_to"] is None
    assert oldest["properties"]["v"] == 1
    assert oldest["valid_to"] is not None  # closed
    # created_at carries forward from the prior version
    assert newest["created_at"] == oldest["created_at"]

    # count_nodes only sees the current row
    assert graph_store.count_nodes() == 1


def test_bulk_node_upsert_mixed_new_and_existing(graph_store: SQLiteGraphStore):
    """A single batch with both new and existing node_ids must close
    the existing nodes' prior versions while inserting fresh rows for
    the new ones."""
    # Seed two existing nodes
    graph_store.upsert_nodes_bulk(
        [
            {"node_id": "existing_a", "node_type": "service", "properties": {"v": 1}},
            {"node_id": "existing_b", "node_type": "service", "properties": {"v": 1}},
        ]
    )

    # Mixed batch: one update + two new
    graph_store.upsert_nodes_bulk(
        [
            {"node_id": "existing_a", "node_type": "service", "properties": {"v": 2}},
            {"node_id": "new_c", "node_type": "service", "properties": {"v": 1}},
            {"node_id": "existing_b", "node_type": "service", "properties": {"v": 2}},
            {"node_id": "new_d", "node_type": "service", "properties": {"v": 1}},
        ]
    )

    # Both existing nodes have two versions; new ones have one
    assert len(graph_store.get_node_history("existing_a")) == 2
    assert len(graph_store.get_node_history("existing_b")) == 2
    assert len(graph_store.get_node_history("new_c")) == 1
    assert len(graph_store.get_node_history("new_d")) == 1

    # All four are current
    assert graph_store.count_nodes() == 4

    # Updated nodes carry the latest payload
    assert graph_store.get_node("existing_a")["properties"]["v"] == 2
    assert graph_store.get_node("existing_b")["properties"]["v"] == 2


def test_bulk_edge_upsert_preserves_scd2_versioning(graph_store: SQLiteGraphStore):
    """Re-upserting an edge via the bulk path closes the prior version
    and reuses the same logical ``edge_id``."""
    graph_store.upsert_nodes_bulk(
        [
            {"node_id": "a", "node_type": "service", "properties": {}},
            {"node_id": "b", "node_type": "service", "properties": {}},
        ]
    )

    [eid_first] = graph_store.upsert_edges_bulk(
        [
            {
                "source_id": "a",
                "target_id": "b",
                "edge_type": "depends_on",
                "properties": {"w": 1.0},
            }
        ]
    )

    # Second insert against the same triplet — should reuse edge_id
    [eid_second] = graph_store.upsert_edges_bulk(
        [
            {
                "source_id": "a",
                "target_id": "b",
                "edge_type": "depends_on",
                "properties": {"w": 2.0},
            }
        ]
    )

    assert eid_second == eid_first
    assert graph_store.count_edges() == 1

    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["properties"]["w"] == 2.0
