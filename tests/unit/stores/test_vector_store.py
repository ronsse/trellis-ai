"""Unit tests for Vector Store."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("numpy")

from trellis.stores.vector import SQLiteVectorStore, VectorStore


@pytest.fixture
def vector_store(tmp_path: Path) -> SQLiteVectorStore:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield store  # type: ignore[misc]
    store.close()


def test_implements_abc() -> None:
    assert issubclass(SQLiteVectorStore, VectorStore)


def test_upsert_and_get(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0, 0.0], {"label": "x"})
    result = vector_store.get("v1")
    assert result is not None
    assert result["item_id"] == "v1"
    assert result["dimensions"] == 3
    assert result["metadata"]["label"] == "x"


def test_upsert_updates(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0], {"v": 1})
    vector_store.upsert("v1", [0.0, 1.0], {"v": 2})
    result = vector_store.get("v1")
    assert result is not None
    assert result["metadata"]["v"] == 2
    assert result["dimensions"] == 2


def test_query_similar(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("a", [1.0, 0.0, 0.0])
    vector_store.upsert("b", [0.9, 0.1, 0.0])
    vector_store.upsert("c", [0.0, 0.0, 1.0])
    results = vector_store.query([1.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0]["item_id"] == "a"  # exact match
    assert results[0]["score"] > results[1]["score"]


def test_query_with_filters(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("a", [1.0, 0.0], {"cat": "x"})
    vector_store.upsert("b", [0.9, 0.1], {"cat": "y"})
    results = vector_store.query([1.0, 0.0], filters={"cat": "x"})
    assert len(results) == 1
    assert results[0]["item_id"] == "a"


def test_delete(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0])
    assert vector_store.delete("v1") is True
    assert vector_store.get("v1") is None
    assert vector_store.delete("v1") is False


def test_count(vector_store: SQLiteVectorStore) -> None:
    assert vector_store.count() == 0
    vector_store.upsert("a", [1.0])
    vector_store.upsert("b", [0.5])
    assert vector_store.count() == 2


def test_get_nonexistent(vector_store: SQLiteVectorStore) -> None:
    assert vector_store.get("nope") is None


def test_query_empty(vector_store: SQLiteVectorStore) -> None:
    results = vector_store.query([1.0, 0.0])
    assert results == []
