"""Dataclasses and identity helpers for corpus ingestion."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

#: Separator between a parent document id and its chunk suffix. Chunk
#: doc ids are ``f"{parent_doc_id}{CHUNK_ID_SEPARATOR}{index}"``; the
#: explore/documents views filter chunk rows by this marker.
CHUNK_ID_SEPARATOR = "#chunk-"

#: Actions the sync plan can assign to a walked file.
FileAction = Literal["new", "update", "skip", "move"]


def corpus_doc_id(source_system: str, relpath: str) -> str:
    """Stable document id for a corpus file.

    ``corpus:<source_system>:<sha1(relpath)>`` — stable across runs and
    independent of content, so edits re-use the same row (ADR §4). The
    human-readable path travels in ``metadata.source_path``.
    """
    digest = hashlib.sha1(  # noqa: S324 - identity, not security
        relpath.encode()
    ).hexdigest()
    return f"corpus:{source_system}:{digest}"


def corpus_id_prefix(source_system: str) -> str:
    """Doc-id prefix shared by every document of one corpus source."""
    return f"corpus:{source_system}:"


def chunk_doc_id(parent_doc_id: str, index: int) -> str:
    """Doc id of chunk *index* of *parent_doc_id*."""
    return f"{parent_doc_id}{CHUNK_ID_SEPARATOR}{index}"


def is_chunk_doc_id(doc_id: str) -> bool:
    """``True`` iff *doc_id* names a chunk document."""
    return CHUNK_ID_SEPARATOR in doc_id


@dataclass
class SyncRecord:
    """One document to sync, as produced by a source reader.

    The record-oriented seam between a *reader* (file walker, conversation
    export parser, future REST bulk route) and the shared idempotent sync
    core (:func:`trellis.ingest_corpus.sync.sync_records`). A reader is
    responsible only for turning its source into these; every write
    decision (new/update/skip/move, chunking, embedding, events) is the
    core's.
    """

    #: Stable document id — the reader owns the id scheme
    #: (``corpus:<sys>:<sha1(relpath)>``, ``conversation:<sys>:<uuid>``…).
    doc_id: str
    #: Human-readable source locator stored as ``metadata.source_path``
    #: and shown in the run report (a relpath, a conversation title…).
    source_key: str
    #: Full document content, stored verbatim on the parent row.
    content: str
    #: Reader-extracted metadata (frontmatter, conversation fields…).
    handler_metadata: dict[str, Any] = field(default_factory=dict)
    #: Non-fatal reader findings surfaced in the run report.
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkSpan:
    """One chunk of a parent document, as a span into its content.

    ``start``/``end`` index the parent's content string; the chunk's
    stored content is exactly ``parent_content[start:end]``, so spans
    are reproducible from the parent row alone.
    """

    index: int
    start: int
    end: int


@dataclass
class FileOutcome:
    """Per-file result (or dry-run plan entry) of a sync run."""

    relpath: str
    doc_id: str
    action: FileAction
    chunk_count: int = 0
    #: Doc id the content previously lived under, for ``action="move"``.
    moved_from: str | None = None
    #: Chunk docs actually (re-)written — differs from ``chunk_count``
    #: when an edit leaves some chunks byte-identical.
    chunks_written: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.relpath,
            "doc_id": self.doc_id,
            "action": self.action,
            "chunks": self.chunk_count,
        }
        if self.moved_from is not None:
            payload["moved_from"] = self.moved_from
        return payload


@dataclass
class CorpusSyncReport:
    """Full report of one ``sync_corpus`` run."""

    root: str
    source_system: str
    dry_run: bool
    prune: bool
    files: list[FileOutcome] = field(default_factory=list)
    #: Files under *root* no handler supports (reported, never ingested).
    unsupported: list[str] = field(default_factory=list)
    #: Parent doc ids deleted (or, on dry runs, that would be deleted)
    #: because their source file vanished. Only populated with ``prune``.
    pruned: list[dict[str, Any]] = field(default_factory=list)
    #: Non-fatal findings: near-duplicate pairs, unreadable files,
    #: malformed frontmatter. Each entry has a ``kind`` key.
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        by_action = {"new": 0, "update": 0, "skip": 0, "move": 0}
        for outcome in self.files:
            by_action[outcome.action] += 1
        return {
            "files_seen": len(self.files),
            "ingested": by_action["new"],
            "updated": by_action["update"],
            "moved": by_action["move"],
            "skipped_unchanged": by_action["skip"],
            "skipped_unsupported": len(self.unsupported),
            "pruned": len(self.pruned),
            "chunks_written": sum(o.chunks_written for o in self.files),
            "warnings": len(self.warnings),
        }

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready shape shared by the CLI and the summary event."""
        return {
            "root": self.root,
            "source_system": self.source_system,
            "dry_run": self.dry_run,
            "prune": self.prune,
            "counts": self.counts(),
            "files": [o.to_payload() for o in self.files],
            "unsupported": list(self.unsupported),
            "pruned": list(self.pruned),
            "warnings": list(self.warnings),
        }
