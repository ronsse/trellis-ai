"""Reference documents: a caller-written summary is embedded and served (#1250).

A reference document is one row: ``content`` is the full text,
``metadata["summary"]`` is the summary the caller wrote, ``metadata["uri"]`` is a
link to the original. A non-chunk row carrying the ``reference`` form **and** a
summary of at most ``EMBED_INPUT_CHAR_CAP`` characters is embedded and excerpted
from the summary; every other row, including one that merely carries a
``summary`` or a ``uri``, and a reference whose summary is over the cap, behaves
as it always did.

The guard tests at the bottom pass on ``main`` as well. They pin what must NOT
change, so a mutant that widens the predicate fails one of them.

Text here is invented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.llm.egress import EgressGate
from trellis.retrieve.embed_ingest_hook import (
    EMBED_INPUT_CHAR_CAP,
    VECTOR_METADATA_EXCERPT_CHARS,
    build_vector_row,
)
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.formatters import format_pack_as_markdown
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch, SemanticSearch
from trellis.schemas.pack import PackBudget
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.vector import SQLiteVectorStore
from trellis_cli.admin_reindex_vectors import run_reindex_vectors

if TYPE_CHECKING:
    from pathlib import Path

_OPEN_GATE = EgressGate(None, local=True)

#: Spelled out rather than imported: this file must import and run on ``main``,
#: where the guard tests pass, so it names nothing the change adds.
EXCERPT_SOURCE_KEY = "excerpt_source"

#: A word only the body holds: a hit on it proves the body was searched, and its
#: presence in a rendered pack proves the body leaked into the excerpt.
BODY_ONLY_TERM = "zephyrquartz"
BODY = f"{BODY_ONLY_TERM} opens the handbook. " + (
    "The body text goes on at some length about kiln schedules. " * 300
)
#: Long enough that its excerpt is cut, so "cut from the summary" is observable.
SUMMARY = (
    "Invented handbook for the quasar kiln: firing schedules, safety checks and "
    "a glossary. " * 9
).strip()
URI = "https://docs.example.test/kiln/handbook"
#: The intent the formatter prints. Not the search term: that must stay out of
#: the rendered text, or "the body leaked" could not be told from the header.
RENDER_INTENT = "how a handbook is looked up"
REFERENCE_META: dict[str, Any] = {
    "document_form": "reference",
    "summary": SUMMARY,
    "uri": URI,
}


class RecordingEmbedder:
    """Remembers every text it is handed."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.texts.append(text)
        return [1.0, 0.0, 0.0]


def _row(
    metadata: dict[str, Any],
    content: str = BODY,
    embed: RecordingEmbedder | None = None,
) -> tuple[dict[str, Any], RecordingEmbedder]:
    embedder = embed or RecordingEmbedder()
    row = build_vector_row("doc-1", content, metadata, embedder, egress=_OPEN_GATE)
    return row, embedder


def test_the_fixture_is_the_case_it_claims_to_be() -> None:
    assert len(BODY) > EMBED_INPUT_CHAR_CAP
    assert len(SUMMARY) > VECTOR_METADATA_EXCERPT_CHARS
    assert len(SUMMARY) < EMBED_INPUT_CHAR_CAP
    assert BODY_ONLY_TERM not in SUMMARY


class TestEmbedding:
    def test_the_embedder_receives_exactly_the_summary(self) -> None:
        _, embedder = _row(REFERENCE_META)
        assert embedder.texts == [SUMMARY]

    def test_the_vector_excerpt_is_cut_from_the_summary(self) -> None:
        row, _ = _row(REFERENCE_META)
        excerpt = row["metadata"]["content"]
        assert excerpt == truncate_excerpt(SUMMARY, VECTOR_METADATA_EXCERPT_CHARS)
        assert BODY_ONLY_TERM not in excerpt

    def test_the_summary_is_not_copied_into_the_vector_row_metadata(self) -> None:
        row, _ = _row(REFERENCE_META)
        assert "summary" not in row["metadata"]
        assert row["metadata"][EXCERPT_SOURCE_KEY] == "summary"
        assert row["metadata"]["uri"] == URI

    def test_the_stored_document_keeps_the_full_body(self, tmp_path: Path) -> None:
        store = SQLiteDocumentStore(tmp_path / "documents.db")
        try:
            store.put("doc-1", BODY, dict(REFERENCE_META))
            stored = store.get("doc-1")
        finally:
            store.close()
        assert stored is not None
        assert stored["content"] == BODY
        assert stored["metadata"]["summary"] == SUMMARY

    def test_a_stored_excerpt_source_is_not_trusted(self) -> None:
        # A caller cannot make a plain document look summary-served.
        row, _ = _row({EXCERPT_SOURCE_KEY: "summary", "title": "plain"}, "plain body")
        assert EXCERPT_SOURCE_KEY not in row["metadata"]


class TestUnchangedWhenNotASummaryServedReference:
    """These pass on ``main`` and must keep passing."""

    def test_a_summary_without_the_reference_form_embeds_the_body(self) -> None:
        row, embedder = _row({"summary": SUMMARY, "uri": URI})
        assert embedder.texts == [BODY[:EMBED_INPUT_CHAR_CAP]]
        assert row["metadata"]["content"] == truncate_excerpt(
            BODY, VECTOR_METADATA_EXCERPT_CHARS
        )
        assert row["metadata"]["summary"] == SUMMARY
        assert EXCERPT_SOURCE_KEY not in row["metadata"]

    def test_another_form_with_a_summary_embeds_the_body(self) -> None:
        _, embedder = _row({"document_form": "note", "summary": SUMMARY})
        assert embedder.texts == [BODY[:EMBED_INPUT_CHAR_CAP]]

    def test_a_chunk_of_a_reference_embeds_its_own_text(self) -> None:
        chunk_text = "one section of the handbook, embedded as itself"
        chunk_meta = {**REFERENCE_META, "parent_doc_id": "parent-doc", "chunk_index": 2}
        row, embedder = _row(chunk_meta, chunk_text)
        assert embedder.texts == [chunk_text]
        assert row["metadata"]["content"] == chunk_text
        assert EXCERPT_SOURCE_KEY not in row["metadata"]

    def test_a_summary_over_the_embed_cap_falls_back_to_the_whole_body(self) -> None:
        # The embedder would see only the first EMBED_INPUT_CHAR_CAP characters of
        # it, so it is not used at all (corpus sync does not run the write cap).
        meta = {
            "document_form": "reference",
            "summary": "s" * (EMBED_INPUT_CHAR_CAP + 1),
            "uri": URI,
        }
        row, embedder = _row(meta)
        assert embedder.texts == [BODY[:EMBED_INPUT_CHAR_CAP]]
        assert row["metadata"]["content"] == truncate_excerpt(
            BODY, VECTOR_METADATA_EXCERPT_CHARS
        )
        assert EXCERPT_SOURCE_KEY not in row["metadata"]

    @pytest.mark.parametrize("summary", [None, "", "   \n", 5, ["a summary"]])
    def test_a_reference_without_a_usable_summary_embeds_its_body(
        self, summary: object
    ) -> None:
        meta: dict[str, Any] = {"document_form": "reference", "uri": URI}
        if summary is not None:
            meta["summary"] = summary
        _, embedder = _row(meta)
        assert embedder.texts == [BODY[:EMBED_INPUT_CHAR_CAP]]


# ---------------------------------------------------------------------------
# A real pack, on both axes
# ---------------------------------------------------------------------------


def _items(pack: Any) -> list[dict[str, Any]]:
    """The dict shape ``get_context`` hands the formatter."""
    return [
        {
            "item_id": item.item_id,
            "item_type": item.item_type,
            "excerpt": item.excerpt,
            "relevance_score": item.relevance_score,
            "estimated_tokens": item.estimated_tokens,
            "metadata": item.metadata,
        }
        for item in pack.items
    ]


def _pack(builder: PackBuilder) -> Any:
    return builder.build(
        intent=BODY_ONLY_TERM,
        budget=PackBudget(max_items=10, max_tokens=100_000),
        clearance="restricted",
    )


@pytest.fixture
def doc_store(tmp_path: Path) -> Any:
    store = SQLiteDocumentStore(tmp_path / "documents.db")
    yield store
    store.close()


@pytest.fixture
def vector_store(tmp_path: Path) -> Any:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield store
    store.close()


def _store(
    doc_store: SQLiteDocumentStore,
    vector_store: SQLiteVectorStore,
    metadata: dict[str, Any],
) -> None:
    doc_store.put("doc-1", BODY, dict(metadata))
    row, _ = _row(metadata)
    vector_store.upsert(row["item_id"], row["vector"], row["metadata"])


class TestPackServing:
    def test_keyword_axis_serves_the_summary_and_the_link(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        _store(doc_store, vector_store, REFERENCE_META)
        pack = _pack(PackBuilder(strategies=[KeywordSearch(doc_store)]))

        # The body matched the query (it is the only place the term lives),
        # yet what is served is the summary.
        assert [item.item_id for item in pack.items] == ["doc-1"]
        item = pack.items[0]
        assert item.excerpt == truncate_excerpt(SUMMARY, 500)
        assert "summary" not in item.metadata
        assert item.metadata[EXCERPT_SOURCE_KEY] == "summary"

        text = format_pack_as_markdown(_items(pack), RENDER_INTENT)
        assert "Invented handbook for the quasar kiln" in text
        assert BODY_ONLY_TERM not in text
        assert "body text goes on" not in text
        assert f"Source: `{URI}`" in text
        assert "Full text: get_items" in text

    def test_semantic_axis_serves_the_summary_and_the_link(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        _store(doc_store, vector_store, REFERENCE_META)
        pack = _pack(
            PackBuilder(
                strategies=[
                    SemanticSearch(vector_store, embedding_fn=RecordingEmbedder())
                ]
            )
        )
        assert [item.item_id for item in pack.items] == ["doc-1"]

        text = format_pack_as_markdown(_items(pack), RENDER_INTENT)
        assert "Invented handbook for the quasar kiln" in text
        assert BODY_ONLY_TERM not in text
        assert "body text goes on" not in text
        assert f"Source: `{URI}`" in text
        assert "Full text: get_items" in text

    def test_both_axes_show_the_same_excerpt(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        _store(doc_store, vector_store, REFERENCE_META)
        keyword = _pack(PackBuilder(strategies=[KeywordSearch(doc_store)]))
        semantic = _pack(
            PackBuilder(
                strategies=[
                    SemanticSearch(vector_store, embedding_fn=RecordingEmbedder())
                ]
            )
        )
        assert keyword.items[0].excerpt == semantic.items[0].excerpt

    def test_a_reference_without_a_uri_still_says_where_the_full_text_is(
        self, doc_store: SQLiteDocumentStore, vector_store: SQLiteVectorStore
    ) -> None:
        meta = {"document_form": "reference", "summary": SUMMARY}
        _store(doc_store, vector_store, meta)
        pack = _pack(PackBuilder(strategies=[KeywordSearch(doc_store)]))
        text = format_pack_as_markdown(_items(pack), RENDER_INTENT)
        assert "Summary only. Full text: get_items." in text
        assert "Source:" not in text


class TestPackUnchanged:
    """A row that is not a summary-served reference renders as on ``main``."""

    @pytest.mark.parametrize(
        "metadata",
        [
            {"uri": URI},
            {"uri": URI, "summary": SUMMARY},
            {"document_form": "note", "uri": URI, "summary": SUMMARY},
            {"document_form": "reference", "uri": URI},
        ],
    )
    def test_no_link_line_and_the_body_is_the_excerpt(
        self,
        doc_store: SQLiteDocumentStore,
        vector_store: SQLiteVectorStore,
        metadata: dict[str, Any],
    ) -> None:
        _store(doc_store, vector_store, metadata)
        for strategies in (
            [KeywordSearch(doc_store)],
            [SemanticSearch(vector_store, embedding_fn=RecordingEmbedder())],
        ):
            pack = _pack(PackBuilder(strategies=strategies))
            assert [item.item_id for item in pack.items] == ["doc-1"]
            assert EXCERPT_SOURCE_KEY not in pack.items[0].metadata
            text = format_pack_as_markdown(_items(pack), RENDER_INTENT)
            assert "Summary only" not in text
            assert "Full text:" not in text
            assert "quasar kiln" not in text
            assert BODY_ONLY_TERM in pack.items[0].excerpt

    def test_a_keyword_hit_keeps_its_whole_metadata_bag(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("doc-1", BODY, {"summary": SUMMARY, "uri": URI, "title": "T"})
        pack = _pack(PackBuilder(strategies=[KeywordSearch(doc_store)]))
        meta = pack.items[0].metadata
        assert meta["summary"] == SUMMARY
        assert meta["uri"] == URI


# ---------------------------------------------------------------------------
# reindex-vectors shares build_vector_row, and says so here
# ---------------------------------------------------------------------------


def test_reindex_vectors_re_embeds_a_stored_reference_from_its_summary(
    tmp_path: Path,
) -> None:
    stores = tmp_path / "stores"
    stores.mkdir()
    registry = StoreRegistry(stores_dir=stores, embedding_config={"local": True})
    embedder = RecordingEmbedder()
    registry._embedding_fn_cache = embedder  # type: ignore[attr-defined]
    docs = registry.knowledge.document_store
    docs.put("ref-1", BODY, dict(REFERENCE_META))
    docs.put("plain-1", BODY, {"summary": SUMMARY})

    summary = run_reindex_vectors(registry, force=True)

    assert summary["embedded"] == 2
    assert sorted(embedder.texts) == sorted([SUMMARY, BODY[:EMBED_INPUT_CHAR_CAP]])
    stored = registry.knowledge.vector_store.get("ref-1")
    assert stored is not None
    assert stored["metadata"]["content"] == truncate_excerpt(
        SUMMARY, VECTOR_METADATA_EXCERPT_CHARS
    )
    assert "summary" not in stored["metadata"]
    # The stored document is untouched.
    assert docs.get("ref-1")["content"] == BODY
