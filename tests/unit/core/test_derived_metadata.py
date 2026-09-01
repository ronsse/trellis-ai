"""Tests for :mod:`trellis.core.derived_metadata` (#421).

The unit under test is the re-read seam every slow batch writer now goes
through. The two call sites (``worker enrich``, ``classify shadow``) pin the
end-to-end race in their own files; this file pins the seam's contract, and
in particular the two ways it could silently do nothing: a merge that drops
the caller's updates, and a ``content_changed`` flag that can never be true.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from tests.document_recency import fake_document_clock
from trellis.core.derived_metadata import apply_derived_metadata
from trellis.stores.base.document import DocumentStore
from trellis.stores.sqlite.document import SQLiteDocumentStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def document_store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


def _tag(_current: dict[str, Any]) -> dict[str, Any]:
    """A caller that owns exactly one key and derives nothing from the prior."""
    return {"derived": "yes"}


class TestTheMergeItself:
    def test_updates_are_applied(self, document_store: SQLiteDocumentStore) -> None:
        """The obvious half, pinned because a no-op merge is the worst failure.

        A seam that silently drops its updates would leave every batch worker
        reporting successful enrichment of rows it never touched — the exact
        shape of the defect this module exists to close, one layer up.
        """
        document_store.put("d1", "body", {"title": "T"})
        write = apply_derived_metadata(document_store, "d1", _tag)

        assert write.written is True
        assert write.vanished is False
        stored = document_store.get("d1")
        assert stored["metadata"]["derived"] == "yes"
        assert write.metadata == stored["metadata"]

    def test_keys_the_caller_does_not_own_are_carried_through(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        document_store.put("d1", "body", {"title": "T", "source_system": "s"})
        apply_derived_metadata(document_store, "d1", _tag)

        metadata = document_store.get("d1")["metadata"]
        assert metadata["title"] == "T"
        assert metadata["source_system"] == "s"

    def test_content_is_never_written_by_the_caller(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """``snapshot_content`` is a comparand, not a payload.

        The whole point is that the row's own content is what lands, so the
        argument the caller passes for detection must be structurally unable
        to reach the store.
        """
        document_store.put("d1", "stored body", {})
        apply_derived_metadata(
            document_store, "d1", _tag, snapshot_content="a stale snapshot"
        )
        assert document_store.get("d1")["content"] == "stored body"

    def test_updates_are_built_against_the_current_metadata(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """A prior-dependent update must see the row as it is *now*.

        This is the metadata half of the lost update, and it is the half a
        content-hash check cannot see: a concurrent metadata write leaves
        ``content`` byte-identical.
        """
        document_store.put("d1", "body", {"counter": 41})
        seen: list[dict[str, Any]] = []

        def bump(current: dict[str, Any]) -> dict[str, Any]:
            seen.append(dict(current))
            return {"counter": current["counter"] + 1}

        apply_derived_metadata(document_store, "d1", bump)
        assert seen == [{"counter": 41}]
        assert document_store.get("d1")["metadata"]["counter"] == 42

    def test_the_write_preserves_updated_at(
        self,
        document_store: SQLiteDocumentStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The flag this module exists to make honest.

        Paired with an assertion on the *seeded* stamp so the vacuity caveat
        on ``fake_document_clock`` cannot make it pass silently.
        """
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]
        clock["now"] = now - timedelta(days=365)
        document_store.put("d1", "body", {})
        before = document_store.get("d1")["updated_at"]
        assert before == (now - timedelta(days=365)).isoformat()

        clock["now"] = now
        apply_derived_metadata(document_store, "d1", _tag)
        assert document_store.get("d1")["updated_at"] == before


class TestDetection:
    def test_content_changed_fires_when_the_snapshot_is_stale(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        document_store.put("d1", "the new body", {})
        write = apply_derived_metadata(
            document_store, "d1", _tag, snapshot_content="the old body"
        )
        assert write.content_changed is True
        # Merged, not refused — the enrichment still lands.
        assert write.written is True
        assert document_store.get("d1")["content"] == "the new body"

    def test_content_changed_is_false_when_nothing_raced(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The counter must be able to read zero as well as non-zero.

        A detector wired to a constant is the failure this repo keeps
        producing; both arms are asserted so neither can drift to one.
        """
        document_store.put("d1", "body", {})
        write = apply_derived_metadata(
            document_store, "d1", _tag, snapshot_content="body"
        )
        assert write.content_changed is False

    def test_content_changed_is_false_when_no_snapshot_was_supplied(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """No comparand means *unmeasured*, and unmeasured reports as false.

        A caller with nothing to compare against must not have a race
        manufactured for it out of ``None != "body"``.
        """
        document_store.put("d1", "body", {})
        assert apply_derived_metadata(document_store, "d1", _tag).content_changed is (
            False
        )


class TestVanishedRow:
    def test_a_deleted_row_is_not_resurrected(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """``put`` on a missing id inserts, which would undo a delete.

        Un-guarded, a batch worker holding a snapshot of a document that was
        deleted mid-run would write it back — pre-slow-work content, a fresh
        ``created_at``, and no record that it had ever been removed.
        """
        write = apply_derived_metadata(
            document_store, "gone", _tag, snapshot_content="body"
        )
        assert write.written is False
        assert write.vanished is True
        assert write.metadata is None
        assert document_store.get("gone") is None

    def test_the_update_builder_is_not_called_for_a_vanished_row(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        calls: list[dict[str, Any]] = []
        apply_derived_metadata(
            document_store, "gone", lambda current: calls.append(current) or {}
        )
        assert calls == []


class TestFailuresPropagate:
    """A store error must not present as "row vanished" or as a clean write.

    Reporting a successful enrichment of a row the store never returned is
    strictly worse than failing the run, and a read error resolved to
    ``vanished`` would look identical to a concurrent delete in the counters.
    """

    def test_a_read_failure_is_not_swallowed(self) -> None:
        store = MagicMock(spec=DocumentStore)
        store.get.side_effect = RuntimeError("store is down")
        with pytest.raises(RuntimeError, match="store is down"):
            apply_derived_metadata(store, "d1", _tag)

    def test_a_write_failure_is_not_swallowed(self) -> None:
        store = MagicMock(spec=DocumentStore)
        store.get.return_value = {"doc_id": "d1", "content": "body", "metadata": {}}
        store.put.side_effect = RuntimeError("disk full")
        with pytest.raises(RuntimeError, match="disk full"):
            apply_derived_metadata(store, "d1", _tag)
