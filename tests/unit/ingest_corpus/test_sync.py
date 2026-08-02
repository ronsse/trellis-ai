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

from trellis.classify.ingest import CLASSIFY_ON_INGEST_FLAG
from trellis.ingest_corpus.models import chunk_doc_id, corpus_doc_id
from trellis.ingest_corpus.sync import sync_corpus
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
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

        # Edit the head of the document so every chunk shifts and is re-put.
        # The second run classifies nothing (parent is already tagged), so the
        # chunks' tags can only come from the parent's stored metadata.
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
