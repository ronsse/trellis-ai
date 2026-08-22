"""Tests for file-scoped context retrieval (#307, server-side half)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.file_context import (
    build_file_context,
    source_path_matches,
)
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path


class TestSourcePathMatches:
    def test_exact_match(self) -> None:
        assert source_path_matches("notes/foo.md", "notes/foo.md")

    def test_absolute_query_matches_stored_relpath(self) -> None:
        assert source_path_matches("notes/foo.md", "/home/n/vault/notes/foo.md")

    def test_relpath_query_matches_stored_absolute(self) -> None:
        assert source_path_matches("/home/n/vault/notes/foo.md", "notes/foo.md")

    def test_suffix_must_sit_on_path_boundary(self) -> None:
        # "otes/foo.md" is a string suffix of "notes/foo.md" but not a
        # path suffix — matching it would cross a filename.
        assert not source_path_matches("notes/foo.md", "otes/foo.md")
        assert not source_path_matches("my-notes/foo.md", "notes/foo.md")

    def test_different_file_no_match(self) -> None:
        assert not source_path_matches("notes/foo.md", "notes/bar.md")

    def test_non_string_or_empty_stored_never_matches(self) -> None:
        assert not source_path_matches(None, "notes/foo.md")
        assert not source_path_matches(42, "notes/foo.md")
        assert not source_path_matches("", "notes/foo.md")

    def test_empty_query_never_matches(self) -> None:
        assert not source_path_matches("notes/foo.md", "")


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True)
    return StoreRegistry(stores_dir=stores_dir)


def _build(registry: StoreRegistry, paths: list[str], **kwargs: object) -> dict:
    return build_file_context(
        registry.knowledge.document_store,
        registry.knowledge.graph_store,
        paths,
        **kwargs,  # type: ignore[arg-type]
    )


class TestBuildFileContext:
    def test_no_matches_returns_empty_entry_per_path(
        self, registry: StoreRegistry
    ) -> None:
        result = _build(registry, ["notes/foo.md"])
        assert result == {
            "paths": [
                {
                    "path": "notes/foo.md",
                    "documents": [],
                    "entities": [],
                    "newest_item_at": None,
                }
            ]
        }

    def test_document_matched_by_source_path(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "corpus:vault:abc",
            "Gotcha: the API times out on cold start.",
            metadata={"source_path": "notes/foo.md", "source_system": "vault"},
        )
        result = _build(registry, ["/home/n/vault/notes/foo.md"])
        (entry,) = result["paths"]
        (doc,) = entry["documents"]
        assert doc["doc_id"] == "corpus:vault:abc"
        assert doc["source_path"] == "notes/foo.md"
        assert doc["source_system"] == "vault"
        assert "cold start" in doc["excerpt"]
        assert doc["created_at"] is not None
        assert entry["newest_item_at"] is not None

    def test_unrelated_document_not_matched(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "d1", "content", metadata={"source_path": "notes/bar.md"}
        )
        result = _build(registry, ["notes/foo.md"])
        assert result["paths"][0]["documents"] == []

    def test_chunk_documents_anchor_entities_but_are_not_listed(
        self, registry: StoreRegistry
    ) -> None:
        parent_id = "corpus:vault:abc"
        chunk_id = chunk_doc_id(parent_id, 0)
        registry.knowledge.document_store.put(
            parent_id, "parent content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.document_store.put(
            chunk_id,
            "chunk content",
            metadata={"source_path": "notes/foo.md", "parent_doc_id": parent_id},
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-1",
            "concept",
            {"name": "Cold Start", "description": "API cold-start latency"},
            document_ids=[chunk_id],
        )
        result = _build(registry, ["notes/foo.md"])
        (entry,) = result["paths"]
        assert [d["doc_id"] for d in entry["documents"]] == [parent_id]
        (entity,) = entry["entities"]
        assert entity["entity_id"] == "ent-1"
        assert entity["name"] == "Cold Start"
        assert entity["document_ids"] == [chunk_id]

    def test_entity_linked_via_document_ids(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-1",
            "Product",
            {"name": "Oura Ring", "description": "A sleep tracker option"},
            document_ids=["doc-1"],
        )
        result = _build(registry, ["notes/foo.md"])
        (entity,) = result["paths"][0]["entities"]
        assert entity["entity_type"] == "Product"
        assert entity["node_role"] == "semantic"
        assert entity["description"] == "A sleep tracker option"
        assert entity["updated_at"] is not None

    def test_entity_without_matching_doc_link_excluded(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-other", "concept", {"name": "Other"}, document_ids=["doc-other"]
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-unlinked", "concept", {"name": "Unlinked"}
        )
        result = _build(registry, ["notes/foo.md"])
        assert result["paths"][0]["entities"] == []

    def test_unconfirmed_mint_gated_by_default(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-unconfirmed",
            "Product",
            {"name": "Whoop", "extraction_status": "unconfirmed"},
            document_ids=["doc-1"],
        )
        assert _build(registry, ["notes/foo.md"])["paths"][0]["entities"] == []

        included = _build(registry, ["notes/foo.md"], include_unconfirmed=True)
        (entity,) = included["paths"][0]["entities"]
        assert entity["entity_id"] == "ent-unconfirmed"
        assert entity["extraction_status"] == "unconfirmed"

    def test_confirmed_mint_served_by_default(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-confirmed",
            "Product",
            {"name": "Oura", "extraction_status": "confirmed"},
            document_ids=["doc-1"],
        )
        (entity,) = _build(registry, ["notes/foo.md"])["paths"][0]["entities"]
        assert entity["extraction_status"] == "confirmed"

    def test_structural_node_gated_by_default(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-structural",
            "file",
            {"name": "a column"},
            node_role="structural",
            document_ids=["doc-1"],
        )
        assert _build(registry, ["notes/foo.md"])["paths"][0]["entities"] == []

        included = _build(registry, ["notes/foo.md"], include_structural=True)
        (entity,) = included["paths"][0]["entities"]
        assert entity["node_role"] == "structural"

    def test_multiple_paths_grouped_independently(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-a", "content a", metadata={"source_path": "notes/a.md"}
        )
        registry.knowledge.document_store.put(
            "doc-b", "content b", metadata={"source_path": "notes/b.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-a", "concept", {"name": "A"}, document_ids=["doc-a"]
        )
        result = _build(registry, ["notes/a.md", "notes/b.md", "notes/none.md"])
        by_path = {entry["path"]: entry for entry in result["paths"]}
        assert [d["doc_id"] for d in by_path["notes/a.md"]["documents"]] == ["doc-a"]
        assert [e["entity_id"] for e in by_path["notes/a.md"]["entities"]] == ["ent-a"]
        assert [d["doc_id"] for d in by_path["notes/b.md"]["documents"]] == ["doc-b"]
        assert by_path["notes/b.md"]["entities"] == []
        assert by_path["notes/none.md"]["documents"] == []

    def test_blank_and_duplicate_paths_collapsed(self, registry: StoreRegistry) -> None:
        result = _build(registry, ["notes/foo.md", "  notes/foo.md  ", "", "   "])
        assert [entry["path"] for entry in result["paths"]] == ["notes/foo.md"]

    def test_newest_item_at_is_the_max_across_docs_and_entities(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-1", "concept", {"name": "A"}, document_ids=["doc-1"]
        )
        (entry,) = _build(registry, ["notes/foo.md"])["paths"]
        stamps = [
            i["updated_at"] or i["created_at"]
            for i in entry["documents"] + entry["entities"]
        ]
        assert entry["newest_item_at"] is not None
        # The reported stamp is the newest of the item stamps (all ISO-UTC,
        # so lexicographic max agrees with chronological max here).
        assert entry["newest_item_at"] >= max(str(s) for s in stamps)
