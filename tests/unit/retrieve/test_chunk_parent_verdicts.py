"""A chunk inherits its parent document's archived and noise verdicts.

The collect-seam gates (:func:`trellis.retrieve.lifecycle.partition_archived`,
:func:`trellis.retrieve.noise.partition_by_signal_quality`) read only the
item's own metadata. A chunk row never gets its own ``Lifecycle``/
``content_tags`` update when its *parent* is archived or noise-tagged — both
``apply_noise_tags`` and the retention prune act per document id — so a
verdict on the parent never reached the chunk. See
:mod:`trellis.retrieve.parent_verdicts` for the fix under test here.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.lifecycle import ARCHIVED_REJECTION_REASON
from trellis.retrieve.noise import NOISE_REJECTION_REASON
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.pack import PackBudget
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore

if TYPE_CHECKING:
    from pathlib import Path

INTENT = "distinctive narwhal content"


class _FakeEventLog(EventLog):
    """In-memory event log, enough to capture a ``PACK_ASSEMBLED`` payload."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def _append(self, event: Event) -> None:
        self.events.append(event)

    def get_events(
        self,
        *,
        event_type: EventType | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        order: str = "asc",
    ) -> list[Event]:
        return self.events[:limit]

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        return len(self.events)

    def close(self) -> None:
        pass


class _CountingDocumentStore:
    """Wraps a real store and records every ``get`` call, by id."""

    def __init__(self, inner: SQLiteDocumentStore) -> None:
        self._inner = inner
        self.get_calls: list[str] = []

    def get(self, doc_id: str) -> dict[str, Any] | None:
        self.get_calls.append(doc_id)
        return self._inner.get(doc_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RaisingDocumentStore:
    """A store whose ``get`` always raises — the fetch-failure path."""

    def __init__(self, inner: SQLiteDocumentStore) -> None:
        self._inner = inner

    def get(self, doc_id: str) -> dict[str, Any] | None:
        msg = "store unavailable"
        raise RuntimeError(msg)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def doc_store(tmp_path: Path) -> SQLiteDocumentStore:
    store = SQLiteDocumentStore(tmp_path / "documents.db")
    yield store
    store.close()


def _put_parent(
    doc_store: SQLiteDocumentStore,
    parent_id: str,
    *,
    signal_quality: str | None = "standard",
    archived: bool = False,
) -> None:
    """A parent document, deliberately off-intent so only its chunks match."""
    metadata: dict[str, Any] = {"title": parent_id}
    if signal_quality is not None:
        metadata["content_tags"] = {"signal_quality": signal_quality}
    if archived:
        metadata["lifecycle"] = {"state": "archived"}
    doc_store.put(parent_id, f"unrelated filler for {parent_id}", metadata)


def _put_chunk(
    doc_store: SQLiteDocumentStore,
    parent_id: str,
    index: int,
    *,
    signal_quality: str | None = "standard",
) -> str:
    """A chunk row carrying ``metadata['parent_doc_id']``, tagged on its own."""
    chunk_id = chunk_doc_id(parent_id, index)
    metadata: dict[str, Any] = {"parent_doc_id": parent_id}
    if signal_quality is not None:
        metadata["content_tags"] = {"signal_quality": signal_quality}
    doc_store.put(chunk_id, f"{INTENT} chunk {index} of {parent_id}", metadata)
    return chunk_id


def _build(builder: PackBuilder, **kwargs: Any) -> Any:
    return builder.build(
        intent=INTENT,
        budget=PackBudget(max_items=25, max_tokens=100_000),
        **kwargs,
    )


def _served_ids(pack: Any) -> set[str]:
    return {item.item_id for item in pack.items}


def _rejection_reasons(pack: Any) -> dict[str, str]:
    return {r.item_id: r.reason for r in pack.retrieval_report.rejected_items}


class TestParentVerdictsWithholdChunks:
    def test_chunk_withheld_when_parent_is_noise(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "parent-noise", signal_quality="noise")
        chunk_id = _put_chunk(doc_store, "parent-noise", 0)
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(builder)

        assert chunk_id not in _served_ids(pack)
        assert _rejection_reasons(pack)[chunk_id] == NOISE_REJECTION_REASON

    def test_chunk_withheld_when_parent_is_archived(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "parent-archived", archived=True)
        chunk_id = _put_chunk(doc_store, "parent-archived", 0)
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(builder)

        assert chunk_id not in _served_ids(pack)
        assert _rejection_reasons(pack)[chunk_id] == ARCHIVED_REJECTION_REASON

    def test_chunk_served_when_parent_is_missing(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """No verdict, not a reason to withhold: a missing parent passes."""
        chunk_id = _put_chunk(doc_store, "ghost-parent", 0)
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(builder)

        assert chunk_id in _served_ids(pack)

    def test_chunk_served_when_parent_is_standard(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "parent-standard", signal_quality="standard")
        chunk_id = _put_chunk(doc_store, "parent-standard", 0)
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(builder)

        assert chunk_id in _served_ids(pack)

    def test_caller_spec_admitting_noise_serves_the_chunk(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """Curation tooling that asks for noise gets the parent's noise chunks too."""
        _put_parent(doc_store, "parent-noise-2", signal_quality="noise")
        chunk_id = _put_chunk(doc_store, "parent-noise-2", 0)
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(
            builder,
            tag_filters={"signal_quality": {"in": ["noise", "standard"]}},
        )

        assert chunk_id in _served_ids(pack)


class TestParentFetchStrategy:
    def test_non_chunk_item_causes_no_parent_fetch(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put(
            "solo-document", INTENT, {"content_tags": {"signal_quality": "standard"}}
        )
        spy = _CountingDocumentStore(doc_store)
        builder = PackBuilder(strategies=[KeywordSearch(doc_store)], document_store=spy)

        pack = _build(builder)

        assert "solo-document" in _served_ids(pack)
        assert spy.get_calls == []

    def test_one_fetch_per_distinct_parent(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """Three chunks sharing a parent cost one ``DocumentStore.get()``."""
        _put_parent(doc_store, "shared-parent", signal_quality="standard")
        chunk_ids = [_put_chunk(doc_store, "shared-parent", i) for i in range(3)]
        spy = _CountingDocumentStore(doc_store)
        builder = PackBuilder(strategies=[KeywordSearch(doc_store)], document_store=spy)

        pack = _build(builder)

        assert set(chunk_ids) <= _served_ids(pack)
        assert spy.get_calls.count("shared-parent") == 1


class TestParentFetchDegradation:
    def test_store_error_serves_chunks_and_records_degradation(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """A store error must not fail the pack — serve, and say so visibly."""
        chunk_id = _put_chunk(doc_store, "any-parent", 0)
        event_log = _FakeEventLog()
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)],
            document_store=_RaisingDocumentStore(doc_store),
            event_log=event_log,
        )

        pack = _build(builder)

        assert chunk_id in _served_ids(pack)
        assembled = [
            e for e in event_log.events if e.event_type == EventType.PACK_ASSEMBLED
        ]
        assert len(assembled) == 1
        assert assembled[0].payload["parent_verdict_degraded"] is True
