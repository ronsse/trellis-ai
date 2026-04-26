"""VectorStore contract test suite — runs against every independent backend.

Per ``docs/design/adr-canonical-graph-layer.md`` §3, this base class
defines the shared semantics every ``VectorStore`` backend must
honour. Backend-specific test files subclass
:class:`VectorStoreContractTests` and provide a ``store`` fixture.

**Scope deviation:** ``Neo4jVectorStore`` (shape #2 — embeddings as
optional properties on the graph store's ``:Node`` rows) is NOT covered
by this contract. Its ``upsert`` requires the underlying node to
already exist as a current version; the rest of the backends create
storage independently. The shape #2 contract lives in the per-backend
file ``test_neo4j_vector.py`` and is exercised against a real Neo4j
instance via ``TRELLIS_TEST_NEO4J_URI``.

Subclass shape::

    class TestSQLiteVectorContract(VectorStoreContractTests):
        @pytest.fixture
        def store(self, tmp_path):
            store = SQLiteVectorStore(tmp_path / "vec.db")
            yield store
            store.close()

The harness fixes the embedding dimension at ``DIMS = 3`` so all
backends (including pgvector, which fixes dims at construction)
exercise the same vector shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from trellis.stores.base.vector import VectorStore


# All contract tests use 3-D vectors so backends that pin dimensions
# at construction time (pgvector) and backends that infer them from
# the first upsert (LanceDB) exercise the same shape.
DIMS = 3


def _vec(x: float, y: float, z: float) -> list[float]:
    return [x, y, z]


class VectorStoreContractTests:
    """Contract tests every independent ``VectorStore`` backend must pass."""

    # ------------------------------------------------------------------
    # Empty store
    # ------------------------------------------------------------------

    def test_empty_count_is_zero(self, store: VectorStore) -> None:
        assert store.count() == 0

    def test_empty_query_returns_empty_list(self, store: VectorStore) -> None:
        assert store.query(_vec(1, 0, 0), top_k=10) == []

    def test_get_missing_returns_none(self, store: VectorStore) -> None:
        assert store.get("nonexistent") is None

    def test_delete_missing_returns_false(self, store: VectorStore) -> None:
        assert store.delete("nonexistent") is False

    # ------------------------------------------------------------------
    # Upsert + get round-trip
    # ------------------------------------------------------------------

    def test_upsert_then_get_roundtrips_vector(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(0.1, 0.2, 0.3), metadata={"kind": "doc"})
        result = store.get("a")
        assert result is not None
        assert result["item_id"] == "a"
        assert result["dimensions"] == DIMS
        assert len(result["vector"]) == DIMS
        # Stored vector should be approximately the input (float
        # round-trip via numpy/pgvector/lancedb may lose precision).
        for got, want in zip(result["vector"], _vec(0.1, 0.2, 0.3), strict=False):
            assert abs(got - want) < 1e-5

    def test_upsert_with_no_metadata_yields_empty_dict(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(1, 0, 0))
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == {}

    def test_upsert_replace_overwrites_metadata(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 2})
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == {"v": 2}

    def test_upsert_replace_overwrites_vector(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("a", _vec(0, 1, 0))
        result = store.get("a")
        assert result is not None
        for got, want in zip(result["vector"], _vec(0, 1, 0), strict=False):
            assert abs(got - want) < 1e-5

    def test_upsert_replace_keeps_count_at_one(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("a", _vec(0, 1, 0))
        assert store.count() == 1

    # ------------------------------------------------------------------
    # Metadata round-trip
    # ------------------------------------------------------------------

    def test_metadata_roundtrips_str_int_float_bool(
        self, store: VectorStore
    ) -> None:
        meta = {"name": "auth", "tier": 1, "weight": 0.5, "active": True}
        store.upsert("a", _vec(1, 0, 0), metadata=meta)
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == meta

    def test_metadata_roundtrips_nested_structures(
        self, store: VectorStore
    ) -> None:
        meta = {"tags": ["a", "b"], "nested": {"x": 1}}
        store.upsert("a", _vec(1, 0, 0), metadata=meta)
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == meta

    # ------------------------------------------------------------------
    # Delete + count
    # ------------------------------------------------------------------

    def test_delete_existing_returns_true(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0))
        assert store.delete("a") is True

    def test_delete_removes_from_get(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0))
        store.delete("a")
        assert store.get("a") is None

    def test_count_decreases_after_delete(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("b", _vec(0, 1, 0))
        store.delete("a")
        assert store.count() == 1

    def test_count_tracks_multiple_upserts(self, store: VectorStore) -> None:
        for i, v in enumerate(
            [_vec(1, 0, 0), _vec(0, 1, 0), _vec(0, 0, 1)]
        ):
            store.upsert(f"v{i}", v)
        assert store.count() == 3

    # ------------------------------------------------------------------
    # Query — ordering and top_k
    # ------------------------------------------------------------------

    def test_query_orders_by_similarity_descending(
        self, store: VectorStore
    ) -> None:
        store.upsert("right", _vec(1, 0, 0))
        store.upsert("up", _vec(0, 1, 0))
        store.upsert("near_right", _vec(0.9, 0.1, 0))
        results = store.query(_vec(1, 0, 0), top_k=3)
        # Closest first; scores are non-increasing.
        assert results[0]["item_id"] == "right"
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_query_self_match_is_top(self, store: VectorStore) -> None:
        # Cosine similarity with self should be the maximum result.
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("b", _vec(0, 1, 0))
        store.upsert("c", _vec(0, 0, 1))
        results = store.query(_vec(1, 0, 0), top_k=3)
        assert results[0]["item_id"] == "a"

    def test_query_top_k_caps_results(self, store: VectorStore) -> None:
        for i in range(5):
            store.upsert(f"v{i}", _vec(float(i + 1), 0, 0))
        results = store.query(_vec(1, 0, 0), top_k=2)
        assert len(results) == 2

    def test_query_returns_metadata(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        results = store.query(_vec(1, 0, 0), top_k=1)
        assert len(results) == 1
        assert results[0]["metadata"] == {"kind": "doc"}

    def test_query_result_shape(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"k": "v"})
        results = store.query(_vec(1, 0, 0), top_k=1)
        assert len(results) == 1
        result = results[0]
        assert set(result.keys()) >= {"item_id", "score", "metadata"}
        assert isinstance(result["item_id"], str)
        assert isinstance(result["score"], float)
        assert isinstance(result["metadata"], dict)

    # ------------------------------------------------------------------
    # Query — metadata filters
    # ------------------------------------------------------------------

    def test_query_filter_by_str_metadata(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        store.upsert("b", _vec(0.9, 0.1, 0), metadata={"kind": "code"})
        results = store.query(
            _vec(1, 0, 0), top_k=10, filters={"kind": "code"}
        )
        assert len(results) == 1
        assert results[0]["item_id"] == "b"

    def test_query_filter_by_int_metadata(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"tier": 1})
        store.upsert("b", _vec(0.9, 0.1, 0), metadata={"tier": 2})
        results = store.query(
            _vec(1, 0, 0), top_k=10, filters={"tier": 2}
        )
        assert len(results) == 1
        assert results[0]["item_id"] == "b"

    def test_query_filter_with_multiple_keys_is_and(
        self, store: VectorStore
    ) -> None:
        store.upsert(
            "a", _vec(1, 0, 0), metadata={"kind": "doc", "team": "platform"}
        )
        store.upsert(
            "b", _vec(0.9, 0.1, 0), metadata={"kind": "doc", "team": "growth"}
        )
        results = store.query(
            _vec(1, 0, 0),
            top_k=10,
            filters={"kind": "doc", "team": "platform"},
        )
        assert len(results) == 1
        assert results[0]["item_id"] == "a"

    def test_query_filter_no_match_returns_empty(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        results = store.query(
            _vec(1, 0, 0), top_k=10, filters={"kind": "nothing"}
        )
        assert results == []

    def test_query_filter_on_unknown_key_returns_empty(
        self, store: VectorStore
    ) -> None:
        # Filter key not present on any item -> no item satisfies the
        # filter -> empty list.
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        results = store.query(
            _vec(1, 0, 0), top_k=10, filters={"absent_key": "x"}
        )
        assert results == []

    # ------------------------------------------------------------------
    # bulk upsert
    # ------------------------------------------------------------------

    def test_upsert_bulk_writes_all_rows(self, store: VectorStore) -> None:
        store.upsert_bulk(
            [
                {"item_id": "a", "vector": _vec(1, 0, 0)},
                {"item_id": "b", "vector": _vec(0, 1, 0)},
                {"item_id": "c", "vector": _vec(0, 0, 1)},
            ]
        )
        assert store.count() == 3
        assert store.get("a") is not None
        assert store.get("b") is not None
        assert store.get("c") is not None

    def test_upsert_bulk_empty_list_is_noop(self, store: VectorStore) -> None:
        store.upsert_bulk([])
        assert store.count() == 0

    def test_upsert_bulk_round_trips_metadata(self, store: VectorStore) -> None:
        store.upsert_bulk(
            [
                {
                    "item_id": "a",
                    "vector": _vec(1, 0, 0),
                    "metadata": {"kind": "doc", "tier": 1},
                }
            ]
        )
        item = store.get("a")
        assert item is not None
        assert item["metadata"] == {"kind": "doc", "tier": 1}

    def test_upsert_bulk_replaces_existing_row(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        store.upsert_bulk(
            [{"item_id": "a", "vector": _vec(0, 1, 0), "metadata": {"v": 2}}]
        )
        assert store.count() == 1
        item = store.get("a")
        assert item is not None
        assert item["metadata"] == {"v": 2}

    def test_upsert_bulk_rejects_missing_required_keys(
        self, store: VectorStore
    ) -> None:
        with pytest.raises(ValueError, match="vector"):
            store.upsert_bulk([{"item_id": "a"}])

        with pytest.raises(ValueError, match="item_id"):
            store.upsert_bulk([{"vector": _vec(1, 0, 0)}])

    def test_upsert_bulk_results_visible_to_query(
        self, store: VectorStore
    ) -> None:
        store.upsert_bulk(
            [
                {"item_id": "right", "vector": _vec(1, 0, 0)},
                {"item_id": "up", "vector": _vec(0, 1, 0)},
            ]
        )
        results = store.query(_vec(1, 0, 0), top_k=2)
        assert len(results) == 2
        assert results[0]["item_id"] == "right"
