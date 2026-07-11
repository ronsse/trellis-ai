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
from trellis.ingest_corpus.handlers import handler_for, supported_extensions
from trellis.ingest_corpus.models import (
    ChunkSpan,
    CorpusSyncReport,
    FileOutcome,
    corpus_doc_id,
)
from trellis.ingest_corpus.sync import sync_corpus
from trellis.ingest_corpus.walker import walk_corpus

__all__ = [
    "ChunkSpan",
    "CorpusSyncReport",
    "FileOutcome",
    "chunk_spans",
    "corpus_doc_id",
    "handler_for",
    "supported_extensions",
    "sync_corpus",
    "walk_corpus",
]
