"""Tests for PgVectorStore — requires a real PostgreSQL instance with pgvector.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg/pgvector are importable.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")

DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


@pytest.fixture
def store():
    """Create a PgVectorStore and ensure a clean table for each test."""
    from trellis.stores.pgvector.store import PgVectorStore

    s = PgVectorStore(dsn=DSN, dimensions=3)
    # Truncate between tests for isolation.
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE vectors")
    s._conn.commit()
    yield s
    s.close()


def _vec(x: float, y: float, z: float) -> list[float]:
    return [x, y, z]


class TestUpsert:
    def test_insert_and_count(self, store):
        store.upsert("a", _vec(1, 0, 0))
        assert store.count() == 1

    def test_update_replaces_metadata(self, store):
        store.upsert("a", _vec(1, 0, 0), metadata={"v": 1})
        store.upsert("a", _vec(0, 1, 0), metadata={"v": 2})
        assert store.count() == 1
        result = store.get("a")
        assert result is not None
        assert result["metadata"]["v"] == 2


class TestGet:
    def test_returns_none_when_missing(self, store):
        assert store.get("nonexistent") is None

    def test_returns_dict(self, store):
        store.upsert("x", _vec(0.1, 0.2, 0.3), metadata={"tag": "test"})
        result = store.get("x")
        assert result is not None
        assert result["item_id"] == "x"
        assert result["dimensions"] == 3
        assert result["metadata"]["tag"] == "test"
        assert len(result["vector"]) == 3


class TestDelete:
    def test_delete_existing(self, store):
        store.upsert("d", _vec(1, 1, 1))
        assert store.delete("d") is True
        assert store.count() == 0

    def test_delete_missing(self, store):
        assert store.delete("nope") is False


class TestQuery:
    def test_cosine_ordering(self, store):
        store.upsert("right", _vec(1, 0, 0))
        store.upsert("up", _vec(0, 1, 0))
        store.upsert("diag", _vec(0.7, 0.7, 0))

        results = store.query(_vec(1, 0, 0), top_k=3)
        ids = [r["item_id"] for r in results]
        # "right" should be the best match for [1,0,0]
        assert ids[0] == "right"
        assert all(0 <= r["score"] <= 1.0001 for r in results)

    def test_top_k_limits_results(self, store):
        for i in range(5):
            store.upsert(f"v{i}", _vec(float(i), 0, 0))
        results = store.query(_vec(1, 0, 0), top_k=2)
        assert len(results) == 2

    def test_filter_by_metadata(self, store):
        store.upsert("a", _vec(1, 0, 0), metadata={"kind": "doc"})
        store.upsert("b", _vec(0.9, 0.1, 0), metadata={"kind": "code"})
        results = store.query(_vec(1, 0, 0), top_k=10, filters={"kind": "code"})
        assert len(results) == 1
        assert results[0]["item_id"] == "b"


class TestCount:
    def test_empty(self, store):
        assert store.count() == 0

    def test_after_inserts(self, store):
        store.upsert("a", _vec(1, 0, 0))
        store.upsert("b", _vec(0, 1, 0))
        assert store.count() == 2


class TestDimMismatchFailsFast:
    """``CREATE TABLE IF NOT EXISTS`` is a no-op against an existing
    table, so a caller that constructs the store with a different
    ``dimensions`` value than the column was provisioned with would
    silently inherit the old dim and crash on the first upsert. The
    store now detects this at construction and raises with an
    actionable message.

    Relies on the ``store`` fixture (dimensions=3) running first to
    provision the column at dim=3; if the table doesn't exist yet the
    mismatch check is silently skipped.
    """

    def test_matching_dim_succeeds(self, store):
        from trellis.stores.pgvector.store import PgVectorStore

        # The fixture constructed at dim=3; constructing again at dim=3
        # is a no-op against the existing column and must not raise.
        s2 = PgVectorStore(dsn=DSN, dimensions=3)
        try:
            assert s2._dimensions == 3
        finally:
            s2.close()

    def test_mismatched_dim_raises_with_hint(self, store):
        from trellis.stores.pgvector.store import PgVectorStore

        with pytest.raises(ValueError, match="dimensions=3"):
            PgVectorStore(dsn=DSN, dimensions=16)
