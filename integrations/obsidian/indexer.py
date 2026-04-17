"""Vault indexer — indexes Obsidian notes into Trellis stores."""

from __future__ import annotations

import hashlib

import structlog
from pydantic import Field

from integrations.obsidian.vault import ObsidianVault
from trellis.core.base import TrellisModel
from trellis.stores.document import DocumentStore
from trellis.stores.graph import GraphStore

logger = structlog.get_logger(__name__)


class IndexResult(TrellisModel):
    """Result of indexing a single note."""

    note_id: str
    path: str
    action: str  # "created", "updated", "unchanged", "error"
    error: str | None = None


class IndexSummary(TrellisModel):
    """Summary of a vault indexing run."""

    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    results: list[IndexResult] = Field(default_factory=list)


class VaultIndexer:
    """Indexes Obsidian vault notes into trellis stores.

    Pipeline per note:
    1. Read note from vault
    2. Compute content hash for change detection
    3. Store content in document store
    4. Create/update graph node
    """

    def __init__(
        self,
        vault: ObsidianVault,
        document_store: DocumentStore | None = None,
        graph_store: GraphStore | None = None,
    ) -> None:
        self._vault = vault
        self._doc_store = document_store
        self._graph_store = graph_store
        self._content_hashes: dict[str, str] = {}

    def index_note(self, path: str, force: bool = False) -> IndexResult:
        """Index a single note."""
        note = self._vault.read_note(path)
        if note is None:
            return IndexResult(
                note_id="",
                path=path,
                action="error",
                error="Note not found",
            )

        content_hash = _compute_hash(note.content)
        cached = self._content_hashes.get(path)

        if not force and cached == content_hash:
            return IndexResult(
                note_id=self._get_note_id(path),
                path=path,
                action="unchanged",
            )

        note_id = self._get_or_create_id(path)
        is_new = cached is None

        # Store in document store
        if self._doc_store is not None:
            self._doc_store.put(
                doc_id=f"obsidian:{note_id}",
                content=note.content,
                metadata={
                    "title": note.title,
                    "path": note.path,
                    "tags": note.tags,
                    "source": "obsidian",
                    "content_hash": content_hash,
                },
            )

        # Create graph node
        if self._graph_store is not None:
            self._graph_store.upsert_node(
                node_id=f"obsidian:{note_id}",
                node_type="obsidian_note",
                properties={
                    "title": note.title,
                    "path": note.path,
                    "tags": note.tags,
                    "source": "obsidian",
                    "content_hash": content_hash,
                    "modified_at": (
                        note.modified.isoformat() if note.modified else None
                    ),
                },
            )

            # Wire wiki-links as edges
            for link in note.links:
                self._graph_store.upsert_edge(
                    source_id=f"obsidian:{note_id}",
                    target_id=f"obsidian_link:{link}",
                    edge_type="wiki_link",
                    properties={"link_text": link},
                )

        self._content_hashes[path] = content_hash
        action = "created" if is_new else "updated"

        logger.info(
            "note_indexed",
            note_id=note_id,
            path=path,
            action=action,
        )

        return IndexResult(note_id=note_id, path=path, action=action)

    def index_vault(
        self,
        folder: str | None = None,
        force: bool = False,
    ) -> IndexSummary:
        """Index all notes in the vault (or a subfolder)."""
        summary = IndexSummary()
        paths = self._vault.list_notes(folder=folder)

        for path in paths:
            result = self.index_note(path, force=force)
            summary.results.append(result)
            summary.total += 1

            if result.action == "created":
                summary.created += 1
            elif result.action == "updated":
                summary.updated += 1
            elif result.action == "unchanged":
                summary.unchanged += 1
            elif result.action == "error":
                summary.errors += 1

        return summary

    def _get_note_id(self, path: str) -> str:
        """Get existing note ID or empty string."""
        if self._doc_store is None:
            return ""
        # Use a deterministic ID from path
        return _path_to_id(path)

    def _get_or_create_id(self, path: str) -> str:
        """Get or create a stable note ID."""
        return _path_to_id(path)


def _compute_hash(content: str) -> str:
    """SHA256 hash of content (first 16 hex chars)."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _path_to_id(path: str) -> str:
    """Generate a deterministic ID from a vault path."""
    return hashlib.sha256(path.encode()).hexdigest()[:12]
