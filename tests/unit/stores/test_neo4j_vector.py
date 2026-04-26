"""Tests for Neo4jVectorStore (shape #2 — embedding on :Node).

Skipped unless ``TRELLIS_TEST_NEO4J_URI`` is set and the ``neo4j``
driver is importable. See ``test_neo4j_graph.py`` for the docker
setup.

The vector store attaches embeddings as properties on graph-store
``:Node`` rows, so every test creates a node via ``Neo4jGraphStore``
first, then attaches a vector via ``Neo4jVectorStore``.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("neo4j")

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]


@pytest.fixture
def stores():
    """Yield (graph_store, vector_store) sharing the same database."""
    from trellis.stores.neo4j.graph import Neo4jGraphStore
    from trellis.stores.neo4j.vector import Neo4jVectorStore

    graph = Neo4jGraphStore(
        URI, user=USER, password=PASSWORD, database=DATABASE
    )
    vector = Neo4jVectorStore(
        URI,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        dimensions=3,
        index_name="trellis_test_node_embeddings",
    )
    with graph._driver.session(database=graph._database) as session:
        session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
    yield graph, vector
    vector.close()
    graph.close()


def _vec(x: float, y: float, z: float) -> list[float]:
    return [x, y, z]


def _make_node(graph, node_id: str, **props) -> str:
    return graph.upsert_node(node_id, "doc", props)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_attaches_embedding_to_existing_node(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        vector.upsert("a", _vec(1, 0, 0))
        assert vector.count() == 1

    def test_replace_overrides_metadata(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        vector.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        vector.upsert("a", _vec(0, 1, 0), metadata={"v": 2})
        assert vector.count() == 1
        result = vector.get("a")
        assert result is not None
        assert result["metadata"]["v"] == 2

    def test_dimension_mismatch_raises(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        with pytest.raises(ValueError, match="dimensions"):
            vector.upsert("a", [1.0, 0.0])

    def test_missing_node_raises(self, stores):
        _, vector = stores
        with pytest.raises(ValueError, match="no current version"):
            vector.upsert("ghost", _vec(1, 0, 0))


class TestUpsertBulk:
    def test_attaches_embeddings_to_existing_nodes(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        _make_node(graph, "b")
        _make_node(graph, "c")
        vector.upsert_bulk(
            [
                {"item_id": "a", "vector": _vec(1, 0, 0)},
                {"item_id": "b", "vector": _vec(0, 1, 0)},
                {"item_id": "c", "vector": _vec(0, 0, 1)},
            ]
        )
        assert vector.count() == 3

    def test_empty_list_is_noop(self, stores):
        _, vector = stores
        vector.upsert_bulk([])
        assert vector.count() == 0

    def test_replace_overrides_metadata(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        vector.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        vector.upsert_bulk(
            [{"item_id": "a", "vector": _vec(0, 1, 0), "metadata": {"v": 2}}]
        )
        result = vector.get("a")
        assert result is not None
        assert result["metadata"]["v"] == 2

    def test_dimension_mismatch_raises_with_index(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        _make_node(graph, "b")
        with pytest.raises(ValueError, match=r"upsert_bulk\[1\]"):
            vector.upsert_bulk(
                [
                    {"item_id": "a", "vector": _vec(1, 0, 0)},
                    {"item_id": "b", "vector": [1.0, 0.0]},  # wrong dim
                ]
            )

    def test_missing_required_key_raises_with_index(self, stores):
        _, vector = stores
        with pytest.raises(ValueError, match=r"upsert_bulk\[0\]"):
            vector.upsert_bulk([{"item_id": "a"}])

    def test_missing_node_raises_with_index(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        with pytest.raises(
            ValueError, match=r"upsert_bulk\[1\].*no current version"
        ):
            vector.upsert_bulk(
                [
                    {"item_id": "a", "vector": _vec(1, 0, 0)},
                    {"item_id": "ghost", "vector": _vec(0, 1, 0)},
                ]
            )


# ---------------------------------------------------------------------------
# Get / delete / count
# ---------------------------------------------------------------------------


class TestGet:
    def test_returns_none_when_missing(self, stores):
        _, vector = stores
        assert vector.get("nonexistent") is None

    def test_returns_none_for_node_without_embedding(self, stores):
        graph, vector = stores
        _make_node(graph, "n_only")
        assert vector.get("n_only") is None

    def test_returns_dict_with_vector_and_metadata(self, stores):
        graph, vector = stores
        _make_node(graph, "x")
        vector.upsert("x", _vec(0.1, 0.2, 0.3), metadata={"tag": "t"})
        result = vector.get("x")
        assert result is not None
        assert result["item_id"] == "x"
        assert result["dimensions"] == 3
        assert result["metadata"]["tag"] == "t"
        assert len(result["vector"]) == 3


class TestDelete:
    def test_delete_removes_embedding_only_not_node(self, stores):
        graph, vector = stores
        _make_node(graph, "d", name="keep_me")
        vector.upsert("d", _vec(1, 1, 1))
        assert vector.delete("d") is True
        assert vector.count() == 0
        # Underlying node is still there.
        assert graph.get_node("d") is not None

    def test_delete_missing_returns_false(self, stores):
        _, vector = stores
        assert vector.delete("nope") is False

    def test_delete_on_node_without_embedding_returns_false(self, stores):
        graph, vector = stores
        _make_node(graph, "n_only")
        assert vector.delete("n_only") is False


class TestCount:
    def test_empty(self, stores):
        _, vector = stores
        assert vector.count() == 0

    def test_only_counts_nodes_with_embedding(self, stores):
        graph, vector = stores
        _make_node(graph, "with_emb")
        _make_node(graph, "without_emb")
        vector.upsert("with_emb", _vec(1, 0, 0))
        assert vector.count() == 1


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_cosine_ordering(self, stores):
        graph, vector = stores
        for nid, v in [
            ("right", _vec(1, 0, 0)),
            ("up", _vec(0, 1, 0)),
            ("diag", _vec(0.7, 0.7, 0)),
        ]:
            _make_node(graph, nid)
            vector.upsert(nid, v)

        results = vector.query(_vec(1, 0, 0), top_k=3)
        ids = [r["item_id"] for r in results]
        assert ids[0] == "right"
        assert all(0 <= r["score"] <= 1.0001 for r in results)

    def test_top_k_limits_results(self, stores):
        graph, vector = stores
        for i in range(5):
            nid = f"v{i}"
            _make_node(graph, nid)
            vector.upsert(nid, _vec(float(i + 1), 0, 0))
        results = vector.query(_vec(1, 0, 0), top_k=2)
        assert len(results) == 2

    def test_filter_by_metadata(self, stores):
        graph, vector = stores
        _make_node(graph, "a")
        _make_node(graph, "b")
        vector.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        vector.upsert("b", _vec(0.9, 0.1, 0), metadata={"kind": "code"})
        results = vector.query(
            _vec(1, 0, 0), top_k=10, filters={"kind": "code"}
        )
        assert len(results) == 1
        assert results[0]["item_id"] == "b"

    def test_query_dimension_mismatch_raises(self, stores):
        _, vector = stores
        with pytest.raises(ValueError, match="dimensions"):
            vector.query([1.0, 0.0], top_k=1)

    def test_excludes_historical_versions(self, stores):
        """A node updated after embedding leaves the embedding on the
        closed version, but the index call filters them out."""
        graph, vector = stores
        _make_node(graph, "n1", v=1)
        vector.upsert("n1", _vec(1, 0, 0))
        # Update creates a new (current) version with no embedding;
        # the old version (now closed) keeps the embedding on disk.
        graph.upsert_node("n1", "doc", {"v": 2})
        # The current node has no embedding ⇒ count == 0 ⇒ query empty.
        assert vector.count() == 0
        assert vector.query(_vec(1, 0, 0), top_k=5) == []


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_invalid_similarity_rejected(self):
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match="similarity"):
            Neo4jVectorStore(
                URI,
                user=USER,
                password=PASSWORD,
                dimensions=3,
                similarity="jaccard",
            )

    def test_zero_dimensions_rejected(self):
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match="dimensions"):
            Neo4jVectorStore(URI, user=USER, password=PASSWORD, dimensions=0)
