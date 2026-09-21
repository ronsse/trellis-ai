"""The document/vector seam's own contract (#360).

Everything else in this module's coverage is a *caller's* test that happens
to route through here. That is the wrong place to pin the seam's own
promises — a caller's test dies for its own reasons, and the two properties
that matter most (the document row survives a mirror failure; the mirror is
bidirectional) are ones no caller asserts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from trellis.core.document_write import DocumentWriteResult, put_document
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

VECTOR_DOWN = "vector backend unreachable"
DOCUMENT_DOWN = "document backend unreachable"


@pytest.fixture
def document_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


@pytest.fixture
def vector_store(tmp_path: Path):
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield store
    store.close()


class _ExplodingVectorStore:
    """A vector store whose every read throws.

    Not a ``MagicMock``: the point is that an *arbitrary* backend failure is
    absorbed, and a mock configured with ``side_effect`` tests the one
    exception type the author thought of.
    """

    def get(self, item_id: str) -> dict[str, Any] | None:
        raise RuntimeError(VECTOR_DOWN)

    def upsert(self, item_id: str, vector: list[float], metadata: Any) -> None:
        msg = "must not be reached"
        raise AssertionError(msg)


class TestTheDocumentPlaneIsAuthoritative:
    def test_a_mirror_failure_does_not_lose_the_document_write(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The write has already landed by the time the mirror runs.

        Raising here would report a *lost* write to a caller whose row is
        on disk, which is strictly worse than the divergence it announces.
        """
        result = put_document(
            document_store,
            _ExplodingVectorStore(),  # type: ignore[arg-type]
            "d1",
            "body",
            {"content_tags": {"signal_quality": "noise"}},
        )

        assert result.mirror == "failed"
        stored = document_store.get("d1")
        assert stored is not None
        assert stored["content"] == "body"
        assert stored["metadata"]["content_tags"] == {"signal_quality": "noise"}

    def test_a_mirror_failure_is_loud(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """Fail-soft, not fail-silent — the pair is the whole contract.

        A soft failure nobody can see is how a vector row goes stale
        without anything to read afterwards, which is #338.
        """
        with capture_logs() as logs:
            put_document(
                document_store,
                _ExplodingVectorStore(),  # type: ignore[arg-type]
                "d1",
                "body",
                {"content_tags": {"signal_quality": "noise"}},
            )

        events = [entry["event"] for entry in logs]
        assert "document_write_mirror_failed" in events
        warning = next(
            entry for entry in logs if entry["event"] == "document_write_mirror_failed"
        )
        assert warning["log_level"] == "warning"
        assert warning["doc_id"] == "d1"
        # The line names the repair, because an operator reading it at 3am
        # needs the command and not a restatement of the event name.
        assert "resync-vector-metadata" in warning["consequence"]

    def test_a_document_store_failure_propagates(
        self, vector_store: SQLiteVectorStore
    ) -> None:
        """The opposite asymmetry, and it is deliberate.

        A caller that reads a lost authoritative write as done is the
        failure the fail-soft mirror is *not* allowed to generalise into.
        """

        class _ExplodingDocumentStore:
            def put(self, *args: Any, **kwargs: Any) -> str:
                raise RuntimeError(DOCUMENT_DOWN)

        with pytest.raises(RuntimeError, match=DOCUMENT_DOWN):
            put_document(
                _ExplodingDocumentStore(),  # type: ignore[arg-type]
                vector_store,
                "d1",
                "body",
                {},
            )


class TestTheMirrorIsBidirectional:
    def test_a_key_removed_from_the_document_is_removed_from_the_row(
        self, document_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """ "Agreeing" has to mean agreeing.

        A one-way mirror leaves a deleted tag alive in the snapshot
        forever — the same staleness #338 is, one plane down. This is why
        ``metadata`` is documented as the *complete* bag.
        """
        vector_store.upsert(
            "d1",
            [0.1, 0.2, 0.3],
            {"doc_id": "d1", "content_tags": {"signal_quality": "noise"}},
        )

        result = put_document(document_store, vector_store, "d1", "body", {})

        assert result.mirror == "synced"
        assert "content_tags" not in vector_store.get("d1")["metadata"]

    def test_every_mirrored_key_travels_together(
        self, document_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """The three keys are one set, not three independent syncs.

        ``auto_importance`` without the ``importance_scored_at`` stamp
        inside ``content_tags`` is the broken pair ``_apply_importance``
        raises on, and #337's lifecycle key joined the set for the same
        reason: a writer that carried a subset is exactly the bug.
        """
        vector_store.upsert("d1", [0.1, 0.2, 0.3], {"doc_id": "d1"})
        bag = {
            "content_tags": {"importance_scored_at": "2026-09-12T00:00:00Z"},
            "auto_importance": 0.42,
            LIFECYCLE_KEY: "archived",
        }

        put_document(document_store, vector_store, "d1", "body", bag)

        row = vector_store.get("d1")["metadata"]
        assert row["content_tags"] == bag["content_tags"]
        assert row["auto_importance"] == 0.42
        assert row[LIFECYCLE_KEY] == "archived"

    def test_the_rows_own_keys_are_not_clobbered(
        self, document_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """Mirroring a fixed key set, never the bag.

        ``content`` on a vector row is its embed-time excerpt (#338) and is
        the last copy of the text a pack consumer sees; copying the
        document bag wholesale would overwrite it with whatever the
        document happened to carry.
        """
        vector_store.upsert(
            "d1", [0.1, 0.2, 0.3], {"doc_id": "d1", "content": "the excerpt"}
        )

        put_document(
            document_store,
            vector_store,
            "d1",
            "body",
            {"auto_importance": 0.42, "source_path": "/notes/a.md"},
        )

        row = vector_store.get("d1")["metadata"]
        assert row["content"] == "the excerpt"
        assert row["doc_id"] == "d1"
        assert "source_path" not in row


class TestTheOutcomeDistinguishesItsNoOps:
    def test_no_vector_store_configured(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        assert put_document(
            document_store, None, "d1", "body", {}
        ) == DocumentWriteResult(doc_id="d1", mirror="no_store")

    def test_a_document_that_was_never_embedded(
        self, document_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """The ordinary case, and it must not read as a failure.

        Anything the embed hook has not reached has no row; reporting that
        as divergence would make the signal fire on most of a corpus.
        """
        assert put_document(
            document_store, vector_store, "d1", "body", {}
        ) == DocumentWriteResult(doc_id="d1", mirror="absent")

    def test_a_row_that_already_agrees_is_not_rewritten(
        self, document_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """The short-circuit is what makes routing content writes cheap.

        A content writer re-embeds *after* the seam, so the seam's mirror
        is pure overhead on that path — one ``get``, no upsert — and that
        is the whole cost argument for a single routing rule instead of a
        per-site judgement about which writes "need" mirroring.
        """
        bag = {"auto_importance": 0.42}
        vector_store.upsert("d1", [0.1, 0.2, 0.3], {"doc_id": "d1", **bag})

        upserts: list[str] = []
        original = vector_store.upsert
        vector_store.upsert = lambda *a, **k: (  # type: ignore[method-assign]
            upserts.append(a[0]),
            original(*a, **k),
        )[1]

        assert put_document(document_store, vector_store, "d1", "body", bag).mirror == (
            "unchanged"
        )
        assert upserts == []


class TestPreserveUpdatedAtIsForwarded:
    def test_a_metadata_only_write_does_not_restamp_the_row(
        self, document_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """The #406 stamp, which only the caller knows the truth about.

        A metadata-only write that re-stamps ``updated_at`` hands
        ``KeywordSearch``'s recency decay the sweep's own clock as the
        document's age.
        """
        put_document(document_store, vector_store, "d1", "body", {})
        before = document_store.get("d1")["updated_at"]

        put_document(
            document_store,
            vector_store,
            "d1",
            "body",
            {"auto_importance": 0.42},
            preserve_updated_at=True,
        )
        assert document_store.get("d1")["updated_at"] == before

        put_document(document_store, vector_store, "d1", "revised body", {})
        assert document_store.get("d1")["updated_at"] != before
