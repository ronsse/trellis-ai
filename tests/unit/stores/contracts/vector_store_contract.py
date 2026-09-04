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
# at construction time (pgvector) and backends that store them as a
# node property (neo4j shape #2) exercise the same shape.
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

    def test_upsert_then_get_roundtrips_vector(self, store: VectorStore) -> None:
        store.upsert("a", _vec(0.1, 0.2, 0.3), metadata={"kind": "doc"})
        result = store.get("a")
        assert result is not None
        assert result["item_id"] == "a"
        assert result["dimensions"] == DIMS
        assert len(result["vector"]) == DIMS
        # Stored vector should be approximately the input (float
        # round-trip via numpy/pgvector may lose precision).
        for got, want in zip(result["vector"], _vec(0.1, 0.2, 0.3), strict=False):
            assert abs(got - want) < 1e-5

    def test_upsert_with_no_metadata_yields_empty_dict(
        self, store: VectorStore
    ) -> None:
        store.upsert("a", _vec(1, 0, 0))
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == {}

    def test_upsert_replace_overwrites_metadata(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 2})
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == {"v": 2}

    def test_upsert_replace_overwrites_vector(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("a", _vec(0, 1, 0))
        result = store.get("a")
        assert result is not None
        for got, want in zip(result["vector"], _vec(0, 1, 0), strict=False):
            assert abs(got - want) < 1e-5

    def test_upsert_replace_keeps_count_at_one(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("a", _vec(0, 1, 0))
        assert store.count() == 1

    def test_get_then_reupsert_preserves_vector(self, store: VectorStore) -> None:
        """A read-modify-write of metadata alone must not disturb the embedding.

        This is the store-level guarantee the post-embed metadata sync
        depends on (``trellis.core.vector_metadata.sync_vector_metadata``,
        and ``trellis.mutate.handlers._sync_vector_lifecycle`` before it):
        both refresh a row's metadata by feeding ``get()``'s ``vector`` back
        into ``upsert()``, precisely so nothing is re-embedded. If a backend
        hands back a vector shape its own ``upsert`` cannot accept, that
        round-trip breaks — which is not hypothetical, since #339 found
        ``pgvector`` 0.5.0 returning a ``Vector`` object that raised
        ``TypeError`` on iteration where 0.4.x returned a list.

        Pinned here rather than per backend so every current and future
        backend inherits it.
        """
        original = _vec(0.1, 0.2, 0.3)
        store.upsert("a", original, metadata={"content_tags": {"q": "standard"}})
        row = store.get("a")
        assert row is not None

        store.upsert("a", row["vector"], {"content_tags": {"q": "noise"}})

        result = store.get("a")
        assert result is not None
        assert result["metadata"] == {"content_tags": {"q": "noise"}}
        assert store.count() == 1
        for got, want in zip(result["vector"], original, strict=False):
            assert abs(got - want) < 1e-5

    def test_get_then_reupsert_keeps_row_queryable(self, store: VectorStore) -> None:
        """…and the re-upserted row is still found by similarity search.

        Preserving the stored floats is not sufficient on backends where the
        vector lives in an index — a metadata-only rewrite must leave the row
        retrievable, or the sync would silently un-index everything it
        repaired.
        """
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        row = store.get("a")
        assert row is not None
        store.upsert("a", row["vector"], {"v": 2})

        results = store.query(_vec(1, 0, 0), top_k=1)
        assert [r["item_id"] for r in results] == ["a"]
        assert results[0]["metadata"] == {"v": 2}

    # ------------------------------------------------------------------
    # Metadata round-trip
    # ------------------------------------------------------------------

    def test_metadata_roundtrips_str_int_float_bool(self, store: VectorStore) -> None:
        meta = {"name": "auth", "tier": 1, "weight": 0.5, "active": True}
        store.upsert("a", _vec(1, 0, 0), metadata=meta)
        result = store.get("a")
        assert result is not None
        assert result["metadata"] == meta

    def test_metadata_roundtrips_nested_structures(self, store: VectorStore) -> None:
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
        for i, v in enumerate([_vec(1, 0, 0), _vec(0, 1, 0), _vec(0, 0, 1)]):
            store.upsert(f"v{i}", v)
        assert store.count() == 3

    # ------------------------------------------------------------------
    # Query — ordering and top_k
    # ------------------------------------------------------------------

    def test_query_orders_by_similarity_descending(self, store: VectorStore) -> None:
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
        results = store.query(_vec(1, 0, 0), top_k=10, filters={"kind": "code"})
        assert len(results) == 1
        assert results[0]["item_id"] == "b"

    def test_query_filter_by_int_metadata(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"tier": 1})
        store.upsert("b", _vec(0.9, 0.1, 0), metadata={"tier": 2})
        results = store.query(_vec(1, 0, 0), top_k=10, filters={"tier": 2})
        assert len(results) == 1
        assert results[0]["item_id"] == "b"

    def test_query_filter_with_multiple_keys_is_and(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc", "team": "platform"})
        store.upsert("b", _vec(0.9, 0.1, 0), metadata={"kind": "doc", "team": "growth"})
        results = store.query(
            _vec(1, 0, 0),
            top_k=10,
            filters={"kind": "doc", "team": "platform"},
        )
        assert len(results) == 1
        assert results[0]["item_id"] == "a"

    def test_query_filter_no_match_returns_empty(self, store: VectorStore) -> None:
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        results = store.query(_vec(1, 0, 0), top_k=10, filters={"kind": "nothing"})
        assert results == []

    def test_query_filter_on_unknown_key_returns_empty(
        self, store: VectorStore
    ) -> None:
        # Filter key not present on any item -> no item satisfies the
        # filter -> empty list.
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        results = store.query(_vec(1, 0, 0), top_k=10, filters={"absent_key": "x"})
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

    def test_upsert_bulk_results_visible_to_query(self, store: VectorStore) -> None:
        store.upsert_bulk(
            [
                {"item_id": "right", "vector": _vec(1, 0, 0)},
                {"item_id": "up", "vector": _vec(0, 1, 0)},
            ]
        )
        results = store.query(_vec(1, 0, 0), top_k=2)
        assert len(results) == 2
        assert results[0]["item_id"] == "right"

    def test_upsert_bulk_rejects_duplicate_item_ids(self, store: VectorStore) -> None:
        """Within-batch duplicate ``item_id`` rejected — last-write-wins
        is non-deterministic across backends (e.g. Neo4j UNWIND ordering),
        so the contract requires de-dup before the call."""
        before = store.count()
        with pytest.raises(ValueError, match=r"upsert_bulk\[1\].*duplicate"):
            store.upsert_bulk(
                [
                    {"item_id": "dup", "vector": _vec(1, 0, 0)},
                    {"item_id": "dup", "vector": _vec(0, 1, 0)},
                ]
            )
        assert store.count() == before
        assert store.get("dup") is None

    # ------------------------------------------------------------------
    # Declarations (#512) — a backend answers for itself
    # ------------------------------------------------------------------
    #
    # Both declarations existed as facts before they existed as API, and
    # a caller outside the package was inferring them from private
    # attributes: ``getattr(store, "_dimensions", None)`` for the width
    # and a probe for ``_pool`` / ``_conn`` for the reset. The inference
    # failed *as a false statement rather than as an error* — a backend
    # that pinned a width under another spelling had "declares no fixed
    # dimensionality" published about it — so what these cases check is
    # not that a value exists but that it **agrees with what the store
    # does**. A declaration nothing cross-checks is the same defect one
    # layer down.

    def test_declares_an_embedding_width(self, store: VectorStore) -> None:
        """``dimensions`` answers, with a positive int or a real ``None``.

        ``None`` is a substantive answer — "this backend pins no width" —
        and the case below is what stops it being a shrug.
        """
        declared = store.dimensions
        assert declared is None or (isinstance(declared, int) and declared > 0)

    def test_declared_width_matches_the_widths_actually_accepted(
        self, store: VectorStore
    ) -> None:
        """The declaration is checked against the store's own behaviour.

        This is the case that cannot be satisfied by a constant. A
        backend declaring ``None`` has to *prove* it pins no width by
        storing two different ones; a backend declaring ``N`` has to
        agree with ``get()`` and refuse anything but ``N``. Either
        constant fails against the other kind of backend, and the pinned
        constant fails against SQLite alone.
        """
        declared = store.dimensions
        wider = [*_vec(1, 0, 0), 0.5]

        if declared is None:
            store.upsert("narrow", _vec(1, 0, 0))
            store.upsert("wide", wider)
            narrow_row = store.get("narrow")
            wide_row = store.get("wide")
            assert narrow_row is not None
            assert wide_row is not None
            assert narrow_row["dimensions"] == DIMS
            assert wide_row["dimensions"] == DIMS + 1
            return

        assert declared == DIMS, (
            "the store fixture builds a DIMS-wide store, so a different "
            "declaration means the declaration is not the store's width"
        )
        store.upsert("narrow", _vec(1, 0, 0))
        row = store.get("narrow")
        assert row is not None
        assert row["dimensions"] == declared
        with pytest.raises(ValueError, match="dimensions"):
            store.upsert("wide", wider)
        assert store.get("wide") is None

    def test_dimensions_is_a_declaration_not_a_measurement(
        self, store: VectorStore
    ) -> None:
        """It is constant across writes, including the first one.

        Callers report it beside destructive work — ``POST
        /api/v1/vectors/reset`` prints it in the body of a 200 — so an
        implementation that derived it from stored rows would answer
        differently on an empty store than on a populated one and make
        that report a function of timing. It also must not need the
        store to be non-empty to have an answer.
        """
        before = store.dimensions
        store.upsert("a", _vec(1, 0, 0))
        assert store.dimensions == before
        store.upsert("b", _vec(0, 1, 0))
        assert store.dimensions == before

    def test_reset_storage_matches_supports_reset(self, store: VectorStore) -> None:
        """The reset declaration is checked against the store's behaviour.

        ``supports_reset()`` is derived from the ``reset_storage``
        override, so the two *cannot* disagree by construction — this
        pins the half construction cannot reach: that an override
        actually empties the store and leaves it usable, and that a
        backend which declines really declines rather than half-running.
        """
        store.upsert("a", _vec(1, 0, 0))
        assert store.count() == 1

        if not type(store).supports_reset():
            with pytest.raises(NotImplementedError, match="reset_storage"):
                store.reset_storage()
            assert store.count() == 1
            return

        store.reset_storage()
        assert store.count() == 0
        assert store.get("a") is None
        # Recreated, not merely dropped: the store is usable immediately.
        store.upsert("b", _vec(0, 1, 0))
        assert store.count() == 1
        assert store.query(_vec(0, 1, 0), top_k=1)[0]["item_id"] == "b"
