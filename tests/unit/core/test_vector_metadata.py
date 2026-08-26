"""Unit tests for ``trellis.core.vector_metadata`` (trellis-ai#338).

The write-through half of the fix: a metadata-only re-upsert that makes a
vector row's snapshot agree with the document behind it, without re-embedding
anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from trellis.core.vector_metadata import (
    SYNCED_METADATA_KEYS,
    resolve_vector_store,
    sync_vector_metadata,
    vector_metadata_diverges,
)
from trellis.stores.base.vector import VectorStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> SQLiteVectorStore:
    vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield vector_store
    vector_store.close()


def _seed(store: SQLiteVectorStore, **metadata: Any) -> None:
    store.upsert("doc1", [0.1, 0.2, 0.3], {"doc_id": "doc1", **metadata})


class TestSyncedKeys:
    """What is mirrored, and — as importantly — what is not."""

    def test_keys_are_the_classify_layer_pair(self) -> None:
        assert SYNCED_METADATA_KEYS == ("content_tags", "auto_importance")

    def test_row_owned_keys_are_left_alone(self, store: SQLiteVectorStore) -> None:
        """``content`` / ``doc_id`` / ``created_at`` belong to the row.

        ``content`` is the row's excerpt, cut at embed time because that is
        the last point holding the full document. Copying the document bag
        wholesale would clobber it.
        """
        _seed(store, content="row excerpt", created_at="2020-01-01T00:00:00Z")

        sync_vector_metadata(
            store,
            "doc1",
            {
                "content": "THE ENTIRE DOCUMENT",
                "created_at": "2026-08-26T00:00:00Z",
                "content_tags": {"signal_quality": "noise"},
            },
        )

        row = store.get("doc1")
        assert row is not None
        assert row["metadata"]["content"] == "row excerpt"
        assert row["metadata"]["created_at"] == "2020-01-01T00:00:00Z"
        assert row["metadata"]["content_tags"] == {"signal_quality": "noise"}


class TestSync:
    def test_writes_the_document_value(self, store: SQLiteVectorStore) -> None:
        _seed(store, content_tags={"signal_quality": "standard"})

        assert (
            sync_vector_metadata(
                store, "doc1", {"content_tags": {"signal_quality": "noise"}}
            )
            is True
        )

        row = store.get("doc1")
        assert row is not None
        assert row["metadata"]["content_tags"] == {"signal_quality": "noise"}

    def test_adds_a_key_the_row_never_had(self, store: SQLiteVectorStore) -> None:
        """28 of the 45 divergent production rows had no facet at all."""
        _seed(store)

        assert sync_vector_metadata(
            store, "doc1", {"content_tags": {"signal_quality": "noise"}}
        )

        row = store.get("doc1")
        assert row is not None
        assert row["metadata"]["content_tags"]["signal_quality"] == "noise"

    def test_removes_a_key_the_document_dropped(self, store: SQLiteVectorStore) -> None:
        """Agreement has to mean agreement in both directions."""
        _seed(store, content_tags={"signal_quality": "noise"}, auto_importance=0.9)

        assert sync_vector_metadata(store, "doc1", {"title": "t"})

        row = store.get("doc1")
        assert row is not None
        assert "content_tags" not in row["metadata"]
        assert "auto_importance" not in row["metadata"]

    def test_preserves_the_embedding(self, store: SQLiteVectorStore) -> None:
        _seed(store, content_tags={"signal_quality": "standard"})

        sync_vector_metadata(
            store, "doc1", {"content_tags": {"signal_quality": "noise"}}
        )

        row = store.get("doc1")
        assert row is not None
        assert row["vector"] == pytest.approx([0.1, 0.2, 0.3])

    def test_agreeing_row_is_not_rewritten(self, store: SQLiteVectorStore) -> None:
        """Idempotent: a synced corpus re-syncs to zero work.

        This is what makes the backfill safe to schedule and makes a
        non-zero steady-state count mean "a writer is still bypassing the
        write-through" rather than "nothing to see here".
        """
        tags = {"signal_quality": "noise"}
        _seed(store, content_tags=tags)

        assert sync_vector_metadata(store, "doc1", {"content_tags": tags}) is False

    def test_missing_row_is_a_no_op(self, store: SQLiteVectorStore) -> None:
        assert sync_vector_metadata(store, "never_embedded", {"content_tags": {}}) is (
            False
        )

    def test_no_vector_store_is_a_no_op(self) -> None:
        assert sync_vector_metadata(None, "doc1", {"content_tags": {}}) is False

    def test_backend_failure_is_swallowed_not_raised(self) -> None:
        """The caller has already written the authoritative document row.

        Raising here would report a failed tag write for a tag that landed;
        the divergence is logged instead. Same discipline as
        ``_sync_vector_lifecycle`` on the retention path.
        """
        broken = MagicMock(spec=VectorStore)
        broken.get.side_effect = RuntimeError("vector backend down")

        assert sync_vector_metadata(broken, "doc1", {"content_tags": {}}) is False

    def test_upsert_failure_is_swallowed_not_raised(self) -> None:
        broken = MagicMock(spec=VectorStore)
        broken.get.return_value = {"item_id": "doc1", "vector": [1.0], "metadata": {}}
        broken.upsert.side_effect = RuntimeError("write failed")

        assert sync_vector_metadata(broken, "doc1", {"content_tags": {"x": 1}}) is False


class TestDivergence:
    """The read-only predicate the backfill counts with."""

    def test_agreement_is_not_divergence(self) -> None:
        tags = {"signal_quality": "noise"}
        assert not vector_metadata_diverges(
            {"content_tags": tags}, {"content_tags": tags}
        )

    def test_missing_on_the_row_diverges(self) -> None:
        assert vector_metadata_diverges({"content_tags": {"a": 1}}, {})

    def test_missing_on_the_document_diverges(self) -> None:
        assert vector_metadata_diverges({}, {"content_tags": {"a": 1}})

    def test_different_value_diverges(self) -> None:
        assert vector_metadata_diverges(
            {"content_tags": {"signal_quality": "noise"}},
            {"content_tags": {"signal_quality": "standard"}},
        )

    def test_unsynced_keys_do_not_count(self) -> None:
        """A row's own excerpt differing from the document is not divergence."""
        assert not vector_metadata_diverges(
            {"content": "the whole document"}, {"content": "excerpt…"}
        )

    def test_none_bags_agree(self) -> None:
        assert not vector_metadata_diverges(None, None)

    def test_predicate_and_writer_agree(self, store: SQLiteVectorStore) -> None:
        """Whatever the writer rewrites, the predicate calls divergent."""
        _seed(store, content_tags={"signal_quality": "standard"})
        doc_metadata = {"content_tags": {"signal_quality": "noise"}}
        row = store.get("doc1")
        assert row is not None

        assert vector_metadata_diverges(doc_metadata, row["metadata"])
        assert sync_vector_metadata(store, "doc1", doc_metadata)

        row = store.get("doc1")
        assert row is not None
        assert not vector_metadata_diverges(doc_metadata, row["metadata"])


class TestResolveVectorStore:
    def test_returns_the_configured_store(self) -> None:
        registry = MagicMock()
        registry.knowledge.vector_store = "the-store"
        assert resolve_vector_store(registry) == "the-store"

    def test_degrades_to_none_when_unavailable(self) -> None:
        """A deployment without a vector store must still be able to demote."""
        registry = MagicMock()
        type(registry.knowledge).vector_store = property(
            lambda _self: (_ for _ in ()).throw(RuntimeError("no vector backend"))
        )
        assert resolve_vector_store(registry) is None
