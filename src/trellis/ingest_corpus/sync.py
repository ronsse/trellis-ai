"""Idempotent corpus sync — the shared ingest routine.

The CLI (and any future REST bulk route) calls :func:`sync_corpus`; all
behaviour lives here so every entry point is identical (ADR §2).

Write semantics (ADR §4):

* **Unchanged file** (stored ``content_hash`` equals the file text's
  hash) → no re-put, no re-embed. A second run over an unchanged tree
  performs zero Knowledge-Plane writes.
* **Edited file** → re-put under the same ``doc_id``, re-chunk,
  re-embed *changed* chunks only, delete orphaned chunk docs beyond the
  new chunk count. Stored metadata is merged under the newly computed
  metadata so keys added by enrichment survive the re-put.
* **Moved file** (new relpath, known content hash under this corpus
  prefix) → re-keyed: stored under the new ``doc_id``, the old document
  tree deleted. Reported as a move, not a new ingest.
* **Vanished file** → deleted only with ``prune=True``.
* **Near-duplicates** across files are *warned about*, never skipped —
  unlike ``save_memory``, two legitimately similar notes are common in
  a vault.

Every new/changed file-level document emits ``MEMORY_STORED`` (the same
event the MCP ``save_memory`` path emits) so downstream consumers see
one signal regardless of entry point. Chunk documents are derivatives
of their parent and do not emit their own events. The run itself emits
``CORPUS_SYNCED`` with the run counts — on dry runs too, flagged
``dry_run=True`` (the ``BLOB_GC_SWEPT`` convention). Unlike
``save_memory``, event-emission failure does not abort a bulk sync; it
is reported as a run warning instead.

Embedding rides the existing flag-gated, fail-soft
:func:`~trellis.retrieve.embed_ingest_hook.run_embed_on_ingest` hook:
chunked documents embed their chunks (each under the embedder input cap
by construction), unchunked documents embed the parent row itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.hashing import content_hash
from trellis.ingest_corpus.chunker import chunk_spans
from trellis.ingest_corpus.handlers import handler_for, supported_extensions
from trellis.ingest_corpus.models import (
    CorpusSyncReport,
    FileOutcome,
    chunk_doc_id,
    corpus_doc_id,
    corpus_id_prefix,
    is_chunk_doc_id,
)
from trellis.ingest_corpus.walker import walk_corpus
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from pathlib import Path

    from trellis.ingest_corpus.models import ChunkSpan
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

_PRUNE_PAGE_SIZE = 500


def sync_corpus(
    registry: StoreRegistry,
    root: Path,
    *,
    source_system: str = "corpus",
    extra_metadata: dict[str, Any] | None = None,
    include: tuple[str, ...] = (),
    dry_run: bool = False,
    prune: bool = False,
    requested_by: str = "cli:ingest-corpus",
) -> CorpusSyncReport:
    """Sync the file tree at *root* into the document store.

    Args:
        registry: Active store registry; uses the Knowledge-Plane
            document store (plus vector store via the embed hook) and
            the Operational-Plane event log.
        root: Directory to walk, or a single file.
        source_system: Corpus namespace — part of every ``doc_id`` and
            stored as ``metadata.source_system`` (the classification
            layer keys on it, e.g. ``"obsidian"``).
        extra_metadata: Operator tags (``--domain``/``--tag``) merged
            into every document written this run. Applies to new and
            updated documents only; unchanged files are not re-tagged.
        include: Optional glob filter over relative paths.
        dry_run: Compute and report the full plan without writing.
        prune: Delete documents whose source file vanished.
        requested_by: Audit identifier for events and embed logging.

    Returns:
        A :class:`CorpusSyncReport` with per-file outcomes, prune list,
        and warnings.
    """
    root = root.resolve()
    doc_store = registry.knowledge.document_store
    report = CorpusSyncReport(
        root=str(root),
        source_system=source_system,
        dry_run=dry_run,
        prune=prune,
    )
    supported, report.unsupported = walk_corpus(
        root, include=tuple(include), extensions=supported_extensions()
    )

    current_ids = {corpus_doc_id(source_system, rel) for rel, _ in supported}
    minhash = MinHashIndex()
    moved_from_ids: set[str] = set()

    for relpath, path in supported:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.warnings.append(
                {"kind": "unreadable_file", "path": relpath, "detail": str(exc)}
            )
            continue

        handler = handler_for(relpath)
        if handler is None:  # pragma: no cover - walker only yields supported
            continue
        handler_metadata, handler_warnings = handler.parse(relpath, text)
        report.warnings.extend(handler_warnings)

        outcome = _plan_file(
            doc_store,
            relpath=relpath,
            text=text,
            source_system=source_system,
            current_ids=current_ids,
        )
        for match_id, similarity in minhash.query(text):
            report.warnings.append(
                {
                    "kind": "near_duplicate",
                    "path": relpath,
                    "match_doc_id": match_id,
                    "similarity": round(similarity, 3),
                }
            )
        minhash.add(outcome.doc_id, text)

        spans = chunk_spans(text)
        outcome.chunk_count = len(spans)
        if outcome.moved_from is not None:
            moved_from_ids.add(outcome.moved_from)

        if not dry_run and outcome.action != "skip":
            _apply_file(
                registry,
                report,
                outcome=outcome,
                text=text,
                handler_metadata=handler_metadata,
                extra_metadata=extra_metadata or {},
                spans=spans,
                source_system=source_system,
                requested_by=requested_by,
            )
        report.files.append(outcome)

    if prune:
        _prune_vanished(
            registry,
            report,
            keep_ids=current_ids | moved_from_ids,
            dry_run=dry_run,
        )

    _emit_summary(registry, report, requested_by=requested_by)
    return report


def _plan_file(
    doc_store: DocumentStore,
    *,
    relpath: str,
    text: str,
    source_system: str,
    current_ids: set[str],
) -> FileOutcome:
    """Decide new / update / skip / move for one walked file."""
    doc_id = corpus_doc_id(source_system, relpath)
    chash = content_hash(text)

    existing = doc_store.get(doc_id)
    if existing is not None:
        if existing.get("content_hash") == chash:
            return FileOutcome(relpath=relpath, doc_id=doc_id, action="skip")
        return FileOutcome(relpath=relpath, doc_id=doc_id, action="update")

    # New id — same content under another id of this corpus whose file
    # is gone from the tree means the file moved: re-key, don't duplicate.
    hit = doc_store.get_by_hash(chash)
    if (
        hit is not None
        and hit["doc_id"].startswith(corpus_id_prefix(source_system))
        and not is_chunk_doc_id(hit["doc_id"])
        and hit["doc_id"] not in current_ids
    ):
        return FileOutcome(
            relpath=relpath, doc_id=doc_id, action="move", moved_from=hit["doc_id"]
        )
    return FileOutcome(relpath=relpath, doc_id=doc_id, action="new")


def _apply_file(
    registry: StoreRegistry,
    report: CorpusSyncReport,
    *,
    outcome: FileOutcome,
    text: str,
    handler_metadata: dict[str, Any],
    extra_metadata: dict[str, Any],
    spans: list[ChunkSpan],
    source_system: str,
    requested_by: str,
) -> None:
    """Execute the writes for one new / updated / moved file."""
    doc_store = registry.knowledge.document_store
    vector_store = getattr(registry.knowledge, "vector_store", None)

    metadata: dict[str, Any] = {
        **extra_metadata,
        **handler_metadata,
        "source_system": source_system,
        "source_path": outcome.relpath,
    }
    if spans:
        metadata["chunk_count"] = len(spans)

    old_chunk_count = 0
    if outcome.action == "update":
        stored = doc_store.get(outcome.doc_id)
        old_chunk_count = _chunk_count_of(stored)
        # Merge under the fresh metadata so keys this run does not own
        # (enrichment tags, noise flags) survive the re-put.
        metadata = {**((stored or {}).get("metadata") or {}), **metadata}
        if not spans:
            metadata.pop("chunk_count", None)

    doc_store.put(outcome.doc_id, text, metadata=metadata)
    outcome.chunks_written = _write_chunks(
        registry,
        parent_doc_id=outcome.doc_id,
        text=text,
        spans=spans,
        relpath=outcome.relpath,
        source_system=source_system,
        extra_metadata=extra_metadata,
        requested_by=requested_by,
    )
    for index in range(len(spans), old_chunk_count):
        _delete_doc_and_vector(
            doc_store, vector_store, chunk_doc_id(outcome.doc_id, index)
        )
    if not spans:
        run_embed_on_ingest(
            registry, outcome.doc_id, text, metadata, source=requested_by
        )

    if outcome.action == "move" and outcome.moved_from is not None:
        old = doc_store.get(outcome.moved_from)
        _delete_document_tree(
            doc_store, vector_store, outcome.moved_from, _chunk_count_of(old)
        )

    _emit_memory_stored(
        registry,
        report,
        outcome=outcome,
        text=text,
        metadata=metadata,
        requested_by=requested_by,
    )


def _write_chunks(
    registry: StoreRegistry,
    *,
    parent_doc_id: str,
    text: str,
    spans: list[ChunkSpan],
    relpath: str,
    source_system: str,
    extra_metadata: dict[str, Any],
    requested_by: str,
) -> int:
    """Write (and embed) chunk documents; skip byte-identical chunks.

    Returns the number of chunk docs actually written. Operator tags
    propagate to chunks — chunks are the retrievable unit, so retrieval
    tag filters must see them; handler metadata stays on the parent.
    """
    doc_store = registry.knowledge.document_store
    written = 0
    for span in spans:
        cid = chunk_doc_id(parent_doc_id, span.index)
        chunk_content = text[span.start : span.end]
        existing = doc_store.get(cid)
        content_changed = existing is None or existing.get(
            "content_hash"
        ) != content_hash(chunk_content)
        count_stale = existing is not None and (
            (existing.get("metadata") or {}).get("chunk_count") != len(spans)
        )
        if not content_changed and not count_stale:
            continue
        metadata: dict[str, Any] = {
            **extra_metadata,
            "source_system": source_system,
            "source_path": relpath,
            "parent_doc_id": parent_doc_id,
            "chunk_index": span.index,
            "chunk_count": len(spans),
            "char_span": [span.start, span.end],
        }
        doc_store.put(cid, chunk_content, metadata=metadata)
        if content_changed:
            # Metadata-only refreshes deliberately don't re-embed —
            # same convention as the document ingest paths.
            run_embed_on_ingest(
                registry, cid, chunk_content, metadata, source=requested_by
            )
        written += 1
    return written


def _prune_vanished(
    registry: StoreRegistry,
    report: CorpusSyncReport,
    *,
    keep_ids: set[str],
    dry_run: bool,
) -> None:
    """Delete corpus documents whose source file is gone from the tree."""
    doc_store = registry.knowledge.document_store
    vector_store = getattr(registry.knowledge, "vector_store", None)
    prefix = corpus_id_prefix(report.source_system)

    candidates: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = doc_store.list_documents(limit=_PRUNE_PAGE_SIZE, offset=offset)
        if not page:
            break
        offset += len(page)
        for doc in page:
            doc_id = doc["doc_id"]
            if (
                not doc_id.startswith(prefix)
                or is_chunk_doc_id(doc_id)
                or doc_id in keep_ids
            ):
                continue
            metadata = doc.get("metadata") or {}
            candidates.append(
                {
                    "doc_id": doc_id,
                    "source_path": metadata.get("source_path"),
                    "chunk_count": _chunk_count_of(doc),
                }
            )
        if len(page) < _PRUNE_PAGE_SIZE:
            break

    for candidate in sorted(candidates, key=lambda c: str(c["doc_id"])):
        if not dry_run:
            _delete_document_tree(
                doc_store,
                vector_store,
                candidate["doc_id"],
                candidate["chunk_count"],
            )
        report.pruned.append(
            {"doc_id": candidate["doc_id"], "source_path": candidate["source_path"]}
        )


def _delete_document_tree(
    doc_store: DocumentStore,
    vector_store: Any,
    doc_id: str,
    chunk_count: int,
) -> None:
    """Delete a parent document, its chunk docs, and their vector rows."""
    for index in range(chunk_count):
        _delete_doc_and_vector(doc_store, vector_store, chunk_doc_id(doc_id, index))
    _delete_doc_and_vector(doc_store, vector_store, doc_id)


def _delete_doc_and_vector(
    doc_store: DocumentStore,
    vector_store: Any,
    doc_id: str,
) -> None:
    doc_store.delete(doc_id)
    if vector_store is None:
        return
    try:
        vector_store.delete(doc_id)
    except Exception:
        # GRACEFUL-DEGRADATION: a stale vector row degrades retrieval
        # quality; a failed vector backend must not abort the sync.
        logger.exception("corpus_vector_delete_failed", doc_id=doc_id)


def _chunk_count_of(doc: dict[str, Any] | None) -> int:
    if not doc:
        return 0
    value = (doc.get("metadata") or {}).get("chunk_count", 0)
    return value if isinstance(value, int) else 0


def _emit_memory_stored(
    registry: StoreRegistry,
    report: CorpusSyncReport,
    *,
    outcome: FileOutcome,
    text: str,
    metadata: dict[str, Any],
    requested_by: str,
) -> None:
    payload: dict[str, Any] = {
        "doc_id": outcome.doc_id,
        "content_hash": content_hash(text),
        "content_length": len(text),
        "metadata": metadata,
        "action": outcome.action,
        "chunk_count": outcome.chunk_count,
    }
    if outcome.moved_from is not None:
        payload["moved_from"] = outcome.moved_from
    try:
        registry.operational.event_log.emit(
            EventType.MEMORY_STORED,
            source=requested_by,
            entity_id=outcome.doc_id,
            entity_type="document",
            payload=payload,
        )
    except Exception as exc:
        logger.exception("corpus_memory_stored_emit_failed", doc_id=outcome.doc_id)
        report.warnings.append(
            {
                "kind": "event_emit_failed",
                "doc_id": outcome.doc_id,
                "detail": str(exc),
            }
        )


def _emit_summary(
    registry: StoreRegistry,
    report: CorpusSyncReport,
    *,
    requested_by: str,
) -> None:
    """Emit ``CORPUS_SYNCED`` with the run counts (dry runs included)."""
    try:
        registry.operational.event_log.emit(
            EventType.CORPUS_SYNCED,
            source=requested_by,
            entity_id=f"corpus:{report.source_system}",
            entity_type="corpus",
            payload={
                "root": report.root,
                "source_system": report.source_system,
                "dry_run": report.dry_run,
                "prune": report.prune,
                **report.counts(),
            },
        )
    except Exception as exc:
        logger.exception("corpus_synced_emit_failed", root=report.root)
        report.warnings.append(
            {"kind": "event_emit_failed", "doc_id": None, "detail": str(exc)}
        )
