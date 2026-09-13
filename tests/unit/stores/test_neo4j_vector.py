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

    graph = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
    # No ``index_name`` override: take the production default, which is also
    # what ``tests/integration/conftest.py`` pins as INTEGRATION_VECTOR_INDEX.
    # Neo4j allows exactly one vector index per ``(label, property)`` pair and
    # rejects a second one *silently* — measured on ``neo4j:2025.12``, a
    # ``CREATE VECTOR INDEX <other name> IF NOT EXISTS FOR (n:Node) ON
    # (n.embedding)`` returns success and never appears in ``SHOW INDEXES``,
    # after which ``wait_for_vector_index_online`` times out. Since #356 put
    # this file in the same ``live-infra.yml`` pytest invocation as
    # ``tests/integration/test_neo4j_e2e.py``, against the same container, a
    # private name here would mean whichever suite ran second errored on every
    # test. Sharing one name at matching dimensions makes ``CREATE ... IF NOT
    # EXISTS`` a true no-op for the second suite; per-test node wipes (below)
    # are what isolate the data. Pinned by
    # tests/unit/test_neo4j_vector_live_infra_rule.py.
    vector = Neo4jVectorStore(
        URI,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        dimensions=3,
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
        with pytest.raises(ValueError, match=r"upsert_bulk\[1\].*no current version"):
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

    def test_reversioning_drops_the_node_from_the_count(self, stores):
        """Updating an embedded node leaves the current row unembedded.

        The SCD-2 write opens a new current version that does **not**
        inherit the prior embedding; the closed version keeps it on disk
        for time-travel reads. ``count`` reads ``valid_to IS NULL``, so it
        is the cheapest observation of that fact — and it is a plain
        ``MATCH``, not a ``SEARCH``, so unlike its query-side twin in
        :class:`TestQuery` it runs on every Neo4j the suite can reach.
        """
        graph, vector = stores
        _make_node(graph, "n1", v=1)
        vector.upsert("n1", _vec(1, 0, 0))
        assert vector.count() == 1

        graph.upsert_node("n1", "doc", {"v": 2})
        assert vector.count() == 0


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class TestQuery:
    """``query`` issues Cypher 25 ``SEARCH ... IN ( VECTOR INDEX ... )``.

    That clause is AuraDB-grade: ``neo4j:2025.12`` — the image live-infra CI
    provisions — rejects it at parse time with ``Invalid input 'SEARCH'``, and
    a server that gets far enough to resolve it raises ``51N26``. So every
    test below that reaches Cypher takes ``require_neo4j_vector_search``,
    which probes the connected instance once per session and skips when the
    clause is unavailable. Without the gate these four are the reason the
    whole file could not be named in ``.github/workflows/live-infra.yml``,
    which is what kept the other 23 tests here from ever running in CI.

    ``test_query_dimension_mismatch_raises`` is deliberately **not** gated:
    ``Neo4jVectorStore.query`` validates the vector length in Python and
    raises before it builds any Cypher, so that test needs a store, not a
    capability. ``tests/unit/test_neo4j_vector_live_infra_rule.py`` holds
    that exemption by name, and fails if it ever stops being true.
    """

    def test_cosine_ordering(self, require_neo4j_vector_search, stores):
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

    def test_top_k_limits_results(self, require_neo4j_vector_search, stores):
        graph, vector = stores
        for i in range(5):
            nid = f"v{i}"
            _make_node(graph, nid)
            vector.upsert(nid, _vec(float(i + 1), 0, 0))
        results = vector.query(_vec(1, 0, 0), top_k=2)
        assert len(results) == 2

    def test_filter_by_metadata(self, require_neo4j_vector_search, stores):
        graph, vector = stores
        _make_node(graph, "a")
        _make_node(graph, "b")
        vector.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        vector.upsert("b", _vec(0.9, 0.1, 0), metadata={"kind": "code"})
        results = vector.query(_vec(1, 0, 0), top_k=10, filters={"kind": "code"})
        assert len(results) == 1
        assert results[0]["item_id"] == "b"

    def test_query_dimension_mismatch_raises(self, stores):
        _, vector = stores
        with pytest.raises(ValueError, match="dimensions"):
            vector.query([1.0, 0.0], top_k=1)

    def test_excludes_historical_versions(self, require_neo4j_vector_search, stores):
        """A node updated after embedding leaves the embedding on the
        closed version, but the index call filters them out.

        The store-side half of this — that the current row ends up
        unembedded at all — is asserted by
        ``TestCount::test_reversioning_drops_the_node_from_the_count``,
        which needs no ``SEARCH`` and so keeps running where this one skips.
        """
        graph, vector = stores
        _make_node(graph, "n1", v=1)
        vector.upsert("n1", _vec(1, 0, 0))
        # Update creates a new (current) version with no embedding;
        # the old version (now closed) keeps the embedding on disk.
        graph.upsert_node("n1", "doc", {"v": 2})
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

    def test_zero_m_rejected(self):
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match=r"^m must be > 0"):
            Neo4jVectorStore(URI, user=USER, password=PASSWORD, dimensions=3, m=0)

    def test_zero_ef_construction_rejected(self):
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match="ef_construction"):
            Neo4jVectorStore(
                URI,
                user=USER,
                password=PASSWORD,
                dimensions=3,
                ef_construction=0,
            )
