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
from trellis.retrieve.noise import DEFAULT_SIGNAL_QUALITY_SPEC, NOISE_REJECTION_REASON
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.parent_verdicts import (
    ParentVerdictCache,
    partition_by_parent_signal_quality,
)
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
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
    kwargs.setdefault("clearance", "restricted")
    return builder.build(
        intent=INTENT,
        budget=PackBudget(max_items=25, max_tokens=100_000),
        **kwargs,
    )


def _build_sectioned(builder: PackBuilder, **kwargs: Any) -> Any:
    kwargs.setdefault("clearance", "restricted")
    return builder.build_sectioned(
        intent=INTENT,
        sections=[SectionRequest(name="s", max_items=25, max_tokens=100_000)],
        **kwargs,
    )


def _served_ids(pack: Any) -> set[str]:
    return {item.item_id for item in pack.items}


def _sectioned_served_ids(pack: Any) -> set[str]:
    return {item.item_id for section in pack.sections for item in section.items}


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


class TestHeldOutTelemetryReportsTheGateRun:
    """A held-out row must report the gate run, not a hardcoded default.

    ``_withheld_flat_telemetry``/``_withheld_sectioned_telemetry`` used to
    hardcode ``parent_verdict_degraded=False`` regardless of what actually
    happened. The gate runs once per strategy pass, before the holdout
    draw (:func:`trellis.core.pack_holdout.is_held_out`), so a store that
    failed must still be visible on the withheld row. ``holdout_rate=1.0``
    withholds every pack deterministically.
    """

    def test_flat_holdout_reports_degradation(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_chunk(doc_store, "any-parent", 0)
        event_log = _FakeEventLog()
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)],
            document_store=_RaisingDocumentStore(doc_store),
            event_log=event_log,
            holdout_rate=1.0,
        )

        pack = _build(builder)

        assert pack.items == []
        assembled = [
            e for e in event_log.events if e.event_type == EventType.PACK_ASSEMBLED
        ]
        assert len(assembled) == 1
        payload = assembled[0].payload
        assert payload["holdout"] is True
        assert payload["parent_verdict_degraded"] is True

    def test_sectioned_holdout_reports_degradation(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_chunk(doc_store, "any-parent", 0)
        event_log = _FakeEventLog()
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)],
            document_store=_RaisingDocumentStore(doc_store),
            event_log=event_log,
            holdout_rate=1.0,
        )

        pack = _build_sectioned(builder)

        assert _sectioned_served_ids(pack) == set()
        assembled = [
            e for e in event_log.events if e.event_type == EventType.PACK_ASSEMBLED
        ]
        assert len(assembled) == 1
        payload = assembled[0].payload
        assert payload["holdout"] is True
        assert payload["parent_verdict_degraded"] is True


class TestParentContributesOnlyNoiseVerdict:
    """The parent lends a chunk only its noise-ness, never the caller's spec.

    ``partition_by_parent_signal_quality`` used to apply the caller's
    whole ``spec`` to the parent's metadata. Under a curation spec that
    allowlists noise (``{"in": ["noise"]}``), a parent tagged ``standard``
    fails that allowlist too — withholding a noise-tagged chunk the caller
    explicitly asked to see, via a parent that was never noise at all. The
    default-spec scenario (standard chunk, noise parent, withheld) is
    already covered by ``TestParentVerdictsWithholdChunks
    .test_chunk_withheld_when_parent_is_noise`` above — both the old and
    fixed logic agree there, so it does not discriminate this bug.
    """

    def test_noise_chunk_with_standard_parent_served_under_noise_allowlist(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "parent-standard-2", signal_quality="standard")
        chunk_id = _put_chunk(doc_store, "parent-standard-2", 0, signal_quality="noise")
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(builder, tag_filters={"signal_quality": {"in": ["noise"]}})

        assert chunk_id in _served_ids(pack)

    def test_standard_chunk_with_noise_parent_withheld_under_noise_allowlist(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """The item's own check already excludes a standard-tagged chunk
        under an allowlist that only admits noise, independent of what the
        parent-level fix does with this noise-tagged parent. Only the
        outcome (withheld) is asserted, not which gate produced it.
        """
        _put_parent(doc_store, "parent-noise-4", signal_quality="noise")
        chunk_id = _put_chunk(doc_store, "parent-noise-4", 0, signal_quality="standard")
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build(builder, tag_filters={"signal_quality": {"in": ["noise"]}})

        assert chunk_id not in _served_ids(pack)


class TestParentResolvedOnlyForRealChunkId:
    """A non-chunk item never inherits a parent verdict.

    ``resolve_parent_id`` trusts ``metadata['parent_doc_id']``
    unconditionally when present, so a non-chunk whose metadata happens to
    carry that key (caller-set, not written by the chunker) would
    otherwise fetch and apply another document's verdict to itself.
    """

    def test_non_chunk_with_parent_doc_id_is_served_and_get_not_called(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "noise-document", signal_quality="noise")
        doc_store.put(
            "solo-document-2",
            INTENT,
            {
                "content_tags": {"signal_quality": "standard"},
                "parent_doc_id": "noise-document",
            },
        )
        spy = _CountingDocumentStore(doc_store)
        builder = PackBuilder(strategies=[KeywordSearch(doc_store)], document_store=spy)

        pack = _build(builder)

        assert "solo-document-2" in _served_ids(pack)
        assert spy.get_calls == []


class TestBuildSectionedAppliesParentVerdicts:
    """``build_sectioned`` must share the parent-verdict gate with ``build``.

    A ``ParentVerdictCache(None)`` (no document store wired in) would pass
    every other test silently, since ``resolve`` always returns ``None``
    for a cache with no store — this pins the sectioned path's
    ``document_store=`` wiring behaviourally.
    """

    def test_sectioned_chunk_withheld_when_parent_is_noise(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "parent-noise-sectioned", signal_quality="noise")
        chunk_id = _put_chunk(doc_store, "parent-noise-sectioned", 0)
        builder = PackBuilder(
            strategies=[KeywordSearch(doc_store)], document_store=doc_store
        )

        pack = _build_sectioned(builder)

        assert chunk_id not in _sectioned_served_ids(pack)
        assert (
            pack.metadata["withholding"]["by_reason"].get(NOISE_REJECTION_REASON) == 1
        )


class _OneBadRowDocumentStore(_CountingDocumentStore):
    """``get`` raises for one id only: the one-unreadable-row failure."""

    def __init__(self, inner: SQLiteDocumentStore, bad_id: str) -> None:
        super().__init__(inner)
        self._bad_id = bad_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        if doc_id == self._bad_id:
            self.get_calls.append(doc_id)
            msg = "row unreadable"
            raise RuntimeError(msg)
        return super().get(doc_id)


class TestOneFailedParentLeavesTheRestChecked:
    """A failed fetch caches a miss for that parent only, not for the pack."""

    def test_noise_parent_after_a_failed_one_still_withholds(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "parent-noise-5", signal_quality="noise")
        bad, noisy = (
            PackItem(
                item_id=_put_chunk(doc_store, parent, 0),
                item_type="document",
                metadata={"parent_doc_id": parent},
            )
            for parent in ("parent-bad", "parent-noise-5")
        )
        store = _OneBadRowDocumentStore(doc_store, "parent-bad")
        cache = ParentVerdictCache(store)

        _, withheld = partition_by_parent_signal_quality(
            [bad, noisy], DEFAULT_SIGNAL_QUALITY_SPEC, cache
        )

        assert cache.degraded is True
        assert store.get_calls == ["parent-bad", "parent-noise-5"]
        assert [item.item_id for item in withheld] == [noisy.item_id]
