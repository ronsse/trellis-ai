"""Ingest routes -- traces, evidence, vectors, and bulk (entities+edges+aliases)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from trellis.auth import SCOPE_ADMIN, SCOPE_MUTATE, scopes_satisfy
from trellis.core.error_sanitize import (
    describe_validation_error,
    sanitize_error_message,
)
from trellis.core.ids import generate_ulid
from trellis.core.vector_metadata import MIRRORED_METADATA_KEYS
from trellis.extract.trace_ingest_hook import (
    run_trace_extraction,
    trace_already_ingested,
)
from trellis.ingest_corpus.models import chunk_parent_id
from trellis.mutate import (
    build_curate_executor,
    build_evidence_ingest_command,
    requires_admin_scope,
)
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandStatus,
    Operation,
)
from trellis.retrieve.lifecycle import EVIDENCE_REF_KEY
from trellis.retrieve.strategies import DEFAULT_IMPORTANCE_DECAY_THRESHOLD
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.schemas.evidence import Evidence
from trellis.schemas.trace import Trace
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
from trellis_api.routes._principal import (
    refuse_document_overwrite,
    refuse_vector_write,
    request_clearance,
    resolve_requested_by,
    stamp_request_sensitivity,
)
from trellis_wire.dtos import (
    BulkAliasItem,
    BulkEdgeItem,
    BulkEntityItem,
    BulkGroupResult,
    BulkIngestRequest,
    BulkIngestResponse,
    BulkItemResult,
    DefaultValidationError,
    IngestResponse,
    StringDetailError,
)
from trellis_workers.trace_embed.render import DOC_ID_PREFIX as TRACE_SUMMARY_ID_PREFIX

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore

#: Every hand-raised 422 in this module carries a plain-string ``detail``
#: (describing the bad trace/evidence/vectors body), not FastAPI's own
#: default list-of-field-errors shape -- but each route's body param is
#: typed ``dict[str, Any]``, so FastAPI's own validation can still fail
#: with that default shape first (e.g. a JSON body that isn't an object
#: at all). Declared once and reused on every route below.
_BODY_422 = {
    "description": (
        "The body isn't a JSON object at all (DefaultValidationError -- "
        "``detail`` is a list of field errors), or it is one but fails "
        "the route's own schema validation (StringDetailError -- "
        "``detail`` is a plain string naming the problem)."
    ),
    "model": DefaultValidationError | StringDetailError,
}

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/traces", response_model=IngestResponse, responses={422: _BODY_422})
def ingest_trace(
    body: dict[str, Any],
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> IngestResponse:
    """Ingest a trace through the governed mutation pipeline."""
    try:
        trace = Trace.model_validate(body)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid trace: {describe_validation_error(exc)}",
        ) from exc

    registry = get_registry()
    executor = build_curate_executor(registry, clearance=request_clearance(ctx))
    # Read before the write: the handler answers a stored trace_id as a
    # success that stores nothing, and only its message says which.
    already_ingested = trace_already_ingested(registry, trace.trace_id)
    result = executor.execute(
        Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": trace},
            target_id=trace.trace_id,
            target_type="trace",
            requested_by="api:ingest-trace",
        )
    )
    if result.status == CommandStatus.REJECTED:
        # A refusal stored nothing, so it is neither extracted nor
        # answered "ok". 400, as on /evidence and the curate routes.
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(result.message)
        )
    if result.status == CommandStatus.FAILED:
        # The handler raised, e.g. StoreError from append. A duplicate
        # trace_id never lands here: the handler answers it as success.
        raise HTTPException(
            status_code=409, detail=sanitize_error_message(result.message)
        )

    # Feature-flagged post-ingest trace->graph extraction
    # (TRELLIS_ENABLE_TRACE_EXTRACTION=1). Runs after the trace is durably
    # stored, and not when the read above found the trace_id stored: a
    # duplicate's agent and artifacts would land on the stored trace's node.
    # Fail-soft inside the hook so it never fails the request.
    if not already_ingested:
        run_trace_extraction(registry, trace, requested_by="api:ingest-trace")

    return IngestResponse(
        trace_id=result.created_id or trace.trace_id,
        already_ingested=already_ingested,
    )


@router.post("/evidence", response_model=IngestResponse, responses={422: _BODY_422})
def ingest_evidence(
    body: dict[str, Any],
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> IngestResponse:
    """Ingest evidence.

    An ``evidence_id`` that already names a labelled document is refused (409):
    the write carries no label, so it would replace the row and downgrade it.

    An optional ``sensitivity`` labels the stored document. The key needs that
    clearance itself (403 otherwise), and an unknown label is a 422.

    An optional boolean ``reingest_redacted`` says the write is meant to bring a
    redacted ``evidence_id`` back (#1034). Without it a write to one is refused
    (400) and the row stays blank; with it the row is written and stamped
    ``reingested_after_redaction``. Setting it needs the ``mutate`` scope as
    well as ``ingest`` (403 otherwise, nothing written), the bar the command
    route already sets, because undoing a redaction is not an ingest. It
    changes nothing for an id that was not redacted, and it is no way past the
    clearance rules above: a key that may not read a labelled document still
    cannot overwrite it (409). A value that is not a boolean is a 422. A reused
    ``idempotency_key`` answers as the write it first named, so a flagged retry
    needs a fresh one.
    """
    fields = dict(body)
    sensitivity = fields.pop("sensitivity", None)
    reingest_redacted = fields.pop("reingest_redacted", False)
    if not isinstance(reingest_redacted, bool):
        raise HTTPException(
            status_code=422, detail="reingest_redacted must be a boolean"
        )
    if reingest_redacted and not scopes_satisfy(ctx.scopes, SCOPE_MUTATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reingest_redacted requires the mutate scope; nothing was written",
        )
    try:
        evidence = Evidence.model_validate(fields)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid evidence: {describe_validation_error(exc)}",
        ) from exc

    registry = get_registry()
    evidence_metadata: dict[str, Any] = {
        "evidence_type": evidence.evidence_type,
        "source_origin": evidence.source_origin,
    }
    # The body's own ``metadata`` is never stored here, but a raw label in it is
    # refused all the same: one rule, whichever key a caller reaches for.
    if isinstance(fields.get("metadata"), dict):
        stamp_request_sensitivity(fields["metadata"], None, ctx=ctx)
    if sensitivity is not None and not isinstance(sensitivity, str):
        raise HTTPException(status_code=422, detail="sensitivity must be a string")
    evidence_metadata = stamp_request_sensitivity(
        evidence_metadata, sensitivity, ctx=ctx
    )
    refuse_document_overwrite(
        registry.knowledge.document_store,
        evidence.evidence_id,
        ctx=ctx,
        metadata=evidence_metadata,
    )
    command = build_evidence_ingest_command(
        doc_id=evidence.evidence_id,
        content=evidence.content or "",
        uri=evidence.uri,
        metadata=evidence_metadata,
        reingest_redacted=reingest_redacted,
        requested_by="api:ingest-evidence",
    )
    result = build_curate_executor(registry, clearance=request_clearance(ctx)).execute(
        command
    )
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(result.message)
        )
    if (
        result.status is CommandStatus.DUPLICATE
        and registry.knowledge.document_store.get(evidence.evidence_id) is None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate mutation references missing document: "
                f"{evidence.evidence_id}"
            ),
        )

    return IngestResponse(evidence_id=evidence.evidence_id)


# ── Vector batch upsert ─────────────────────────────────────────────────


#: ``metadata`` key naming a chunk's parent document. Not mirrored from the
#: document, but :func:`trellis.retrieve.concentration.parent_id_of` and the
#: serve-time sensitivity judgement of a chunk both read it.
_PARENT_DOC_ID_KEY = "parent_doc_id"

#: Row ``metadata`` keys that act on an item other than the row. The pack
#: builder withholds *by id*: a ``lifecycle`` that says ``superseded`` withholds
#: every item holding the row's id, and an ``evidence_ref`` makes the row the
#: successor that another item's ``superseded_by`` resolves to. A row that carries
#: either at an id naming no stored document could hide a real item from another
#: axis, so it is refused there; at a stored document the document's value is
#: written instead of the caller's.
_ITEM_POINTER_KEYS: tuple[str, ...] = (LIFECYCLE_KEY, EVIDENCE_REF_KEY)

#: The largest finite float32. A vector element past it becomes ``inf`` when the
#: store keeps it as float32.
_FLOAT32_MAX = 3.4028234663852886e38

#: The largest squared length a vector may have. The store takes the float32
#: norm of every row it compares against; the sum of squares overflows float32
#: at about 3.4e38, which makes the norm ``inf`` and the cosine score ``nan`` or a
#: silent 0. The bound sits well under that so accumulation error cannot cross it.
_MAX_SQUARED_LENGTH = 1e38


def _vector_row_metadata(
    document_store: DocumentStore, item_id: str, supplied: dict[str, Any]
) -> dict[str, Any]:
    """The metadata to store on ``item_id``'s vector row, agreeing with its document.

    A row's mirrored keys (:data:`MIRRORED_METADATA_KEYS`: the label,
    ``lifecycle``, ``content_tags``, ``auto_importance``) are the document's, not
    the caller's: the semantic axis builds its pack item, excerpt included, from
    this row, so a rewrite that dropped the label would serve a restricted
    excerpt to a lower clearance. ``evidence_ref`` is taken from the document the
    same way, since it names a successor to the pack builder. The rule is
    ``sync_vector_metadata_outcome``'s, applied before the one write instead of
    after it, so the row this route writes is never stripped: a key the document
    carries is copied, a key it lacks is removed from the row. (A relabel whose
    own vector sync runs between the read here and the write is overwritten by
    this write, and the row stays stale until a later sync or
    ``trellis admin resync-vector-metadata``.)

    A chunk id is mirrored from its own stored document, which is what the chunk
    embed path does; its parent's label is judged at serve time through the id.
    ``parent_doc_id`` is not mirrored but it is not the caller's either: serving
    trusts it before the id prefix, so for a chunk id it is set to the chunk
    document's own, else to the parent the id names, whatever was supplied. A
    non-chunk id keeps what was supplied. An id that names no document is
    otherwise written as supplied, except that the keys in
    :data:`_ITEM_POINTER_KEYS` are dropped: the route has already refused them
    there, so they can only be present if the document was deleted between its
    first-pass read and this one.
    """
    row = dict(supplied)
    stored = document_store.get(item_id)
    source = stored.get("metadata") if stored is not None else None
    if not isinstance(source, dict):
        source = {}
    if stored is not None:
        for key in (*MIRRORED_METADATA_KEYS, EVIDENCE_REF_KEY):
            if key in source:
                row[key] = source[key]
            else:
                row.pop(key, None)
    else:
        # The first pass refused these keys only if no document was there then.
        # One deleted since would let the caller's through, so they are dropped
        # here (silently: the batch has already been judged).
        for key in _ITEM_POINTER_KEYS:
            row.pop(key, None)
    id_parent = chunk_parent_id(item_id)
    if id_parent is not None:
        own_parent = source.get(_PARENT_DOC_ID_KEY)
        row[_PARENT_DOC_ID_KEY] = (
            own_parent if isinstance(own_parent, str) and own_parent else id_parent
        )
    return row


def _vector_width(vectors: list[Any], declared: Any) -> tuple[int, str] | None:
    """The width every vector in the batch must have, and whose it is.

    The store's own when it declares one (``VectorStore.dimensions``; the SQLite
    backend pins none). Otherwise the first non-empty vector in the batch: rows
    of different widths cannot be compared with each other, so at least the
    batch has to agree.
    """
    if isinstance(declared, int) and not isinstance(declared, bool):
        return declared, "the store's width"
    for index, item in enumerate(vectors):
        vector = item.get("vector") if isinstance(item, dict) else None
        if isinstance(vector, list) and vector:
            return len(vector), f"the width of vectors[{index}].vector"
    return None


def _vector_element_problem(position: int, value: Any) -> str | None:
    """Why one vector element is refused, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"vector[{position}] must be a number"
    # NaN, the infinities and anything past float32 all fail this one
    # comparison, which also holds for an int too large for a float.
    if not abs(value) <= _FLOAT32_MAX:
        return f"vector[{position}] must be a finite number within float32 range"
    return None


def _vector_problem(vector: Any, width: tuple[int, str] | None) -> str | None:
    """Why ``vector`` would break a semantic read, or ``None``.

    A missing, ``null`` or empty-list vector is not judged: the second pass counts
    it in ``errors`` and stores nothing. Anything else must be a list of finite
    numbers (never a bool) that fit float32, whose squared length stays inside
    float32 so the store's norm is finite, whose length is ``width``'s, and whose
    float32 norm is not zero (the SQLite store skips such a row, so it is never
    served).
    """
    if vector is None or vector == []:
        return None
    if not isinstance(vector, list):
        return "vector must be a list of numbers"
    squared = 0.0
    for position, value in enumerate(vector):
        problem = _vector_element_problem(position, value)
        if problem is not None:
            return problem
        squared += float(value) ** 2
    if squared > _MAX_SQUARED_LENGTH:
        return "vector is too large to compare: its squared length overflows float32"
    if width is not None and len(vector) != width[0]:
        return f"vector has {len(vector)} dimensions, expected {width[0]} ({width[1]})"
    # The store's own arithmetic (``SQLiteVectorStore.query``): all zeros, or so
    # small the float32 sum of squares underflows, and the row is skipped.
    if float(np.linalg.norm(np.array(vector, dtype=np.float32))) == 0.0:
        return (
            "vector has no length: every element is zero or too small for "
            "float32, and the store never serves such a row"
        )
    return None


def _item_pointer_problem(
    document_store: DocumentStore, item_id: Any, metadata: dict[str, Any]
) -> str | None:
    """Why a row at an id naming no document may not carry a pointer key, or ``None``.

    Phrased from the ``metadata`` key, like :func:`_vector_metadata_problem`.
    Reads the document only for a row that carries one of
    :data:`_ITEM_POINTER_KEYS`, so an ordinary row costs no read here.
    """
    key = next((k for k in _ITEM_POINTER_KEYS if k in metadata), None)
    if key is None or not item_id or document_store.get(item_id) is not None:
        return None
    return (
        f"{key} needs item_id to name a stored document: on any other id it "
        "would act on whatever item holds that id in a pack"
    )


def _vector_item_problem(
    item: dict[str, Any],
    document_store: DocumentStore,
    width: tuple[int, str] | None,
) -> str | None:
    """Why one batch row is a 422, phrased from the row's own field, or ``None``.

    ``item_id`` that is present must be a string (one that is absent, ``null`` or
    empty is a row with no id, which the second pass counts), ``metadata`` must be
    an object or ``null``, and then the reserved-id, metadata, pointer-key and
    vector checks run. Everything here is judged on what the caller sent.
    """
    item_id = item.get("item_id")
    if item_id is not None and not isinstance(item_id, str):
        return "item_id must be a string"
    metadata = item.get("metadata")
    # ``null`` is stored as ``{}``; anything else that is not an object would
    # be stored as sent and then be dropped from (or break) every semantic read.
    if metadata is not None and not isinstance(metadata, dict):
        return "metadata must be an object"
    if isinstance(item_id, str) and item_id.startswith(TRACE_SUMMARY_ID_PREFIX):
        return (
            f"item_id may not start with {TRACE_SUMMARY_ID_PREFIX!r}: that "
            "namespace is the trace-embed worker's"
        )
    if metadata is not None:
        problem = _vector_metadata_problem(metadata)
        if problem is None:
            problem = _item_pointer_problem(document_store, item_id, metadata)
        if problem is not None:
            return f"metadata.{problem}"
    return _vector_problem(item.get("vector"), width)


def _vector_metadata_problem(metadata: dict[str, Any]) -> str | None:
    """Why a supplied vector-row ``metadata`` would not survive a semantic read.

    Returns ``None`` for a row that would. Since the semantic axis skips and logs
    a row it cannot read (``semantic_row_skipped_malformed_metadata``) instead of
    raising, such a row would be stored, accepted with a 200, and then never
    served; refusing it here tells the caller. Judged on what the caller sent,
    whether or not the row's own document will overwrite the key
    (``auto_importance`` is mirrored), so the answer does not depend on the
    document store. Mirrors what ``SemanticSearch`` and ``_apply_importance``
    demand of a row: ``content`` and ``excerpt`` strings
    (serving reads ``content``, else ``excerpt``, and truncates it), and
    ``auto_importance`` a finite number (never a bool), with a stamp for a score at
    or above the decay threshold, in ``content_tags`` or at the top level.
    """
    for key in ("content", "excerpt"):
        if key in metadata and not isinstance(metadata[key], str):
            return f"{key} must be a string"
    if "auto_importance" not in metadata:
        return None
    importance = metadata["auto_importance"]
    if isinstance(importance, bool) or not isinstance(importance, int | float):
        return "auto_importance must be a number"
    try:
        finite = math.isfinite(importance)
    except OverflowError:  # an int too large for a float
        finite = False
    if not finite:
        return "auto_importance must be a finite number"
    if importance >= DEFAULT_IMPORTANCE_DECAY_THRESHOLD:
        tags = metadata.get("content_tags")
        stamp = tags.get("importance_scored_at") if isinstance(tags, dict) else None
        if stamp is None:
            stamp = metadata.get("importance_scored_at")
        if stamp is None:
            return (
                f"auto_importance at or above {DEFAULT_IMPORTANCE_DECAY_THRESHOLD} "
                "needs importance_scored_at"
            )
    return None


@router.post("/vectors", responses={422: _BODY_422})
def upsert_vectors(
    body: dict[str, Any],
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> dict[str, Any]:
    """Batch upsert vectors into the vector store.

    Body: ``{"vectors": [{"item_id": "...", "vector": [...], "metadata": {...}}, ...]}``

    **What is judged before any write.** Every row is checked in a first pass
    and any refusal in it stores nothing from the batch. A row that is not an
    object, whose ``item_id`` is present but not a string, or whose ``metadata``
    is neither an object nor ``null``, is a 422. So is a ``metadata`` that would
    break a semantic read: ``content`` or ``excerpt`` that is not a string,
    ``auto_importance`` that is not a finite number (a bool is not one), or an
    ``auto_importance`` at or above the importance decay threshold with no
    ``importance_scored_at`` (in ``metadata`` or ``metadata.content_tags``). So
    is a ``vector`` that is present and not a list of finite numbers (a bool is
    not one) within float32 range, one whose squared length would overflow float32
    in the store's norm, one whose float32 norm is zero (all zeros, or too small
    to survive float32), or one whose length differs from the store's width (when
    the store declares one; the SQLite backend does not, and then the batch's first
    non-empty vector sets the width the rest must match, and a later batch of
    another width is accepted; a query there scores only the rows of its own
    width). So is an ``item_id``
    starting ``trace-summary:``, for every caller: that namespace is the
    trace-embed worker's, and a row there makes it skip the trace for good. A
    row's ``metadata`` may not carry a raw ``data_classification`` (422), whoever
    the caller is: a vector row's label is mirrored from its document, never set
    here. A row whose ``item_id`` names no stored document may not carry
    ``metadata.lifecycle`` or ``metadata.evidence_ref`` (422): the pack builder
    withholds by id, so those keys would act on whatever item holds that id.
    Those two pointer keys are the only metadata refused at such an id; every
    other key is stored as sent. A row whose ``item_id`` is a document (or a chunk
    of one) above the key's
    clearance is refused (409): replacing its vector row would replace what a
    semantic hit shows and carries.

    **What is not atomic.** The second pass writes row by row. A row with no
    ``item_id`` (absent, ``null`` or empty) or no ``vector`` (absent, ``null`` or
    empty) is counted in ``errors`` and stored as nothing. So is a row whose
    document read or upsert fails, which does not stop the rest: a batch can
    answer ``upserted: 1, errors: 1`` with the first row stored. Nothing makes
    the mirror below atomic with a concurrent relabel either: if the relabel's
    own vector sync runs between this route's read of the document and its upsert,
    this upsert overwrites the synced row, and it stays stale until a later sync
    or ``trellis admin resync-vector-metadata``.

    When ``item_id`` names a stored document the row is written carrying that
    document's mirrored metadata (its label and lifecycle among them) and its
    ``evidence_ref``, for every caller, whatever the row's own ``metadata`` says.
    For a chunk id (``<parent>#chunk-<digits>``) ``parent_doc_id`` is the chunk
    document's own, else the parent the id names, whatever the row supplied; for
    any other id it is kept as supplied.
    """
    vectors = body.get("vectors", [])
    if not isinstance(vectors, list):
        raise HTTPException(status_code=422, detail="'vectors' must be a list")

    registry = get_registry()
    vector_store = getattr(registry.knowledge, "vector_store", None)
    if vector_store is None:
        raise HTTPException(status_code=501, detail="Vector store not configured")

    document_store = registry.knowledge.document_store
    width = _vector_width(vectors, vector_store.dimensions)
    for index, item in enumerate(vectors):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=422, detail=f"vectors[{index}] must be an object"
            )
        # A row the semantic axis could not serve (it would skip it, or a bad
        # vector would poison scores), that squats on the trace-embed worker's
        # ids, or that carries ``lifecycle`` / ``evidence_ref`` at an id naming no
        # stored document, is refused here, where the caller can be told, rather
        # than stored for packs (or the worker) to trip on.
        problem = _vector_item_problem(item, document_store, width)
        if problem is not None:
            raise HTTPException(status_code=422, detail=f"vectors[{index}].{problem}")
        item_id = item.get("item_id")
        metadata = item.get("metadata")
        # A raw label is refused whoever the caller is, as on ``/evidence``.
        if metadata is not None:
            stamp_request_sensitivity(metadata, None, ctx=ctx)
        if item_id:
            refuse_vector_write(document_store, item_id, ctx=ctx)

    upserted = 0
    errors = 0
    for item in vectors:
        item_id = item.get("item_id")
        vector = item.get("vector")
        if not item_id or not vector:
            errors += 1
            continue
        try:
            row_metadata = _vector_row_metadata(
                document_store, item_id, item.get("metadata") or {}
            )
            vector_store.upsert(item_id=item_id, vector=vector, metadata=row_metadata)
            upserted += 1
        # AGGREGATE: per-row failures are counted and surfaced in the
        # response's ``errors`` field so a single bad row does not block
        # the rest of the batch. Each failure is logged so operators
        # can diagnose patterns across rows.
        except Exception:
            logger.warning(
                "ingest_vectors_upsert_failed",
                item_id=item_id,
                exc_info=True,
            )
            errors += 1

    return {"status": "ok", "upserted": upserted, "errors": errors}


# ── Bulk ingest (entities + edges + aliases in one request) ─────────────


def _claim_metadata(requested_by_claim: str | None) -> dict[str, Any]:
    """A fresh ``metadata`` dict carrying the body's claim, when present.

    Fresh per call: several commands in a bulk batch would otherwise
    share one dict instance.
    """
    return (
        {"requested_by_claim": requested_by_claim}
        if requested_by_claim is not None
        else {}
    )


def _entity_command(
    item: BulkEntityItem, requested_by: str, requested_by_claim: str | None
) -> Command:
    args: dict[str, Any] = {
        "entity_type": item.entity_type,
        "name": item.name,
        "properties": dict(item.properties),
        "node_role": item.node_role,
    }
    if item.entity_id is not None:
        args["entity_id"] = item.entity_id
    if item.generation_spec is not None:
        args["generation_spec"] = item.generation_spec
    return Command(
        operation=Operation.ENTITY_CREATE,
        args=args,
        target_type="entity",
        idempotency_key=item.idempotency_key,
        metadata=_claim_metadata(requested_by_claim),
        requested_by=requested_by,
    )


def _edge_command(
    item: BulkEdgeItem, requested_by: str, requested_by_claim: str | None
) -> Command:
    return Command(
        operation=Operation.LINK_CREATE,
        args={
            "source_id": item.source_id,
            "target_id": item.target_id,
            "edge_kind": item.edge_kind,
            "properties": dict(item.properties),
            "allow_dangling": item.allow_dangling,
        },
        target_id=item.source_id,
        target_type="entity",
        idempotency_key=item.idempotency_key,
        metadata=_claim_metadata(requested_by_claim),
        requested_by=requested_by,
    )


def _alias_command(
    item: BulkAliasItem, requested_by: str, requested_by_claim: str | None
) -> Command:
    return Command(
        operation=Operation.ALIAS_UPSERT,
        args={
            "entity_id": item.entity_id,
            "source_system": item.source_system,
            "raw_id": item.raw_id,
            "raw_name": item.raw_name,
            "match_confidence": item.match_confidence,
            "is_primary": item.is_primary,
        },
        target_id=item.entity_id,
        target_type="alias",
        metadata=_claim_metadata(requested_by_claim),
        requested_by=requested_by,
    )


def _record_status(group: BulkGroupResult, status: CommandStatus) -> None:
    if status == CommandStatus.SUCCESS:
        group.succeeded += 1
    elif status == CommandStatus.FAILED:
        group.failed += 1
    elif status == CommandStatus.REJECTED:
        group.rejected += 1
    elif status == CommandStatus.DUPLICATE:
        group.duplicates += 1


def _is_terminal_failure(status: CommandStatus) -> bool:
    """stop_on_error halts on FAILED or REJECTED, not DUPLICATE."""
    return status in (CommandStatus.FAILED, CommandStatus.REJECTED)


def _sanitized_item_message(status: CommandStatus, message: str) -> str:
    """Sanitize a per-item ``BulkItemResult.message`` only when *status*
    is FAILED or REJECTED (#829 follow-up). A SUCCESS or DUPLICATE
    message only restates the caller's own request -- a name or id --
    so it is returned verbatim rather than risking the suppression
    marker on an ordinary long name or email-named entity.
    """
    return sanitize_error_message(message) if _is_terminal_failure(status) else message


@router.post(
    "/ingest/bulk",
    response_model=BulkIngestResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "The request carries aliases but the caller's key lacks "
                "the admin scope -- alias.upsert is a destructive "
                "operation (see requires_admin_scope) and requires the "
                "same scope here as it does through /commands/batch."
            )
        }
    },
)
def ingest_bulk(
    req: BulkIngestRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> BulkIngestResponse:
    """Bulk ingest entities, edges, and aliases in one request.

    Entities, edges, and aliases flow through the governed mutation pipeline
    (validation, policy, idempotency, execution, and audit emission).

    Strategies:

    - ``continue_on_error`` *(default)* -- run every item, report per-item
      status. Suited for backfill where partial success is acceptable.
    - ``stop_on_error`` -- halt at the first FAILED/REJECTED result and
      skip remaining items, including later groups.
    - ``sequential`` -- behaves like ``continue_on_error`` (errors don't
      halt); kept for consistency with ``/commands/batch``.

    Processing order is entities → edges → aliases (downstream groups
    reference entities, so entities must land first).

    A request carrying any ``aliases`` is refused whole, with nothing
    executed, unless the caller's key carries the ``admin`` scope: alias
    rebinds are ``alias.upsert``, a :data:`~trellis.mutate.DESTRUCTIVE_OPERATIONS`
    member, and this route must require the same scope the equivalent
    command needs through ``/commands/batch`` rather than letting the
    router-level ``ingest`` scope stand in for it.

    ``requested_by`` is derived from the authenticated principal when one
    is present (see
    :func:`trellis_api.routes._principal.resolve_requested_by`), the same
    rule ``/commands/batch`` applies -- a mutate-scoped caller cannot
    stamp an arbitrary ``requested_by`` onto a bulk-ingested entity or
    edge. The body's own claim rides along as each command's
    ``metadata["requested_by_claim"]``.
    """
    if (
        req.aliases
        and requires_admin_scope(Operation.ALIAS_UPSERT)
        and not scopes_satisfy(ctx.scopes, SCOPE_ADMIN)
    ):
        logger.warning(
            "bulk_ingest_scope_refused",
            route="/ingest/bulk",
            required_scope=SCOPE_ADMIN,
            operation=Operation.ALIAS_UPSERT.value,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"operation requires the admin scope: {Operation.ALIAS_UPSERT.value}"
            ),
        )

    registry = get_registry()
    executor = build_curate_executor(registry, clearance=request_clearance(ctx))
    requested_by, requested_by_claim = resolve_requested_by(
        ctx, req.requested_by, claim_was_set="requested_by" in req.model_fields_set
    )

    response = BulkIngestResponse(
        batch_id=generate_ulid(),
        strategy=req.strategy.value,
        entities=BulkGroupResult(total=len(req.entities)),
        edges=BulkGroupResult(total=len(req.edges)),
        aliases=BulkGroupResult(total=len(req.aliases)),
    )

    halted = False

    # -- Entities --
    for entity in req.entities:
        if halted:
            response.entities.skipped += 1
            response.entities.results.append(
                BulkItemResult(status="skipped", name=entity.name, message="halted")
            )
            continue
        entity_cmd = _entity_command(entity, requested_by, requested_by_claim)
        entity_result = executor.execute(entity_cmd)
        _record_status(response.entities, entity_result.status)
        response.entities.results.append(
            BulkItemResult(
                status=entity_result.status.value,
                id=entity_result.created_id,
                name=entity.name,
                message=_sanitized_item_message(
                    entity_result.status, entity_result.message
                ),
            )
        )
        if req.strategy == BatchStrategy.STOP_ON_ERROR and _is_terminal_failure(
            entity_result.status
        ):
            halted = True

    # -- Edges --
    for edge in req.edges:
        if halted:
            response.edges.skipped += 1
            response.edges.results.append(
                BulkItemResult(
                    status="skipped",
                    name=f"{edge.source_id}->{edge.target_id}",
                    message="halted",
                )
            )
            continue
        edge_cmd = _edge_command(edge, requested_by, requested_by_claim)
        edge_result = executor.execute(edge_cmd)
        _record_status(response.edges, edge_result.status)
        response.edges.results.append(
            BulkItemResult(
                status=edge_result.status.value,
                id=edge_result.created_id,
                name=f"{edge.source_id}->{edge.target_id}",
                message=_sanitized_item_message(
                    edge_result.status, edge_result.message
                ),
            )
        )
        if req.strategy == BatchStrategy.STOP_ON_ERROR and _is_terminal_failure(
            edge_result.status
        ):
            halted = True

    # -- Aliases --
    for alias in req.aliases:
        if halted:
            response.aliases.skipped += 1
            response.aliases.results.append(
                BulkItemResult(
                    status="skipped",
                    name=f"{alias.source_system}:{alias.raw_id}",
                    message="halted",
                )
            )
            continue
        result = executor.execute(
            _alias_command(alias, requested_by, requested_by_claim)
        )
        _record_status(response.aliases, result.status)
        response.aliases.results.append(
            BulkItemResult(
                status=result.status.value,
                id=result.created_id,
                name=f"{alias.source_system}:{alias.raw_id}",
                message=_sanitized_item_message(result.status, result.message),
            )
        )
        if req.strategy == BatchStrategy.STOP_ON_ERROR and _is_terminal_failure(
            result.status
        ):
            halted = True

    logger.info(
        "bulk_ingest_completed",
        batch_id=response.batch_id,
        strategy=req.strategy.value,
        entities_total=response.entities.total,
        entities_succeeded=response.entities.succeeded,
        edges_total=response.edges.total,
        edges_succeeded=response.edges.succeeded,
        aliases_total=response.aliases.total,
        aliases_succeeded=response.aliases.succeeded,
    )

    return response
