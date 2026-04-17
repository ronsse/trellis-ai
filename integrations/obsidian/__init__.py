"""Obsidian vault integration for Trellis."""

from integrations.obsidian.indexer import IndexResult, IndexSummary, VaultIndexer
from integrations.obsidian.vault import ObsidianNote, ObsidianVault

__all__ = [
    "IndexResult",
    "IndexSummary",
    "ObsidianNote",
    "ObsidianVault",
    "VaultIndexer",
]
