"""A moved or renamed corpus file keeps its lifecycle state (#1132, item 2).

``ingest-corpus`` re-keys a file whose bytes reappear under a new path: the new
row gets a new id and the old row and its chunks are deleted. Before this fix
the new row carried only this run's own metadata, so a document retention had
archived, or that a reconcile verdict or ``supersedes=`` had stamped
superseded, came back as current and was served again. Each test renames a
real file on disk and runs the real sync against real SQLite stores, then asks
the pack builder rather than the metadata the sync wrote.

The comparison arm is always the same pack before the move, so a pack that
withholds nothing for an unrelated reason cannot pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.core.document_write import put_document
from trellis.ingest_corpus import chunker
from trellis.ingest_corpus.models import SyncRecord, chunk_doc_id, corpus_doc_id
from trellis.ingest_corpus.sync import sync_corpus, sync_records
from trellis.mcp.reconcile import SUPERSEDES_DOC_KEY, mark_document_superseded
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.registry import StoreRegistry

#: Dotted path handed to ``TRELLIS_EMBEDDING_FN``; it cannot carry a closure.
EMBED_FN_PATH = "tests.unit.ingest_corpus.test_move_keeps_lifecycle.embed"

OLD_PATH = "notes/old-name.md"
NEW_PATH = "archive/new-name.md"
SOURCE = "vault"
OLD_ID = corpus_doc_id(SOURCE, OLD_PATH)
NEW_ID = corpus_doc_id(SOURCE, NEW_PATH)
#: A memory that replaces the corpus file; it is not a corpus row.
SUCCESSOR_ID = "memory-successor"

#: A word no other test document carries, so a pack for it finds this one only.
INTENT = "zebrafish"
SHORT_TEXT = "Notes about zebrafish genetics, kept short so the file is not chunked.\n"


def embed(text: str) -> list[float]:
    return [1.0, float(len(text) % 7), 0.5]


def _long_text() -> str:
    """Three sections, long enough to chunk (chunk rows carry their own state)."""
    sections = []
    for i, topic in enumerate((INTENT, "axolotl", "newts")):
        para = (f"All about {topic}. The {topic} facts continue here. ") * 80
        sections.append(f"## Section {i}\n\n{para.strip()}")
    return "\n\n".join(sections)


@pytest.fixture
def semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embed on ingest, so the semantic axis has vector rows to serve."""
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")


def _registry(tmp_path: Path) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir(exist_ok=True)
    return StoreRegistry(stores_dir=stores, embedding_config={"local": True})


def _vault(tmp_path: Path, text: str) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / OLD_PATH).write_text(text, encoding="utf-8")
    return root


def _rename(root: Path) -> None:
    (root / "archive").mkdir(exist_ok=True)
    (root / NEW_PATH).write_text(
        (root / OLD_PATH).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / OLD_PATH).unlink()


def _move(registry: StoreRegistry, root: Path) -> Any:
    _rename(root)
    report = sync_corpus(registry, root, source_system=SOURCE)
    outcome = next(f for f in report.files if f.relpath == NEW_PATH)
    assert outcome.action == "move"
    assert outcome.moved_from == OLD_ID
    assert registry.knowledge.document_store.get(OLD_ID) is None
    return report


def _chunk_ids(registry: StoreRegistry, parent_id: str) -> list[str]:
    count = registry.knowledge.document_store.get(parent_id)["metadata"].get(
        "chunk_count", 0
    )
    return [chunk_doc_id(parent_id, i) for i in range(count)]


def _lifecycle(registry: StoreRegistry, doc_id: str) -> Any:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None, doc_id
    return row["metadata"].get(LIFECYCLE_KEY)


def _archive(registry: StoreRegistry, doc_ids: list[str]) -> None:
    """Archive documents through retention, the way the noise sweep does.

    Tags each noise (the criterion), then runs the real ``retention.prune``
    handler, so the stamp is exactly what production writes.
    """
    docs = registry.knowledge.document_store
    for doc_id in doc_ids:
        row = docs.get(doc_id)
        assert row is not None, doc_id
        put_document(
            docs,
            registry.knowledge.vector_store,
            doc_id,
            row["content"],
            {**row["metadata"], "content_tags": {"signal_quality": "noise"}},
            preserve_updated_at=True,
        )
    result = build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=Operation.RETENTION_PRUNE,
            args={
                "criteria": {"noise_documents": True},
                "reason": "test archival",
                "dry_run": False,
            },
            requested_by="test",
        )
    )
    assert result.status == CommandStatus.SUCCESS
    for doc_id in doc_ids:
        assert _lifecycle(registry, doc_id)["state"] == "archived"


def _pack(registry: StoreRegistry) -> tuple[set[str], dict[str, str]]:
    """``(served ids, {withheld id: reason})`` for a pack on the intent."""
    pack = build_pack_builder(registry, surface="test").build(
        INTENT, clearance="internal"
    )
    return (
        {item.item_id for item in pack.items},
        {i.item_id: i.reason for i in pack.retrieval_report.rejected_items},
    )


def _of_document(ids: set[str], parent_id: str) -> set[str]:
    """The ids in *ids* that are the document or one of its chunks."""
    return {i for i in ids if i == parent_id or i.startswith(f"{parent_id}#chunk-")}


class TestArchivedFile:
    """A document retention archived stays archived, and withheld, after a move."""

    def test_a_moved_archived_document_is_still_archived_and_withheld(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, SHORT_TEXT)
        sync_corpus(registry, root, source_system=SOURCE)
        served, _ = _pack(registry)
        assert OLD_ID in served  # the probe finds the file while it is current

        _archive(registry, [OLD_ID])
        before = _lifecycle(registry, OLD_ID)
        served, withheld = _pack(registry)
        assert OLD_ID not in served
        assert withheld[OLD_ID] == "archived"

        _move(registry, root)

        served, withheld = _pack(registry)
        assert NEW_ID not in served
        assert withheld[NEW_ID] == "archived"
        assert _lifecycle(registry, NEW_ID) == before

    def test_every_chunk_that_was_archived_stays_archived(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        old_chunks = _chunk_ids(registry, OLD_ID)
        assert len(old_chunks) >= 2
        _archive(registry, [OLD_ID, *old_chunks])
        before = {i: _lifecycle(registry, i) for i in (OLD_ID, *old_chunks)}
        served, _ = _pack(registry)
        assert _of_document(served, OLD_ID) == set()

        _move(registry, root)

        new_chunks = _chunk_ids(registry, NEW_ID)
        assert len(new_chunks) == len(old_chunks)
        served, withheld = _pack(registry)
        assert _of_document(served, NEW_ID) == set()
        assert {withheld[i] for i in (NEW_ID, *new_chunks) if i in withheld} == {
            "archived"
        }
        assert _lifecycle(registry, NEW_ID) == before[OLD_ID]
        for old_id, new_id in zip(old_chunks, new_chunks, strict=True):
            assert _lifecycle(registry, new_id) == before[old_id]

    def test_only_the_chunks_that_were_archived_are_carried(
        self, tmp_path: Path
    ) -> None:
        """A chunk is carried by its own state, never stamped from its parent."""
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        old_chunks = _chunk_ids(registry, OLD_ID)
        _archive(registry, [old_chunks[0]])
        assert _lifecycle(registry, OLD_ID) is None

        _move(registry, root)

        new_chunks = _chunk_ids(registry, NEW_ID)
        assert _lifecycle(registry, new_chunks[0])["state"] == "archived"
        assert _lifecycle(registry, NEW_ID) is None
        assert all(_lifecycle(registry, c) is None for c in new_chunks[1:])

    def test_a_chunk_whose_text_moved_under_it_is_not_carried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The file's bytes match, but a chunker change shifts the spans.

        Index ``i`` then holds other text, so the archive stamp must not land
        on it; only a chunk holding the old chunk ``i``'s exact text is carried.
        """
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        docs = registry.knowledge.document_store
        old_chunks = _chunk_ids(registry, OLD_ID)
        _archive(registry, old_chunks)
        old_text = [docs.get(c)["content"] for c in old_chunks]

        monkeypatch.setattr(chunker, "CHUNK_TARGET_CHARS", 1200)
        monkeypatch.setattr(chunker, "CHUNK_MAX_CHARS", 1500)
        _move(registry, root)

        new_chunks = _chunk_ids(registry, NEW_ID)
        shifted = 0
        for i, cid in enumerate(new_chunks):
            same_text = i < len(old_text) and docs.get(cid)["content"] == old_text[i]
            if same_text:
                assert _lifecycle(registry, cid)["state"] == "archived", cid
            else:
                shifted += 1
                assert _lifecycle(registry, cid) is None, cid
        assert shifted  # the chunker change really moved text under an index

    def test_the_semantic_axis_is_withheld_too(
        self, tmp_path: Path, semantic: None
    ) -> None:
        """A vector row snapshots metadata at embed time; the moved one must say so."""
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        old_chunks = _chunk_ids(registry, OLD_ID)
        assert registry.knowledge.vector_store.get(old_chunks[0]) is not None
        _archive(registry, [OLD_ID, *old_chunks])

        _move(registry, root)

        for chunk_id in _chunk_ids(registry, NEW_ID):
            row = registry.knowledge.vector_store.get(chunk_id)
            assert row is not None
            assert row["metadata"][LIFECYCLE_KEY]["state"] == "archived"
        served, _ = _pack(registry)
        assert _of_document(served, NEW_ID) == set()


class TestSupersededFile:
    """A superseded document stays superseded after a move."""

    @staticmethod
    def _supersede(registry: StoreRegistry) -> None:
        """Store the successor, then stamp the corpus file as a verdict does."""
        docs = registry.knowledge.document_store
        vectors = registry.knowledge.vector_store
        put_document(
            docs,
            vectors,
            SUCCESSOR_ID,
            "A newer note about zebrafish genetics that replaces the old one.",
            {"reconciliation": "supersede", SUPERSEDES_DOC_KEY: OLD_ID},
            preserve_updated_at=False,
        )
        assert (
            mark_document_superseded(
                docs, old_doc_id=OLD_ID, new_doc_id=SUCCESSOR_ID, vector_store=vectors
            )
            is None
        )

    def test_a_moved_superseded_document_is_still_withheld_beside_its_successor(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, SHORT_TEXT)
        sync_corpus(registry, root, source_system=SOURCE)
        self._supersede(registry)
        before = _lifecycle(registry, OLD_ID)
        assert before["state"] == "superseded"
        served, withheld = _pack(registry)
        assert SUCCESSOR_ID in served
        assert OLD_ID not in served
        assert withheld[OLD_ID] == "superseded"

        _move(registry, root)

        served, withheld = _pack(registry)
        assert SUCCESSOR_ID in served
        assert NEW_ID not in served
        assert withheld[NEW_ID] == "superseded"
        assert _lifecycle(registry, NEW_ID) == before

    def test_the_successors_marker_is_left_naming_the_pre_move_id(
        self, tmp_path: Path
    ) -> None:
        """Documents the residue: a move carries the loser's state, not the pointer.

        The successor's ``supersedes_doc_id`` is not rewritten, so it names an
        id the move deleted. Nothing that decides what is served reads it (the
        pairwise gate reads the loser's own ``lifecycle.superseded_by``, which
        is carried), so the pack above is right. When a seam that repoints the
        marker exists, this is the assertion to flip.
        """
        registry = _registry(tmp_path)
        root = _vault(tmp_path, SHORT_TEXT)
        sync_corpus(registry, root, source_system=SOURCE)
        self._supersede(registry)

        _move(registry, root)

        docs = registry.knowledge.document_store
        marker = docs.get(SUCCESSOR_ID)["metadata"][SUPERSEDES_DOC_KEY]
        assert marker == OLD_ID
        assert docs.get(marker) is None
        assert _lifecycle(registry, NEW_ID)["superseded_by"] == SUCCESSOR_ID


class TestWhatIsCarried:
    def test_the_record_is_carried_whole(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, SHORT_TEXT)
        sync_corpus(registry, root, source_system=SOURCE)
        _archive(registry, [OLD_ID])
        stored = _lifecycle(registry, OLD_ID)
        assert stored["deprecation_reason"] == "test archival"
        assert stored["valid_until"] is not None

        _move(registry, root)

        assert _lifecycle(registry, NEW_ID) == stored

    def test_a_move_of_a_current_document_gains_no_lifecycle_key(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)

        _move(registry, root)

        for doc_id in (NEW_ID, *_chunk_ids(registry, NEW_ID)):
            assert _lifecycle(registry, doc_id) is None
        served, _ = _pack(registry)
        assert _of_document(served, NEW_ID)

    def test_a_lifecycle_this_run_supplies_wins_as_it_does_on_an_update(
        self, tmp_path: Path
    ) -> None:
        """Carried *under* this run's metadata, the precedence an update gives."""
        registry = _registry(tmp_path)
        prefix = f"corpus:{SOURCE}:"

        def run(doc_id: str, metadata: dict[str, Any]) -> None:
            sync_records(
                registry,
                [
                    SyncRecord(
                        doc_id=doc_id,
                        source_key=doc_id.removeprefix(prefix),
                        content=SHORT_TEXT,
                        handler_metadata=metadata,
                    )
                ],
                source_system=SOURCE,
                id_prefix=prefix,
                root_label=SOURCE,
                requested_by="test",
            )

        run(OLD_ID, {})
        _archive(registry, [OLD_ID])
        run(NEW_ID, {LIFECYCLE_KEY: {"state": "current"}})

        assert _lifecycle(registry, NEW_ID) == {"state": "current"}
        assert registry.knowledge.document_store.get(OLD_ID) is None
