"""Tests for ArcadeDBVectorStore (shape #2 — embedding on :Node).

Skipped unless ``TRELLIS_TEST_ARCADEDB_URI`` is set and the ``neo4j``
driver (used by the paired graph store) is importable.

ArcadeDB's vector store, like Neo4j's, attaches embeddings as
``LIST OF FLOAT`` properties on graph-store ``:Node`` rows, so every
test creates a node via :class:`ArcadeDBGraphStore` first and then
attaches a vector via :class:`ArcadeDBVectorStore`. This pairing
follows the established Neo4j shape-#2 test pattern (see
``test_neo4j_vector.py``) — neither vector store satisfies the
independent ``VectorStoreContractTests`` suite because their upserts
require a pre-existing graph row.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("neo4j")

URI = os.environ.get("TRELLIS_TEST_ARCADEDB_URI", "")
USER = os.environ.get("TRELLIS_TEST_ARCADEDB_USER", "root")
PASSWORD = os.environ.get("TRELLIS_TEST_ARCADEDB_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_ARCADEDB_DATABASE", "trellis_vector_test")
HTTP_URL = os.environ.get("TRELLIS_TEST_ARCADEDB_HTTP_URL", "http://localhost:2480")

pytestmark = [
    pytest.mark.arcadedb,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_ARCADEDB_URI not set"),
]


@pytest.fixture
def stores():
    """Yield ``(graph_store, vector_store)`` sharing the same ArcadeDB database."""
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
    from trellis.stores.arcadedb.vector import ArcadeDBVectorStore

    graph = ArcadeDBGraphStore(
        URI,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        http_url=HTTP_URL,
        ensure_database_exists=True,
    )
    vector = ArcadeDBVectorStore(
        http_url=HTTP_URL,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        dimensions=3,
        index_name="trellis_test_node_embeddings",
    )
    # Wipe nodes/aliases between tests so the vector + graph state is
    # deterministic.
    with graph._driver.session(database=graph._database) as session:
        session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
    yield graph, vector
    vector.close()
    graph.close()


def _vec(x: float, y: float, z: float) -> list[float]:
    return [x, y, z]


def _make_node(graph, node_id: str) -> str:
    return graph.upsert_node(node_id, "doc", {})


# ----------------------------------------------------------------------
# Upsert + get round-trip
# ----------------------------------------------------------------------


def test_upsert_then_get_roundtrips_vector(stores):
    graph, vector = stores
    _make_node(graph, "a")
    vector.upsert("a", _vec(0.1, 0.2, 0.3), metadata={"kind": "doc"})
    got = vector.get("a")
    assert got is not None
    assert got["item_id"] == "a"
    assert got["dimensions"] == 3
    for actual, expected in zip(got["vector"], _vec(0.1, 0.2, 0.3), strict=True):
        assert abs(actual - expected) < 1e-5
    assert got["metadata"] == {"kind": "doc"}


def test_upsert_with_no_metadata_yields_empty_dict(stores):
    graph, vector = stores
    _make_node(graph, "a")
    vector.upsert("a", _vec(1, 0, 0))
    got = vector.get("a")
    assert got is not None
    assert got["metadata"] == {}


def test_upsert_replaces_existing(stores):
    graph, vector = stores
    _make_node(graph, "a")
    vector.upsert("a", _vec(0.1, 0.2, 0.3), metadata={"v": 1})
    vector.upsert("a", _vec(0.4, 0.5, 0.6), metadata={"v": 2})
    got = vector.get("a")
    assert got is not None
    assert got["metadata"] == {"v": 2}
    for actual, expected in zip(got["vector"], _vec(0.4, 0.5, 0.6), strict=True):
        assert abs(actual - expected) < 1e-5


# ----------------------------------------------------------------------
# Missing node
# ----------------------------------------------------------------------


def test_upsert_without_node_raises(stores):
    _graph, vector = stores
    with pytest.raises(ValueError, match="has no current version"):
        vector.upsert("ghost", _vec(0.1, 0.2, 0.3))


def test_upsert_bulk_propagates_missing_node(stores):
    graph, vector = stores
    _make_node(graph, "a")
    with pytest.raises(ValueError, match=r"upsert_bulk\[1\].*has no current version"):
        vector.upsert_bulk(
            [
                {"item_id": "a", "vector": _vec(0.1, 0.2, 0.3)},
                {"item_id": "ghost", "vector": _vec(0.9, 0.8, 0.7)},
            ]
        )


# ----------------------------------------------------------------------
# Bulk upsert
# ----------------------------------------------------------------------


def test_upsert_bulk_attaches_all(stores):
    graph, vector = stores
    for nid in ("a", "b", "c"):
        _make_node(graph, nid)
    vector.upsert_bulk(
        [
            {"item_id": "a", "vector": _vec(0.1, 0.2, 0.3)},
            {"item_id": "b", "vector": _vec(0.9, 0.8, 0.7)},
            {"item_id": "c", "vector": _vec(0.1, 0.21, 0.31), "metadata": {"k": "v"}},
        ]
    )
    assert vector.count() == 3
    got_c = vector.get("c")
    assert got_c is not None
    assert got_c["metadata"] == {"k": "v"}


def test_upsert_bulk_rejects_duplicate_item_ids(stores):
    graph, vector = stores
    _make_node(graph, "a")
    with pytest.raises(ValueError, match=r"upsert_bulk\[1\].*duplicate"):
        vector.upsert_bulk(
            [
                {"item_id": "a", "vector": _vec(0.1, 0.2, 0.3)},
                {"item_id": "a", "vector": _vec(0.9, 0.8, 0.7)},
            ]
        )


def test_upsert_bulk_rejects_dimension_mismatch(stores):
    graph, vector = stores
    _make_node(graph, "a")
    with pytest.raises(ValueError, match=r"upsert_bulk\[0\].*dimensions"):
        vector.upsert_bulk([{"item_id": "a", "vector": [0.1, 0.2]}])


# ----------------------------------------------------------------------
# Query (similarity search)
# ----------------------------------------------------------------------


def test_query_returns_most_similar(stores):
    graph, vector = stores
    for nid in ("a", "b", "c"):
        _make_node(graph, nid)
    vector.upsert("a", _vec(1.0, 0.0, 0.0))
    vector.upsert("b", _vec(0.0, 1.0, 0.0))
    vector.upsert("c", _vec(0.0, 0.0, 1.0))
    results = vector.query(_vec(1.0, 0.0, 0.0), top_k=2)
    assert len(results) >= 1
    assert results[0]["item_id"] == "a"
    # Scores are descending — best match first.
    if len(results) >= 2:
        assert results[0]["score"] >= results[1]["score"]


def test_query_respects_top_k(stores):
    graph, vector = stores
    for i in range(5):
        _make_node(graph, f"n{i}")
        vector.upsert(f"n{i}", _vec(float(i + 1), 0.1 * (i + 1), 0.0))
    # Query vector is non-zero — ArcadeDB rejects zero query vectors
    # for cosine similarity (undefined: 0/0).
    results = vector.query(_vec(1.0, 0.1, 0.0), top_k=2)
    assert len(results) <= 2


def test_query_with_metadata_filter(stores):
    graph, vector = stores
    for nid in ("a", "b"):
        _make_node(graph, nid)
    vector.upsert("a", _vec(0.1, 0.2, 0.3), metadata={"kind": "doc"})
    vector.upsert("b", _vec(0.4, 0.5, 0.6), metadata={"kind": "code"})
    results = vector.query(_vec(0.1, 0.2, 0.3), top_k=10, filters={"kind": "doc"})
    assert all(r["metadata"]["kind"] == "doc" for r in results)


def test_query_rejects_dimension_mismatch(stores):
    _graph, vector = stores
    with pytest.raises(ValueError, match="dimensions"):
        vector.query([0.1, 0.2])


# ----------------------------------------------------------------------
# Delete + count
# ----------------------------------------------------------------------


def test_delete_returns_true_when_existed(stores):
    graph, vector = stores
    _make_node(graph, "a")
    vector.upsert("a", _vec(0.1, 0.2, 0.3))
    assert vector.delete("a") is True
    assert vector.get("a") is None


def test_delete_returns_false_when_absent(stores):
    _graph, vector = stores
    assert vector.delete("nonexistent") is False


def test_count_tracks_upserts_and_deletes(stores):
    graph, vector = stores
    assert vector.count() == 0
    for nid in ("a", "b"):
        _make_node(graph, nid)
        vector.upsert(nid, _vec(0.1, 0.2, 0.3))
    assert vector.count() == 2
    vector.delete("a")
    assert vector.count() == 1


# ----------------------------------------------------------------------
# Get edge cases
# ----------------------------------------------------------------------


def test_get_missing_returns_none(stores):
    _graph, vector = stores
    assert vector.get("nonexistent") is None


def test_get_node_without_embedding_returns_none(stores):
    graph, vector = stores
    _make_node(graph, "no_vec")
    assert vector.get("no_vec") is None
