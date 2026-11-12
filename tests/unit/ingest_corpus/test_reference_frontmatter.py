"""A corpus file whose frontmatter names the reference form and a summary (#1250).

Corpus sync turns frontmatter into document metadata and does not run the
write boundary that ``save_memory`` and the evidence handler apply to a
``summary``. So a markdown file whose frontmatter carries ``summary:`` **and**
``content_type: reference`` (or ``document_form: reference``) is stored as a
reference document with no one having checked the summary. These tests pin what
the real sync then does with it, against real SQLite stores:

* an unchunked file is embedded from its summary and says so;
* a chunked file's chunk rows embed their own text (a chunk carries
  ``parent_doc_id``, so it is never summary-served), while the parent document
  is the summary-served row;
* a summary over the embed cap is never summary-served (the embedder would see
  only a cut of it). Such a file is longer than the chunk threshold, which is
  the embed cap, so sync chunks it and never embeds the parent; the parent is
  embedded only by ``trellis admin reindex-vectors``, and there it is read as
  any other document is, from its verbatim text (frontmatter first);
* a ``summary:`` with no reference form is untouched.

Text here is invented.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trellis.ingest_corpus.models import chunk_doc_id, corpus_doc_id
from trellis.ingest_corpus.sync import sync_corpus
from trellis.retrieve.embed_ingest_hook import (
    EMBED_INPUT_CHAR_CAP,
    EMBED_ON_INGEST_FLAG,
)
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.reference import summary_of
from trellis.stores.registry import StoreRegistry
from trellis_cli.admin_reindex_vectors import run_reindex_vectors

#: Dotted path handed to ``TRELLIS_EMBEDDING_FN``; it cannot carry a closure.
EMBED_FN_PATH = "tests.unit.ingest_corpus.test_reference_frontmatter.embed"

SOURCE = "vault"
PATH = "notes/kiln-handbook.md"
DOC_ID = corpus_doc_id(SOURCE, PATH)
EXCERPT_SOURCE_KEY = "excerpt_source"

SUMMARY = "Invented handbook for the quasar kiln: firing schedules and safety checks."
#: A word only the body holds.
BODY_ONLY_TERM = "zephyrquartz"
SHORT_BODY = (
    f"# Kiln handbook\n\n{BODY_ONLY_TERM} opens the handbook. "
    + "The body goes on about kiln schedules and glazes. " * 20
)

TEXTS: list[str] = []


def embed(text: str) -> list[float]:
    """Recording embedder: every text it is handed lands in ``TEXTS``."""
    TEXTS.append(text)
    return [1.0, float(len(text) % 7), 0.5]


@pytest.fixture(autouse=True)
def _recording(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    TEXTS.clear()
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
    yield
    TEXTS.clear()


def _long_body() -> str:
    """Three sections, long enough to chunk."""
    sections = []
    for i, topic in enumerate(("glazes", "firing", "cooling")):
        para = (f"All about {topic}. The {topic} facts continue here. ") * 80
        sections.append(f"## Section {i}\n\n{para.strip()}")
    return "\n\n".join(sections)


def _sync(tmp_path: Path, frontmatter: dict[str, str], body: str) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    registry = StoreRegistry(stores_dir=stores, embedding_config={"local": True})
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    # A JSON string is a valid YAML scalar, and a bare one holding ": " is not.
    head = "\n".join(
        f"{key}: {json.dumps(value)}" for key, value in frontmatter.items()
    )
    (root / PATH).write_text(f"---\n{head}\n---\n\n{body}", encoding="utf-8")
    sync_corpus(registry, root, source_system=SOURCE)
    return registry


def _parent(registry: StoreRegistry) -> dict[str, Any]:
    row = registry.knowledge.document_store.get(DOC_ID)
    assert row is not None
    return row


def _vector_metadata(registry: StoreRegistry, item_id: str) -> dict[str, Any]:
    row = registry.knowledge.vector_store.get(item_id)
    assert row is not None, f"no vector row for {item_id}"
    return row["metadata"]


FORMS = [
    pytest.param({"content_type": "reference"}, id="content_type"),
    pytest.param({"document_form": "reference"}, id="document_form"),
]


class TestAnUnchunkedReference:
    @pytest.mark.parametrize("form", FORMS)
    def test_the_parent_is_embedded_from_its_summary(
        self, tmp_path: Path, form: dict[str, str]
    ) -> None:
        registry = _sync(tmp_path, {**form, "summary": SUMMARY}, SHORT_BODY)

        parent = _parent(registry)
        assert parent["metadata"]["document_form"] == "reference"
        assert summary_of(parent["metadata"]) == SUMMARY
        assert BODY_ONLY_TERM in parent["content"]
        assert TEXTS == [SUMMARY]
        row = _vector_metadata(registry, DOC_ID)
        assert row["content"] == SUMMARY
        assert row[EXCERPT_SOURCE_KEY] == "summary"
        assert "summary" not in row

    def test_a_summary_without_the_form_is_untouched(self, tmp_path: Path) -> None:
        registry = _sync(tmp_path, {"summary": SUMMARY}, SHORT_BODY)

        parent = _parent(registry)
        assert summary_of(parent["metadata"]) is None
        assert [parent["content"][:EMBED_INPUT_CHAR_CAP]] == TEXTS
        row = _vector_metadata(registry, DOC_ID)
        assert row["content"] == truncate_excerpt(parent["content"], 500)
        assert EXCERPT_SOURCE_KEY not in row


class TestAChunkedReference:
    def test_chunks_embed_their_own_text_and_the_parent_is_summary_served(
        self, tmp_path: Path
    ) -> None:
        registry = _sync(
            tmp_path, {"content_type": "reference", "summary": SUMMARY}, _long_body()
        )

        parent = _parent(registry)
        assert summary_of(parent["metadata"]) == SUMMARY
        count = parent["metadata"]["chunk_count"]
        assert count >= 2
        chunk_ids = [chunk_doc_id(DOC_ID, i) for i in range(count)]
        # No chunk is embedded from the summary: each is its own text.
        assert SUMMARY not in TEXTS
        stored = {
            c: registry.knowledge.document_store.get(c)["content"] for c in chunk_ids
        }
        assert sorted(TEXTS) == sorted(
            text[:EMBED_INPUT_CHAR_CAP] for text in stored.values()
        )
        for chunk_id in chunk_ids:
            chunk = registry.knowledge.document_store.get(chunk_id)
            assert chunk is not None
            assert summary_of(chunk["metadata"]) is None
            row = _vector_metadata(registry, chunk_id)
            assert EXCERPT_SOURCE_KEY not in row
            assert row["content"] == truncate_excerpt(stored[chunk_id], 500)

    def test_an_over_cap_summary_changes_nothing_for_a_chunked_file(
        self, tmp_path: Path
    ) -> None:
        long_summary = ("kiln summary words " * 500)[:8500]
        registry = _sync(
            tmp_path,
            {"content_type": "reference", "summary": long_summary},
            _long_body(),
        )

        parent = _parent(registry)
        assert summary_of(parent["metadata"]) is None
        count = parent["metadata"]["chunk_count"]
        assert count >= 2
        assert all(not text.startswith("kiln summary words") for text in TEXTS)
        assert len(TEXTS) == count


class TestAnOverCapSummary:
    """A summary the embedder would cut is never the served text (#1250)."""

    LONG_SUMMARY = ("kiln summary words " * 500)[:8500]

    @pytest.mark.parametrize("form", FORMS)
    def test_sync_chunks_the_file_and_embeds_no_parent(
        self, tmp_path: Path, form: dict[str, str]
    ) -> None:
        assert len(self.LONG_SUMMARY) > EMBED_INPUT_CHAR_CAP
        registry = _sync(tmp_path, {**form, "summary": self.LONG_SUMMARY}, SHORT_BODY)

        parent = _parent(registry)
        assert parent["metadata"]["document_form"] == "reference"
        assert summary_of(parent["metadata"]) is None
        count = parent["metadata"]["chunk_count"]
        assert count >= 2
        # The file is longer than the embed cap, so it is chunked, and a chunked
        # file's parent is not embedded at sync: the chunks are.
        assert registry.knowledge.vector_store.get(DOC_ID) is None
        assert len(TEXTS) == count
        assert all(len(text) <= EMBED_INPUT_CHAR_CAP for text in TEXTS)

    @pytest.mark.parametrize("form", FORMS)
    def test_reindex_reads_the_parent_as_any_other_document(
        self, tmp_path: Path, form: dict[str, str]
    ) -> None:
        registry = _sync(tmp_path, {**form, "summary": self.LONG_SUMMARY}, SHORT_BODY)
        TEXTS.clear()

        summary = run_reindex_vectors(registry)

        # Only the parent had no vector. It is embedded from its verbatim text,
        # which starts with the frontmatter, not from a cut summary, and its row
        # is not marked as summary-served.
        assert summary["embedded"] == 1
        parent = _parent(registry)
        assert [parent["content"][:EMBED_INPUT_CHAR_CAP]] == TEXTS
        row = _vector_metadata(registry, DOC_ID)
        assert EXCERPT_SOURCE_KEY not in row
        assert row["content"] == truncate_excerpt(parent["content"], 500)
        assert row["summary"] == self.LONG_SUMMARY
