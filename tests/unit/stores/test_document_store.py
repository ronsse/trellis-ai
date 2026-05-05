"""Tests for the document store."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.stores.document import SQLiteDocumentStore


@pytest.fixture
def doc_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


def test_put_and_get(doc_store: SQLiteDocumentStore) -> None:
    doc_id = doc_store.put(None, "hello world", {"tag": "test"})
    doc = doc_store.get(doc_id)
    assert doc is not None
    assert doc["content"] == "hello world"
    assert doc["metadata"]["tag"] == "test"


def test_put_with_explicit_id(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put("my-id", "content")
    doc = doc_store.get("my-id")
    assert doc is not None
    assert doc["doc_id"] == "my-id"


def test_update_document(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put("d1", "v1")
    doc_store.put("d1", "v2")
    doc = doc_store.get("d1")
    assert doc is not None
    assert doc["content"] == "v2"


def test_delete(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put("d1", "content")
    assert doc_store.delete("d1") is True
    assert doc_store.get("d1") is None
    assert doc_store.delete("d1") is False


def test_search(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(None, "python programming language")
    doc_store.put(None, "java programming language")
    doc_store.put(None, "unrelated document")
    results = doc_store.search("programming")
    assert len(results) >= 2


def test_search_with_filters(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(None, "python guide", {"category": "tutorial"})
    doc_store.put(None, "python reference", {"category": "reference"})
    results = doc_store.search("python", filters={"category": "tutorial"})
    assert len(results) == 1
    assert results[0]["metadata"]["category"] == "tutorial"


def test_list_documents(doc_store: SQLiteDocumentStore) -> None:
    for i in range(5):
        doc_store.put(None, f"doc {i}")
    docs = doc_store.list_documents(limit=3)
    assert len(docs) == 3


def test_count(doc_store: SQLiteDocumentStore) -> None:
    assert doc_store.count() == 0
    doc_store.put(None, "one")
    doc_store.put(None, "two")
    assert doc_store.count() == 2


def test_get_by_hash(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(None, "unique content")
    doc = doc_store.get("nonexistent")
    assert doc is None
    # Find by hash
    all_docs = doc_store.list_documents()
    content_hash = all_docs[0].get("content_hash")
    assert content_hash is not None
    found = doc_store.get_by_hash(content_hash)
    assert found is not None
    assert found["content"] == "unique content"


def test_get_nonexistent(doc_store: SQLiteDocumentStore) -> None:
    assert doc_store.get("nope") is None


def test_get_by_hash_nonexistent(doc_store: SQLiteDocumentStore) -> None:
    assert doc_store.get_by_hash("nope") is None


def test_content_hash_computed_on_put(doc_store: SQLiteDocumentStore) -> None:
    doc_id = doc_store.put(None, "some content")
    doc = doc_store.get(doc_id)
    assert doc is not None
    assert doc["content_hash"] is not None
    assert len(doc["content_hash"]) == 16


def test_search_empty_query(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(None, "some content")
    results = doc_store.search("")
    assert results == []


def test_search_special_characters(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(None, "test document with content")
    # Should not crash with FTS5 special characters
    results = doc_store.search("test AND OR NOT ()")
    assert len(results) >= 1


def test_search_with_content_tags_domain(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(
        None,
        "spark etl pipeline documentation",
        {"content_tags": {"domain": ["data-pipeline"], "signal_quality": "standard"}},
    )
    doc_store.put(
        None,
        "kubernetes deploy pipeline docs",
        {"content_tags": {"domain": ["infrastructure"], "signal_quality": "standard"}},
    )
    results = doc_store.search(
        "pipeline",
        filters={"content_tags": {"domain": ["data-pipeline"]}},
    )
    assert len(results) == 1
    assert results[0]["metadata"]["content_tags"]["domain"] == ["data-pipeline"]


def test_search_with_content_tags_signal_quality(
    doc_store: SQLiteDocumentStore,
) -> None:
    doc_store.put(
        None,
        "useful reference guide",
        {"content_tags": {"signal_quality": "high"}},
    )
    doc_store.put(
        None,
        "noisy reference filler",
        {"content_tags": {"signal_quality": "noise"}},
    )
    results = doc_store.search(
        "reference",
        filters={"content_tags": {"signal_quality": ["high", "standard"]}},
    )
    assert len(results) == 1
    assert results[0]["metadata"]["content_tags"]["signal_quality"] == "high"


def test_search_with_content_tags_multi_facet(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put(
        None,
        "debugging the spark pipeline error",
        {
            "content_tags": {
                "domain": ["data-pipeline"],
                "content_type": "error-resolution",
                "signal_quality": "high",
            },
        },
    )
    doc_store.put(
        None,
        "spark pipeline architecture overview",
        {
            "content_tags": {
                "domain": ["data-pipeline"],
                "content_type": "pattern",
                "signal_quality": "standard",
            },
        },
    )
    results = doc_store.search(
        "spark pipeline",
        filters={
            "content_tags": {
                "domain": ["data-pipeline"],
                "content_type": ["error-resolution"],
            },
        },
    )
    assert len(results) == 1
    assert results[0]["metadata"]["content_tags"]["content_type"] == "error-resolution"


def test_search_without_content_tags_still_works(
    doc_store: SQLiteDocumentStore,
) -> None:
    """Documents without content_tags still returned with no tag filter."""
    doc_store.put(None, "plain document without tags")
    results = doc_store.search("plain document")
    assert len(results) == 1


def test_search_untagged_doc_passes_signal_quality_filter(
    doc_store: SQLiteDocumentStore,
) -> None:
    """Untagged docs survive a scalar ``signal_quality`` allowlist (default-pass)."""
    doc_store.put(
        None,
        "tagged high-signal reference",
        {
            "content_tags": {"signal_quality": "high"},
        },
    )
    doc_store.put(
        None,
        "tagged noise filler",
        {
            "content_tags": {"signal_quality": "noise"},
        },
    )
    doc_store.put(None, "untagged reference doc")

    results = doc_store.search(
        "reference",
        filters={"content_tags": {"signal_quality": ["high", "standard", "low"]}},
    )
    contents = {r["content"] for r in results}
    assert "tagged high-signal reference" in contents
    assert "untagged reference doc" in contents
    assert "tagged noise filler" not in contents


def test_search_untagged_doc_passes_domain_filter(
    doc_store: SQLiteDocumentStore,
) -> None:
    """List facets honour the same default-pass rule as scalar facets."""
    doc_store.put(
        None,
        "matching domain doc",
        {
            "content_tags": {"domain": ["data-pipeline"]},
        },
    )
    doc_store.put(
        None,
        "wrong domain doc",
        {
            "content_tags": {"domain": ["infrastructure"]},
        },
    )
    doc_store.put(None, "untagged domain doc")

    results = doc_store.search(
        "doc",
        filters={"content_tags": {"domain": ["data-pipeline"]}},
    )
    contents = {r["content"] for r in results}
    assert "matching domain doc" in contents
    assert "untagged domain doc" in contents
    assert "wrong domain doc" not in contents


def test_update_preserves_created_at(doc_store: SQLiteDocumentStore) -> None:
    doc_store.put("d1", "v1")
    doc1 = doc_store.get("d1")
    assert doc1 is not None
    created = doc1["created_at"]

    doc_store.put("d1", "v2")
    doc2 = doc_store.get("d1")
    assert doc2 is not None
    assert doc2["created_at"] == created
    # updated_at should change (or at least not be before created_at)
    assert doc2["updated_at"] >= created
