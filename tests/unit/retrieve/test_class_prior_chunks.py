"""The class prior's opt-in chunk scope (#986).

``scope="parents"`` (the default) withholds a document form's parents only.
``scope="parents_and_chunks"`` also withholds a chunk whose *parent* carries a
configured form. Only a chunk id (``<parent>#chunk-N``) resolves through a
parent, by the same lookup the archived and noise gates make: the item's
``metadata["parent_doc_id"]``, else the id's own prefix (a vector snapshot
embedded before that key existed), read through the build's parent cache. A
plain item that merely names a parent is not a chunk and borrows nothing.

Pure matching lives in the first classes; the later ones drive a real
``SQLiteDocumentStore`` through ``PackBuilder`` so the document-store read
count and the unhappy paths are observed, not asserted against a mock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.class_prior import (
    CLASS_PRIOR_REJECTION_REASON,
    CLASS_PRIOR_SCOPE_ENV,
    DEFAULT_SCOPE,
    OFF,
    ClassPriorConfig,
    apply_class_prior,
    class_prior_telemetry,
    effective_form,
    resolve_class_prior_config,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch, SemanticSearch
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore

if TYPE_CHECKING:
    from pathlib import Path

INTENT = "distinctive narwhal content"
CHUNKS = "parents_and_chunks"


def _chunk_item(
    item_id: str, *, parent: str | None, form: str | None = None
) -> PackItem:
    metadata: dict[str, Any] = {}
    if parent is not None:
        metadata["parent_doc_id"] = parent
    if form is not None:
        metadata["document_form"] = form
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="text",
        relevance_score=0.5,
        metadata=metadata,
    )


def _withhold(scope: str, **kwargs: Any) -> ClassPriorConfig:
    return ClassPriorConfig(mode="withhold", scope=scope, **kwargs)  # type: ignore[arg-type]


class TestScopeConfig:
    def test_default_scope_is_parents(self) -> None:
        assert DEFAULT_SCOPE == "parents"
        assert OFF.scope == "parents"
        assert ClassPriorConfig(mode="withhold").scope == "parents"

    def test_invalid_scope_is_refused(self) -> None:
        with pytest.raises(ValueError, match="scope"):
            ClassPriorConfig(scope="chunks")  # type: ignore[arg-type]

    def test_env_unset_resolves_to_parents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_SCOPE_ENV, raising=False)
        assert resolve_class_prior_config().scope == "parents"

    @pytest.mark.parametrize("raw", [CHUNKS, "  Parents_And_Chunks "])
    def test_env_opts_in_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_SCOPE_ENV, raw)
        assert resolve_class_prior_config().scope == CHUNKS

    def test_unrecognised_env_degrades_to_parents_and_never_logs_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_SCOPE_ENV, "bogus-scope-value")
        with capture_logs() as logs:
            config = resolve_class_prior_config()
        assert config.scope == "parents"
        warned = [e for e in logs if e["event"] == "class_prior_scope_env_unrecognised"]
        assert len(warned) == 1
        assert warned[0]["effective"] == "parents"
        assert "bogus-scope-value" not in str(logs)

    def test_scope_does_not_move_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASS_PRIOR_SCOPE_ENV, CHUNKS)
        monkeypatch.delenv("TRELLIS_CLASS_PRIOR_MODE", raising=False)
        assert resolve_class_prior_config().mode == "off"


class TestTelemetryScope:
    def test_default_scope_leaves_the_payload_unchanged(self) -> None:
        assert class_prior_telemetry(OFF) == {
            "mode": "off",
            "forms": ["conversation"],
            "axes": ["keyword"],
        }
        assert "scope" not in class_prior_telemetry(_withhold("parents"))

    def test_chunk_scope_is_visible(self) -> None:
        assert class_prior_telemetry(_withhold(CHUNKS)) == {
            "mode": "withhold",
            "forms": ["conversation"],
            "axes": ["keyword"],
            "scope": CHUNKS,
        }


class TestApplyWithParentResolver:
    def _resolver(self, forms: dict[str, str | None]) -> tuple[Any, list[str]]:
        calls: list[str] = []

        def parent_form_of(item: PackItem) -> str | None:
            calls.append(item.item_id)
            return forms.get(item.item_id)

        return parent_form_of, calls

    def test_default_scope_never_consults_the_resolver(self) -> None:
        resolver, calls = self._resolver({"c0": "conversation"})
        items = [_chunk_item("c0", parent="p")]
        kept, rejected = apply_class_prior(
            items,
            config=_withhold("parents"),
            strategy_name="keyword",
            parent_form_of=resolver,
        )
        assert kept == items
        assert rejected == []
        assert calls == []

    def test_chunk_scope_withholds_a_chunk_of_a_configured_form(self) -> None:
        resolver, _ = self._resolver({"c0": "conversation"})
        kept, rejected = apply_class_prior(
            [_chunk_item("c0", parent="p"), _chunk_item("m0", parent=None)],
            config=_withhold(CHUNKS),
            strategy_name="keyword",
            parent_form_of=resolver,
        )
        assert [i.item_id for i in kept] == ["m0"]
        assert [(r.item_id, r.reason, r.strategy_source) for r in rejected] == [
            ("c0", CLASS_PRIOR_REJECTION_REASON, "keyword")
        ]

    def test_an_items_own_form_wins_and_the_parent_is_not_read(self) -> None:
        resolver, calls = self._resolver({"x": "conversation"})
        kept, rejected = apply_class_prior(
            [_chunk_item("x", parent="p", form="capture")],
            config=_withhold(CHUNKS),
            strategy_name="keyword",
            parent_form_of=resolver,
        )
        assert [i.item_id for i in kept] == ["x"]
        assert rejected == []
        assert calls == []

    def test_a_parent_form_outside_config_forms_is_served(self) -> None:
        resolver, _ = self._resolver({"c0": "capture"})
        kept, rejected = apply_class_prior(
            [_chunk_item("c0", parent="p")],
            config=_withhold(CHUNKS),
            strategy_name="keyword",
            parent_form_of=resolver,
        )
        assert len(kept) == 1
        assert rejected == []

    def test_no_resolver_or_no_answer_serves_the_chunk(self) -> None:
        item = _chunk_item("c0", parent="p")
        resolver, _ = self._resolver({})
        for parent_form_of in (None, resolver):
            kept, rejected = apply_class_prior(
                [item],
                config=_withhold(CHUNKS),
                strategy_name="keyword",
                parent_form_of=parent_form_of,
            )
            assert kept == [item]
            assert rejected == []

    def test_an_unconfigured_axis_never_consults_the_resolver(self) -> None:
        resolver, calls = self._resolver({"c0": "conversation"})
        items = [_chunk_item("c0", parent="p")]
        kept, _ = apply_class_prior(
            items,
            config=_withhold(CHUNKS),
            strategy_name="semantic",
            parent_form_of=resolver,
        )
        assert kept is items
        assert calls == []

    def test_off_with_chunk_scope_is_still_a_true_noop(self) -> None:
        resolver, calls = self._resolver({"c0": "conversation"})
        items = [_chunk_item("c0", parent="p")]
        config = ClassPriorConfig(mode="off", scope=CHUNKS)
        kept, rejected = apply_class_prior(
            items, config=config, strategy_name="keyword", parent_form_of=resolver
        )
        assert kept is items
        assert rejected == []
        assert calls == []

    def test_effective_form_reads_the_parent_only_under_chunk_scope(self) -> None:
        resolver, _ = self._resolver({"c0": "conversation"})
        item = _chunk_item("c0", parent="p")
        assert (
            effective_form(item, config=_withhold(CHUNKS), parent_form_of=resolver)
            == "conversation"
        )
        assert (
            effective_form(item, config=_withhold("parents"), parent_form_of=resolver)
            is None
        )


# --------------------------------------------------------------------------
# Through PackBuilder, against a real document store.
# --------------------------------------------------------------------------


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
    doc_store: SQLiteDocumentStore, parent_id: str, *, form: str | None
) -> None:
    metadata: dict[str, Any] = {"title": parent_id}
    if form is not None:
        metadata["document_form"] = form
    doc_store.put(parent_id, f"{INTENT} parent {parent_id}", metadata)


def _put_chunks(
    doc_store: SQLiteDocumentStore, parent_id: str, count: int
) -> list[str]:
    ids = []
    for index in range(count):
        cid = chunk_doc_id(parent_id, index)
        doc_store.put(cid, f"{INTENT} chunk {index}", {"parent_doc_id": parent_id})
        ids.append(cid)
    return ids


def _builder(
    doc_store: SQLiteDocumentStore,
    config: ClassPriorConfig,
    *,
    store: Any = None,
    **kwargs: Any,
) -> PackBuilder:
    return PackBuilder(
        strategies=[KeywordSearch(doc_store)],
        document_store=store if store is not None else doc_store,
        class_prior_config=config,
        **kwargs,
    )


def _build(builder: PackBuilder) -> Any:
    return builder.build(
        intent=INTENT, budget=PackBudget(max_items=25, max_tokens=100_000)
    )


def _served(pack: Any) -> set[str]:
    return {item.item_id for item in pack.items}


def _reasons(pack: Any) -> dict[str, str]:
    return {r.item_id: r.reason for r in pack.retrieval_report.rejected_items}


class TestPackBuilderChunkScope:
    def test_parents_scope_withholds_the_parent_and_still_serves_its_chunks(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """The shipped behaviour, pinned so the opt-in cannot widen it."""
        _put_parent(doc_store, "conv", form="conversation")
        chunks = _put_chunks(doc_store, "conv", 3)

        pack = _build(_builder(doc_store, _withhold("parents")))

        assert "conv" not in _served(pack)
        assert set(chunks) <= _served(pack)
        assert _reasons(pack) == {"conv": CLASS_PRIOR_REJECTION_REASON}

    def test_chunk_scope_withholds_parent_and_chunks_under_one_reason(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        chunks = _put_chunks(doc_store, "conv", 3)
        _put_parent(doc_store, "note", form="capture")
        kept_chunks = _put_chunks(doc_store, "note", 2)

        pack = _build(_builder(doc_store, _withhold(CHUNKS)))

        assert _served(pack) == {"note", *kept_chunks}
        assert _reasons(pack) == dict.fromkeys(
            ["conv", *chunks], CLASS_PRIOR_REJECTION_REASON
        )
        detail = pack.metadata["withholding"]["class_prior_detail"]
        assert [(d["form"], d["axis"], d["count"]) for d in detail] == [
            ("conversation", "keyword", 4)
        ]

    def test_chunk_scope_adds_no_document_store_read(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """The archived and noise gates already fetched every chunk parent."""
        _put_parent(doc_store, "conv", form="conversation")
        _put_chunks(doc_store, "conv", 3)
        _put_parent(doc_store, "other", form=None)
        _put_chunks(doc_store, "other", 2)

        reads: dict[str, list[str]] = {}
        for scope in ("parents", CHUNKS):
            spy = _CountingDocumentStore(doc_store)
            _build(_builder(doc_store, _withhold(scope), store=spy))
            reads[scope] = sorted(spy.get_calls)

        assert reads[CHUNKS] == reads["parents"]
        assert reads["parents"].count("conv") == 1

    def test_a_chunk_with_no_parent_form_is_served(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "plain", form=None)
        chunks = _put_chunks(doc_store, "plain", 2)

        pack = _build(_builder(doc_store, _withhold(CHUNKS)))

        assert set(chunks) <= _served(pack)
        assert _reasons(pack) == {}

    def test_a_chunk_whose_parent_row_is_gone_is_served(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        chunks = _put_chunks(doc_store, "deleted-parent", 2)

        pack = _build(_builder(doc_store, _withhold(CHUNKS)))

        assert set(chunks) <= _served(pack)

    def test_an_unreadable_store_serves_the_chunks_and_flags_degradation(
        self, doc_store: SQLiteDocumentStore, tmp_path: Path
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        chunks = _put_chunks(doc_store, "conv", 2)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        builder = _builder(
            doc_store,
            _withhold(CHUNKS),
            store=_RaisingDocumentStore(doc_store),
            event_log=event_log,
        )

        pack = _build(builder)

        assert set(chunks) <= _served(pack)
        [event] = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
        assert event.payload["parent_verdict_degraded"] is True
        event_log.close()

    def test_a_plain_item_naming_a_parent_is_not_withheld(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """Only a chunk id resolves through a parent. A plain memory whose
        ``parent_doc_id`` points at a conversation is not a chunk of it, and
        ``ParentVerdictCache.resolve`` already refuses it a borrowed verdict;
        the chunk scope must not take the one the archived gate would not."""
        _put_parent(doc_store, "conv", form="conversation")
        doc_store.put("linked-by-data", INTENT, {"parent_doc_id": "conv"})

        pack = _build(_builder(doc_store, _withhold(CHUNKS)))

        assert "linked-by-data" in _served(pack)
        assert "linked-by-data" not in _reasons(pack)

    def test_a_plain_item_naming_a_parent_adds_no_document_store_read(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "elsewhere", form="conversation")
        doc_store.put("linked-by-data", INTENT, {"parent_doc_id": "elsewhere"})
        spy = _CountingDocumentStore(doc_store)

        _build(_builder(doc_store, _withhold(CHUNKS), store=spy))

        assert "elsewhere" not in spy.get_calls

    def test_a_chunk_id_with_no_parent_key_resolves_through_its_prefix(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """The fallback every other parent-aware reader takes
        (``concentration.parent_id_of``): a chunk row lacking ``parent_doc_id``
        is still a chunk of the document its id names."""
        _put_parent(doc_store, "conv", form="conversation")
        shaped = chunk_doc_id("conv", 7)
        doc_store.put(shaped, INTENT, {})

        pack = _build(_builder(doc_store, _withhold(CHUNKS)))

        assert shaped not in _served(pack)
        assert _reasons(pack)[shaped] == CLASS_PRIOR_REJECTION_REASON

    def test_the_default_scope_ignores_a_data_linked_item(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        doc_store.put("linked-by-data", INTENT, {"parent_doc_id": "conv"})

        pack = _build(_builder(doc_store, _withhold("parents")))

        assert "linked-by-data" in _served(pack)

    def test_chunk_scope_is_stamped_on_the_pack_event(
        self, doc_store: SQLiteDocumentStore, tmp_path: Path
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        _put_chunks(doc_store, "conv", 1)
        event_log = SQLiteEventLog(tmp_path / "events.db")

        _build(_builder(doc_store, _withhold(CHUNKS), event_log=event_log))

        [event] = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
        assert event.payload["class_prior"] == {
            "mode": "withhold",
            "forms": ["conversation"],
            "axes": ["keyword"],
            "scope": CHUNKS,
        }
        event_log.close()

    def test_sectioned_pack_applies_the_chunk_scope(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        chunks = _put_chunks(doc_store, "conv", 2)
        _put_parent(doc_store, "note", form="capture")

        pack = _builder(doc_store, _withhold(CHUNKS)).build_sectioned(
            intent=INTENT,
            sections=[SectionRequest(name="s", max_items=25, max_tokens=100_000)],
        )

        served = {i.item_id for s in pack.sections for i in s.items}
        assert served == {"note"}
        withholding = pack.metadata["withholding"]
        assert withholding["by_reason"][CLASS_PRIOR_REJECTION_REASON] == 1 + len(chunks)
        assert withholding["class_prior_detail"][0]["form"] == "conversation"


class TestSemanticAxisChunkScope:
    """The semantic axis serves ``item_type="vector"`` rows whose metadata is an
    embed-time snapshot, so its chunks can lack a key a keyword row carries."""

    AXES = frozenset({"keyword", "semantic"})

    @pytest.fixture
    def vector_store(self, tmp_path: Path) -> SQLiteVectorStore:
        store = SQLiteVectorStore(tmp_path / "vectors.db")
        yield store
        store.close()

    @staticmethod
    def _embed(_text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def _embed_row(
        self,
        vector_store: SQLiteVectorStore,
        item_id: str,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        vector_store.upsert(
            item_id,
            self._embed(""),
            {"content": f"{INTENT} {item_id}", **(snapshot or {})},
        )

    def _semantic_pack(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> Any:
        builder = PackBuilder(
            strategies=[SemanticSearch(vector_store, embedding_fn=self._embed)],
            document_store=doc_store,
            class_prior_config=_withhold(CHUNKS, axes=self.AXES),
        )
        return _build(builder)

    def test_a_vector_chunk_is_withheld_through_its_parent(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        cid = chunk_doc_id("conv", 0)
        self._embed_row(vector_store, cid, {"parent_doc_id": "conv"})

        pack = self._semantic_pack(doc_store, vector_store)

        assert _served(pack) == set()
        assert _reasons(pack) == {cid: CLASS_PRIOR_REJECTION_REASON}

    def test_an_old_snapshot_without_parent_doc_id_is_withheld_through_its_id(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        """A vector row embedded before ``parent_doc_id`` existed carries no
        link; serving must not wave it through while replay prices it."""
        _put_parent(doc_store, "conv", form="conversation")
        cid = chunk_doc_id("conv", 1)
        self._embed_row(vector_store, cid)

        pack = self._semantic_pack(doc_store, vector_store)

        assert _served(pack) == set()
        assert _reasons(pack) == {cid: CLASS_PRIOR_REJECTION_REASON}

    def test_a_vector_row_that_is_not_a_chunk_is_served(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        _put_parent(doc_store, "conv", form="conversation")
        self._embed_row(vector_store, "plain-memory", {"parent_doc_id": "conv"})

        pack = self._semantic_pack(doc_store, vector_store)

        assert _served(pack) == {"plain-memory"}
