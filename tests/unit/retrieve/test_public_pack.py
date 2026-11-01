"""The pieces a public-only caller's pack is made of.

``tests/unit/{api,mcp}/test_external_key_differential.py`` run the whole
pipeline against stores that differ only in what the key may not read. These are
the units under it: what an item is rebuilt from (the stored row, never the
vector row), which metadata keys survive (an allowlist), and how a search page
is filled when unreadable rows outrank readable ones.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.unit.retrieve.public_corpus import PLANTED_METADATA, PLANTED_VALUES, leaked
from tests.unit.retrieve.test_sensitivity import label
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.retrieve.public_pack import (
    project_for_public,
    public_search_page,
    readable_stored_copy,
)
from trellis.retrieve.sensitivity import PUBLIC_METADATA_KEYS, public_metadata
from trellis.schemas.document_metadata import DocumentMetadata
from trellis.schemas.pack import PackItem
from trellis.stores.sqlite.document import SQLiteDocumentStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Keys a store's writers (ingest, save, classify, reconcile, the mutation
#: handlers) put on a document's metadata, read by hand from the call sites of
#: ``put_document``, plus every typed field of ``DocumentMetadata`` so a field
#: added there is covered without anyone remembering to list it. The scoring and
#: gating keys are here too: they are dropped from what a public caller sees.
WRITER_KEYS: frozenset[str] = frozenset(
    {
        *PLANTED_METADATA,
        *DocumentMetadata.model_fields,
        "auto_importance",
        "importance_scored_at",
        "domain",
        "lifecycle",
        "sensitivity",
        "data_classification",
        "retrieval_affinities",
        "recency_clock",
        "source_strategy",
        "graph_selection",
        "content_tags_shadow",
        "chunk_index",
        "chunk_count",
        "char_span",
        "updates_doc_id",
        "classified_by",
        "applied_by",
        "changed_by",
        "importance",
    }
)
SENTINEL = "sentinelvalue-do-not-serve"


def _item(item_id: str = "d", **metadata: Any) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="vector text",
        relevance_score=0.4,
        metadata=metadata,
        score_breakdown={"original_score": 12.5, "rrf_total": 0.03},
    )


class TestAllowlist:
    def test_the_allowlist_is_a_title_and_a_form_only(self) -> None:
        assert PUBLIC_METADATA_KEYS == ("title", "document_form")

    def test_the_roster_of_writer_keys_is_not_vacuous(self) -> None:
        assert len(WRITER_KEYS) > 40
        assert {"entity_ids", "supersedes_doc_id", "source_path", "tags"} <= WRITER_KEYS

    @pytest.mark.parametrize("key", sorted(WRITER_KEYS - set(PUBLIC_METADATA_KEYS)))
    def test_a_writers_key_never_reaches_a_public_caller(self, key: str) -> None:
        bag = {"title": "A title", "document_form": "runbook", key: SENTINEL}
        assert public_metadata(bag) == {"title": "A title", "document_form": "runbook"}
        # The strategy stamp is the one key the pipeline itself writes onto an
        # item, so the bag is checked as a stored row (see TestStoredCopy).
        if key not in ("source_strategy", "recency_clock"):
            served = project_for_public(_item(**bag))
            assert set(served.metadata) <= set(PUBLIC_METADATA_KEYS)
            assert not leaked(served.model_dump_json(), [SENTINEL])

    def test_every_key_at_once_leaves_only_the_allowlist(self) -> None:
        bag: dict[str, Any] = {**PLANTED_METADATA, "title": "T", "document_form": "f"}
        bag.update({key: SENTINEL for key in WRITER_KEYS if key not in bag})
        bag.pop("source_strategy")
        bag.pop("recency_clock")
        served = project_for_public(_item(**bag))
        assert served.metadata == {"title": "T", "document_form": "f"}
        dump = served.model_dump_json()
        assert not leaked(dump, (*PLANTED_VALUES, SENTINEL))

    @pytest.mark.parametrize("title", [None, "", "   ", 5, ["a"], {"a": 1}, True])
    def test_a_title_that_is_not_a_non_empty_string_is_left_out(
        self, title: Any
    ) -> None:
        assert "title" not in public_metadata({"title": title})

    def test_the_result_is_a_new_dict(self) -> None:
        bag = {"title": "T"}
        shown = public_metadata(bag)
        shown["extra"] = 1
        assert bag == {"title": "T"}

    def test_the_strategy_stamp_is_promoted_before_the_metadata_is_cut(self) -> None:
        served = project_for_public(_item(source_strategy="semantic", title="T"))
        assert served.strategy_source == "semantic"
        assert "source_strategy" not in served.metadata

    def test_the_raw_strategy_score_is_dropped_and_the_fused_one_kept(self) -> None:
        served = project_for_public(_item(title="T"))
        assert served.score_breakdown == {"rrf_total": 0.03}
        assert served.relevance_score == 0.4


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    s = SQLiteDocumentStore(tmp_path / "documents.db")
    yield s
    s.close()


def _copy(store: SQLiteDocumentStore, item: PackItem, **kw: Any) -> PackItem | None:
    return readable_stored_copy(
        "public",
        item,
        ParentVerdictCache(store),
        excerpt_max_chars=kw.pop("excerpt_max_chars", 500),
    )


class TestStoredCopy:
    def test_the_text_is_the_stored_rows_not_the_vector_rows(self, store) -> None:
        store.put("d", "the stored text", {"title": "T", **label("public")})
        copy = _copy(store, _item("d", title="old title"))
        assert copy is not None
        assert copy.excerpt == "the stored text"
        assert copy.metadata["title"] == "T"

    def test_the_stored_text_is_cut_as_the_pack_cuts_it(self, store) -> None:
        text = "Sentence number one is here. " * 40
        store.put("d", text, label("public"))
        copy = _copy(store, _item("d"), excerpt_max_chars=200)
        assert copy is not None
        assert copy.excerpt == truncate_excerpt(text, limit=200)
        assert len(copy.excerpt) <= 200 + 3

    def test_an_item_with_no_stored_row_is_withheld(self, store) -> None:
        assert _copy(store, _item("ghost", **label("public"))) is None

    def test_a_vector_row_that_claims_public_over_a_stored_row_that_does_not(
        self, store
    ) -> None:
        store.put("d", "text", label("internal"))
        assert _copy(store, _item("d", **label("public"))) is None

    def test_an_unlabelled_stored_row_is_withheld(self, store) -> None:
        store.put("d", "text", {"title": "T"})
        assert _copy(store, _item("d", **label("public"))) is None

    def test_a_chunk_is_withheld_whatever_its_row_says(self, store) -> None:
        chunk = chunk_doc_id("p", 0)
        store.put("p", "parent", label("public"))
        store.put(chunk, "chunk", label("public"))
        assert _copy(store, _item(chunk, **label("public"))) is None

    def test_the_strategy_stamps_survive_the_refresh(self, store) -> None:
        store.put("d", "text", {"title": "T", **label("public")})
        copy = _copy(store, _item("d", source_strategy="keyword"))
        assert copy is not None
        assert copy.metadata["source_strategy"] == "keyword"

    @pytest.mark.parametrize(
        "key", sorted(WRITER_KEYS - set(PUBLIC_METADATA_KEYS) - {"content_type"})
    )
    def test_no_stored_key_survives_the_pair(self, store, key: str) -> None:
        """A stored row carrying every writer's key, rebuilt and then projected."""
        store.put(
            "d",
            "text",
            {"title": "T", key: SENTINEL, **label("public")},
        )
        copy = _copy(store, _item("d", source_strategy="keyword"))
        assert copy is not None
        served = project_for_public(copy)
        assert set(served.metadata) <= set(PUBLIC_METADATA_KEYS)
        assert served.strategy_source == "keyword"
        assert not leaked(served.model_dump_json(), [SENTINEL])

    def test_the_legacy_flat_form_is_shown_as_the_form_and_nowhere_else(
        self, store
    ) -> None:
        """``content_type`` outside the facet vocabulary is a document form."""
        store.put("d", "text", {"content_type": "legacyform", **label("public")})
        copy = _copy(store, _item("d"))
        assert copy is not None
        served = project_for_public(copy)
        assert served.metadata == {"document_form": "legacyform"}
        assert "content_type" not in served.metadata

    @pytest.mark.parametrize("key", ["source_strategy", "recency_clock"])
    def test_a_stored_stamp_name_is_not_a_strategy_stamp(self, store, key) -> None:
        store.put("d", "text", {"title": "T", key: SENTINEL, **label("public")})
        copy = _copy(store, _item("d"))
        assert copy is not None
        assert key not in copy.metadata
        served = project_for_public(copy)
        assert served.strategy_source is None
        assert not leaked(served.model_dump_json(), [SENTINEL])

    def test_a_store_that_raises_withholds_the_item(self, store) -> None:
        store.put("d", "text", label("public"))
        item = _item("d")
        store.get = lambda _id: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[method-assign]
        assert _copy(store, item) is None


class TestSearchPage:
    def _seed(self, store: SQLiteDocumentStore, hidden: int, public: int) -> None:
        for i in range(hidden):
            store.put(f"hidden-{i:03d}", "alpha alpha alpha alpha alpha", {"n": i})
        for i in range(public):
            store.put(f"public-{i:03d}", "alpha beta", label("public"))

    def test_the_page_is_filled_with_public_documents_only(self, store) -> None:
        self._seed(store, hidden=60, public=5)
        page = public_search_page(store, "alpha", limit=3)
        assert len(page) == 3
        assert {r["doc_id"].split("-")[0] for r in page} == {"public"}

    def test_the_page_is_as_long_as_it_would_be_without_the_hidden_documents(
        self, tmp_path: Path
    ) -> None:
        with_hidden = SQLiteDocumentStore(tmp_path / "a.db")
        without = SQLiteDocumentStore(tmp_path / "b.db")
        try:
            self._seed(with_hidden, hidden=80, public=7)
            self._seed(without, hidden=0, public=7)
            for limit in (1, 2, 5, 7, 10):
                a = public_search_page(with_hidden, "alpha", limit=limit)
                b = public_search_page(without, "alpha", limit=limit)
                assert len(a) == len(b) == min(limit, 7), limit
        finally:
            with_hidden.close()
            without.close()

    def test_fewer_public_matches_than_the_limit_returns_them_all(self, store) -> None:
        self._seed(store, hidden=30, public=2)
        assert len(public_search_page(store, "alpha", limit=10)) == 2

    def test_no_match_returns_an_empty_page(self, store) -> None:
        self._seed(store, hidden=3, public=3)
        assert public_search_page(store, "nomatchword", limit=5) == []

    def test_a_chunk_is_never_a_result(self, store) -> None:
        store.put("p", "alpha parent", label("public"))
        store.put(chunk_doc_id("p", 0), "alpha chunk", label("public"))
        ids = [r["doc_id"] for r in public_search_page(store, "alpha", limit=10)]
        assert ids == ["p"]

    def test_the_result_is_json_serialisable(self, store) -> None:
        self._seed(store, hidden=2, public=2)
        json.dumps(public_search_page(store, "alpha", limit=2), default=str)
