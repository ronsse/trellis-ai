"""Document Store — backward-compatible re-exports."""

from trellis.stores.base.document import DocumentStore
from trellis.stores.sqlite.document import SQLiteDocumentStore

__all__ = ["DocumentStore", "SQLiteDocumentStore"]
