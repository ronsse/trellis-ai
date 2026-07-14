"""Tests for the evidence-document creation seam (pointer-not-prose, doc-first).

Covers issue #260: ``ensure_evidence_document`` is the reusable doc-creation
function that must run *before* a graph write carries its returned id as a
pointer, and it must be idempotent (content-hash dedup) so a retried save
resolves to the same document rather than double-creating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.core.hashing import content_hash
from trellis.mutate import ensure_evidence_document
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


class TestEnsureEvidenceDocument:
    def test_creates_document(self, registry: StoreRegistry) -> None:
        doc_id = ensure_evidence_document(registry, "the evidence prose")
        stored = registry.knowledge.document_store.get(doc_id)
        assert stored is not None
        assert stored["content"] == "the evidence prose"

    def test_is_idempotent_on_same_content(self, registry: StoreRegistry) -> None:
        first = ensure_evidence_document(registry, "identical body")
        second = ensure_evidence_document(registry, "identical body")
        assert first == second
        # Exactly one document persisted despite two calls.
        assert registry.knowledge.document_store.count() == 1

    def test_resolves_to_preexisting_doc_by_hash(
        self, registry: StoreRegistry
    ) -> None:
        content = "pre-existing memory"
        existing_id = registry.knowledge.document_store.put(None, content)
        resolved = ensure_evidence_document(registry, content)
        assert resolved == existing_id
        assert registry.knowledge.document_store.count() == 1

    def test_stores_metadata(self, registry: StoreRegistry) -> None:
        doc_id = ensure_evidence_document(
            registry, "body", metadata={"domain": "platform"}
        )
        stored = registry.knowledge.document_store.get(doc_id)
        assert stored is not None
        assert stored["metadata"]["domain"] == "platform"
        assert stored["content_hash"] == content_hash("body")

    def test_empty_content_raises(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValueError, match="content must not be empty"):
            ensure_evidence_document(registry, "   ")
