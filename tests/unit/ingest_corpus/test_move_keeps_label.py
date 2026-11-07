"""A moved or renamed corpus file keeps the label set on its stored row (#1132).

``ingest-corpus`` re-keys a file whose bytes reappear under a new path. Before
this fix the move wrote only the run's own metadata, so a label put on the
stored row by ``trellis curate classify-sensitivity`` (``classification.set``)
was dropped and every gate then read the document as unlabelled. Each test
renames a real file on disk and runs the real sync, against real SQLite
stores, then asks the gate itself rather than the metadata the sync wrote.

Both gates are covered: the read gate (a pack built at a clearance below the
label) and the egress gate (an embedder or an extractor that is not declared
local). The comparison arm is always the unlabelled document, which must be
served and sent exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trellis.core.document_write import put_document
from trellis.extract.memory_ingest_hook import MEMORY_EXTRACTION_FLAG
from trellis.ingest_corpus.models import SyncRecord, chunk_doc_id, corpus_doc_id
from trellis.ingest_corpus.sync import sync_corpus, sync_records
from trellis.llm.egress import EgressGate, EgressVerdict
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers.classification import (
    ClassificationSetHandler,
    ClassificationTagRegulatoryHandler,
)
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY, sensitivity_of
from trellis.schemas.extraction import (
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.stores.registry import StoreRegistry

#: Dotted path handed to ``TRELLIS_EMBEDDING_FN``; it cannot carry a closure.
EMBED_FN_PATH = "tests.unit.ingest_corpus.test_move_keeps_label.embed"

OLD_PATH = "notes/old-name.md"
NEW_PATH = "archive/new-name.md"
SOURCE = "vault"
OLD_ID = corpus_doc_id(SOURCE, OLD_PATH)
NEW_ID = corpus_doc_id(SOURCE, NEW_PATH)

#: A word no other test document carries, so a pack for it finds this one only.
INTENT = "zebrafish"

TEXTS: list[str] = []


def embed(text: str) -> list[float]:
    """Recording embedder: every text it is handed lands in ``TEXTS``."""
    TEXTS.append(text)
    return [1.0, float(len(text) % 7), 0.5]


def _long_text() -> str:
    """Three sections, long enough to chunk (the gates must hold for chunks too)."""
    sections = []
    for i, topic in enumerate((INTENT, "axolotl", "newts")):
        para = (f"All about {topic}. The {topic} facts continue here. ") * 80
        sections.append(f"## Section {i}\n\n{para.strip()}")
    return "\n\n".join(sections)


@pytest.fixture(autouse=True)
def _recording(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    TEXTS.clear()
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
    yield
    TEXTS.clear()


def _registry(tmp_path: Path, *, local: bool | None = None) -> StoreRegistry:
    """Real SQLite stores; ``embeddings.local`` declared, or silent for ``None``."""
    stores = tmp_path / "stores"
    stores.mkdir(exist_ok=True)
    config = {} if local is None else {"local": local}
    return StoreRegistry(stores_dir=stores, embedding_config=config)


def _vault(tmp_path: Path, text: str) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / OLD_PATH).write_text(text, encoding="utf-8")
    return root


def _label(registry: StoreRegistry, doc_id: str, level: str) -> None:
    """Label the stored row the way ``trellis curate classify-sensitivity`` does."""
    ClassificationSetHandler(registry).handle(
        Command(
            operation=Operation.CLASSIFICATION_SET,
            args={"doc_id": doc_id, "sensitivity": level},
            requested_by="test",
        )
    )


def _rename(root: Path) -> None:
    (root / "archive").mkdir(exist_ok=True)
    (root / NEW_PATH).write_text(
        (root / OLD_PATH).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / OLD_PATH).unlink()


def _stored_label(registry: StoreRegistry, doc_id: str) -> Any:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None
    return row["metadata"].get(DATA_CLASSIFICATION_KEY)


def _chunk_ids(registry: StoreRegistry, parent_id: str) -> list[str]:
    count = registry.knowledge.document_store.get(parent_id)["metadata"].get(
        "chunk_count", 0
    )
    return [chunk_doc_id(parent_id, i) for i in range(count)]


def _pack(registry: StoreRegistry, clearance: str) -> tuple[set[str], set[str]]:
    """``(served ids, ids withheld for sensitivity)`` for a pack on the intent."""
    pack = build_pack_builder(registry, surface="test").build(
        INTENT, clearance=clearance
    )
    return (
        {item.item_id for item in pack.items},
        {
            item.item_id
            for item in pack.retrieval_report.rejected_items
            if item.reason == "sensitivity"
        },
    )


def _of_document(ids: set[str], parent_id: str) -> set[str]:
    """The ids in *ids* that are the document or one of its chunks."""
    return {i for i in ids if i == parent_id or i.startswith(f"{parent_id}#chunk-")}


def _moved_labelled(
    tmp_path: Path, level: str = "restricted", *, local: bool | None = None
) -> tuple[StoreRegistry, Path, Any]:
    """Sync a chunked file, label it, rename it and sync again.

    Returns the registry, the vault root and the second run's report.
    """
    registry = _registry(tmp_path, local=local)
    root = _vault(tmp_path, _long_text())
    sync_corpus(registry, root, source_system=SOURCE)
    assert registry.knowledge.document_store.get(OLD_ID) is not None
    _label(registry, OLD_ID, level)
    TEXTS.clear()
    _rename(root)
    report = sync_corpus(registry, root, source_system=SOURCE)
    outcome = next(f for f in report.files if f.relpath == NEW_PATH)
    assert outcome.action == "move"
    assert outcome.moved_from == OLD_ID
    assert registry.knowledge.document_store.get(OLD_ID) is None
    return registry, root, report


class TestReadGate:
    """A labelled document that moved is withheld below its label."""

    @pytest.mark.parametrize("level", ["confidential", "restricted"])
    def test_a_labelled_moved_document_is_withheld_from_a_lower_clearance(
        self, tmp_path: Path, level: str
    ) -> None:
        registry, _, _ = _moved_labelled(tmp_path, level)

        assert _stored_label(registry, NEW_ID)["sensitivity"] == level
        served, withheld = _pack(registry, "internal")
        assert _of_document(served, NEW_ID) == set()
        # The document itself or a chunk of it was found and refused, so the
        # empty pack is the gate's doing and not a retrieval miss.
        assert _of_document(withheld, NEW_ID)
        served, withheld = _pack(registry, level)
        assert _of_document(served, NEW_ID)
        assert _of_document(withheld, NEW_ID) == set()

    def test_every_chunk_of_a_labelled_moved_document_takes_the_label(
        self, tmp_path: Path
    ) -> None:
        registry, _, _ = _moved_labelled(tmp_path)

        chunks = _chunk_ids(registry, NEW_ID)
        assert len(chunks) >= 2
        served, _ = _pack(registry, "internal")
        assert served.isdisjoint(chunks)

    def test_a_chunk_vector_hit_is_judged_by_the_carried_parent_label(
        self, tmp_path: Path
    ) -> None:
        """A chunk's vector row snapshots no label, so the parent's must decide."""
        registry, _, _ = _moved_labelled(tmp_path, local=True)

        chunks = _chunk_ids(registry, NEW_ID)
        rows = [registry.knowledge.vector_store.get(c) for c in chunks]
        assert rows
        assert all(row is not None for row in rows)
        assert all(
            DATA_CLASSIFICATION_KEY not in (row.get("metadata") or {}) for row in rows
        )
        served, withheld = _pack(registry, "internal")
        assert _of_document(served, NEW_ID) == set()
        assert not withheld.isdisjoint(chunks)
        served, _ = _pack(registry, "restricted")
        assert not served.isdisjoint(chunks)

    def test_the_comparison_arm_is_served_before_the_label_is_set(
        self, tmp_path: Path
    ) -> None:
        """The probe discriminates: the file is served at ``internal`` unlabelled."""
        registry = _registry(tmp_path)
        sync_corpus(registry, _vault(tmp_path, _long_text()), source_system=SOURCE)

        served, _ = _pack(registry, "internal")

        assert _of_document(served, OLD_ID)

    def test_an_unlabelled_moved_document_is_served_exactly_as_before(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        before, _ = _pack(registry, "internal")
        assert _of_document(before, OLD_ID)

        _rename(root)
        sync_corpus(registry, root, source_system=SOURCE)

        assert (
            DATA_CLASSIFICATION_KEY
            not in (registry.knowledge.document_store.get(NEW_ID)["metadata"])
        )
        after, withheld = _pack(registry, "internal")
        assert _of_document(after, NEW_ID)
        assert _of_document(withheld, NEW_ID) == set()
        assert {i.replace(NEW_ID, OLD_ID) for i in _of_document(after, NEW_ID)} == (
            _of_document(before, OLD_ID)
        )


class TestEgressGate:
    """A labelled document that moved does not reach a route not declared local."""

    def test_the_gate_judges_the_moved_document_and_its_chunks_withheld(
        self, tmp_path: Path
    ) -> None:
        registry, _, _ = _moved_labelled(tmp_path)
        docs = registry.knowledge.document_store
        gate = EgressGate.for_embedder(registry, docs)
        assert gate.local is False

        for doc_id in (NEW_ID, *_chunk_ids(registry, NEW_ID)):
            assert gate.verdict(doc_id, docs.get(doc_id)["metadata"]) is (
                EgressVerdict.WITHHELD
            ), doc_id

    def test_a_labelled_moved_document_never_reaches_an_undeclared_embedder(
        self, tmp_path: Path
    ) -> None:
        registry, _, report = _moved_labelled(tmp_path)

        # ``_moved_labelled`` cleared the recorder after the first run, so
        # anything in it was handed to the embedder by the move.
        assert TEXTS == []
        assert report.embed_skipped_egress_withheld >= 1
        vectors = registry.knowledge.vector_store
        assert vectors.get(NEW_ID) is None
        assert all(vectors.get(c) is None for c in _chunk_ids(registry, NEW_ID))

    def test_the_same_move_embeds_when_the_embedder_is_declared_local(
        self, tmp_path: Path
    ) -> None:
        """The embedder is reached on the declared-local arm, so the gate held it."""
        registry, _, report = _moved_labelled(tmp_path, local=True)

        assert report.embed_skipped_egress_withheld == 0
        assert TEXTS
        # A chunked document's semantic handle is its chunk rows, not the parent.
        vectors = registry.knowledge.vector_store
        assert all(vectors.get(c) is not None for c in _chunk_ids(registry, NEW_ID))

    def test_an_unlabelled_moved_document_is_embedded_as_before(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        TEXTS.clear()

        _rename(root)
        report = sync_corpus(registry, root, source_system=SOURCE)

        assert report.embed_skipped_egress_withheld == 0
        assert TEXTS
        vectors = registry.knowledge.vector_store
        assert all(vectors.get(c) is not None for c in _chunk_ids(registry, NEW_ID))


class _Extractor:
    """Async extractor double that records the texts it is handed."""

    name = "fake"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def extract(self, raw, *, source_hint, context):  # type: ignore[no-untyped-def]
        self.texts.append(raw["text"])
        return ExtractionResult(
            entities=[EntityDraft(entity_type="person", name="Theo")],
            edges=[],
            extractor_used="fake",
            tier="deterministic",
            provenance=ExtractionProvenance(extractor_name="fake"),
        )


class TestExtractionGate:
    """``--extract`` sends no labelled text to the extractor, moved or not."""

    PREFIX = f"corpus:{SOURCE}:"
    TEXT = "My son Theo keeps a zebrafish tank.\n"

    def _record(self, doc_id: str) -> SyncRecord:
        return SyncRecord(
            doc_id=doc_id,
            source_key=doc_id.removeprefix(self.PREFIX),
            content=self.TEXT,
        )

    def _sync(self, registry: StoreRegistry, doc_id: str, extractor: Any) -> Any:
        return sync_records(
            registry,
            [self._record(doc_id)],
            source_system=SOURCE,
            id_prefix=self.PREFIX,
            root_label=SOURCE,
            requested_by="test",
            extractor=extractor,
        )

    def test_a_labelled_moved_document_is_not_sent_to_the_extractor(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path, local=True)
        self._sync(registry, OLD_ID, None)
        _label(registry, OLD_ID, "restricted")
        extractor = _Extractor()

        report = self._sync(registry, NEW_ID, extractor)

        assert next(f for f in report.files).action == "move"
        assert extractor.texts == []
        assert report.extraction_skipped_labelled == 1
        assert report.entities_extracted == 0

    def test_an_unlabelled_moved_document_is_still_extracted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MEMORY_EXTRACTION_FLAG, "1")
        registry = _registry(tmp_path, local=True)
        self._sync(registry, OLD_ID, None)
        extractor = _Extractor()

        report = self._sync(registry, NEW_ID, extractor)

        assert next(f for f in report.files).action == "move"
        assert extractor.texts == [self.TEXT]
        assert report.extraction_skipped_labelled == 0
        assert report.entities_extracted == 1


class TestWhatIsCarried:
    """The record is carried whole, and this run's own label outranks it."""

    def test_the_record_is_carried_whole_including_regulatory_tags(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        _label(registry, OLD_ID, "confidential")
        ClassificationTagRegulatoryHandler(registry).handle(
            Command(
                operation=Operation.CLASSIFICATION_TAG_REGULATORY,
                args={"doc_id": OLD_ID, "tags": ["hipaa", "gdpr"]},
                requested_by="test",
            )
        )
        stored = _stored_label(registry, OLD_ID)
        assert stored["regulatory_tags"] == ["gdpr", "hipaa"]

        _rename(root)
        sync_corpus(registry, root, source_system=SOURCE)

        assert _stored_label(registry, NEW_ID) == stored

    def test_a_record_that_cannot_be_read_is_carried_as_it_stands_and_withholds(
        self, tmp_path: Path
    ) -> None:
        """A malformed label reads as restricted; a move must not clear it."""
        registry = _registry(tmp_path)
        root = _vault(tmp_path, _long_text())
        sync_corpus(registry, root, source_system=SOURCE)
        docs = registry.knowledge.document_store
        row = docs.get(OLD_ID)
        put_document(
            docs,
            registry.knowledge.vector_store,
            OLD_ID,
            row["content"],
            {**row["metadata"], DATA_CLASSIFICATION_KEY: "oops"},
            preserve_updated_at=True,
        )

        _rename(root)
        sync_corpus(registry, root, source_system=SOURCE)

        assert _stored_label(registry, NEW_ID) == "oops"
        assert sensitivity_of(docs.get(NEW_ID)["metadata"]) == "restricted"
        served, _ = _pack(registry, "internal")
        assert _of_document(served, NEW_ID) == set()

    @pytest.mark.parametrize(
        "supplied",
        [
            {"sensitivity": "confidential"},
            {"sensitivity": "internal"},
            {"sensitivity": "public"},
        ],
        ids=["higher", "lower", "lowest"],
    )
    def test_a_label_the_handler_supplies_this_run_still_wins(
        self, tmp_path: Path, supplied: dict[str, Any]
    ) -> None:
        registry = _registry(tmp_path)
        prefix = f"corpus:{SOURCE}:"
        text = "Notes about zebrafish genetics.\n"

        def run(doc_id: str, metadata: dict[str, Any]) -> None:
            sync_records(
                registry,
                [
                    SyncRecord(
                        doc_id=doc_id,
                        source_key=doc_id.removeprefix(prefix),
                        content=text,
                        handler_metadata=metadata,
                    )
                ],
                source_system=SOURCE,
                id_prefix=prefix,
                root_label=SOURCE,
                requested_by="test",
            )

        run(OLD_ID, {})
        _label(registry, OLD_ID, "restricted")
        run(NEW_ID, {DATA_CLASSIFICATION_KEY: supplied})

        assert _stored_label(registry, NEW_ID) == supplied
        assert registry.knowledge.document_store.get(OLD_ID) is None

    @pytest.mark.parametrize(
        "supplied",
        [None, {"sensitivity": "confidential"}, {"sensitivity": "internal"}],
        ids=["none", "higher", "lower"],
    )
    def test_a_move_resolves_the_label_as_an_update_does(
        self, tmp_path: Path, supplied: dict[str, Any] | None
    ) -> None:
        """The same stored label and the same handler label, once per action."""
        prefix = f"corpus:{SOURCE}:"
        handler_metadata = (
            {} if supplied is None else {DATA_CLASSIFICATION_KEY: supplied}
        )

        def resolved(action: str) -> Any:
            stores = tmp_path / action
            stores.mkdir()
            registry = StoreRegistry(stores_dir=stores)

            def run(doc_id: str, text: str, metadata: dict[str, Any]) -> str:
                report = sync_records(
                    registry,
                    [
                        SyncRecord(
                            doc_id=doc_id,
                            source_key=doc_id.removeprefix(prefix),
                            content=text,
                            handler_metadata=metadata,
                        )
                    ],
                    source_system=SOURCE,
                    id_prefix=prefix,
                    root_label=SOURCE,
                    requested_by="test",
                )
                return report.files[0].action

            run(OLD_ID, "First draft about zebrafish.\n", {})
            _label(registry, OLD_ID, "restricted")
            if action == "update":
                assert run(OLD_ID, "Second draft about zebrafish.\n", handler_metadata)
                return _stored_label(registry, OLD_ID)
            assert (
                run(NEW_ID, "First draft about zebrafish.\n", handler_metadata)
                == "move"
            )
            return _stored_label(registry, NEW_ID)

        assert resolved("move") == resolved("update")
