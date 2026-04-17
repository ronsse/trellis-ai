"""Tests for VaultIndexer."""

from __future__ import annotations

from pathlib import Path

import pytest
from integrations.obsidian.indexer import VaultIndexer, _compute_hash, _path_to_id
from integrations.obsidian.vault import ObsidianVault

from trellis.stores.document import SQLiteDocumentStore
from trellis.stores.graph import SQLiteGraphStore


@pytest.fixture
def vault(tmp_path: Path) -> ObsidianVault:
    """Create a vault with sample notes."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    (vault_dir / "note-a.md").write_text(
        "# Note A\n\nContent of note A with [[Note B]] link.\n",
        encoding="utf-8",
    )
    (vault_dir / "note-b.md").write_text(
        "# Note B\n\nContent of note B.\n",
        encoding="utf-8",
    )

    sub = vault_dir / "folder"
    sub.mkdir()
    (sub / "note-c.md").write_text(
        "# Note C\n\nContent in subfolder.\n",
        encoding="utf-8",
    )

    return ObsidianVault(vault_dir)


@pytest.fixture
def doc_store(tmp_path: Path) -> SQLiteDocumentStore:
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


@pytest.fixture
def graph_store(tmp_path: Path) -> SQLiteGraphStore:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


class TestIndexNote:
    def test_creates_doc_and_graph_entries(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        result = indexer.index_note("note-a.md")

        assert result.action == "created"
        assert result.note_id != ""

        # Document was stored
        doc = doc_store.get(f"obsidian:{result.note_id}")
        assert doc is not None
        assert "Content of note A" in doc["content"]
        assert doc["metadata"]["source"] == "obsidian"
        assert doc["metadata"]["title"] == "Note A"

        # Graph node was created
        node = graph_store.get_node(f"obsidian:{result.note_id}")
        assert node is not None
        assert node["node_type"] == "obsidian_note"
        assert node["properties"]["title"] == "Note A"

    def test_unchanged_on_reindex(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        result1 = indexer.index_note("note-b.md")
        assert result1.action == "created"

        result2 = indexer.index_note("note-b.md")
        assert result2.action == "unchanged"

    def test_updates_on_content_change(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        result1 = indexer.index_note("note-b.md")
        assert result1.action == "created"

        # Modify the note
        (vault.vault_path / "note-b.md").write_text(
            "# Note B\n\nUpdated content.\n",
            encoding="utf-8",
        )

        result2 = indexer.index_note("note-b.md")
        assert result2.action == "updated"

        # Document content updated
        doc = doc_store.get(f"obsidian:{result2.note_id}")
        assert doc is not None
        assert "Updated content" in doc["content"]

    def test_force_reindex(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        result1 = indexer.index_note("note-b.md")
        assert result1.action == "created"

        result2 = indexer.index_note("note-b.md", force=True)
        assert result2.action == "updated"

    def test_missing_note_returns_error(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store)
        result = indexer.index_note("nonexistent.md")
        assert result.action == "error"
        assert result.error == "Note not found"
        assert result.note_id == ""

    def test_wiki_link_edges(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        result = indexer.index_note("note-a.md")

        # Note A links to [[Note B]], so there should be a wiki_link edge
        edges = graph_store.get_edges(
            f"obsidian:{result.note_id}",
            direction="outgoing",
            edge_type="wiki_link",
        )
        assert len(edges) == 1
        assert edges[0]["target_id"] == "obsidian_link:Note B"
        assert edges[0]["properties"]["link_text"] == "Note B"


class TestIndexVault:
    def test_indexes_all_notes(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        summary = indexer.index_vault()

        assert summary.total == 3
        assert summary.created == 3
        assert summary.errors == 0
        assert len(summary.results) == 3

        # Verify all docs stored
        assert doc_store.count() == 3
        assert graph_store.count_nodes() >= 3

    def test_folder_scoping(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        summary = indexer.index_vault(folder="folder")

        assert summary.total == 1
        assert summary.created == 1
        assert summary.results[0].path == "folder/note-c.md"

    def test_reindex_shows_unchanged(
        self,
        vault: ObsidianVault,
        doc_store: SQLiteDocumentStore,
        graph_store: SQLiteGraphStore,
    ) -> None:
        indexer = VaultIndexer(vault, doc_store, graph_store)
        indexer.index_vault()

        summary2 = indexer.index_vault()
        assert summary2.total == 3
        assert summary2.unchanged == 3
        assert summary2.created == 0


class TestHelpers:
    def test_compute_hash_deterministic(self) -> None:
        assert _compute_hash("hello") == _compute_hash("hello")
        assert _compute_hash("hello") != _compute_hash("world")
        assert len(_compute_hash("test")) == 16

    def test_path_to_id_deterministic(self) -> None:
        assert _path_to_id("a/b.md") == _path_to_id("a/b.md")
        assert _path_to_id("a.md") != _path_to_id("b.md")
        assert len(_path_to_id("test.md")) == 12
