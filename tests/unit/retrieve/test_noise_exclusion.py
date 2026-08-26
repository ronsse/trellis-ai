"""Noise exclusion has to hold on the **semantic** axis (trellis-ai#338).

``CLAUDE.md`` and ``docs/PRD.md`` both state that items demoted to
``signal_quality="noise"`` are excluded from packs by default. Production
measurement said otherwise, and the reason it stayed invisible is that every
pack-level test in this repo exercised ``KeywordSearch`` — which reads the
document store and therefore honours a tag the moment it is written. The
divergence only exists on the vector path.

Three independent defects had to line up for the guarantee to fail, and each
gets its own test here:

1. The vector row's metadata is an **embed-time snapshot**;
   ``apply_noise_tags`` wrote only to the document store, so the row kept
   advertising the item's pre-demotion tags.
2. ``SemanticSearch`` **strips** ``content_tags`` from the filters it passes
   to the vector store, so the store-side noise predicate never reached the
   semantic axis at all — a row that *did* say ``"noise"`` was served
   anyway.
3. ``PackBuilder._build_filters`` returns early when ``tag_filters is
   None``, so the default was never constructed for MCP ``get_context``,
   which passes none. On that calling convention noise exclusion did not
   hold on the keyword axis either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.classify.feedback import apply_noise_tags
from trellis.core.vector_metadata import vector_metadata_diverges
from trellis.retrieve.embed_ingest_hook import build_vector_row
from trellis.retrieve.noise import (
    DEFAULT_SIGNAL_QUALITY_SPEC,
    exclude_noise,
    passes_signal_quality,
    resolve_signal_quality_spec,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch, SemanticSearch
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

if TYPE_CHECKING:
    from pathlib import Path

INTENT = "distinctive kangaroo content"


def _embedding_fn(_text: str) -> list[float]:
    """Every item embeds identically — membership is what these tests assert."""
    return [1.0, 0.0, 0.0]


@pytest.fixture
def doc_store(tmp_path: Path) -> SQLiteDocumentStore:
    store = SQLiteDocumentStore(tmp_path / "documents.db")
    yield store
    store.close()


@pytest.fixture
def vector_store(tmp_path: Path) -> SQLiteVectorStore:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield store
    store.close()


def _ingest_and_embed(
    doc_store: SQLiteDocumentStore,
    vector_store: SQLiteVectorStore,
    doc_id: str,
    *,
    signal_quality: str | None = "standard",
) -> None:
    """Store a document and embed it through the production embed path.

    Uses :func:`build_vector_row` rather than a hand-written row so the
    snapshot under test is the one the live ingest hook and
    ``trellis admin reindex-vectors`` actually write.
    """
    metadata: dict[str, Any] = {"title": doc_id}
    if signal_quality is not None:
        metadata["content_tags"] = {"signal_quality": signal_quality}
    content = f"{INTENT} for {doc_id}"
    doc_store.put(doc_id, content, metadata)
    row = build_vector_row(doc_id, content, metadata, _embedding_fn)
    vector_store.upsert(row["item_id"], row["vector"], row["metadata"])


def _semantic_builder(vector_store: SQLiteVectorStore) -> PackBuilder:
    return PackBuilder(
        strategies=[SemanticSearch(vector_store, embedding_fn=_embedding_fn)]
    )


def _ids(builder: PackBuilder, **kwargs: Any) -> list[str]:
    pack = builder.build(
        intent=INTENT,
        budget=PackBudget(max_items=25, max_tokens=100_000),
        **kwargs,
    )
    return sorted(item.item_id for item in pack.items)


class TestAcceptance:
    """The issue's acceptance criterion, stated as a test."""

    def test_document_demoted_after_embedding_leaves_the_semantic_pack(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """Demote *after* embedding, then assert the semantic axis stops serving.

        The ordering is the whole point: a document tagged noise before it is
        embedded never had a stale snapshot to begin with.
        """
        _ingest_and_embed(doc_store, vector_store, "noisy")
        _ingest_and_embed(doc_store, vector_store, "useful")
        builder = _semantic_builder(vector_store)
        assert _ids(builder) == ["noisy", "useful"]

        assert apply_noise_tags(["noisy"], doc_store, vector_store) == 1

        assert _ids(builder) == ["useful"]

    def test_demotion_reaches_the_vector_row(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """The invariant: document and vector agree on ``signal_quality``.

        This is the state production was measured in violation of — 45
        noise-tagged documents, 28 whose vector row carried no
        ``signal_quality`` key and 17 still reading ``"standard"``.
        """
        _ingest_and_embed(doc_store, vector_store, "noisy")
        apply_noise_tags(["noisy"], doc_store, vector_store)

        doc = doc_store.get("noisy")
        row = vector_store.get("noisy")
        assert doc is not None
        assert row is not None
        assert (
            doc["metadata"]["content_tags"]["signal_quality"]
            == row["metadata"]["content_tags"]["signal_quality"]
            == "noise"
        )
        assert not vector_metadata_diverges(doc["metadata"], row["metadata"])

    def test_demotion_re_embeds_nothing(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """Metadata-only: the embedding and the row's own excerpt survive."""
        _ingest_and_embed(doc_store, vector_store, "noisy")
        before = vector_store.get("noisy")
        assert before is not None

        apply_noise_tags(["noisy"], doc_store, vector_store)

        after = vector_store.get("noisy")
        assert after is not None
        assert after["vector"] == pytest.approx(before["vector"])
        assert after["metadata"]["content"] == before["metadata"]["content"]
        assert after["metadata"]["doc_id"] == "noisy"

    def test_un_embedded_document_is_still_demotable(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """A missing vector row is normal, not an error.

        Embed-on-ingest is opt-in, so plenty of documents have no row. The
        demotion still has to land in the document store.
        """
        doc_store.put("never_embedded", INTENT, {"title": "t"})

        assert apply_noise_tags(["never_embedded"], doc_store, vector_store) == 1

        doc = doc_store.get("never_embedded")
        assert doc is not None
        assert doc["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert vector_store.get("never_embedded") is None


class TestSemanticAxisHonoursTheTag:
    """Defect 2: the store-side predicate never reached the semantic axis.

    ``SemanticSearch`` drops ``content_tags`` from the filters it forwards
    (vector backends offer only hard-equality scalar filters, and passing the
    facet bag through matches nothing — #254). So even a *correctly synced*
    vector row was served. These tests need none of the new write-path API
    and therefore fail on ``main`` for the reason the issue describes.
    """

    def test_noise_tagged_vector_row_is_excluded(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        _ingest_and_embed(doc_store, vector_store, "noisy", signal_quality="noise")
        _ingest_and_embed(doc_store, vector_store, "useful")

        assert _ids(_semantic_builder(vector_store)) == ["useful"]

    def test_untagged_row_still_served(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """Default-pass: an untagged memory is never hard-excluded."""
        _ingest_and_embed(doc_store, vector_store, "untagged", signal_quality=None)

        assert _ids(_semantic_builder(vector_store)) == ["untagged"]

    def test_caller_can_ask_for_noise(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """Curation tooling legitimately wants to see what was demoted."""
        _ingest_and_embed(doc_store, vector_store, "noisy", signal_quality="noise")
        _ingest_and_embed(doc_store, vector_store, "useful")
        builder = _semantic_builder(vector_store)

        assert _ids(builder, tag_filters={"signal_quality": {"in": ["noise"]}}) == [
            "noisy"
        ]

    def test_sectioned_packs_honour_it_too(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
    ) -> None:
        """``build_sectioned`` shares the collect seam, so it shares the rule."""
        _ingest_and_embed(doc_store, vector_store, "noisy", signal_quality="noise")
        _ingest_and_embed(doc_store, vector_store, "useful")

        pack = _semantic_builder(vector_store).build_sectioned(
            intent=INTENT,
            sections=[SectionRequest(name="all", max_tokens=100_000, max_items=25)],
        )
        served = {item.item_id for section in pack.sections for item in section.items}
        assert served == {"useful"}


class TestKeywordAxisWithoutTagFilters:
    """Defect 3: the default was unreachable for a caller passing no filters.

    MCP ``get_context`` passes ``tag_filters=None`` unless a ``domain`` is
    supplied, and ``_build_filters`` returns early on ``None`` — so the
    ``{"not_in": ["noise"]}`` default was never constructed. The keyword axis
    reads the document store and would have honoured the tag immediately if
    it had ever been asked to.
    """

    def test_noise_excluded_with_no_tag_filters(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("noisy", INTENT, {"content_tags": {"signal_quality": "noise"}})
        doc_store.put(
            "useful", INTENT, {"content_tags": {"signal_quality": "standard"}}
        )
        builder = PackBuilder(strategies=[KeywordSearch(doc_store)])

        assert _ids(builder) == ["useful"]

    def test_store_side_pushdown_still_applies(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """The seam is a backstop, not a replacement — both paths agree."""
        doc_store.put("noisy", INTENT, {"content_tags": {"signal_quality": "noise"}})
        doc_store.put(
            "useful", INTENT, {"content_tags": {"signal_quality": "standard"}}
        )
        builder = PackBuilder(strategies=[KeywordSearch(doc_store)])

        assert _ids(builder, tag_filters={}) == ["useful"]


class TestSpecSemantics:
    """Unit-level: the four operators, and what an absent facet means."""

    @staticmethod
    def _item(signal_quality: str | None) -> PackItem:
        tags = {} if signal_quality is None else {"signal_quality": signal_quality}
        return PackItem(
            item_id=signal_quality or "untagged",
            item_type="vector",
            excerpt="x",
            relevance_score=1.0,
            metadata={"content_tags": tags},
        )

    def test_default_spec_is_a_negation(self) -> None:
        assert resolve_signal_quality_spec(None) == DEFAULT_SIGNAL_QUALITY_SPEC
        assert resolve_signal_quality_spec({}) == {"not_in": ["noise"]}

    def test_caller_spec_wins(self) -> None:
        spec = {"eq": "noise"}
        assert resolve_signal_quality_spec({"signal_quality": spec}) is spec

    def test_non_dict_spec_falls_back_to_default(self) -> None:
        """A bare list is rejected downstream; here it must not silently deny."""
        assert (
            resolve_signal_quality_spec({"signal_quality": ["noise"]})
            == DEFAULT_SIGNAL_QUALITY_SPEC
        )

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ({"not_in": ["noise"]}, ["standard", "untagged"]),
            ({"in": ["noise"]}, ["noise", "untagged"]),
            ({"eq": "noise"}, ["noise", "untagged"]),
            ({"ne": "noise"}, ["standard", "untagged"]),
        ],
    )
    def test_operators_match_the_document_store_filter(
        self, spec: dict[str, Any], expected: list[str]
    ) -> None:
        """Same answers ``SQLiteDocumentStore``'s tag filter gives.

        The untagged item passes every operator on both paths — a
        default-pass filter is the one behaviour the two implementations
        absolutely must share, or the seam would drop rows the pushdown
        admits.
        """
        items = [self._item("noise"), self._item("standard"), self._item(None)]
        assert sorted(i.item_id for i in exclude_noise(items, spec)) == expected

    def test_unreadable_metadata_passes(self) -> None:
        """A bad filter must never silently shrink a pack."""
        assert passes_signal_quality(None, DEFAULT_SIGNAL_QUALITY_SPEC)
        assert passes_signal_quality({}, DEFAULT_SIGNAL_QUALITY_SPEC)
        assert passes_signal_quality(
            {"content_tags": "not-a-dict"}, DEFAULT_SIGNAL_QUALITY_SPEC
        )
        assert passes_signal_quality(
            {"content_tags": {"signal_quality": ["noise"]}},
            DEFAULT_SIGNAL_QUALITY_SPEC,
        )

    def test_unrecognised_operator_passes(self) -> None:
        assert passes_signal_quality(
            {"content_tags": {"signal_quality": "noise"}}, {"regex": "noi.*"}
        )

    def test_malformed_operand_passes(self) -> None:
        assert passes_signal_quality(
            {"content_tags": {"signal_quality": "noise"}}, {"not_in": "noise"}
        )
