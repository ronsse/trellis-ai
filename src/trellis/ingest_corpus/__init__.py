"""Corpus ingestion — importers into Trellis.

Walks a directory of source files (a notes vault, a folder of
transcripts), parses each through a pure format-handler registry, chunks
long documents into embeddable chunk documents, and syncs the result
into the :class:`~trellis.stores.base.document.DocumentStore`
idempotently: unchanged files are skipped, edited files are re-put and
re-embedded, moved files are re-keyed, and vanished files are pruned on
request. See ``docs/design/adr-corpus-ingestion.md``.
"""

from trellis.ingest_corpus.chunker import chunk_spans
from trellis.ingest_corpus.conversations import (
    conversation_doc_id,
    read_claude_export,
    sync_conversations,
)
from trellis.ingest_corpus.handlers import handler_for, supported_extensions
from trellis.ingest_corpus.models import (
    ChunkSpan,
    CorpusSyncReport,
    FileOutcome,
    SyncRecord,
    corpus_doc_id,
)
from trellis.ingest_corpus.sync import sync_corpus, sync_records
from trellis.ingest_corpus.walker import walk_corpus

__all__ = [
    "ChunkSpan",
    "CorpusSyncReport",
    "FileOutcome",
    "SyncRecord",
    "chunk_spans",
    "conversation_doc_id",
    "corpus_doc_id",
    "handler_for",
    "read_claude_export",
    "supported_extensions",
    "sync_conversations",
    "sync_corpus",
    "sync_records",
    "walk_corpus",
]
