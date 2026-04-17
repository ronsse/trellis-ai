"""Vector Store — backward-compatible re-exports."""

from trellis.stores.base.vector import VectorStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

__all__ = ["VectorStore", "SQLiteVectorStore"]
