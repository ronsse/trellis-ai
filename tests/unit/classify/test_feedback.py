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
