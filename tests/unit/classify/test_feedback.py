"""Focused unit tests for ``trellis.classify.feedback.apply_noise_tags``.

The companion ``test_feedback_loop.py`` exercises the function against a
real SQLite document store. These tests use ``MagicMock(spec=...)`` to
isolate the function from the store backend and pin down the put/get
contract precisely.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from trellis.classify.feedback import apply_noise_tags
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.vector import VectorStore


def _make_store(docs: dict[str, dict]) -> MagicMock:
    """Build a MagicMock DocumentStore that returns ``docs`` from get()."""
    store = MagicMock(spec=DocumentStore)
    store.get.side_effect = docs.get
    return store


class TestEmptyInput:
    """Empty candidate list short-circuits without touching the store."""

    def test_empty_candidates_returns_zero(self) -> None:
        store = MagicMock(spec=DocumentStore)
        assert apply_noise_tags([], store) == 0
        store.get.assert_not_called()
        store.put.assert_not_called()


class TestHappyPath:
    """A known item gets stamped with signal_quality='noise' and classified_at."""

    def test_marks_signal_quality_and_stamps_classified_at(self) -> None:
        docs = {
            "doc1": {
                "content": "noisy content",
                "metadata": {
                    "content_tags": {
                        "domain": ["api"],
                        "signal_quality": "standard",
                    }
                },
            }
        }
        store = _make_store(docs)

        updated = apply_noise_tags(["doc1"], store)

        assert updated == 1
        store.put.assert_called_once()
        args = store.put.call_args
        item_id, content, metadata = args.args
        assert item_id == "doc1"
        assert content == "noisy content"
        tags = metadata["content_tags"]
        assert tags["signal_quality"] == "noise"
        assert tags["domain"] == ["api"]
        # classified_at should be a parseable ISO timestamp
        stamp = tags["classified_at"]
        # If this fails, the function emitted an unparseable stamp
        datetime.fromisoformat(stamp)

    def test_also_stamps_importance_scored_at(self) -> None:
        """Flipping signal_quality to "noise" shifts the
        :func:`compute_importance` boost — so the importance score
        effectively re-aged. ``apply_noise_tags`` must stamp
        ``importance_scored_at`` alongside ``classified_at``
        (adr-importance-score-freshness §3.3 close)."""
        docs = {
            "doc1": {
                "content": "noisy content",
                "metadata": {
                    "content_tags": {
                        "domain": ["api"],
                        "signal_quality": "standard",
                    }
                },
            }
        }
        store = _make_store(docs)
        apply_noise_tags(["doc1"], store)

        _, _, metadata = store.put.call_args.args
        tags = metadata["content_tags"]
        importance_stamp = tags["importance_scored_at"]
        # Same instant as classified_at — both reflect this rescoring event.
        assert importance_stamp == tags["classified_at"]
        # Parseable ISO timestamp.
        datetime.fromisoformat(importance_stamp)


class TestEdgeCaseMissingDocument:
    """When ``store.get`` returns None, the candidate is skipped silently."""

    def test_missing_doc_does_not_increment_counter(self) -> None:
        store = _make_store(docs={})
        updated = apply_noise_tags(["nonexistent"], store)
        assert updated == 0
        store.put.assert_not_called()


class TestPartialBatch:
    """Mixed valid + missing items — only the valid one updates."""

    def test_only_existing_doc_updated(self) -> None:
        docs = {
            "doc_present": {
                "content": "real",
                "metadata": {"content_tags": {"signal_quality": "standard"}},
            }
        }
        store = _make_store(docs)
        updated = apply_noise_tags(["doc_present", "doc_missing"], store)
        assert updated == 1
        assert store.put.call_count == 1


class TestVectorWriteThrough:
    """The demotion has to reach the vector row too (trellis-ai#338).

    A vector row's metadata is a snapshot taken at embed time, so a
    demotion written only through ``document_store.put`` leaves
    ``SemanticSearch`` serving the item's pre-demotion tags. Production
    measured 45 noise-tagged documents and not one whose row agreed.
    """

    @staticmethod
    def _docs() -> dict[str, dict]:
        return {
            "doc1": {
                "content": "noisy content",
                "metadata": {"content_tags": {"signal_quality": "standard"}},
            }
        }

    def test_mirrors_the_tags_onto_the_vector_row(self) -> None:
        store = _make_store(self._docs())
        vector_store = MagicMock(spec=VectorStore)
        vector_store.get.return_value = {
            "item_id": "doc1",
            "vector": [0.1, 0.2, 0.3],
            "metadata": {"content": "excerpt", "content_tags": {}},
        }

        apply_noise_tags(["doc1"], store, vector_store)

        vector_store.upsert.assert_called_once()
        item_id, _vector, metadata = vector_store.upsert.call_args.args
        assert item_id == "doc1"
        assert metadata["content_tags"]["signal_quality"] == "noise"

    def test_re_embeds_nothing(self) -> None:
        """The row's own vector is handed straight back — metadata-only."""
        store = _make_store(self._docs())
        vector_store = MagicMock(spec=VectorStore)
        vector_store.get.return_value = {
            "item_id": "doc1",
            "vector": [0.1, 0.2, 0.3],
            "metadata": {"content": "excerpt"},
        }

        apply_noise_tags(["doc1"], store, vector_store)

        _, vector, metadata = vector_store.upsert.call_args.args
        assert vector == [0.1, 0.2, 0.3]
        assert metadata["content"] == "excerpt"

    def test_document_write_happens_first(self) -> None:
        """The document row is the authority a re-run repairs from."""
        store = _make_store(self._docs())
        vector_store = MagicMock(spec=VectorStore)
        vector_store.get.return_value = {
            "item_id": "doc1",
            "vector": [0.1],
            "metadata": {},
        }
        order: list[str] = []
        store.put.side_effect = lambda *a, **k: order.append("document")
        vector_store.upsert.side_effect = lambda *a, **k: order.append("vector")

        apply_noise_tags(["doc1"], store, vector_store)

        assert order == ["document", "vector"]

    def test_vector_backend_failure_does_not_lose_the_demotion(self) -> None:
        """Fail soft: the tag landed, so the call must not raise."""
        store = _make_store(self._docs())
        vector_store = MagicMock(spec=VectorStore)
        vector_store.get.side_effect = RuntimeError("vector backend down")

        assert apply_noise_tags(["doc1"], store, vector_store) == 1
        store.put.assert_called_once()

    def test_omitting_the_vector_store_still_demotes(self) -> None:
        """A deployment with no vector store can still demote a document."""
        store = _make_store(self._docs())

        assert apply_noise_tags(["doc1"], store) == 1
        store.put.assert_called_once()
