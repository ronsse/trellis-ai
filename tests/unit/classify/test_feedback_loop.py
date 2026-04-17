"""Tests for the feedback loop that applies noise tags."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.classify.feedback import apply_noise_tags
from trellis.stores.sqlite.document import SQLiteDocumentStore


@pytest.fixture
def doc_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


class TestApplyNoiseTags:
    """apply_noise_tags updates signal_quality on noise candidates."""

    def test_marks_noise_candidates(self, doc_store: SQLiteDocumentStore) -> None:
        d1 = doc_store.put(
            None,
            "noisy content",
            {"content_tags": {"domain": ["api"], "signal_quality": "standard"}},
        )
        d2 = doc_store.put(
            None,
            "good content",
            {"content_tags": {"domain": ["api"], "signal_quality": "high"}},
        )

        updated = apply_noise_tags([d1], doc_store)

        doc1 = doc_store.get(d1)
        doc2 = doc_store.get(d2)
        assert doc1 is not None
        assert doc1["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert doc2 is not None
        assert doc2["metadata"]["content_tags"]["signal_quality"] == "high"
        assert updated == 1

    def test_skips_nonexistent_items(self, doc_store: SQLiteDocumentStore) -> None:
        updated = apply_noise_tags(["nonexistent-id"], doc_store)
        assert updated == 0

    def test_empty_candidates_noop(self, doc_store: SQLiteDocumentStore) -> None:
        updated = apply_noise_tags([], doc_store)
        assert updated == 0

    def test_creates_content_tags_if_missing(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        d1 = doc_store.put(None, "content without tags", {})
        updated = apply_noise_tags([d1], doc_store)

        doc = doc_store.get(d1)
        assert doc is not None
        assert doc["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert updated == 1

    def test_preserves_other_tags(self, doc_store: SQLiteDocumentStore) -> None:
        d1 = doc_store.put(
            None,
            "tagged content",
            {
                "content_tags": {
                    "domain": ["data-pipeline"],
                    "content_type": "code",
                    "signal_quality": "standard",
                },
            },
        )
        apply_noise_tags([d1], doc_store)

        doc = doc_store.get(d1)
        assert doc is not None
        tags = doc["metadata"]["content_tags"]
        assert tags["domain"] == ["data-pipeline"]
        assert tags["content_type"] == "code"
        assert tags["signal_quality"] == "noise"
