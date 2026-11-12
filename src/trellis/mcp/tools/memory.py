"""``save_memory`` with its supersede and reconcile paths."""

from __future__ import annotations

import threading
from typing import Any

import structlog
from mcp.shared.exceptions import MCPError

from trellis.auth import SCOPE_INGEST
from trellis.core.document_write import put_document
from trellis.core.vector_metadata import resolve_vector_store
from trellis.extract.memory_ingest_hook import run_memory_extraction
from trellis.llm.egress import EgressGate, may_reach_route
from trellis.llm.routing import LLMConsumer
from trellis.mcp import supersession as _supersession
from trellis.mcp.app import mcp
from trellis.mcp.auth import current_clearance, trellis_scope
from trellis.mcp.boundary import (
    _exception_detail,
    _raise_internal,
    _raise_invalid_params,
    _raise_mutation_failed,
    _result_message,
)
from trellis.mcp.reconcile import (
    RECONCILIATION_KEY,
    SUPERSEDES_DOC_KEY,
    UPDATES_DOC_KEY,
    ReconcileCandidate,
    ReconcileDecision,
    ReconcileFallbackReason,
    ReconcileOutcome,
    candidate_is_cleared,
    configured_model_id,
    emit_reconcile_degraded,
    emit_reconcile_verdict,
    judge_reconcile,
    mark_document_superseded,
    marker_for_fallback,
    reconcile_on_write_enabled,
    reconcile_timeout_seconds,
)
from trellis.mcp.rejections import _note_noop_call, _record_boundary_rejection
from trellis.mcp.tools.memory_common import (
    ReferenceFieldsError,
    _append_note,
    _match_readable,
    _refuse_label_below_request,
    _refuse_memory_pointer_keys,
    _stamp_sensitivity,
    _with_reference_fields,
)
from trellis.mcp.wiring import (
    _build_llm_client,
    _get_memory_extractor,
    _get_minhash_index,
    _get_registry,
)
from trellis.mutate import (
    CommandStatus,
    MutationExecutor,
    build_curate_executor,
    build_evidence_ingest_command,
)
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis.retrieve.sensitivity import (
    OverwriteRefusedError,
    refuse_overwrite,
)
from trellis.schemas.classification import lifecycle_state_of, sensitivity_of
from trellis.schemas.memory_op import REF_TYPE_DOCUMENT
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Surface labels
# ---------------------------------------------------------------------------

#: The ``save_memory`` write-surface label, shared by everything that has to
#: agree on it: the executor's ``requested_by`` on ``MUTATION_EXECUTED``, the
#: ``requested_by`` on the tool's unconditional ``MEMORY_STORED`` accept
#: signal, and the ``mcp:<tool>`` label ``record_write_rejection`` derives for
#: the rejection side. Named once because the capture-health check joins
#: accepts to rejections on exactly this string, and a drift between the two
#: sides is invisible until a banner will not clear (#461).
SAVE_MEMORY_SURFACE = "mcp:save_memory"

#: Serializes ``save_memory``'s dedup-and-store critical section. The
#: MinHashIndex lock makes each index call atomic, but the dedup DECISION
#: spans exact-hash check → fuzzy find → document_store.put → index add,
#: which the per-method lock cannot make atomic. Without this, two http
#: worker threads saving the same (or near-identical) content both miss
#: dedup and both persist. save_memory is a write, not a hot path, so
#: serializing its dedup section is cheap. Held only around the decision;
#: event emit / extraction / embedding run outside it.
_save_memory_lock = threading.Lock()


def _emit_memory_stored_and_enrich(
    registry: StoreRegistry,
    stored_id: str,
    content: str,
    metadata: dict[str, Any],
    chash: str,
) -> str | None:
    """Emit required acceptance, then run best-effort enrichment outside lock.

    Returns a note for the write response when a step was skipped on purpose.
    """
    try:
        registry.operational.event_log.emit(
            EventType.MEMORY_STORED,
            source="save_memory",
            entity_id=stored_id,
            entity_type="document",
            payload={
                "doc_id": stored_id,
                "content_hash": chash,
                "content_length": len(content),
                "metadata": metadata,
                "requested_by": SAVE_MEMORY_SURFACE,
            },
        )
    except Exception as exc:
        logger.exception("memory_stored_event_emission_failed", doc_id=stored_id)
        detail = _exception_detail(exc)
        _raise_internal(
            f"MEMORY_STORED event emit failed: {detail}",
            cause=exc,
            data={"stage": "memory_stored_emit", "doc_id": stored_id},
        )

    # Feature-flagged tiered extraction (TRELLIS_ENABLE_MEMORY_EXTRACTION=1).
    # Runs AliasMatch + LLM residue via the governed MutationExecutor.
    # Never blocks save_memory success — failures are logged and swallowed.
    # Not on a labelled document: the entity names it would mint land in the
    # graph, which carries no label in this slice, so labelled content would
    # reach callers without the clearance as derived text (#194).
    label = sensitivity_of(metadata)
    note: str | None = None
    # ``local=False`` on purpose: the reason is the unlabelled graph, not the
    # route, so a route declared local does not lift this skip.
    labelled = not may_reach_route(label, local=False)
    memory_extractor = None if labelled else _get_memory_extractor(registry)
    if labelled:
        note = (
            f"Note: memory extraction skipped; the document is labelled {label!r} "
            "and extracted entities would not carry the label."
        )
    if memory_extractor is not None:
        run_memory_extraction(
            registry,
            memory_extractor,
            stored_id,
            content,
            requested_by=SAVE_MEMORY_SURFACE,
        )

    # Feature-flagged embedding (TRELLIS_ENABLE_EMBED_ON_INGEST=1) so
    # SemanticSearch can retrieve the memory. Fail-soft inside the hook —
    # a broken embedder never fails save_memory.
    run_embed_on_ingest(
        registry, stored_id, content, metadata, source=SAVE_MEMORY_SURFACE
    )
    return note


def _check_memory_supersedes(
    registry: StoreRegistry,
    supersedes: str | None,
    *,
    doc_id: str | None,
    existing: dict[str, Any] | None,
) -> None:
    """Refuse ``save_memory(supersedes=…)`` before anything is stored.

    A refusal is recorded and raised here rather than in
    :mod:`trellis.mcp.supersession`, so the rejection site carries the literal
    ``tool=`` the capture-surface roster reads.
    """
    if supersedes is None:
        return
    refusal = _supersession.check_memory_target(
        registry,
        supersedes,
        doc_id=doc_id,
        existing=existing,
        clearance=current_clearance(),
    )
    if refusal is None:
        return
    _record_boundary_rejection(tool="save_memory", rejections=[refusal.row()])
    _raise_invalid_params(
        refusal.msg, data={"field": refusal.loc, "supersedes": supersedes}
    )


def _memory_exists_response(
    registry: StoreRegistry, existing_id: str, supersedes: str | None
) -> str:
    """Reply to an exact-hash hit, stamping ``supersedes`` onto the match.

    The existing document is the successor. Nothing new is stored, so a
    failed stamp is an error — the call did nothing. This branch is also how
    a re-send finishes a stamp that failed after the memory was stored.
    """
    if supersedes is None:
        _note_noop_call("save_memory", "exact_duplicate")
        return f"Memory already exists: {existing_id}"
    # Read before the stamp, which is what tells the two outcomes apart: a
    # stamp that runs emits its own ``MUTATION_EXECUTED``, and that is already
    # this call's accepted count, so it must not also be counted as a no-op.
    # Only a stamp that was already in place writes nothing.
    already_stamped = _supersession.stamp_in_place(
        registry,
        doc_id=supersedes,
        successor=existing_id,
        clearance=current_clearance(),
    )
    error = _supersession.supersede_document(
        registry,
        doc_id=supersedes,
        successor=existing_id,
        requested_by=SAVE_MEMORY_SURFACE,
        clearance=current_clearance(),
    )
    if error is not None:
        _raise_mutation_failed(
            f"failed to supersede {supersedes}: {error}; nothing was written "
            f"({existing_id} already holds this content)",
            data={
                "stage": "supersede",
                "supersedes": supersedes,
                "existing": existing_id,
            },
        )
    if already_stamped:
        _note_noop_call("save_memory", "exact_duplicate")
    return f"Memory already exists (supersedes {supersedes}): {existing_id}"


def _refuse_near_duplicate_supersedes(
    supersedes: str | None, match_id: str, similarity: float
) -> None:
    """Refuse a ``supersedes=`` call whose content near-duplicates a memory.

    Without ``supersedes`` a near-duplicate is answered with the match and
    nothing is stored. With it that answer is wrong: the caller declared a
    replacement, and reporting the match as the save would leave the target
    served and unmarked. The target itself is excluded from the lookup,
    since a close revision of it is exactly what ``supersedes=`` stores.
    """
    if supersedes is None:
        return
    message = (
        f"content is a near-duplicate ({similarity:.0%}) of {match_id}; nothing "
        f"was written and {supersedes} is unchanged"
    )
    _record_boundary_rejection(
        tool="save_memory",
        rejections=[{"kind": "value", "loc": "content", "msg": message}],
    )
    _raise_invalid_params(
        message,
        data={"field": "content", "supersedes": supersedes, "duplicate_of": match_id},
    )


def _fuzzy_duplicate_response(
    supersedes: str | None, match_id: str, similarity: float
) -> str:
    """Reply to a near-duplicate hit, refusing it first when ``supersedes`` was set.

    With no ``supersedes`` the match is the answer and nothing is stored, so
    the call is recorded as a no-op (:func:`_note_noop_call`): no
    ``MUTATION_EXECUTED`` will stand for it.
    """
    _refuse_near_duplicate_supersedes(supersedes, match_id, similarity)
    _note_noop_call("save_memory", "fuzzy_duplicate")
    return f"Fuzzy duplicate (similarity {similarity:.0%}): {match_id}"


def _supersede_saved_memory(
    registry: StoreRegistry, stored_id: str, supersedes: str | None
) -> str:
    """Stamp ``supersedes`` once a new memory is stored, and word the reply.

    The memory is already persisted, so a failed stamp is a warning rather
    than an error: raising would tell the caller the save failed when it did
    not. The warning says how to finish — a re-send hits the exact-hash
    branch, which stores nothing and retries the stamp.
    """
    if supersedes is None:
        return f"Memory saved: {stored_id}"
    error = _supersession.supersede_document(
        registry,
        doc_id=supersedes,
        successor=stored_id,
        requested_by=SAVE_MEMORY_SURFACE,
        clearance=current_clearance(),
    )
    if error is not None:
        return (
            f"Memory saved: {stored_id}\nWarning: supersession NOT applied — "
            f"{supersedes} is unchanged ({error}). Re-send the same call to "
            "retry; it will not store a duplicate."
        )
    return f"Memory saved (supersedes {supersedes}): {stored_id}"


def _reference_fields(
    metadata: dict[str, Any] | None, summary: str | None, uri: str | None
) -> dict[str, Any] | None:
    """``save_memory``'s metadata with its ``summary`` and ``uri``, refusals recorded.

    Recorded and raised here rather than in :mod:`trellis.mcp.tools.memory_common`,
    so the rejection site carries the literal ``tool=`` the capture-surface roster
    reads, as the empty-content refusal does.
    """
    try:
        return _with_reference_fields(metadata, summary, uri)
    except ReferenceFieldsError as refusal:
        _record_boundary_rejection(
            tool="save_memory",
            rejections=[
                {"kind": "value", "loc": refusal.field, "msg": refusal.message}
            ],
        )
        _raise_invalid_params(
            refusal.message, data={"field": refusal.field, "tool": "save_memory"}
        )


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def save_memory(
    content: str,
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
    supersedes: str | None = None,
    sensitivity: str | None = None,
    summary: str | None = None,
    uri: str | None = None,
) -> str:
    """Store a document in the experience graph memory.

    Deduplicates by content hash: if an identical document already exists,
    returns its id without storing a duplicate. Emits a ``MEMORY_STORED``
    event on new stores so downstream workers (enrichment, promotion) can
    react.

    Args:
        content: Document content to store.
        metadata: Optional metadata (tags, source, domain, etc.). May not
            carry ``lifecycle`` or ``evidence_ref``, which act on other items
            by id (refused, nothing written); use ``supersedes`` to replace a
            document.
        doc_id: Optional document ID. Auto-generated if not provided. May not
            start with ``trace-summary:``, which belongs to the trace embed
            worker (refused, nothing written).
        supersedes: Optional id of an existing whole document this memory
            replaces. It is marked superseded by the saved (or matching)
            memory, so a pack holding both serves only the new one. Refused,
            with nothing written, if the id is unknown, a graph entity, part
            of a chunked document, archived, or already superseded by another
            document.
        sensitivity: Optional label (``public``, ``internal``,
            ``confidential``, ``restricted``). Readers whose key lacks that
            clearance are never served the document, and memory extraction is
            skipped for anything above ``internal``. A key can only label up
            to its own clearance (the ``confidential`` / ``restricted`` scope).
        summary: Optional short summary you wrote of a long ``content``, at most
            8000 characters. It is what is embedded and shown in a pack, and
            ``get_items`` returns the full text. Stores a ``reference`` document.
        uri: Optional link to the original, shown under the summary.
    """
    if not content or not content.strip():
        _record_boundary_rejection(
            tool="save_memory",
            rejections=[{"kind": "empty_required", "loc": "content", "msg": "empty"}],
            payload_chars=0,
        )
        _raise_invalid_params(
            "content must not be empty",
            data={"field": "content"},
        )

    _refuse_memory_pointer_keys(metadata)

    from trellis.core.hashing import content_hash  # noqa: PLC0415

    registry = _get_registry()
    metadata = _stamp_sensitivity(
        _reference_fields(metadata, summary, uri), sensitivity, tool="save_memory"
    )
    executor = build_curate_executor(
        registry, evidence_embed="none", clearance=current_clearance()
    )

    chash = content_hash(content)

    # Model-judged verdict tier (TRELLIS_ENABLE_RECONCILE_ON_WRITE=1). When a
    # near — not exact — match exists, a local model decides
    # ADD/UPDATE/SUPERSEDE/NOOP instead of today's binary keep/drop. Off by
    # default: the deterministic-only path below is unchanged. An explicit
    # supersedes= bypasses it: the caller has already said what this replaces,
    # and the judge's verdict is not consulted over that declaration.
    if reconcile_on_write_enabled(registry) and supersedes is None:
        return _save_memory_reconciled(
            registry,
            executor,
            content,
            metadata,
            doc_id,
            chash,
        )

    # Dedup decision + store, serialized so concurrent http workers can't
    # both pass the checks and both persist. The returns for an existing
    # exact or fuzzy match release the lock on the way out.
    with _save_memory_lock:
        # Dedup stage 1: exact content hash match.
        existing = registry.knowledge.document_store.get_by_hash(chash)
        # A match above the caller's clearance is no match: answering "already
        # exists" would confirm a guess about content they may not read (#194).
        if existing is not None and not _match_readable(registry, existing["doc_id"]):
            existing = None
        # Refused before any write, and checked against the exact match
        # too: that match, not a new row, is then the successor.
        _check_memory_supersedes(registry, supersedes, doc_id=doc_id, existing=existing)
        if existing is not None:
            existing_id = existing["doc_id"]
            _refuse_label_below_request(registry, existing_id, metadata)
            logger.debug(
                "save_memory_dedup_exact", doc_id=existing_id, content_hash=chash
            )
            return _memory_exists_response(registry, existing_id, supersedes)

        # Dedup stage 2: fuzzy MinHash/LSH (catches typos, casing, punctuation).
        try:
            minhash_index = _get_minhash_index(registry)
            if minhash_index is not None:
                match = minhash_index.find_duplicate(
                    content,
                    exclude_ids={supersedes} if supersedes is not None else None,
                )
                if match is not None and not _match_readable(registry, match[0]):
                    match = None
                if match is not None:
                    match_id, similarity = match
                    _refuse_label_below_request(registry, match_id, metadata)
                    logger.debug(
                        "save_memory_dedup_fuzzy",
                        match_id=match_id,
                        similarity=round(similarity, 3),
                    )
                    return _fuzzy_duplicate_response(supersedes, match_id, similarity)
        except MCPError:
            # _get_minhash_index already wrapped the cause structurally.
            raise
        except Exception as exc:
            logger.exception("save_memory_minhash_failed")
            detail = _exception_detail(exc)
            _raise_internal(
                f"fuzzy dedup query failed: {detail}",
                cause=exc,
                data={"stage": "minhash_find"},
            )

        stored_id, metadata = _store_new_memory(
            registry,
            executor,
            registry.knowledge.document_store,
            doc_id,
            content,
            metadata,
        )

        # Add to MinHash index for future fuzzy dedup. The write already
        # succeeded; an index-add failure means future calls won't see this
        # doc in fuzzy lookups but the doc itself is persisted — surface as
        # a real error so operators can diagnose the dedup drift.
        try:
            minhash_index = _get_minhash_index(registry)
            if minhash_index is not None:
                minhash_index.add(stored_id, content)
        except MCPError:
            raise
        except Exception as exc:
            logger.exception("save_memory_minhash_index_add_failed", doc_id=stored_id)
            detail = _exception_detail(exc)
            _raise_internal(
                f"failed to index stored memory for fuzzy dedup: {detail}",
                cause=exc,
                data={"stage": "minhash_add", "doc_id": stored_id},
            )

        # Under the lock, so no concurrent save_memory can pass the checks
        # against a target this call is about to stamp.
        reply = _supersede_saved_memory(registry, stored_id, supersedes)

    return _append_note(
        reply,
        _emit_memory_stored_and_enrich(registry, stored_id, content, metadata, chash),
    )


# ---------------------------------------------------------------------------
# save_memory — model-judged reconcile-on-write verdict tier (#263)
# ---------------------------------------------------------------------------
#
# Layered on top of the deterministic tier (exact-hash → MinHash near-dup,
# serialized by ``_save_memory_lock``). Feature-flagged
# (TRELLIS_ENABLE_RECONCILE_ON_WRITE); off by default. The lock discipline is
# the whole point: an 8B verdict takes seconds, so the model call happens
# OUTSIDE the lock, and preconditions are re-verified UNDER the lock before the
# verdict commits (the world may have changed while the model thought).


def _index_stored_memory(registry: StoreRegistry, stored_id: str, content: str) -> None:
    """Add a freshly stored doc to the MinHash index (loud on failure)."""
    try:
        minhash_index = _get_minhash_index(registry)
        if minhash_index is not None:
            minhash_index.add(stored_id, content)
    except MCPError:
        raise
    except Exception as exc:
        logger.exception("save_memory_minhash_index_add_failed", doc_id=stored_id)
        detail = _exception_detail(exc)
        _raise_internal(
            f"failed to index stored memory for fuzzy dedup: {detail}",
            cause=exc,
            data={"stage": "minhash_add", "doc_id": stored_id},
        )


def _store_new_memory(
    registry: StoreRegistry,
    executor: MutationExecutor,
    document_store: Any,
    doc_id: str | None,
    content: str,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Persist a new memory doc and index it. Runs under ``_save_memory_lock``.

    Returns ``(stored_id, stored_metadata)``. The metadata comes back because
    classify-on-write may have added tags to it, and the post-store tail
    (MEMORY_STORED payload, embed hook) must see what was actually persisted —
    every reconcile verdict that stores a doc funnels through here.

    A caller-chosen ``doc_id`` that already names a document is refused when
    that document's label is above the caller's clearance or above the label
    being written (#194): the put replaces the row, metadata included, so it
    would destroy content the caller may not read and publish its chunks.
    """
    if doc_id is not None:
        try:
            refuse_overwrite(
                document_store,
                doc_id,
                clearance=current_clearance(),
                metadata=metadata,
            )
        except OverwriteRefusedError as exc:
            _raise_invalid_params(exc.message, data={"field": "doc_id"})
    command = build_evidence_ingest_command(
        doc_id=doc_id,
        content=content,
        metadata=metadata,
        requested_by=SAVE_MEMORY_SURFACE,
        embed_mode="none",
        # Dedup/repair is state-based under _save_memory_lock. A historical
        # audit key must not suppress rebuilding a missing explicit doc_id.
        derive_idempotency=False,
    )
    try:
        result = executor.execute(command)
    # The arm named the Stage 5 audit emit because that was the only thing
    # that could escape ``execute`` (#551). It no longer can — the emit is
    # guarded and degrades onto ``CommandResult.warnings`` — so the one
    # cause this message used to name is now the one cause it cannot have.
    # Whatever still escapes is a bug in the pipeline, not an emit, and is
    # reported as itself. ``evidence_ingest`` is this function's existing
    # stage name rather than a second word for the same place (#325/#326).
    except Exception as exc:
        logger.exception(
            "memory_governed_write_failed",
            doc_id=command.target_id,
        )
        detail = _exception_detail(exc)
        _raise_internal(
            f"governed memory write failed: {detail}",
            cause=exc,
            data={
                "stage": "evidence_ingest",
                "doc_id": command.target_id,
                "error_class": type(exc).__name__,
            },
        )
    if result.status is not CommandStatus.SUCCESS or result.created_id is None:
        _raise_mutation_failed(
            f"failed to store memory: {_result_message(result)}",
            data={
                "status": result.status.value,
                "command_id": result.command_id,
                "message": _result_message(result),
            },
        )
    stored_id = result.created_id
    stored = document_store.get(stored_id)
    if stored is None:
        _raise_internal(
            f"governed memory write returned missing document: {stored_id}",
            data={"stage": "evidence_ingest", "doc_id": stored_id},
        )
    stored_metadata = dict(stored.get("metadata") or {})
    _index_stored_memory(registry, stored_id, content)
    return stored_id, stored_metadata


def _gather_reconcile_candidate(
    registry: StoreRegistry, document_store: Any, content: str
) -> ReconcileCandidate | None:
    """Return the top MinHash near-match to adjudicate, or ``None`` if clean.

    Runs under ``_save_memory_lock`` — the same near-dup scan the deterministic
    tier does, but it hands the match to the model instead of auto-dropping it.
    """
    minhash_index = _get_minhash_index(registry)
    if minhash_index is None:
        return None
    matches = minhash_index.query(content)
    if not matches:
        return None
    match_id, similarity = matches[0]
    doc = document_store.get(match_id)
    if doc is None:
        return None
    if not candidate_is_cleared(document_store, match_id, doc, current_clearance()):
        return None
    return ReconcileCandidate(
        doc_id=match_id, content=doc["content"], similarity=float(similarity)
    )


def _reconcile_egress_permits(
    registry: StoreRegistry, metadata: dict[str, Any], candidate: ReconcileCandidate
) -> bool:
    """Whether the incoming memory and the stored candidate may both be sent.

    The judge is shown both texts, so both labels count: the incoming
    memory's own, and the candidate's effective one (a chunk takes its
    parent's). A candidate that cannot be read back is unreadable, so it is
    withheld rather than sent.
    """
    document_store = registry.knowledge.document_store
    gate = EgressGate.for_route(registry, LLMConsumer.RECONCILE, document_store)
    if gate.local:
        return True
    if not gate.permits_metadata(metadata):
        return False
    try:
        stored = document_store.get(candidate.doc_id)
    except Exception:
        # GRACEFUL-DEGRADATION: fail closed. A candidate whose label cannot
        # be read is not sent; the memory is stored as a plain ADD.
        return False
    if stored is None:
        return False
    return gate.permits(candidate.doc_id, stored.get("metadata"))


def _compute_reconcile_outcome(
    registry: StoreRegistry,
    content: str,
    candidate: ReconcileCandidate,
    *,
    metadata: dict[str, Any],
) -> ReconcileOutcome:
    """Build the client and compute the verdict — **outside** the lock.

    The model is never a hard dependency: an unbuildable / unavailable client
    yields a fallback ADD outcome rather than raising. So does a pair the
    reconcile route may not carry (:func:`_reconcile_egress_permits`), before
    any client is built.
    """
    model_id = configured_model_id(registry)
    if not _reconcile_egress_permits(registry, metadata, candidate):
        logger.info("reconcile_egress_withheld", candidate_id=candidate.doc_id)
        return ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=0.0,
            model_id=model_id,
            fallback=True,
            fallback_reason=ReconcileFallbackReason.EGRESS_WITHHELD,
        )
    try:
        client = _build_llm_client(registry, LLMConsumer.RECONCILE)
    except Exception:
        # A misconfigured / uninstalled provider must not fail a capture —
        # the memory is saved as a plain ADD, marked for a later sweep.
        logger.warning("reconcile_client_build_failed")
        client = None
    if client is None:
        logger.info("reconcile_model_unavailable")
        return ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=0.0,
            model_id=model_id,
            fallback=True,
            fallback_reason=ReconcileFallbackReason.MODEL_UNAVAILABLE,
        )
    return judge_reconcile(
        client,
        new_content=content,
        candidate=candidate,
        timeout=reconcile_timeout_seconds(registry),
        model_id=model_id,
    )


def _candidate_is_stale(document_store: Any, candidate: ReconcileCandidate) -> bool:
    """True when the candidate no longer matches what the model judged.

    Two staleness axes, both re-checked under the lock:

    * **Content** — the candidate was edited or deleted while the model
      thought; the verdict is about text that no longer exists.
    * **Lifecycle** — the candidate was superseded/deprecated by a concurrent
      writer. ``mark_document_superseded`` preserves content (SCD-2), so a
      content-hash check alone would let two racing SUPERSEDE verdicts both
      commit and fork the supersession chain — ``superseded_by`` can only name
      one successor. Any real non-``current`` lifecycle state fails re-verify;
      one that is missing, empty, non-string or not a ``Lifecycle`` state is
      malformed and reads as current (:func:`lifecycle_state_of`).
    """
    from trellis.core.hashing import content_hash  # noqa: PLC0415

    current = document_store.get(candidate.doc_id)
    if current is None:
        return True
    if content_hash(current["content"]) != content_hash(candidate.content):
        return True
    # A missing, empty, non-string or unknown state is malformed, not a state of
    # its own, and reads as current here as it does in the precedent handlers
    # and the retrieval gates: a bad record is never by itself a reason to refuse.
    return lifecycle_state_of(current.get("metadata")) != "current"


def _reverify_candidate(
    document_store: Any,
    candidate: ReconcileCandidate,
    outcome: ReconcileOutcome,
) -> ReconcileOutcome:
    """Re-check the candidate under the lock; downgrade to ADD if it changed.

    Another writer may have edited, deleted, or (lifecycle axis) superseded
    the candidate while the model was thinking. Applying a stale verdict
    would act on a doc that no longer is what the model judged, so we
    downgrade to a plain ADD marked ``stale_recheck`` (data is never lost;
    no synchronous re-judge — the marker queues it for a later sweep).
    """
    if outcome.fallback:
        return outcome  # already an ADD — nothing to re-verify against
    if _candidate_is_stale(document_store, candidate):
        logger.info(
            "reconcile_candidate_changed_downgrade_to_add",
            candidate_id=candidate.doc_id,
        )
        return ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=outcome.confidence,
            model_id=outcome.model_id,
            fallback=True,
            fallback_reason=ReconcileFallbackReason.STALE_RECHECK,
        )
    return outcome


def _commit_reconcile_verdict(
    registry: StoreRegistry,
    executor: MutationExecutor,
    document_store: Any,
    doc_id: str | None,
    content: str,
    metadata: dict[str, Any],
    candidate: ReconcileCandidate,
    outcome: ReconcileOutcome,
) -> tuple[str | None, dict[str, Any] | None, ReconcileOutcome]:
    """Apply the verdict under ``_save_memory_lock``.

    Returns ``(stored_id, stored_metadata, applied_outcome)`` —
    ``(None, None, outcome)`` for NOOP, which stores nothing. SUPERSEDE rides
    SCD-2: a new doc plus Lifecycle stale-marking of the old, never a delete.

    The outcome comes back because it can be *downgraded* here: a SUPERSEDE
    whose target vanished, or whose target is a promoted precedent's row,
    did not supersede anything, and the caller renders the tool result and
    decides whether to emit ``MEMORY_OP_JUDGED`` from it. Returning the input
    outcome unchanged would put the caller back in the position #407
    describes — asserting an effect it never checked.
    """
    if outcome.fallback:
        meta = {
            **metadata,
            RECONCILIATION_KEY: marker_for_fallback(outcome.fallback_reason),
        }
        stored = _store_new_memory(
            registry, executor, document_store, doc_id, content, meta
        )
        return stored[0], stored[1], outcome

    decision = outcome.decision
    if decision == ReconcileDecision.NOOP:
        return None, None, outcome
    if decision == ReconcileDecision.UPDATE:
        meta = {
            **metadata,
            RECONCILIATION_KEY: ReconcileDecision.UPDATE.value,
            UPDATES_DOC_KEY: candidate.doc_id,
        }
        stored = _store_new_memory(
            registry, executor, document_store, doc_id, content, meta
        )
        return stored[0], stored[1], outcome
    if decision == ReconcileDecision.SUPERSEDE:
        meta = {
            **metadata,
            RECONCILIATION_KEY: ReconcileDecision.SUPERSEDE.value,
            SUPERSEDES_DOC_KEY: candidate.doc_id,
        }
        stored_id, stored_meta = _store_new_memory(
            registry, executor, document_store, doc_id, content, meta
        )
        vector_store = resolve_vector_store(registry)
        refusal = mark_document_superseded(
            document_store,
            old_doc_id=candidate.doc_id,
            new_doc_id=stored_id,
            vector_store=vector_store,
        )
        if refusal is None:
            return stored_id, stored_meta, outcome
        # The supersession was not applied, for one of two reasons the return
        # names. ``STALE_RECHECK``: the target vanished between the under-lock
        # re-verify and this write (``_save_memory_lock`` is process-local, so
        # another process can delete it). ``TARGET_PROTECTED``: the target is
        # a promoted precedent's row or a redaction tombstone, which
        # reconcile never stamps (#1039). Either
        # way the memory is stored and the supersession is not — so strip the
        # claim from what was just written, mark it with the reason, and hand
        # the caller an outcome downgraded to that same reason, so the marker
        # and the ``RECONCILE_DEGRADED`` event agree. The write is
        # metadata-only, hence ``preserve_updated_at`` (#397).
        meta = {k: v for k, v in stored_meta.items() if k != SUPERSEDES_DOC_KEY}
        meta[RECONCILIATION_KEY] = marker_for_fallback(refusal)
        put_document(
            document_store,
            vector_store,
            stored_id,
            content,
            meta,
            preserve_updated_at=True,
        )
        return stored_id, meta, _downgraded_unapplied(outcome, refusal)
    # ADD
    meta = {**metadata, RECONCILIATION_KEY: ReconcileDecision.ADD.value}
    stored = _store_new_memory(
        registry, executor, document_store, doc_id, content, meta
    )
    return stored[0], stored[1], outcome


def _downgraded_unapplied(
    outcome: ReconcileOutcome, reason: ReconcileFallbackReason
) -> ReconcileOutcome:
    """Mark a verdict as not applied: a fallback ADD carrying *reason*.

    ``fallback=True`` is what suppresses the ``MEMORY_OP_JUDGED`` emit and
    makes :func:`_reconcile_result_message` render a plain save, which is the
    whole point — the model judged, but nothing was superseded, so the join
    key would point at a document that was never stamped, or no longer exists.
    *reason* is what ``mark_document_superseded`` refused with, so the
    ``RECONCILE_DEGRADED`` event says which of the two it was.
    """
    return ReconcileOutcome(
        decision=ReconcileDecision.ADD,
        confidence=outcome.confidence,
        model_id=outcome.model_id,
        fallback=True,
        fallback_reason=reason,
    )


def _reconcile_subject(
    decision: ReconcileDecision, candidate: ReconcileCandidate, stored_id: str | None
) -> tuple[str, str]:
    """Pick the ``subject_ref`` the verdict is *about* (the feedback join key).

    ADD is about the new memory; UPDATE / SUPERSEDE / NOOP are about the
    pre-existing candidate that triggered the reconciliation.
    """
    if decision == ReconcileDecision.ADD and stored_id is not None:
        return REF_TYPE_DOCUMENT, stored_id
    return REF_TYPE_DOCUMENT, candidate.doc_id


def _reconcile_result_message(
    outcome: ReconcileOutcome, candidate: ReconcileCandidate, stored_id: str | None
) -> str:
    """Human-readable tool result for a reconciled write."""
    if outcome.fallback or outcome.decision == ReconcileDecision.ADD:
        return f"Memory saved: {stored_id}"
    if outcome.decision == ReconcileDecision.NOOP:
        return f"Memory already covered by: {candidate.doc_id}"
    if outcome.decision == ReconcileDecision.UPDATE:
        return f"Memory saved (update of {candidate.doc_id}): {stored_id}"
    return f"Memory saved (supersedes {candidate.doc_id}): {stored_id}"


def _save_memory_reconciled(
    registry: StoreRegistry,
    executor: MutationExecutor,
    content: str,
    metadata: dict[str, Any],
    doc_id: str | None,
    chash: str,
) -> str:
    """Reconcile-on-write path: deterministic short-circuit + model verdict.

    Three phases, per the binding guide's lock discipline:
      A. under the lock — exact-hash short-circuit; gather the near-dup
         candidate; a *clean* ADD (no candidate) stores immediately;
      B. outside the lock — the (slow) model verdict;
      C. under the lock — re-verify the candidate, then commit the verdict.
    """
    document_store = registry.knowledge.document_store

    # -- Phase A: gather under the lock --------------------------------------
    with _save_memory_lock:
        existing = document_store.get_by_hash(chash)
        if existing is not None and not _match_readable(registry, existing["doc_id"]):
            existing = None
        if existing is not None:
            existing_id = existing["doc_id"]
            _refuse_label_below_request(registry, existing_id, metadata)
            logger.debug(
                "save_memory_dedup_exact", doc_id=existing_id, content_hash=chash
            )
            _note_noop_call("save_memory", "exact_duplicate")
            return f"Memory already exists: {existing_id}"

        candidate = _gather_reconcile_candidate(registry, document_store, content)
        if candidate is not None:
            _refuse_label_below_request(registry, candidate.doc_id, metadata)
        if candidate is None:
            # No near match: an unambiguous ADD — no model call, no verdict.
            # Store under the lock exactly as the deterministic tier would.
            clean_id, clean_meta = _store_new_memory(
                registry,
                executor,
                document_store,
                doc_id,
                content,
                metadata,
            )

    if candidate is None:
        note = _emit_memory_stored_and_enrich(
            registry, clean_id, content, clean_meta, chash
        )
        return (
            f"Memory saved: {clean_id}\n{note}" if note else f"Memory saved: {clean_id}"
        )

    # -- Phase B: verdict OUTSIDE the lock (never serialize saves on a model) -
    # _compute_reconcile_outcome is internally exhaustive (client build, model
    # transport, timeout, malformed JSON all resolve to fallback ADDs), but
    # "capture never fails because a judge failed" must be total, not
    # near-total — a belt-and-suspenders guard turns any escape into the same
    # offline fallback. Fail-soft, never silent: the traceback goes to stderr.
    try:
        outcome = _compute_reconcile_outcome(
            registry, content, candidate, metadata=metadata
        )
    except Exception:
        logger.exception("reconcile_phase_b_failed", candidate_id=candidate.doc_id)
        outcome = ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=0.0,
            model_id=configured_model_id(registry),
            fallback=True,
            fallback_reason=ReconcileFallbackReason.JUDGE_ERROR,
        )

    # -- Phase C: re-verify + commit under the lock --------------------------
    with _save_memory_lock:
        raced = document_store.get_by_hash(chash)
        if raced is not None:
            # A concurrent writer stored identical content while we judged —
            # the deterministic dedup wins; drop our now-duplicate write.
            _note_noop_call("save_memory", "exact_duplicate")
            return f"Memory already exists: {raced['doc_id']}"
        outcome = _reverify_candidate(document_store, candidate, outcome)
        stored_id, stored_meta, outcome = _commit_reconcile_verdict(
            registry,
            executor,
            document_store,
            doc_id,
            content,
            metadata,
            candidate,
            outcome,
        )

    # -- Emit + enrich outside the lock --------------------------------------
    # A genuine model verdict (not a fallback / unapplied downgrade) is a training
    # pair: emit MEMORY_OP_JUDGED. Fallback ADDs judged nothing, so they are
    # kept out of that stream — and emit RECONCILE_DEGRADED instead, so the
    # fallback leaves a countable trace rather than a log line nothing reads
    # (#514). Every fallback route funnels through this one branch, which is
    # why the emit lives here and not at each of the four mint sites.
    subject_type, subject_id = _reconcile_subject(
        outcome.decision, candidate, stored_id
    )
    if not outcome.fallback:
        emit_reconcile_verdict(
            registry.operational.event_log,
            outcome=outcome,
            new_content=content,
            candidate=candidate,
            subject_ref_type=subject_type,
            subject_ref_id=subject_id,
        )
    else:
        emit_reconcile_degraded(
            registry.operational.event_log,
            outcome=outcome,
            subject_ref_type=subject_type,
            subject_ref_id=subject_id,
        )

    # Everything but NOOP persisted a doc → MEMORY_STORED + enrichment.
    enrich_note: str | None = None
    if stored_id is not None:
        enrich_note = _emit_memory_stored_and_enrich(
            registry, stored_id, content, stored_meta or metadata, chash
        )

    if not outcome.fallback and outcome.decision == ReconcileDecision.NOOP:
        # The judge found the content already covered and nothing was
        # written, so no ``MUTATION_EXECUTED`` stands for this call.
        _note_noop_call("save_memory", "judged_covered")
    message = _reconcile_result_message(outcome, candidate, stored_id)
    return f"{message}\n{enrich_note}" if enrich_note else message
