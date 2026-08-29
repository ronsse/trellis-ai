"""Idempotent corpus sync against real SQLite stores.

Covers the ADR §4 done-criteria: second run over an unchanged tree is
zero writes; an edited file re-puts and re-embeds only changed chunks;
a moved file is re-keyed via ``get_by_hash``; ``--prune`` removes
vanished documents; chunks are semantically retrievable once embedded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from trellis.classify.feedback import apply_noise_tags
from trellis.classify.ingest import (
    CLASSIFY_METADATA_KEYS,
    CLASSIFY_ON_INGEST_FLAG,
)
from trellis.core.vector_metadata import (
    SYNCED_METADATA_KEYS,
    vector_metadata_diverges,
)
from trellis.ingest_corpus.models import (
    chunk_doc_id,
    corpus_doc_id,
    is_chunk_doc_id,
)
from trellis.ingest_corpus.sync import sync_corpus
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
from trellis.retrieve.evaluate import BreadthScorer, EvaluationScenario
from trellis.retrieve.noise import exclude_noise
from trellis.retrieve.strategies import SemanticSearch
from trellis.schemas.pack import Pack, PackItem
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore

_DIMS = 64


def _embed(text: str) -> list[float]:
    """Deterministic bag-of-words hash embedding — real cosine geometry."""
    vector = [0.0] * _DIMS
    for word in text.lower().split():
        digest = hashlib.md5(word.encode(), usedforsecurity=False).digest()
        vector[digest[0] % _DIMS] += 1.0
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    return [v / norm for v in vector]


@pytest.fixture
def registry(tmp_path: Path) -> MagicMock:
    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    reg.embedding_fn = _embed
    return reg


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "sub").mkdir(parents=True)
    (root / "note-a.md").write_text(
        "---\ntitle: Note A\n---\n\nAlpha content with a [[Link]].\n"
    )
    (root / "sub" / "note-b.md").write_text("Beta content.\n\nSecond paragraph.\n")
    return root


def _long_markdown(
    topics: tuple[str, ...] = ("kubernetes", "grapes", "violins"),
) -> str:
    sections = []
    for i, topic in enumerate(topics):
        para = (f"All about {topic}. The {topic} facts continue here. ") * 80
        sections.append(f"## Section {i}\n\n{para.strip()}")
    return "\n\n".join(sections)


class TestFirstRun:
    def test_ingests_parents_with_metadata_and_events(self, registry, vault):
        report = sync_corpus(registry, vault, source_system="obsidian")
        assert report.counts()["ingested"] == 2

        doc_id = corpus_doc_id("obsidian", "note-a.md")
        stored = registry.knowledge.document_store.get(doc_id)
        assert stored is not None
        assert stored["metadata"]["title"] == "Note A"
        assert stored["metadata"]["wikilinks"] == ["Link"]
        assert stored["metadata"]["source_path"] == "note-a.md"
        assert stored["metadata"]["source_system"] == "obsidian"
        # Parent stores the file text verbatim.
        assert stored["content"].startswith("---\ntitle: Note A")

        events = registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED
        )
        assert {e.entity_id for e in events} == {
            corpus_doc_id("obsidian", "note-a.md"),
            corpus_doc_id("obsidian", "sub/note-b.md"),
        }
        assert all(e.payload["action"] == "new" for e in events)

        summary = registry.operational.event_log.get_events(
            event_type=EventType.CORPUS_SYNCED
        )
        assert len(summary) == 1
        assert summary[0].payload["ingested"] == 2
        assert summary[0].payload["dry_run"] is False

    def test_long_document_stores_chunk_docs(self, registry, vault):
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        parent_id = corpus_doc_id("obsidian", "long.md")
        parent = registry.knowledge.document_store.get(parent_id)
        count = parent["metadata"]["chunk_count"]
        assert count >= 2
        for index in range(count):
            chunk = registry.knowledge.document_store.get(
                chunk_doc_id(parent_id, index)
            )
            assert chunk is not None
            meta = chunk["metadata"]
            assert meta["parent_doc_id"] == parent_id
            assert meta["chunk_index"] == index
            assert meta["chunk_count"] == count
            assert meta["source_path"] == "long.md"
            start, end = meta["char_span"]
            assert chunk["content"] == parent["content"][start:end]

    def test_operator_tags_propagate_to_parent_and_chunks(self, registry, vault):
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(
            registry,
            vault,
            source_system="obsidian",
            extra_metadata={"domain": "ops", "team": "core"},
        )
        parent_id = corpus_doc_id("obsidian", "long.md")
        parent = registry.knowledge.document_store.get(parent_id)
        chunk = registry.knowledge.document_store.get(chunk_doc_id(parent_id, 0))
        for doc in (parent, chunk):
            assert doc["metadata"]["domain"] == "ops"
            assert doc["metadata"]["team"] == "core"


class TestClassifyOnIngest:
    """Classify-on-write (flag-gated): ingested documents carry content_tags
    from the first write, so noise-exclusion / sectioning / importance have
    signal to work with instead of an all-untagged store."""

    def test_disabled_by_default_stores_no_content_tags(self, registry, vault):
        # No flag set — behaviour is exactly as before this feature.
        sync_corpus(registry, vault, source_system="obsidian")
        doc = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note-a.md")
        )
        assert "content_tags" not in doc["metadata"]
        assert "auto_importance" not in doc["metadata"]

    def test_enabled_tags_parent_and_chunks(self, registry, vault, monkeypatch):
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        parent_id = corpus_doc_id("obsidian", "long.md")
        parent = registry.knowledge.document_store.get(parent_id)
        chunk = registry.knowledge.document_store.get(chunk_doc_id(parent_id, 0))
        for doc in (parent, chunk):
            ct = doc["metadata"]["content_tags"]
            # The full-document classification is stamped on the parent and
            # propagated to the chunk (the retrievable unit), so both carry the
            # same signal_quality and freshness stamp.
            assert ct["signal_quality"]
            assert ct["classified_at"]
            assert "auto_importance" in doc["metadata"]
        # Chunk inherits the parent's tags rather than being classified alone
        # (a short chunk in isolation would be marked low-signal).
        assert (
            chunk["metadata"]["content_tags"]["signal_quality"]
            == parent["metadata"]["content_tags"]["signal_quality"]
        )

    def test_enabled_does_not_auto_set_domain_facet(self, registry, vault, monkeypatch):
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        # Content the keyword/source-system classifiers would domain-tag.
        (vault / "infra.md").write_text(
            "# infra\n\n" + ("kubernetes helm terraform deployment pipeline. " * 40)
        )
        sync_corpus(registry, vault, source_system="obsidian")
        doc = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "infra.md")
        )
        # The hard-excluding facet is not auto-persisted.
        assert doc["metadata"]["content_tags"]["domain"] == []

    def test_explicit_operator_domain_survives_classification(
        self, registry, vault, monkeypatch
    ):
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        sync_corpus(
            registry,
            vault,
            source_system="obsidian",
            extra_metadata={"domain": "personal"},
        )
        doc = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note-a.md")
        )
        # Operator's explicit scalar domain is a separate key from the dropped
        # content_tags.domain facet — it is untouched by classify-on-write.
        assert doc["metadata"]["domain"] == "personal"
        assert doc["metadata"]["content_tags"]["domain"] == []

    def test_reingest_preserves_existing_content_tags(
        self, registry, vault, monkeypatch
    ):
        """Fill-if-absent: a re-ingest must not clobber tags a prior run or the
        enrichment pass already wrote (both persist to content_tags)."""
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        note = vault / "note-a.md"
        sync_corpus(registry, vault, source_system="obsidian")
        doc_id = corpus_doc_id("obsidian", "note-a.md")
        store = registry.knowledge.document_store

        # Simulate an enrichment pass promoting the tags to high-confidence.
        doc = store.get(doc_id)
        meta = dict(doc["metadata"])
        meta["content_tags"] = {
            **meta["content_tags"],
            "signal_quality": "high",
            "enriched": True,
        }
        store.put(doc_id, doc["content"], meta)

        # Edit the file so the next sync is an update (re-put), not a skip.
        note.write_text("---\ntitle: Note A\n---\n\nAlpha content EDITED [[Link]].\n")
        sync_corpus(registry, vault, source_system="obsidian")

        after = store.get(doc_id)["metadata"]["content_tags"]
        assert after.get("enriched") is True
        assert after["signal_quality"] == "high"

    def test_reingest_keeps_chunks_tagged(self, registry, vault, monkeypatch):
        """The round trip that matters: editing a tagged document must not
        un-tag its chunks. Chunks are the retrievable unit, so a wiped chunk
        is invisible to noise-exclusion / sectioning / importance weighting —
        exactly for the documents being actively maintained."""
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        store = registry.knowledge.document_store
        parent_id = corpus_doc_id("obsidian", "long.md")
        before = store.get(chunk_doc_id(parent_id, 0))["metadata"]
        assert before["content_tags"]["signal_quality"]

        # Edit the head of the document. Only the leading chunks reflow and get
        # re-put (the markdown chunker keeps the later sections byte-identical,
        # so they take the `continue` skip) — the assertion below covers both
        # paths. The second run classifies nothing (parent is already tagged),
        # so a re-put chunk's tags can only come from the parent's stored
        # metadata.
        note.write_text("Edited opening paragraph.\n\n" + _long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        parent_meta = store.get(parent_id)["metadata"]
        assert parent_meta["chunk_count"] >= 2
        for index in range(parent_meta["chunk_count"]):
            meta = store.get(chunk_doc_id(parent_id, index))["metadata"]
            assert meta["content_tags"] == before["content_tags"]
            assert meta["auto_importance"] == before["auto_importance"]
            # And still in step with the parent they derive from.
            assert meta["content_tags"] == parent_meta["content_tags"]
            assert meta["auto_importance"] == parent_meta["auto_importance"]

    def test_reingest_propagates_enrichment_tags_to_chunks(
        self, registry, vault, monkeypatch
    ):
        """Chunk tags derive from the post-merge parent metadata, so tags the
        LLM enrichment pass wrote onto the parent reach the chunks on the next
        re-ingest — not just tags this run's classifier produced."""
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        store = registry.knowledge.document_store
        parent_id = corpus_doc_id("obsidian", "long.md")
        parent = store.get(parent_id)
        store.put(
            parent_id,
            parent["content"],
            metadata={
                **parent["metadata"],
                "content_tags": {
                    **parent["metadata"]["content_tags"],
                    "signal_quality": "high",
                    "enriched": True,
                },
                "auto_importance": 0.91,
            },
        )

        note.write_text("Edited opening paragraph.\n\n" + _long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        chunk_meta = store.get(chunk_doc_id(parent_id, 0))["metadata"]
        assert chunk_meta["content_tags"]["enriched"] is True
        assert chunk_meta["content_tags"]["signal_quality"] == "high"
        assert chunk_meta["auto_importance"] == 0.91

    def test_tags_reach_chunks_when_classify_is_enabled_later(
        self, registry, vault, monkeypatch
    ):
        """The dominant real-world shape: a corpus ingested before
        classify-on-write, then re-synced with it on. Chunk bytes barely move,
        so a purely content-triggered propagation would leave most chunks —
        the retrievable unit — tag-dark while the parent looks tagged."""
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        store = registry.knowledge.document_store
        parent_id = corpus_doc_id("obsidian", "long.md")
        chunk_count = store.get(parent_id)["metadata"]["chunk_count"]
        assert chunk_count >= 2
        assert "content_tags" not in store.get(chunk_doc_id(parent_id, 0))["metadata"]

        # Flag on, and an edit small enough that only the trailing chunk's
        # bytes change — every other chunk takes the skip path.
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        note.write_text(_long_markdown() + " tail.")
        sync_corpus(registry, vault, source_system="obsidian")

        parent_meta = store.get(parent_id)["metadata"]
        assert parent_meta["content_tags"]
        for index in range(parent_meta["chunk_count"]):
            meta = store.get(chunk_doc_id(parent_id, index))["metadata"]
            assert meta["content_tags"] == parent_meta["content_tags"]
            assert meta["auto_importance"] == parent_meta["auto_importance"]

    def test_parent_tags_win_on_reput_but_untouched_chunks_keep_theirs(
        self, registry, vault, monkeypatch
    ):
        """Pins the precedence the propagation rests on.

        A chunk that is rewritten takes the parent's tags — the parent is
        authoritative, so parent and chunks can never disagree. A chunk the run
        does not touch keeps its own, because that is where the feedback loop's
        demote signal lives (``apply_noise_tags`` writes ``signal_quality:
        noise`` onto the served item, which is a chunk).
        """
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        store = registry.knowledge.document_store
        parent_id = corpus_doc_id("obsidian", "long.md")
        last_index = store.get(parent_id)["metadata"]["chunk_count"] - 1
        assert last_index >= 1
        apply_noise_tags(
            [chunk_doc_id(parent_id, 0), chunk_doc_id(parent_id, last_index)], store
        )

        # Head edit: chunk 0 reflows and is re-put, the last chunk does not.
        note.write_text("Edited opening paragraph.\n\n" + _long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        parent_quality = store.get(parent_id)["metadata"]["content_tags"][
            "signal_quality"
        ]
        assert parent_quality != "noise"
        reput = store.get(chunk_doc_id(parent_id, 0))["metadata"]
        assert reput["content_tags"]["signal_quality"] == parent_quality
        skipped = store.get(chunk_doc_id(parent_id, last_index))["metadata"]
        assert skipped["content_tags"]["signal_quality"] == "noise"

    def test_tagged_chunks_still_answer_a_domain_scoped_search(
        self, registry, vault, monkeypatch
    ):
        """Tagging a document must never *hide* it.

        classify-on-write deliberately persists ``content_tags.domain == []``
        (the facet is the only hard-excluding one, so it is not auto-assigned).
        A store that treats an empty facet as a value rather than as absent
        turns propagation into a disappearing act: every chunk of every tagged
        document drops out of every domain-scoped query.
        """
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")
        note.write_text("Edited opening paragraph.\n\n" + _long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        store = registry.knowledge.document_store
        parent_id = corpus_doc_id("obsidian", "long.md")
        assert store.get(chunk_doc_id(parent_id, 0))["metadata"]["content_tags"]

        hits = store.search(
            "kubernetes",
            filters={
                "content_tags": {
                    "domain": {"in": ["engineering"]},
                    "signal_quality": {"not_in": ["noise"]},
                }
            },
        )
        assert any(is_chunk_doc_id(hit["doc_id"]) for hit in hits)


class TestMetadataValidationSeam:
    """The ingest seam is where document metadata is validated.

    Shape-preserving by design — see
    :mod:`trellis.schemas.document_metadata`. These tests pin the three
    properties that make partial adoption safe: arbitrary frontmatter still
    stores flat, the only rewrite is the reconciled provenance key, and a
    document that has been rewritten still scores what it scored before.
    """

    def test_arbitrary_frontmatter_stores_flat_and_unchanged(
        self, registry, tmp_path: Path
    ):
        root = tmp_path / "frontmatter"
        root.mkdir()
        (root / "note.md").write_text(
            "---\n"
            "title: Odd Note\n"
            "rating: 4.5\n"
            "aliases:\n  - alt\n"
            "sprint: 14\n"
            "---\n\nBody.\n"
        )
        sync_corpus(registry, root, source_system="obsidian")

        metadata = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note.md")
        )["metadata"]
        assert metadata["title"] == "Odd Note"
        assert metadata["rating"] == 4.5
        assert metadata["aliases"] == ["alt"]
        assert metadata["sprint"] == 14
        assert "custom" not in metadata

    def test_non_string_title_does_not_break_ingest(self, registry, tmp_path: Path):
        root = tmp_path / "odd-title"
        root.mkdir()
        (root / "year.md").write_text("---\ntitle: 2026\n---\n\nBody.\n")
        report = sync_corpus(registry, root, source_system="obsidian")

        assert report.counts()["ingested"] == 1
        metadata = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "year.md")
        )["metadata"]
        # YAML parses a bare 2026 as an int; the value is preserved verbatim
        # (demoted to `custom`, re-flattened) rather than coerced or dropped.
        assert metadata["title"] == 2026

    def test_foreign_flat_content_type_is_reconciled_on_write(
        self, registry, tmp_path: Path
    ):
        # An operator ``--tag content_type=conversation`` is the same drift the
        # conversation reader used to produce; the seam normalises it.
        root = tmp_path / "tagged"
        root.mkdir()
        (root / "note.md").write_text("Body.\n")
        sync_corpus(
            registry,
            root,
            source_system="obsidian",
            extra_metadata={"content_type": "conversation"},
        )

        metadata = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note.md")
        )["metadata"]
        assert metadata["document_form"] == "conversation"
        assert "content_type" not in metadata

    def test_in_vocabulary_flat_content_type_is_left_alone(
        self, registry, tmp_path: Path
    ):
        root = tmp_path / "faceted"
        root.mkdir()
        (root / "note.md").write_text("Body.\n")
        sync_corpus(
            registry,
            root,
            source_system="obsidian",
            extra_metadata={"content_type": "decision"},
        )

        metadata = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note.md")
        )["metadata"]
        assert metadata["content_type"] == "decision"
        assert "document_form" not in metadata

    def test_ingested_document_scores_breadth_as_it_did_before(
        self, registry, tmp_path: Path
    ):
        # The composed property the rename can actually break: ingest through
        # the seam, read the stored metadata back, put it on a PackItem the way
        # retrieve.strategies does, and score it. "tutorial" is outside the
        # ContentType vocabulary — exactly the case the read-side fallback in
        # evaluate._item_content_type exists for — so the seam rewriting the
        # key must not move the score.
        root = tmp_path / "foreign"
        root.mkdir()
        (root / "note.md").write_text("---\ncontent_type: tutorial\n---\n\nBody.\n")
        sync_corpus(registry, root, source_system="obsidian")

        stored = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note.md")
        )["metadata"]
        assert stored["document_form"] == "tutorial"
        assert "content_type" not in stored

        pack = Pack(
            pack_id="p",
            intent="i",
            items=[
                PackItem(
                    item_id="doc-1",
                    item_type="document",
                    excerpt="Body.",
                    relevance_score=0.5,
                    metadata={"source_strategy": "keyword", **stored},
                )
            ],
        )
        scenario = EvaluationScenario(
            name="s", intent="i", expected_categories=["tutorial"]
        )
        assert BreadthScorer().score(pack, scenario) == 1.0

    def test_chunk_metadata_is_validated_too(self, registry, tmp_path: Path):
        root = tmp_path / "chunked"
        root.mkdir()
        (root / "long.md").write_text(_long_markdown())
        sync_corpus(
            registry,
            root,
            source_system="obsidian",
            extra_metadata={"content_type": "conversation"},
        )

        parent_id = corpus_doc_id("obsidian", "long.md")
        chunk = registry.knowledge.document_store.get(chunk_doc_id(parent_id, 0))
        assert chunk["metadata"]["document_form"] == "conversation"
        assert "content_type" not in chunk["metadata"]
        assert chunk["metadata"]["char_span"][0] == 0


class TestIdempotentResync:
    def test_second_run_over_unchanged_tree_is_zero_writes(self, registry, vault):
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")
        store = registry.knowledge.document_store
        before = {d["doc_id"]: d["updated_at"] for d in store.list_documents(limit=100)}

        report = sync_corpus(registry, vault, source_system="obsidian")

        counts = report.counts()
        assert counts["skipped_unchanged"] == 3
        assert counts["ingested"] == counts["updated"] == 0
        assert counts["chunks_written"] == 0
        after = {d["doc_id"]: d["updated_at"] for d in store.list_documents(limit=100)}
        assert after == before  # no row was touched
        memory_events = registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED
        )
        assert len(memory_events) == 3  # first run only

    def test_edited_file_reputs_and_reembeds_only_changed_chunks(
        self, registry, vault, monkeypatch
    ):
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")
        parent_id = corpus_doc_id("obsidian", "long.md")
        store = registry.knowledge.document_store
        old_count = store.get(parent_id)["metadata"]["chunk_count"]
        chunk0_before = store.get(chunk_doc_id(parent_id, 0))
        vec0_before = registry.knowledge.vector_store.get(chunk_doc_id(parent_id, 0))

        # Append a paragraph: earlier chunk spans are untouched by
        # construction (offsets before the edit point cannot move).
        (vault / "long.md").write_text(
            _long_markdown() + "\n\nA brand new closing paragraph about tubas.\n"
        )
        report = sync_corpus(registry, vault, source_system="obsidian")

        outcome = next(o for o in report.files if o.relpath == "long.md")
        assert outcome.action == "update"
        # Growing the doc bumps chunk_count, so unchanged chunks get a
        # metadata-refresh re-put — but only changed content re-embeds.
        assert outcome.chunks_written == old_count + 1
        chunk0_after = store.get(chunk_doc_id(parent_id, 0))
        assert chunk0_after["content_hash"] == chunk0_before["content_hash"]
        vec0_after = registry.knowledge.vector_store.get(chunk_doc_id(parent_id, 0))
        assert vec0_after["vector"] == vec0_before["vector"]  # not re-embedded
        # The changed tail chunk both exists and is embedded.
        last_id = chunk_doc_id(
            parent_id, store.get(parent_id)["metadata"]["chunk_count"] - 1
        )
        assert "tubas" in store.get(last_id)["content"]
        assert registry.knowledge.vector_store.get(last_id) is not None

    def test_shrunk_document_deletes_orphaned_chunks(self, registry, vault):
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")
        parent_id = corpus_doc_id("obsidian", "long.md")
        store = registry.knowledge.document_store
        old_count = store.get(parent_id)["metadata"]["chunk_count"]
        assert old_count >= 2

        (vault / "long.md").write_text("Now a short note.\n")
        sync_corpus(registry, vault, source_system="obsidian")

        parent = store.get(parent_id)
        assert parent["content"] == "Now a short note.\n"
        assert "chunk_count" not in parent["metadata"]
        for index in range(old_count):
            assert store.get(chunk_doc_id(parent_id, index)) is None

    def test_enrichment_added_metadata_survives_an_update(self, registry, vault):
        sync_corpus(registry, vault, source_system="obsidian")
        doc_id = corpus_doc_id("obsidian", "note-a.md")
        store = registry.knowledge.document_store
        stored = store.get(doc_id)
        store.put(
            doc_id,
            stored["content"],
            metadata={**stored["metadata"], "signal_quality": "high"},
        )

        (vault / "note-a.md").write_text("---\ntitle: Note A\n---\n\nEdited body.\n")
        sync_corpus(registry, vault, source_system="obsidian")

        assert store.get(doc_id)["metadata"]["signal_quality"] == "high"

    def test_enrichment_added_chunk_metadata_survives_an_update(self, registry, vault):
        """Chunk re-puts honour the same merge contract as the parent: keys the
        run does not own (an earlier run's operator tags, a chunk-local flag)
        survive instead of being rebuilt away."""
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(
            registry,
            vault,
            source_system="obsidian",
            extra_metadata={"team": "core"},
        )
        parent_id = corpus_doc_id("obsidian", "long.md")
        store = registry.knowledge.document_store
        chunk_id = chunk_doc_id(parent_id, 0)
        stored = store.get(chunk_id)
        store.put(
            chunk_id,
            stored["content"],
            metadata={**stored["metadata"], "signal_quality": "high"},
        )

        # Re-ingest without the operator tag — as the parent does, the chunk
        # keeps what this run does not supply.
        (vault / "long.md").write_text(
            "Edited opening paragraph.\n\n" + _long_markdown()
        )
        sync_corpus(registry, vault, source_system="obsidian")

        chunk_meta = store.get(chunk_id)["metadata"]
        assert chunk_meta["signal_quality"] == "high"
        assert chunk_meta["team"] == "core"
        assert store.get(parent_id)["metadata"]["team"] == "core"


class TestMoveDetection:
    def test_moved_file_is_rekeyed_not_duplicated(self, registry, vault):
        sync_corpus(registry, vault, source_system="obsidian")
        old_id = corpus_doc_id("obsidian", "note-a.md")
        content = (vault / "note-a.md").read_text()
        (vault / "note-a.md").unlink()
        (vault / "sub" / "renamed.md").write_text(content)

        report = sync_corpus(registry, vault, source_system="obsidian")

        outcome = next(o for o in report.files if o.relpath == "sub/renamed.md")
        assert outcome.action == "move"
        assert outcome.moved_from == old_id
        store = registry.knowledge.document_store
        assert store.get(old_id) is None
        new_doc = store.get(corpus_doc_id("obsidian", "sub/renamed.md"))
        assert new_doc is not None
        assert new_doc["content"] == content
        assert new_doc["metadata"]["source_path"] == "sub/renamed.md"


class TestPrune:
    def test_vanished_file_is_kept_without_prune(self, registry, vault):
        sync_corpus(registry, vault, source_system="obsidian")
        (vault / "note-a.md").unlink()
        report = sync_corpus(registry, vault, source_system="obsidian")
        assert report.pruned == []
        doc_id = corpus_doc_id("obsidian", "note-a.md")
        assert registry.knowledge.document_store.get(doc_id) is not None

    def test_prune_deletes_vanished_document_tree(self, registry, vault, monkeypatch):
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        (vault / "long.md").write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")
        parent_id = corpus_doc_id("obsidian", "long.md")
        store = registry.knowledge.document_store
        count = store.get(parent_id)["metadata"]["chunk_count"]
        assert registry.knowledge.vector_store.get(chunk_doc_id(parent_id, 0))

        (vault / "long.md").unlink()
        report = sync_corpus(registry, vault, source_system="obsidian", prune=True)

        assert [p["doc_id"] for p in report.pruned] == [parent_id]
        assert store.get(parent_id) is None
        for index in range(count):
            cid = chunk_doc_id(parent_id, index)
            assert store.get(cid) is None
            assert registry.knowledge.vector_store.get(cid) is None

    def test_prune_ignores_other_source_systems(self, registry, vault):
        other_root = vault.parent / "other"
        other_root.mkdir()
        (other_root / "keep.md").write_text("Other corpus content.\n")
        sync_corpus(registry, other_root, source_system="wiki")
        sync_corpus(registry, vault, source_system="obsidian", prune=True)
        keep_id = corpus_doc_id("wiki", "keep.md")
        assert registry.knowledge.document_store.get(keep_id) is not None


class TestDryRun:
    def test_dry_run_writes_nothing_but_reports_plan(self, registry, vault):
        (vault / "long.md").write_text(_long_markdown())
        report = sync_corpus(registry, vault, source_system="obsidian", dry_run=True)

        assert report.counts()["ingested"] == 3
        assert next(o.chunk_count for o in report.files if o.relpath == "long.md") >= 2
        assert registry.knowledge.document_store.count() == 0
        memory_events = registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED
        )
        assert memory_events == []
        summary = registry.operational.event_log.get_events(
            event_type=EventType.CORPUS_SYNCED
        )
        assert len(summary) == 1
        assert summary[0].payload["dry_run"] is True

    def test_dry_run_prune_lists_but_keeps_documents(self, registry, vault):
        sync_corpus(registry, vault, source_system="obsidian")
        (vault / "note-a.md").unlink()
        report = sync_corpus(
            registry, vault, source_system="obsidian", dry_run=True, prune=True
        )
        doc_id = corpus_doc_id("obsidian", "note-a.md")
        assert [p["doc_id"] for p in report.pruned] == [doc_id]
        assert registry.knowledge.document_store.get(doc_id) is not None


class TestNearDuplicates:
    def test_similar_files_warn_but_both_store(self, registry, vault):
        base = "The quarterly report covers revenue, churn and the roadmap. " * 10
        (vault / "dup-1.md").write_text(base)
        (vault / "dup-2.md").write_text(base.replace("roadmap", "Roadmap", 1))

        report = sync_corpus(registry, vault, source_system="obsidian")

        near = [w for w in report.warnings if w["kind"] == "near_duplicate"]
        assert len(near) == 1
        assert near[0]["path"] == "dup-2.md"
        store = registry.knowledge.document_store
        assert store.get(corpus_doc_id("obsidian", "dup-1.md")) is not None
        assert store.get(corpus_doc_id("obsidian", "dup-2.md")) is not None


class TestSemanticRetrieval:
    def test_chunks_are_semantically_retrievable_once_embedded(
        self, registry, vault, monkeypatch
    ):
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        (vault / "long.md").write_text(
            _long_markdown(topics=("kubernetes", "grapes", "violins"))
        )
        sync_corpus(registry, vault, source_system="obsidian")

        hits = registry.knowledge.vector_store.query(_embed("violins"), top_k=1)
        assert hits
        top = hits[0]
        parent_id = corpus_doc_id("obsidian", "long.md")
        assert top["item_id"].startswith(f"{parent_id}#chunk-")
        doc = registry.knowledge.document_store.get(top["item_id"])
        assert "violins" in doc["content"]

    def test_short_document_embeds_the_parent_itself(
        self, registry, vault, monkeypatch
    ):
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        sync_corpus(registry, vault, source_system="obsidian")
        doc_id = corpus_doc_id("obsidian", "sub/note-b.md")
        assert registry.knowledge.vector_store.get(doc_id) is not None


class TestChunkVectorMetadataMirror:
    """#388 — the metadata-only chunk re-put must reach the vector row.

    A vector row's metadata is a snapshot taken at embed time, and
    ``SemanticSearch`` builds its ``PackItem`` from that snapshot rather than
    from the document store. ``_write_chunks`` re-puts an unchanged chunk when
    it is missing a key the parent carries — and deliberately does not
    re-embed it — so before this was fixed the propagated tags landed in the
    document store alone and the semantic axis kept serving the chunk's
    pre-propagation tags. Third site of #338, after ``apply_noise_tags``
    (#343) and the curate/enrich paths (#386).

    Every assertion here is on the **row**, not on a call argument: the whole
    defect class is a mirror nobody performed while every document-store
    assertion still passed.
    """

    @staticmethod
    def _ingest_untagged_then_tag_parent(registry, vault, monkeypatch):
        """The dominant production shape, in three steps.

        1. Sync a long document with classify-on-write *off* — chunks are
           embedded and carry no ``content_tags`` at all.
        2. The enrichment pass tags the parent (a document-store write; the
           parent of a chunked document has no vector row of its own).
        3. A tail edit re-syncs: only the last chunk's bytes change, so every
           earlier chunk takes the ``tags_missing`` re-put — the branch that
           does not re-embed.

        Returns ``(parent_id, chunk_count)``.
        """
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        store = registry.knowledge.document_store
        parent_id = corpus_doc_id("obsidian", "long.md")
        parent = store.get(parent_id)
        chunk_count = parent["metadata"]["chunk_count"]
        assert chunk_count >= 3
        assert "content_tags" not in store.get(chunk_doc_id(parent_id, 0))["metadata"]

        store.put(
            parent_id,
            parent["content"],
            metadata={
                **parent["metadata"],
                # `signal_quality` is the facet the noise boundary acts on;
                # `importance_scored_at` is the stamp `_apply_importance`
                # requires beside a non-zero `auto_importance`, which is why
                # the two keys are mirrored together and never singly.
                "content_tags": {
                    "signal_quality": "noise",
                    "importance_scored_at": "2026-08-29T00:00:00+00:00",
                },
                "auto_importance": 0.91,
            },
        )

        note.write_text(_long_markdown() + " tail.")
        sync_corpus(registry, vault, source_system="obsidian")
        return parent_id, chunk_count

    def test_metadata_only_reput_mirrors_tags_onto_the_chunk_vector_row(
        self, registry, vault, monkeypatch
    ):
        parent_id, chunk_count = self._ingest_untagged_then_tag_parent(
            registry, vault, monkeypatch
        )
        store = registry.knowledge.document_store
        vectors = registry.knowledge.vector_store

        for index in range(chunk_count):
            cid = chunk_doc_id(parent_id, index)
            doc_meta = store.get(cid)["metadata"]
            row = vectors.get(cid)
            assert row is not None, cid
            # The same predicate the writer enforces, not a hand-rolled twin.
            assert not vector_metadata_diverges(doc_meta, row["metadata"]), cid
            assert row["metadata"]["content_tags"]["signal_quality"] == "noise"
            assert row["metadata"]["auto_importance"] == 0.91

    def test_mirror_is_metadata_only_and_keeps_the_embedding_and_excerpt(
        self, registry, vault, monkeypatch
    ):
        """The mirror is a re-upsert, so it must carry the vector through.

        Re-embedding here would be both a cost and a lie: the chunk's bytes
        did not change. The row's ``content`` excerpt is its own — cut at
        embed time by ``build_vector_row`` — and copying the document bag
        wholesale would clobber it.
        """
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        note = vault / "long.md"
        note.write_text(_long_markdown())
        sync_corpus(registry, vault, source_system="obsidian")

        parent_id = corpus_doc_id("obsidian", "long.md")
        first_chunk = chunk_doc_id(parent_id, 0)
        before = registry.knowledge.vector_store.get(first_chunk)

        store = registry.knowledge.document_store
        parent = store.get(parent_id)
        store.put(
            parent_id,
            parent["content"],
            metadata={**parent["metadata"], "auto_importance": 0.42},
        )
        note.write_text(_long_markdown() + " tail.")
        sync_corpus(registry, vault, source_system="obsidian")

        after = registry.knowledge.vector_store.get(first_chunk)
        assert after["vector"] == before["vector"]
        assert after["metadata"]["content"] == before["metadata"]["content"]
        assert after["metadata"]["doc_id"] == before["metadata"]["doc_id"]
        assert after["metadata"]["created_at"] == before["metadata"]["created_at"]
        assert after["metadata"]["auto_importance"] == 0.42

    def test_demoted_chunks_stop_being_served_by_the_semantic_axis(
        self, registry, vault, monkeypatch
    ):
        """The consequence, end to end.

        ``exclude_noise`` reads ``content_tags.signal_quality`` off the
        ``PackItem``, and on the semantic axis that metadata *is* the vector
        row's snapshot. Un-mirrored, every chunk the parent demoted was still
        served.
        """
        parent_id, chunk_count = self._ingest_untagged_then_tag_parent(
            registry, vault, monkeypatch
        )
        semantic = SemanticSearch(registry.knowledge.vector_store, _embed)
        served = [
            item
            for item in semantic.search("kubernetes grapes violins facts", limit=50)
            if item.item_id.startswith(f"{parent_id}#chunk-")
        ]
        assert len(served) == chunk_count
        assert exclude_noise(served) == []

    def test_document_is_still_written_without_a_vector_store(
        self, registry, vault, monkeypatch
    ):
        """A deployment with no vector store must still propagate tags.

        The document store is the authority; the mirror is a best-effort
        second write. Losing it must never lose the tag.
        """
        registry.knowledge.vector_store = None
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        parent_id, chunk_count = self._ingest_untagged_then_tag_parent(
            registry, vault, monkeypatch
        )
        store = registry.knowledge.document_store
        for index in range(chunk_count):
            meta = store.get(chunk_doc_id(parent_id, index))["metadata"]
            assert meta["content_tags"]["signal_quality"] == "noise"


def test_classify_keys_are_covered_by_the_mirror() -> None:
    """The containment `_write_chunks`' mirror silently depends on.

    `_write_chunks` propagates ``CLASSIFY_METADATA_KEYS`` to chunk documents
    and mirrors with ``sync_vector_metadata``'s **default** key set,
    ``SYNCED_METADATA_KEYS``. The two are equal today, and the docstring
    said so — but ``CLASSIFY_METADATA_KEYS``' own comment anticipates
    growth ("adding a facet here cannot silently stop propagating"). Add a
    third key there and the chunk document gets it while the vector row
    silently does not: #388 reintroduced, under a docstring asserting it
    cannot happen.

    Containment, not equality, is the real dependency — the mirror may
    legitimately carry keys the classify layer does not write. Fixing a
    failure here by passing ``keys=CLASSIFY_METADATA_KEYS`` would be the
    wrong repair: ``SYNCED_METADATA_KEYS`` is deliberately narrow about what
    a vector row may receive, and coupling it to the classify layer subverts
    that. Widen the mirror's key set deliberately, or say why the new facet
    is document-only.
    """
    assert set(CLASSIFY_METADATA_KEYS) <= set(SYNCED_METADATA_KEYS)


class TestVectorMirrorIsObservable:
    """A mirror that silently did nothing must say so (#388).

    The first cut of this fix reported the mirror on a per-document
    ``logger.debug`` line. That is a no-op under the CLI's default config —
    ``configure_stderr_logging`` installs
    ``make_filtering_bound_logger(INFO)``, under which ``debug`` is not
    merely filtered downstream but never constructed — so a corpus sync
    could propagate tags to thousands of chunk documents, mirror none of
    them, and say nothing at any visible level. Which is #338's failure
    wearing a fixed label.

    It is now one run-level line at a level that fires, and the count it
    reports is paired with the only denominator it *can* be a fraction of.
    """

    def test_absent_vector_store_warns_and_names_the_repair(
        self, registry, vault, monkeypatch
    ):
        registry.knowledge.vector_store = None
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        with capture_logs() as logs:
            TestChunkVectorMetadataMirror._ingest_untagged_then_tag_parent(
                registry, vault, monkeypatch
            )

        warnings = [
            entry
            for entry in logs
            if entry["event"] == "corpus_sync_vector_mirror_unavailable"
        ]
        assert len(warnings) == 1, "exactly one run-level line, not one per document"
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["metadata_only_writes"] > 0
        # Loud is not enough — it has to be actionable.
        assert "resync-vector-metadata" in warnings[0]["consequence"]

    def test_mirror_count_is_reported_against_its_own_denominator(
        self, registry, vault, monkeypatch
    ):
        """``chunks_written`` is *not* the denominator of ``rows_synced``.

        Only the metadata-only branch can mirror, so pairing the mirror
        count with the total chunks written would print two numbers that
        look like a ratio and are not — a first sync that legitimately
        re-embeds every chunk would read ``N written, 0 synced`` and look
        broken.
        """
        with capture_logs() as logs:
            _, chunk_count = (
                TestChunkVectorMetadataMirror._ingest_untagged_then_tag_parent(
                    registry, vault, monkeypatch
                )
            )

        lines = [
            entry for entry in logs if entry["event"] == "corpus_sync_vector_mirror"
        ]
        assert len(lines) == 1
        # Every chunk but the one whose bytes changed took the mirror path,
        # and every one of those was mirrored.
        assert lines[0]["metadata_only_writes"] == chunk_count - 1
        assert lines[0]["rows_synced"] == chunk_count - 1

    def test_a_run_with_nothing_to_mirror_stays_quiet(
        self, registry, vault, monkeypatch
    ):
        """A zero line on every sync trains the reader to skip it."""
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        (vault / "long.md").write_text(_long_markdown())
        with capture_logs() as logs:
            sync_corpus(registry, vault, source_system="obsidian")

        assert not [
            entry
            for entry in logs
            if entry["event"].startswith("corpus_sync_vector_mirror")
        ]
