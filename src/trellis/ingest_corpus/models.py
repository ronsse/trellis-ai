"""Dataclasses and identity helpers for corpus ingestion."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

#: Separator between a parent document id and its chunk suffix. Chunk doc
#: ids are ``f"{parent_doc_id}{CHUNK_ID_SEPARATOR}{index}"``.
#:
#: This marker is the repo's chunk discriminator. **Who filters on it is a
#: rule, not a roster**, and the rule is:
#:
#:     A surface that hands back *whole document rows* excludes chunks by
#:     default. A surface that feeds the pack budget keeps them, because
#:     there the chunk is the retrievable unit and the excerpt is what the
#:     budget prices.
#:
#: Stated as a rule because the roster form has now been wrong twice in
#: three changes. It first said "the explore/documents views filter chunk
#: rows by this marker", which was false — ``GET /api/v1/documents``
#: applied no filter at all and served 740 of 1,317 rows as fragments
#: (#385). The enumeration that replaced it listed only the
#: ``list_documents`` callers, so it omitted every
#: :meth:`~trellis.stores.base.document.DocumentStore.search` caller —
#: including a second unfiltered REST surface, ``GET /api/v1/search``
#: (#396). A list of call sites is a snapshot of one commit; both times it
#: was already stale in the commit that wrote it.
#:
#: ``tests/unit/test_chunk_visibility_rule.py`` holds the rule up, and it is
#: worth being exact about how much it can hold. The pack half is enforced
#: directly: :class:`~trellis.retrieve.strategies.KeywordSearch` must keep
#: returning chunk rows, so a future "cleanup" cannot quietly cost the pack
#: its retrievable unit. The whole-row half is enforced *one step short* of
#: the rule — every ``search`` / ``list_documents`` call the scan detects in
#: :mod:`trellis_api.routes` and :mod:`trellis_cli` must **name**
#: ``include_chunks``, not necessarily set it to ``False``, because a walker
#: living in those packages that needs chunk rows is a correct caller. What
#: it buys is that a new whole-row surface cannot inherit a default nobody
#: chose, which is how both #385 and #396 happened. "The scan detects" is
#: also load-bearing: it matches on receiver name, so a document store
#: reached through a receiver not named ``*store*`` slips it.
#:
#: The mechanism is ``include_chunks`` on ``list_documents`` / ``search`` /
#: ``count``, always pushed into SQL. ``list_documents`` and ``count`` take
#: the whole standalone ``WHERE`` from
#: :func:`~trellis.stores.base.document.chunk_exclusion_clause`; ``search``
#: already has a ``WHERE`` (the full-text match), so it appends
#: ``doc_id NOT LIKE`` to its own condition list with
#: :func:`~trellis.stores.base.document.chunk_id_like_pattern` — the same
#: predicate, spelled for a different clause structure. Never a filter over
#: the returned page: ``LIMIT`` is applied after the predicate,
#: so pushing down refills the page with real documents whereas a post-hoc
#: filter yields a short one the caller cannot tell from the end of the
#: data. It defaults to ``True`` at the store, so a caller that does not
#: name it keeps seeing every row — which is what the reindex, resync,
#: retention and classify walkers need, chunks being what carry the
#: embeddings.
#:
#: Code that reads the marker directly rather than through the store flag
#: uses :func:`is_chunk_doc_id` (or, to roll a chunk id up to its parent
#: instead of dropping it, splits on the constant). ``grep`` finds those;
#: this comment deliberately no longer tries to.
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
    #: Entities created by the optional ``--extract`` pass this run.
    entities_extracted: int = 0
    #: Edges created by the optional ``--extract`` pass this run.
    edges_extracted: int = 0

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
            "entities_extracted": self.entities_extracted,
            "edges_extracted": self.edges_extracted,
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
