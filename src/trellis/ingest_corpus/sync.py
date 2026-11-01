"""Idempotent document sync — the shared ingest routine.

Readers (the corpus file walker, the conversation-export parser, any
future REST bulk route) turn their source into :class:`SyncRecord`s and
hand them to :func:`sync_records`; all write behaviour lives there so
every entry point is identical (ADR §2). :func:`sync_corpus` is the
file-walking wrapper.

Write semantics (ADR §4), applied per record:

* **Unchanged** (stored ``content_hash`` equals the new content's hash)
  → no re-put, no re-embed. A second run over an unchanged source
  performs zero Knowledge-Plane writes.
* **Changed** → re-put under the same ``doc_id``, re-chunk, re-embed
  *changed* chunks only, delete orphaned chunk docs beyond the new chunk
  count. Stored metadata is merged under the newly computed metadata so
  keys added by enrichment survive the re-put.
* **Moved** (new id, known content hash under this source's id prefix,
  move detection enabled) → re-keyed: stored under the new ``doc_id``,
  the old document tree deleted. Reported as a move, not a new ingest.
  Move detection is a file-corpus concern (content-derived ids); readers
  with content-independent ids (conversation uuids) disable it.
* **Vanished** → deleted only when prune is on *and* the source is
  verifiably gone. A source the prune check cannot look at is kept and
  reported under ``prune_withheld`` (#633): not having seen a source is
  not evidence that it is gone.
* **Near-duplicates** across records are *warned about*, never skipped —
  unlike ``save_memory``, two legitimately similar notes / conversations
  are common.

Every new/changed document emits ``MEMORY_STORED`` (the same event the
MCP ``save_memory`` path emits) so downstream consumers see one signal
regardless of entry point. Chunk documents are derivatives of their
parent and do not emit their own events. The run emits ``CORPUS_SYNCED``
with the run counts — on dry runs too, flagged ``dry_run=True`` (the
``BLOB_GC_SWEPT`` convention). Unlike ``save_memory``, event-emission
failure does not abort a bulk sync; it is reported as a run warning.

Every document this module writes — parent and chunk — has its metadata run
through :class:`~trellis.schemas.document_metadata.DocumentMetadata` first.
That module owns the contract; this is the only place it is currently
enforced.

Embedding rides the existing flag-gated, fail-soft
:func:`~trellis.retrieve.embed_ingest_hook.run_embed_on_ingest` hook:
chunked documents embed their chunks (each under the embedder input cap
by construction), unchunked documents embed the parent row itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.classify.ingest import (
    CLASSIFY_METADATA_KEYS,
    build_ingest_classifier,
    classify_for_ingest,
    classify_on_ingest_enabled,
)
from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.core.hashing import content_hash
from trellis.core.path_presence import (
    UnknownFileIdentity,
    file_identity,
    path_is_present,
)
from trellis.extract.memory_ingest_hook import (
    build_memory_extractor,
    run_memory_extraction,
)
from trellis.ingest_corpus.chunker import chunk_spans
from trellis.ingest_corpus.handlers import handler_for, supported_extensions
from trellis.ingest_corpus.models import (
    CorpusSyncReport,
    FileOutcome,
    SourceUnverified,
    SyncRecord,
    chunk_doc_id,
    corpus_doc_id,
    corpus_id_prefix,
    is_chunk_doc_id,
)
from trellis.ingest_corpus.walker import walk_corpus
from trellis.llm.egress import may_reach_route
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis.schemas.classification import sensitivity_of
from trellis.schemas.document_metadata import DocumentMetadata
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from pathlib import Path

    from trellis.ingest_corpus.models import ChunkSpan, PruneCheck, SourceState
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
    extract: bool = False,
    requested_by: str = "cli:ingest-corpus",
) -> CorpusSyncReport:
    """Sync the file tree at *root* into the document store.

    Reads each supported file through the format-handler registry and
    delegates every write decision to :func:`sync_records`.

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
        prune: Delete documents whose source file is verifiably gone. A
            document whose file cannot be checked is kept and listed under
            ``prune_withheld`` (see :func:`_corpus_prune_check`).
        extract: Opt into the flag-gated entity/edge extraction pass
            (also requires ``TRELLIS_ENABLE_MEMORY_EXTRACTION``).
        requested_by: Audit identifier for events and embed logging.

    Returns:
        A :class:`CorpusSyncReport` with per-file outcomes, prune list,
        and warnings.
    """
    root = root.resolve()
    walk_warnings: list[dict[str, Any]] = []

    def _unreadable_directory(relpath: str, exc: OSError) -> None:
        walk_warnings.append(
            {"kind": "unreadable_directory", "path": relpath, "detail": str(exc)}
        )

    supported, unsupported = walk_corpus(
        root,
        include=tuple(include),
        extensions=supported_extensions(),
        onerror=_unreadable_directory,
    )

    records: list[SyncRecord] = []
    read_warnings: list[dict[str, Any]] = []
    for relpath, path in supported:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            read_warnings.append(
                {"kind": "unreadable_file", "path": relpath, "detail": str(exc)}
            )
            continue
        handler = handler_for(relpath)
        if handler is None:  # pragma: no cover - walker only yields supported
            continue
        handler_metadata, handler_warnings = handler.parse(relpath, text)
        records.append(
            SyncRecord(
                doc_id=corpus_doc_id(source_system, relpath),
                source_key=relpath,
                content=text,
                handler_metadata=handler_metadata,
                warnings=handler_warnings,
            )
        )

    return sync_records(
        registry,
        records,
        source_system=source_system,
        id_prefix=corpus_id_prefix(source_system),
        extra_metadata=extra_metadata,
        dry_run=dry_run,
        prune_check=_corpus_prune_check(root) if prune else None,
        extractor=build_memory_extractor(registry, opt_in=extract and not dry_run),
        requested_by=requested_by,
        root_label=str(root),
        detect_moves=True,
        unsupported=unsupported,
        initial_warnings=[*walk_warnings, *read_warnings],
    )


def _corpus_prune_check(root: Path) -> PruneCheck:
    """Prune check for a file corpus: is the candidate's file verifiably gone?

    Only ``FileNotFoundError`` on the candidate's own path is evidence of
    absence — the :mod:`trellis.core.path_presence` rule. Any other failure
    to look (a directory the walk could not read, a symlink loop, a path
    component that became a file) keeps the document and says why, because
    a walk that did not see a file says nothing about whether it is gone
    (#633). A dangling symlink is kept too: a link is a declaration, not an
    absence (#479), and ``path_is_present`` is where that rule lives.

    A *root* that is not a directory — a single file, or a path that is
    missing or broken — cannot vouch for any other document under this
    source system, so every candidate is withheld.
    """
    if not root.is_dir():
        return lambda _doc_id, _metadata: SourceUnverified(
            "root is not a directory; no other document can be checked against it"
        )

    def check(_doc_id: str, metadata: Mapping[str, Any]) -> SourceState:
        relpath = metadata.get("source_path")
        if not isinstance(relpath, str) or not relpath:
            return SourceUnverified("no source_path recorded")
        path = root / relpath
        identity = file_identity(path)
        if isinstance(identity, UnknownFileIdentity):
            return SourceUnverified(str(identity))
        return "present" if path_is_present(path) else "vanished"

    return check


@dataclass
class _VectorMirrorTally:
    """Run-level count of the post-embed vector mirror (#388).

    ``metadata_only_writes`` is the **denominator**, not ``chunks_written``:
    only the metadata-only branch can mirror at all, so pairing the mirror
    count with the total would print two numbers that look like a ratio and
    are not — a run that legitimately re-embeds every chunk would read
    ``5000 written, 0 synced`` and look broken.

    ``rows_absent`` separates the third state. Without it,
    ``metadata_only_writes=5000, rows_synced=0`` reads identically for "none
    of these chunks was ever embedded" (expected, harmless) and "every mirror
    threw" (an outage), and those call for opposite responses. A line that
    cannot distinguish them cannot answer the question it exists to ask.

    Reported once per run rather than per document. A corpus sync touches
    thousands of chunks, and a per-document line is either invisible (DEBUG
    is a no-op under the CLI's default ``INFO`` filtering bound logger) or a
    flood. One line, at a level that fires, answering the one question worth
    asking: did this run's tags reach the vector rows?
    """

    metadata_only_writes: int = 0
    rows_synced: int = 0
    rows_absent: int = 0


def sync_records(
    registry: StoreRegistry,
    records: Iterable[SyncRecord],
    *,
    source_system: str,
    id_prefix: str,
    root_label: str,
    requested_by: str,
    extra_metadata: dict[str, Any] | None = None,
    dry_run: bool = False,
    prune_check: PruneCheck | None = None,
    detect_moves: bool = True,
    extractor: Any = None,
    extraction_participant_names: Sequence[str] = (),
    unsupported: Sequence[str] = (),
    initial_warnings: Sequence[dict[str, Any]] = (),
    on_record_error: Callable[[SyncRecord, Exception], None] | None = None,
    minhash: MinHashIndex | None = None,
) -> CorpusSyncReport:
    """Idempotent sync core over reader-produced :class:`SyncRecord`s.

    Args:
        registry: Active store registry.
        records: The documents to sync — one per source item.
        source_system: Stored as ``metadata.source_system``.
        id_prefix: Doc-id prefix owned by this source (e.g.
            ``corpus:obsidian:``) — scopes move detection and prune so a
            run never touches another source's documents.
        root_label: Human-readable origin for the report / summary event
            (a directory path, an export-file path).
        requested_by: Audit identifier for events and embed logging.
        extra_metadata: Operator tags merged into every written document.
        dry_run: Compute and report the plan without writing.
        prune_check: Turns prune on. Asked about each document under
            ``id_prefix`` that no record of this run produced, with its
            ``doc_id`` and stored metadata: ``"vanished"`` deletes it,
            ``"present"`` keeps it, and :class:`SourceUnverified` keeps it
            and lists it under ``report.prune_withheld`` (#633). ``None``
            prunes nothing.
        detect_moves: Re-key a document whose content reappears under a
            new id. Enable for content-derived ids (files); disable for
            content-independent ids (conversation uuids).
        extractor: Optional memory extractor (from
            :func:`~trellis.extract.memory_ingest_hook.build_memory_extractor`);
            when set, each new/changed document's text is mined for
            entity/edge drafts routed through the governed executor.
        extraction_participant_names: Speaker names of this source's
            documents (e.g. the conversation reader's turn labels),
            forwarded to the extraction draft policy so person-typed
            drafts naming a speaker are dropped instead of minted
            (#299). Ignored when ``extractor`` is ``None``.
        unsupported: Source items with no handler (reported, not
            ingested) — for the run counts.
        initial_warnings: Reader warnings raised before record building
            (e.g. unreadable files and directories), seeded so the summary
            event counts them.
        on_record_error: Turns a per-record boundary on. When one record
            raises (an ``Exception``, never a ``BaseException``), the error
            goes to this callback with the record, the record is left out of
            ``report.files`` and the run goes on with the next one, so a
            caller that writes incrementally keeps the records that landed.
            Unset, the first error propagates as it always did. Records
            already applied are not rolled back, and neither is any part of
            the failed one that was written before it raised. A record that
            raised after its parent was written is not skipped as stored by
            the next sync: :func:`record_landed` reads its chunk rows, finds
            one missing, different or left over from a longer version, and
            the record is written again.
        minhash: The near-duplicate index to compare against and add to.
            A caller that syncs one source in several calls passes the same
            index to each, so a near-duplicate is still warned about across
            the calls. Unset, each call starts an empty index.

    Returns:
        A :class:`CorpusSyncReport`.
    """
    doc_store = registry.knowledge.document_store
    report = CorpusSyncReport(
        root=root_label,
        source_system=source_system,
        dry_run=dry_run,
        prune=prune_check is not None,
    )
    report.unsupported = list(unsupported)
    report.warnings.extend(initial_warnings)

    records = list(records)
    current_ids = {record.doc_id for record in records}
    if minhash is None:
        minhash = MinHashIndex()
    moved_from_ids: set[str] = set()

    # Build the deterministic classify-on-write pipeline once per run when the
    # flag is on. Every sync_records caller (corpus, conversation-export,
    # session capture) inherits tagging through this single seam; disabled →
    # None → documents are stored untagged exactly as before.
    classifier = (
        build_ingest_classifier() if classify_on_ingest_enabled(registry) else None
    )
    mirror = _VectorMirrorTally()

    for record in records:
        try:
            report.warnings.extend(record.warnings)
            outcome = _plan_record(
                doc_store,
                report,
                doc_id=record.doc_id,
                source_key=record.source_key,
                content=record.content,
                id_prefix=id_prefix,
                current_ids=current_ids,
                detect_moves=detect_moves,
            )
            for match_id, similarity in minhash.query(record.content):
                report.warnings.append(
                    {
                        "kind": "near_duplicate",
                        "path": record.source_key,
                        "match_doc_id": match_id,
                        "similarity": round(similarity, 3),
                    }
                )
            minhash.add(outcome.doc_id, record.content)

            spans = chunk_spans(record.content)
            outcome.chunk_count = len(spans)
            if outcome.moved_from is not None:
                moved_from_ids.add(outcome.moved_from)

            if not dry_run and outcome.action != "skip":
                _apply_record(
                    registry,
                    report,
                    outcome=outcome,
                    text=record.content,
                    handler_metadata=record.handler_metadata,
                    extra_metadata=extra_metadata or {},
                    spans=spans,
                    source_system=source_system,
                    requested_by=requested_by,
                    extractor=extractor,
                    extraction_participant_names=extraction_participant_names,
                    classifier=classifier,
                    mirror=mirror,
                )
            report.files.append(outcome)
        except Exception as exc:
            if on_record_error is None:
                raise
            on_record_error(record, exc)

    if prune_check is not None:
        _prune_vanished(
            registry,
            report,
            id_prefix=id_prefix,
            keep_ids=current_ids | moved_from_ids,
            check=prune_check,
            dry_run=dry_run,
        )

    _log_vector_mirror(registry, mirror, dry_run=dry_run)
    _emit_summary(registry, report, requested_by=requested_by)
    return report


def record_landed(doc_store: DocumentStore, doc_id: str, content: str) -> bool:
    """Whether the chunk rows the write path makes of *content* are all stored.

    The parent row is written first, so a stored parent carrying *content*'s
    hash proves only that the run reached it. For a record that chunks, this
    recomputes the spans :func:`sync_records` would write with
    :func:`chunk_spans` and reads each ``#chunk-i`` row: a row that is absent,
    or whose hash is not that of its span, means the record has not landed and
    the caller must write it again, not skip it. So does a live row *past* the
    last span, which a run that shrank the document left when it died before
    deleting the surplus; the parent no longer declares it, so only this read
    finds it. A record that does not chunk has no rows to read and reads as
    landed. A document that shrank from chunked to unchunked and then died
    before deleting its chunk rows is therefore not found: reading one row for
    every record that does not chunk would put a read on every record of every
    sync.

    A chunk that is a redaction tombstone counts as landed, past the last span
    as within it. The write path leaves it alone and warns (#1034), so a retry
    could not change it and a record carrying one would otherwise be rewritten
    by every sync.

    Compares rows, not vectors: a chunk written and then not embedded, because
    the run died between the two, reads as landed here.
    """
    spans = chunk_spans(content)
    for span in spans:
        row = doc_store.get(chunk_doc_id(doc_id, span.index))
        if row is None:
            return False
        if is_redaction_tombstone(row):
            continue
        if row.get("content_hash") != content_hash(content[span.start : span.end]):
            return False
    if not spans:
        return True
    # Surplus rows form a run from ``len(spans)`` (:func:`_delete_surplus_chunks`
    # deletes from the top), so the first absent row ends the search, and a
    # clean record costs exactly this one read.
    index = len(spans)
    while (row := doc_store.get(chunk_doc_id(doc_id, index))) is not None:
        if not is_redaction_tombstone(row):
            return False
        index += 1
    return True


def _redaction_tombstone_warning(path: str, doc_id: str) -> dict[str, Any]:
    """The report entry for a row the sync left alone because it is redacted."""
    return {"kind": "redaction_tombstone", "path": path, "doc_id": doc_id}


def _plan_record(
    doc_store: DocumentStore,
    report: CorpusSyncReport,
    *,
    doc_id: str,
    source_key: str,
    content: str,
    id_prefix: str,
    current_ids: set[str],
    detect_moves: bool,
) -> FileOutcome:
    """Decide new / update / skip / move for one record.

    A row that is a redaction tombstone is **skipped, with a warning on
    ``report``**, whatever the source says. Its blanked content never matches
    the file's hash, so without this it planned as ``update`` and wrote the
    redacted text straight back (#1034). Bringing the id back is a deliberate
    act (``reingest_redacted`` on ``put_document``); a sync does not make it.

    **A matching hash is not by itself a skip.** The parent is written before
    its chunks, so a stored parent whose hash matches may belong to a run that
    died between two chunk writes. The skip also needs
    :func:`record_landed`; a record whose chunks are not all there plans as
    ``update`` and the normal write path rewrites the parent and the chunks.
    """
    chash = content_hash(content)

    existing = doc_store.get(doc_id)
    if existing is not None:
        if is_redaction_tombstone(existing):
            report.warnings.append(_redaction_tombstone_warning(source_key, doc_id))
            return FileOutcome(relpath=source_key, doc_id=doc_id, action="skip")
        if existing.get("content_hash") == chash and record_landed(
            doc_store, doc_id, content
        ):
            return FileOutcome(relpath=source_key, doc_id=doc_id, action="skip")
        return FileOutcome(relpath=source_key, doc_id=doc_id, action="update")

    # New id — same content under another id of this source whose item is
    # gone means it moved: re-key, don't duplicate.
    if detect_moves:
        hit = doc_store.get_by_hash(chash)
        if (
            hit is not None
            and hit["doc_id"].startswith(id_prefix)
            and not is_chunk_doc_id(hit["doc_id"])
            and hit["doc_id"] not in current_ids
        ):
            return FileOutcome(
                relpath=source_key,
                doc_id=doc_id,
                action="move",
                moved_from=hit["doc_id"],
            )
    return FileOutcome(relpath=source_key, doc_id=doc_id, action="new")


def _apply_record(
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
    extractor: Any = None,
    extraction_participant_names: Sequence[str] = (),
    classifier: Any = None,
    mirror: _VectorMirrorTally,
) -> None:
    """Execute the writes for one new / updated / moved record."""
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
    # An ``update`` whose stored parent already carries this text is a repair,
    # not an edit: the planner sends it here because chunk rows are missing or
    # surplus (:func:`record_landed`), and writing the parent again must not
    # move its recency. Only the rows that were absent get a fresh clock, and
    # that is right, since their content is new to the store.
    parent_content_unchanged = False
    if outcome.action == "update":
        stored = doc_store.get(outcome.doc_id)
        old_chunk_count = _chunk_count_of(stored)
        parent_content_unchanged = stored is not None and stored.get(
            "content_hash"
        ) == content_hash(text)
        # Merge under the fresh metadata so keys this run does not own
        # (enrichment tags, noise flags) survive the re-put.
        metadata = {**_stored_metadata(stored), **metadata}
        if not spans:
            metadata.pop("chunk_count", None)

    # Classify-on-write: tag the FULL document once (not per chunk — a chunk in
    # isolation lacks the doc's structural cues, and StructuralClassifier would
    # mark every short chunk low-signal). The resulting tags ride onto the
    # parent here and are propagated to the chunks below, which are the
    # retrievable unit.
    #
    # Fill-if-absent: only classify when the (post-merge) metadata carries no
    # content_tags. This tags every new document but never clobbers tags a
    # prior run — or the LLM enrichment pass — already wrote (both persist to
    # the same content_tags key, and enrichment tags must survive a re-put per
    # the update-merge contract above). Refreshing stale tags on edited content
    # is reclassify_stale's job, not the write path's.
    if classifier is not None and "content_tags" not in metadata:
        metadata.update(
            classify_for_ingest(
                classifier,
                text,
                source_system=source_system,
                title=str(metadata.get("title") or ""),
                doc_id=outcome.doc_id,
                prior_importance=float(metadata.get("auto_importance", 0.0) or 0.0),
            )
        )

    # The validation seam. Shape-preserving apart from the reconciled
    # `content_type` → `document_form` key; see trellis.schemas.document_metadata.
    metadata = DocumentMetadata.from_mapping(metadata).to_metadata()

    # Through the seam, even though the unchunked branch below re-embeds and
    # would rebuild the row wholesale anyway: ``run_embed_on_ingest`` is
    # gated on ``TRELLIS_ENABLE_EMBED_ON_INGEST``, so on a deployment with
    # that flag off the embed is not a mirror at all. The seam's mirror is
    # unconditional. It also reaches the case the embed never covers — a
    # document that has just *become* chunked keeps the whole-document
    # vector row written by an earlier unchunked run, and nothing here
    # re-embeds it (#567).
    put_document(
        doc_store,
        vector_store,
        outcome.doc_id,
        text,
        metadata,
        preserve_updated_at=parent_content_unchanged,
    )
    outcome.chunks_written = _write_chunks(
        registry,
        parent_doc_id=outcome.doc_id,
        text=text,
        spans=spans,
        relpath=outcome.relpath,
        source_system=source_system,
        extra_metadata=extra_metadata,
        requested_by=requested_by,
        # Read off the *post-merge* parent metadata, never off this run's
        # classify output, so a chunk inherits whatever the parent actually
        # carries — tags written this run, by an earlier run, or by the LLM
        # enrichment pass.
        inherited_tags={
            key: metadata[key] for key in CLASSIFY_METADATA_KEYS if key in metadata
        },
        mirror=mirror,
        report=report,
    )
    if outcome.action == "update":
        _delete_surplus_chunks(
            doc_store,
            vector_store,
            outcome.doc_id,
            kept=len(spans),
            old_chunk_count=old_chunk_count,
            report=report,
            relpath=outcome.relpath,
        )
    if not spans:
        run_embed_on_ingest(
            registry, outcome.doc_id, text, metadata, source=requested_by
        )
    else:
        # A document that has just *become* chunked keeps the whole-document
        # vector row an earlier unchunked run wrote, and nothing above
        # rebuilds it: the orphan loop's range is empty on that transition
        # (``old_chunk_count`` is 0) and the re-embed is on the other branch
        # (#567). That row is not a duplicate of the chunks, it is a *stale*
        # one — still carrying the pre-edit embedding and excerpt — so the
        # semantic axis can serve the old text of a document the sync has
        # already rewritten.
        #
        # Unconditional on the chunked branch, not conditioned on
        # ``old_chunk_count == 0``: deleting an absent row is a no-op, the
        # count is only meaningful for updates, and the row's writer is not
        # always this path. The invariant is about the shape of the store —
        # whole-document row XOR chunk rows — not about who wrote it.
        #
        # The parent *document* stays. It holds the full text, it is what the
        # keyword axis serves and what a re-run rebuilds the chunks from;
        # only its semantic handle moves down to the chunks.
        _delete_vector_row(vector_store, outcome.doc_id)

    if outcome.action == "move" and outcome.moved_from is not None:
        old = doc_store.get(outcome.moved_from)
        _delete_document_tree(
            doc_store,
            vector_store,
            outcome.moved_from,
            _chunk_count_of(old),
            report=report,
            relpath=_source_path_or(_stored_metadata(old), outcome.relpath),
        )

    _emit_memory_stored(
        registry,
        report,
        outcome=outcome,
        text=text,
        metadata=metadata,
        requested_by=requested_by,
    )

    if extractor is not None and not may_reach_route(
        sensitivity_of(metadata), local=False
    ):
        # Not on a labelled document, as in ``save_memory``: the entity names
        # it would mint land in the graph, which carries no label, so
        # ``local=False`` whatever the route is declared. Counted, not logged.
        report.extraction_skipped_labelled += 1
    elif extractor is not None:
        # Mine entity/edge drafts from the full document text (not chunks)
        # and route them through the governed executor. Fully fail-soft —
        # the document is already durably stored.
        entities, edges = run_memory_extraction(
            registry,
            extractor,
            outcome.doc_id,
            text,
            requested_by=requested_by,
            participant_names=extraction_participant_names,
        )
        report.entities_extracted += entities
        report.edges_extracted += edges


def _delete_surplus_chunks(
    doc_store: DocumentStore,
    vector_store: Any,
    parent_doc_id: str,
    *,
    kept: int,
    old_chunk_count: int,
    report: CorpusSyncReport,
    relpath: str,
) -> None:
    """Delete the chunk rows of *parent_doc_id* at or past index *kept*.

    *old_chunk_count* is what the parent declared when this run read it. A run
    that died after writing the parent had already replaced that count with the
    new one, so the rows it was meant to delete are past the declared range;
    the rows are therefore probed upward from the declared end until one is
    absent, the way a redaction finds a target's chunks (#1008).

    Deleted from the top down. A kill between two deletes then leaves a run of
    rows that starts at *kept*, which both the probe here and
    :func:`record_landed` find; deleting upward could leave a gap at *kept* and
    hide the rows above it from both.

    A redacted chunk is not an orphan to clean up: deleting its row loses the
    tombstone, and the file growing back would write the redacted text under
    the same id with nothing to refuse it (#1034). It is left in place and the
    probe continues past it.
    """
    stop = max(kept, old_chunk_count)
    while doc_store.get(chunk_doc_id(parent_doc_id, stop)) is not None:
        stop += 1
    # The warnings are met top down and reported bottom up, the order the
    # report has always listed them in.
    kept_tombstones: list[str] = []
    try:
        for index in range(stop - 1, kept - 1, -1):
            orphan_id = chunk_doc_id(parent_doc_id, index)
            orphan = doc_store.get(orphan_id)
            if orphan is not None and is_redaction_tombstone(orphan):
                kept_tombstones.append(orphan_id)
                continue
            _delete_doc_and_vector(doc_store, vector_store, orphan_id)
    finally:
        report.warnings.extend(
            _redaction_tombstone_warning(relpath, orphan_id)
            for orphan_id in reversed(kept_tombstones)
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
    inherited_tags: dict[str, Any],
    mirror: _VectorMirrorTally,
    report: CorpusSyncReport,
) -> int:
    """Write (and embed) chunk documents; skip byte-identical chunks.

    Returns the number of chunk docs actually written. Operator tags and the
    parent's ``content_tags`` / ``auto_importance`` (``inherited_tags``)
    propagate to chunks — chunks are the retrievable unit, so retrieval tag
    filters must see them; handler metadata stays on the parent.

    Stored chunk metadata is merged *under* the freshly computed metadata, the
    same contract the parent re-put honours: keys this run does not own (an
    earlier run's operator tags, a chunk-local flag) survive the re-put instead
    of being rebuilt away.

    ``inherited_tags`` is the deliberate exception, and **how far the
    exception reaches depends on why the chunk is being written**. These two
    rules used to be stated as one, which made the docstring assert both that
    the parent always wins and that an unedited chunk's tags are never
    overwritten:

    * **The chunk's bytes changed** — it is genuinely being rewritten, so the
      parent's tags win outright. A rewritten chunk and its parent cannot
      disagree about what the document is.
    * **Only its metadata is being refreshed** (``count_stale`` or
      ``tags_missing``) — the parent's tags fill keys the chunk *lacks* and
      overwrite nothing. An unedited chunk's tags may carry a demote signal
      the feedback loop put there (``apply_noise_tags`` writes
      ``signal_quality="noise"`` onto the *served* item, which is a chunk),
      and no sync may undo it.

    A chunk whose bytes did not change is otherwise skipped entirely. It is
    re-put only when it is missing a key the parent carries, or when the
    document's chunk count moved — so enabling classify-on-write, or
    backfilling tags onto parents, reaches the chunks without an unrelated
    content edit.

    That metadata-only re-put is a **post-embed write**, so it has to mirror
    onto the vector row explicitly (#388) — see
    :mod:`trellis.core.vector_metadata` for why a document-store-only write
    is invisible to the semantic axis. What is local to *this* branch: it
    deliberately does not re-embed, so nothing else would refresh the row;
    and the keys it propagates are ``inherited_tags``, i.e.
    :data:`~trellis.classify.ingest.CLASSIFY_METADATA_KEYS`, which the
    mirror covers because it is contained in
    :data:`~trellis.core.vector_metadata.MIRRORED_METADATA_KEYS` (pinned by
    ``test_classify_keys_are_covered_by_the_mirror`` — prose asserting an
    invariant is not the same as enforcing it). Un-mirrored, a chunk
    demoted on its parent stayed servable on the semantic axis — #338's
    failure, third site.

    The mirror is scoped to the metadata-only path on purpose. When the
    bytes *did* change, ``run_embed_on_ingest`` is the row's writer and
    rewrites the whole snapshot; when that hook is switched off, the row is
    stale in content, excerpt and embedding as well, and freshening its tags
    alone would pair new tags with an old excerpt — a row that is internally
    inconsistent rather than merely old. ``reindex-vectors`` owns that case.

    **A chunk whose stored row is a redaction tombstone is skipped, with a
    warning on ``report``** (#1034). A redaction can name a chunk without its
    parent, so the parent's own skip (:func:`_plan_record`) does not cover it,
    and a blanked chunk always reads as changed text.
    """
    doc_store = registry.knowledge.document_store
    # Resolved here rather than passed in: `_apply_record` and
    # `_prune_vanished` both resolve it off the registry the same way, so
    # this is the module's convention, and `_get` caches — by the time this
    # runs, `_apply_record` has already instantiated the store.
    vector_store = getattr(registry.knowledge, "vector_store", None)
    written = 0
    for span in spans:
        cid = chunk_doc_id(parent_doc_id, span.index)
        chunk_content = text[span.start : span.end]
        # One read serves all three staleness checks and the merge below —
        # do not split it into a second doc_store.get.
        existing = doc_store.get(cid)
        if existing is not None and is_redaction_tombstone(existing):
            report.warnings.append(_redaction_tombstone_warning(relpath, cid))
            continue
        stored = _stored_metadata(existing)
        content_changed = existing is None or existing.get(
            "content_hash"
        ) != content_hash(chunk_content)
        count_stale = existing is not None and stored.get("chunk_count") != len(spans)
        tags_missing = not (inherited_tags.keys() <= stored.keys())
        if not content_changed and not count_stale and not tags_missing:
            continue
        # The trigger above is missing-only; this write must be too, or the
        # promise it makes is void (#395-B1). `count_stale` alone re-puts
        # every byte-identical chunk of any document whose chunk count moved
        # — append one section to a note and a splat of the parent's whole
        # bag would overwrite a `signal_quality="noise"` the feedback loop
        # put on the chunk. Parent tags win only where the chunk is being
        # genuinely rewritten; on a metadata-only refresh they fill gaps and
        # nothing more.
        applied_tags = (
            inherited_tags
            if content_changed
            else {k: v for k, v in inherited_tags.items() if k not in stored}
        )
        # NO SOURCE CLOCK, DELIBERATELY (#463). This is a derived-row
        # producer and the parent's ``created_at`` / ``updated_at`` metadata —
        # the *source's* clock, which ``resolve_recency_stamp`` (#417) prefers
        # over the row column on both document-backed axes — is not among the
        # keys a chunk inherits. So a slice of a 2024 conversation decays off
        # the 2026-08-07 import instant while the conversation itself decays
        # off 2024: one document, two ages, which is the exact incoherence
        # #417 removed *between strategies* and this leaves *within* a
        # document. Propagating it was measured and refused; the reasoning is
        # kept here rather than only in the ledger because a later reader will
        # arrive at this dict, not at ``docs/design/decision-ledger.md``.
        #
        # The fix is one key and it makes packs slightly worse. 735 chunks sit
        # under 74 stamped parents at a median 128-day column-minus-source gap,
        # so the stamp takes a chunk's recency multiplier from 0.605 to 0.336 —
        # which is **exactly where its parent already sits**, both floor-bound
        # (``RECENCY_FLOOR`` 0.3, 30-day half-life). Once the two halves of the
        # conversation corpus share one multiplier the tiebreak falls to raw
        # relevance, where a median-7994-char parent beats a median-3011-char
        # chunk on surface area alone. A two-arm replay over all 59 attributed
        # packs against production's own stores with the live embedder: 27/59
        # packs change, -49 chunk servings, **+41 stamped-parent** servings,
        # +14 everything else. Only 2 of those 41 are the parent of a chunk
        # dropped from the same pack — the mechanism is not a hand-off, it is
        # the whole 148-row parent population floating up together.
        #
        # That is the wrong direction. Over 55 attributed packs a stamped
        # parent earns P(cited helpful | served) = **0.005** (1 of 219) against
        # a chunk's 0.082 (7 of 85) and the rest of the corpus's 0.131; both
        # halves sit near 0.55 unhelpful. Priced at those rates the swap is
        # -2.0 cited-helpful servings and +0.2 cited-unhelpful, per 59 packs.
        # #417 called this "propagating the stamp would demote the better half"
        # and was right, though for a reason it had not established.
        #
        # What the replay could *not* do is price it by citation directly: arm
        # A reproduces only 22% of the graded ids (122/553), because the corpus
        # grew and today's retrieval for a year-old intent returns largely
        # different items. So the -2.0 is an expected value at measured
        # per-class rates, not an observed loss. Re-open this if the rates
        # converge — a stamped parent at 1 helpful citation in 219 servings is
        # the fact carrying the decision, and it is the one most likely to move.
        #
        # Update (#986): that is still the decision; this does not re-open it.
        # A newer 30-day figure is in a different unit (token share and
        # judged-useful token fraction, not P(cited helpful | served)), so it
        # does not say the old rates converged: the conversation item namespace,
        # almost all chunks, took 9.5% of injected tokens at a 0.0074
        # judged-useful token fraction against 0.35 overall. That is a case for
        # *withholding* chunks, not for stamping them, and the class prior now
        # can (``TRELLIS_CLASS_PRIOR_SCOPE=parents_and_chunks``,
        # :mod:`trellis.retrieve.class_prior`). It reads the chunk's form from
        # its parent at serve time through ``parent_doc_id`` rather than
        # stamping one here, so no chunk written by this function changes and
        # no backfill or vector-metadata re-sync is needed.
        metadata: dict[str, Any] = {
            **stored,
            **extra_metadata,
            **applied_tags,
            "source_system": source_system,
            "source_path": relpath,
            "parent_doc_id": parent_doc_id,
            "chunk_index": span.index,
            "chunk_count": len(spans),
            "char_span": [span.start, span.end],
        }
        # Same seam as the parent — chunks are the retrievable unit.
        metadata = DocumentMetadata.from_mapping(metadata).to_metadata()
        # Conditional, not unconditional (#406): this one put serves both
        # branches. A genuinely rewritten chunk *is* modified and must bump;
        # the metadata-only refresh — entered on ``count_stale`` or
        # ``tags_missing`` alone — must not, or every byte-identical chunk of
        # any document whose parent gained tags is re-stamped to the sync's
        # own clock — precisely what ``KeywordSearch``'s recency decay would
        # then be ranking on.
        # One call, both planes, and the mirror rides the bag that was just
        # written rather than one a later line re-selects (#360). The
        # ``preserve_updated_at`` conditional is unchanged and still what
        # #406 is about; what moved is that neither branch can now reach the
        # document store without the vector row going with it.
        write = put_document(
            doc_store,
            vector_store,
            cid,
            chunk_content,
            metadata,
            preserve_updated_at=not content_changed,
        )
        if content_changed:
            # Re-embed: the text changed, so the *embedding* is stale too and
            # the key mirror above is not enough. Metadata-only refreshes
            # deliberately don't — same convention as the document ingest
            # paths — which is why the mirror is what they are counted on.
            run_embed_on_ingest(
                registry, cid, chunk_content, metadata, source=requested_by
            )
        else:
            mirror.metadata_only_writes += 1
            if write.mirror == "synced":
                mirror.rows_synced += 1
            elif write.mirror == "absent":
                mirror.rows_absent += 1
        written += 1
    return written


def _prune_vanished(
    registry: StoreRegistry,
    report: CorpusSyncReport,
    *,
    id_prefix: str,
    keep_ids: set[str],
    check: PruneCheck,
    dry_run: bool,
) -> None:
    """Delete documents under ``id_prefix`` whose source is verifiably gone.

    ``check`` rules on each candidate, and only ``"vanished"`` deletes. A
    candidate whose source could not be checked is kept and listed under
    ``report.prune_withheld`` (#633). A vanished candidate whose parent row is a
    redaction tombstone is kept whole with a ``redaction_tombstone`` warning and
    is not listed under ``report.pruned`` (#1067).
    """
    doc_store = registry.knowledge.document_store
    vector_store = getattr(registry.knowledge, "vector_store", None)

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
                not doc_id.startswith(id_prefix)
                or is_chunk_doc_id(doc_id)
                or doc_id in keep_ids
            ):
                continue
            metadata = _stored_metadata(doc)
            candidates.append(
                {
                    "doc_id": doc_id,
                    "source_path": metadata.get("source_path"),
                    "metadata": metadata,
                    "chunk_count": _chunk_count_of(doc),
                    # Read off the listed row, so no second ``get`` and no
                    # document text held for every candidate.
                    "tombstone": is_redaction_tombstone(doc),
                }
            )
        if len(page) < _PRUNE_PAGE_SIZE:
            break

    for candidate in sorted(candidates, key=lambda c: str(c["doc_id"])):
        state = check(candidate["doc_id"], candidate["metadata"])
        if isinstance(state, SourceUnverified):
            report.prune_withheld.append(
                {
                    "doc_id": candidate["doc_id"],
                    "source_path": candidate["source_path"],
                    "detail": state.detail,
                }
            )
            continue
        if state != "vanished":
            continue
        relpath = _source_path_or(candidate["metadata"], candidate["doc_id"])
        if candidate["tombstone"]:
            # The blank parent row is what refuses the redacted text when the
            # source comes back, so deleting it, as the id-only conversation
            # check would, lets the next sync write the parent back live
            # (#1067). Kept whole, dry run included, so the plan matches.
            report.warnings.append(
                _redaction_tombstone_warning(relpath, candidate["doc_id"])
            )
            continue
        if dry_run:
            # Nothing is deleted, but the plan still says what the real run
            # would leave behind.
            _warn_tombstoned_chunks(
                report,
                relpath,
                _tombstoned_chunk_ids(
                    doc_store, candidate["doc_id"], candidate["chunk_count"]
                ),
            )
        else:
            _delete_document_tree(
                doc_store,
                vector_store,
                candidate["doc_id"],
                candidate["chunk_count"],
                report=report,
                relpath=relpath,
            )
        report.pruned.append(
            {"doc_id": candidate["doc_id"], "source_path": candidate["source_path"]}
        )


def _tombstoned_chunk_ids(
    doc_store: DocumentStore, doc_id: str, chunk_count: int
) -> list[str]:
    """The chunk ids of ``doc_id`` whose row is a redaction tombstone, in order.

    Read-only, so a dry run can predict what :func:`_delete_document_tree`
    would leave behind.
    """
    tombstoned: list[str] = []
    for index in range(chunk_count):
        chunk_id = chunk_doc_id(doc_id, index)
        row = doc_store.get(chunk_id)
        if row is not None and is_redaction_tombstone(row):
            tombstoned.append(chunk_id)
    return tombstoned


def _warn_tombstoned_chunks(
    report: CorpusSyncReport, relpath: str, chunk_ids: list[str]
) -> None:
    report.warnings.extend(
        _redaction_tombstone_warning(relpath, chunk_id) for chunk_id in chunk_ids
    )


def _source_path_or(metadata: dict[str, Any], fallback: str) -> str:
    """The row's ``source_path`` when it is a string, else ``fallback``."""
    source_path = metadata.get("source_path")
    return source_path if isinstance(source_path, str) else fallback


def _delete_document_tree(
    doc_store: DocumentStore,
    vector_store: Any,
    doc_id: str,
    chunk_count: int,
    *,
    report: CorpusSyncReport,
    relpath: str,
) -> None:
    """Delete a parent document, its chunk docs, and their vector rows.

    A chunk that is a redaction tombstone is **kept, with a warning on
    ``report``**: deleting its row loses the tombstone, and the file coming
    back under the old path would write the redacted text under the same chunk
    id with nothing to refuse it (#1034, the same hole the orphan loop in
    :func:`_apply_record` closes). Its vector row is not touched either; the
    redaction already deleted it. The parent row is deleted as before.
    """
    kept = _tombstoned_chunk_ids(doc_store, doc_id, chunk_count)
    _warn_tombstoned_chunks(report, relpath, kept)
    for index in range(chunk_count):
        chunk_id = chunk_doc_id(doc_id, index)
        if chunk_id not in kept:
            _delete_doc_and_vector(doc_store, vector_store, chunk_id)
    _delete_doc_and_vector(doc_store, vector_store, doc_id)


def _delete_vector_row(vector_store: Any, doc_id: str) -> None:
    """Drop one vector row. Absent is a no-op, and failure is survivable.

    ``VectorStore.delete`` returns ``False`` for an id it does not hold
    (pinned by the contract suite), so callers may delete unconditionally
    rather than reading the store first.
    """
    if vector_store is None:
        return
    try:
        vector_store.delete(doc_id)
    except Exception:
        # GRACEFUL-DEGRADATION: a stale vector row degrades retrieval
        # quality; a failed vector backend must not abort the sync.
        logger.exception("corpus_vector_delete_failed", doc_id=doc_id)


def _delete_doc_and_vector(
    doc_store: DocumentStore,
    vector_store: Any,
    doc_id: str,
) -> None:
    doc_store.delete(doc_id)
    _delete_vector_row(vector_store, doc_id)


def _stored_metadata(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata of an already-stored document, or ``{}`` if it has none.

    Both re-put paths merge this *under* the metadata they compute, so keys
    the run does not own survive; see :func:`_apply_record` and
    :func:`_write_chunks`.
    """
    metadata = (doc or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _chunk_count_of(doc: dict[str, Any] | None) -> int:
    value = _stored_metadata(doc).get("chunk_count", 0)
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


def _log_vector_mirror(
    registry: StoreRegistry,
    mirror: _VectorMirrorTally,
    *,
    dry_run: bool,
) -> None:
    """Say once, per run, whether propagated tags reached the vector rows.

    Silent when the run performed no metadata-only chunk writes — there was
    nothing to mirror, and a zero line every sync trains the reader to skip
    it. When there *were* such writes and no vector store is configured, the
    consequence is stated outright and the repair named: this is exactly the
    state in which an operator runs a tag backfill, sees a clean sync, and
    concludes the semantic axis now honours the tags (#338/#388).
    """
    if dry_run or not mirror.metadata_only_writes:
        return
    vector_store = getattr(registry.knowledge, "vector_store", None)
    if vector_store is None:
        logger.warning(
            "corpus_sync_vector_mirror_unavailable",
            metadata_only_writes=mirror.metadata_only_writes,
            consequence=(
                "propagated tags did not reach any vector row; the semantic "
                "axis will keep serving pre-propagation metadata. Run "
                "`trellis admin resync-vector-metadata` once a vector store "
                "is configured."
            ),
        )
        return
    logger.info(
        "corpus_sync_vector_mirror",
        metadata_only_writes=mirror.metadata_only_writes,
        rows_synced=mirror.rows_synced,
        rows_absent=mirror.rows_absent,
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
