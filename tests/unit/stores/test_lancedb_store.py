"""Unit tests for LanceDB Vector Store."""

from __future__ import annotations

from pathlib import Path

import pytest

lancedb = pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

from trellis.stores.base.vector import VectorStore  # noqa: E402
from trellis.stores.lancedb.store import LanceVectorStore  # noqa: E402


@pytest.fixture
def vector_store(tmp_path: Path) -> LanceVectorStore:
    store = LanceVectorStore(uri=tmp_path / "test_lancedb", table_name="vectors")
    yield store  # type: ignore[misc]
    store.close()


def test_implements_abc() -> None:
    assert issubclass(LanceVectorStore, VectorStore)


def test_upsert_and_get(vector_store: LanceVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0, 0.0], {"label": "x"})
    result = vector_store.get("v1")
    assert result is not None
    assert result["item_id"] == "v1"
    assert result["dimensions"] == 3
    assert result["metadata"]["label"] == "x"
    # Round-trip vector values
    assert len(result["vector"]) == 3
    assert result["vector"][0] == pytest.approx(1.0)


def test_upsert_overwrites(vector_store: LanceVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0], {"v": 1})
    vector_store.upsert("v1", [0.0, 1.0], {"v": 2})
    result = vector_store.get("v1")
    assert result is not None
    assert result["metadata"]["v"] == 2
    assert result["dimensions"] == 2
    # Only one record should exist
    assert vector_store.count() == 1


def test_query_similar(vector_store: LanceVectorStore) -> None:
    vector_store.upsert("a", [1.0, 0.0, 0.0])
    vector_store.upsert("b", [0.9, 0.1, 0.0])
    vector_store.upsert("c", [0.0, 0.0, 1.0])
    results = vector_store.query([1.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0]["item_id"] == "a"  # exact match first
    assert results[0]["score"] > results[1]["score"]


def test_query_top_k(vector_store: LanceVectorStore) -> None:
    for i in range(5):
        vector_store.upsert(f"v{i}", [float(i), 1.0, 0.0])
    results = vector_store.query([3.0, 1.0, 0.0], top_k=3)
    assert len(results) == 3


def test_query_with_filters(vector_store: LanceVectorStore) -> None:
    vector_store.upsert("a", [1.0, 0.0], {"cat": "x"})
    vector_store.upsert("b", [0.9, 0.1], {"cat": "y"})
    results = vector_store.query([1.0, 0.0], filters={"cat": "x"})
    assert len(results) == 1
    assert results[0]["item_id"] == "a"


def test_query_score_is_similarity(vector_store: LanceVectorStore) -> None:
    """Score should be high for similar vectors (cosine similarity)."""
    vector_store.upsert("same", [1.0, 0.0, 0.0])
    results = vector_store.query([1.0, 0.0, 0.0], top_k=1)
    assert len(results) == 1
    # Identical vectors should have score close to 1.0
    assert results[0]["score"] == pytest.approx(1.0, abs=0.01)


def test_delete_existing(vector_store: LanceVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0])
    assert vector_store.delete("v1") is True
    assert vector_store.get("v1") is None


def test_delete_nonexistent(vector_store: LanceVectorStore) -> None:
    # Delete on empty store (no table yet)
    assert vector_store.delete("nope") is False
    # Delete after table exists but item doesn't
    vector_store.upsert("v1", [1.0, 0.0])
    assert vector_store.delete("nope") is False


def test_count(vector_store: LanceVectorStore) -> None:
    assert vector_store.count() == 0
    vector_store.upsert("a", [1.0])
    vector_store.upsert("b", [0.5])
    assert vector_store.count() == 2


def test_count_empty(vector_store: LanceVectorStore) -> None:
    assert vector_store.count() == 0


def test_get_nonexistent(vector_store: LanceVectorStore) -> None:
    assert vector_store.get("nope") is None


def test_query_empty_store(vector_store: LanceVectorStore) -> None:
    results = vector_store.query([1.0, 0.0])
    assert results == []


def test_close_is_safe(vector_store: LanceVectorStore) -> None:
    """close() should not raise."""
    vector_store.close()
    # Calling close again should also be fine
    vector_store.close()
