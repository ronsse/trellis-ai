"""Curate routes -- promote, link, entity and document creation, feedback."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException

from trellis.core.error_sanitize import sanitize_error_message
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import feedback_log_dir
from trellis.feedback.recording import record_feedback as record_pack_feedback
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
    build_evidence_ingest_command,
)
from trellis.mutate.handlers import precedent_already_demoted_message
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
from trellis_api.routes._principal import (
    refuse_document_overwrite,
    request_clearance,
    resolve_requested_by,
    stamp_request_sensitivity,
)
from trellis_api.routes._results import command_response
from trellis_wire.dtos import (
    ClassificationSetRequest,
    CommandResponse,
    EntityCreateRequest,
    FeedbackRequest,
    LinkRequest,
    PackFeedbackRequest,
    PackFeedbackResponse,
    PrecedentDemoteRequest,
    PrecedentDemoteResponse,
    PrecedentReinstateRequest,
    PromoteRequest,
)

if TYPE_CHECKING:
    from trellis.schemas.classification import Sensitivity

router = APIRouter()


def _execute_command(cmd: Command, *, clearance: Sensitivity) -> CommandResponse:
    """Execute a command through the mutation pipeline.

    ``clearance`` is the caller's document clearance (#194). It has no default,
    so a route that forgets to name one fails at the call; a route whose command
    touches no document says ``"restricted"`` and why.
    """
    result = build_curate_executor(get_registry(), clearance=clearance).execute(cmd)
    # Both FAILED (unexpected handler errors) and REJECTED (handler-raised
    # ValidationError, post-Variant A') are 400-class outcomes from the
    # API caller's perspective. See adr-extraction-validation.md §5.5.
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(result.message)
        )
    return command_response(result)


@router.post("/precedents", response_model=CommandResponse)
def promote(
    req: PromoteRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> CommandResponse:
    """Promote a trace to a precedent.

    ``requested_by`` is derived from the authenticated principal when one
    is present (see
    :func:`trellis_api.routes._principal.resolve_requested_by`) rather
    than trusted from the body -- a mutate-scoped key must not be able to
    claim ``requested_by="auto:learning_candidate"`` and forge the
    auto-promote origin ``_was_auto_promoted`` relies on. The body's own
    claim rides along as ``metadata["requested_by_claim"]``.
    """
    requested_by, requested_by_claim = resolve_requested_by(
        ctx, req.requested_by, claim_was_set="requested_by" in req.model_fields_set
    )
    cmd = Command(
        operation=Operation.PRECEDENT_PROMOTE,
        args={
            "trace_id": req.trace_id,
            "title": req.title,
            "description": req.description,
        },
        target_id=req.trace_id,
        target_type="trace",
        metadata=(
            {"requested_by_claim": requested_by_claim}
            if requested_by_claim is not None
            else {}
        ),
        requested_by=requested_by,
    )
    return _execute_command(cmd, clearance=request_clearance(ctx))


@router.post(
    "/precedents/{target_id:path}/demote", response_model=PrecedentDemoteResponse
)
def demote(
    target_id: str,
    req: PrecedentDemoteRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> PrecedentDemoteResponse:
    """Demote a precedent, stamping it ``Lifecycle.state="demoted"``.

    SCD-2 rewrite, not a purge, so the pre-demotion version stays
    readable via ``get_node_history``/``as_of``. The reversal verb is
    ``POST /precedents/{target_id}/reinstate`` (``precedent.reinstate``,
    see :func:`reinstate` below) -- ``entity.update`` refuses a write to
    ``properties.lifecycle`` on a precedent node, so it is no longer a
    way to undo this. A demoted precedent stops reaching
    ``GET /precedents`` / ``get_lessons`` and is withheld from packs at
    the collect seam, recorded in ``rejected_items``. Only a precedent
    promoted from a learning candidate has a graph node and can be
    demoted -- a trace- or miner-sourced precedent id is refused with a
    message naming that gap. Re-demoting an already-demoted precedent is
    a 200 idempotent no-op: ``already_demoted=true`` and nothing changes
    (no new SCD-2 version, no new event, the stored reason unchanged).
    Re-approving its learning candidate instead of reinstating it is
    refused too -- see
    :func:`trellis.learning.scoring.submit_learning_promotion`.

    ``requested_by`` is derived from the authenticated principal when one
    is present (see
    :func:`trellis_api.routes._principal.resolve_requested_by`); the
    body's own claim rides along as ``metadata["requested_by_claim"]``.
    """
    requested_by, requested_by_claim = resolve_requested_by(
        ctx, req.requested_by, claim_was_set="requested_by" in req.model_fields_set
    )
    cmd = Command(
        operation=Operation.PRECEDENT_DEMOTE,
        args={"target_id": target_id, "reason": req.reason},
        target_id=target_id,
        target_type="precedent",
        metadata=(
            {"requested_by_claim": requested_by_claim}
            if requested_by_claim is not None
            else {}
        ),
        requested_by=requested_by,
    )
    result = build_curate_executor(
        get_registry(), clearance=request_clearance(ctx)
    ).execute(cmd)
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(result.message)
        )
    already_demoted = result.message == precedent_already_demoted_message(target_id)
    return PrecedentDemoteResponse(
        status=result.status.value,
        command_id=result.command_id,
        operation=result.operation,
        message=result.message,
        created_id=result.created_id,
        warnings=list(result.warnings),
        already_demoted=already_demoted,
    )


@router.post("/precedents/{target_id:path}/reinstate", response_model=CommandResponse)
def reinstate(
    target_id: str,
    req: PrecedentReinstateRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> CommandResponse:
    """Reinstate a demoted precedent, stamping it ``Lifecycle.state="current"``.

    The reversal verb :func:`demote` shipped without: an SCD-2 rewrite in
    the other direction, so the demoted version stays readable via
    ``get_node_history``/``as_of`` too, and the precedent's vector row is
    re-synced in the same call so a semantic hit stops carrying the
    demoted snapshot. Only a currently ``"demoted"`` precedent may be
    reinstated -- a target that is ``"current"``, ``"archived"``, not
    found, or not graph-backed is refused with a 400 naming its actual
    state; there is no 200 idempotent-no-op branch the way re-demoting an
    already-demoted precedent has one, since reinstating twice already
    moved the state to ``"current"`` on the first call. Human-only: a
    ``requested_by`` equal to the automatic demotion policy's own
    identity is refused outright.

    ``requested_by`` is derived from the authenticated principal when one
    is present (see
    :func:`trellis_api.routes._principal.resolve_requested_by`); the
    body's own claim rides along as ``metadata["requested_by_claim"]``.
    """
    requested_by, requested_by_claim = resolve_requested_by(
        ctx, req.requested_by, claim_was_set="requested_by" in req.model_fields_set
    )
    cmd = Command(
        operation=Operation.PRECEDENT_REINSTATE,
        args={"target_id": target_id, "reason": req.reason},
        target_id=target_id,
        target_type="precedent",
        metadata=(
            {"requested_by_claim": requested_by_claim}
            if requested_by_claim is not None
            else {}
        ),
        requested_by=requested_by,
    )
    return _execute_command(cmd, clearance=request_clearance(ctx))


@router.post("/documents/{doc_id:path}/sensitivity", response_model=CommandResponse)
def set_document_sensitivity(
    doc_id: str,
    req: ClassificationSetRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> CommandResponse:
    """Relabel a document's ``DataClassification.sensitivity`` (#194).

    A metadata-only write through ``classification.set``: the content and the
    row's ``updated_at`` are untouched, and the document's vector row is
    re-synced so a semantic hit carries the new label. A document above the
    caller's clearance answers 400 exactly as a missing one does, so a key
    cannot learn it exists by trying to relabel it; a requested label above the
    caller's clearance is refused too, so a key cannot push a document out of
    its own reach. A chunk id (it reads its parent's label) and a value outside
    the vocabulary are refused. Re-applying the stored label is a no-op.

    ``requested_by`` is derived from the authenticated principal when one is
    present (see :func:`trellis_api.routes._principal.resolve_requested_by`);
    the body's own claim rides along as ``metadata["requested_by_claim"]``.
    """
    requested_by, requested_by_claim = resolve_requested_by(
        ctx, req.requested_by, claim_was_set="requested_by" in req.model_fields_set
    )
    cmd = Command(
        operation=Operation.CLASSIFICATION_SET,
        args={"doc_id": doc_id, "sensitivity": req.sensitivity},
        target_id=doc_id,
        target_type="document",
        metadata=(
            {"requested_by_claim": requested_by_claim}
            if requested_by_claim is not None
            else {}
        ),
        requested_by=requested_by,
    )
    return _execute_command(cmd, clearance=request_clearance(ctx))


@router.post("/links")
def create_link(
    req: LinkRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> dict[str, Any]:
    """Create a graph edge between two entities."""
    cmd = Command(
        operation=Operation.LINK_CREATE,
        args={
            "source_id": req.source_id,
            "target_id": req.target_id,
            "edge_kind": req.edge_kind,
            "properties": req.properties,
            "allow_dangling": req.allow_dangling,
        },
        target_id=req.source_id,
        target_type="entity",
        requested_by="api:link",
    )
    response = _execute_command(cmd, clearance=request_clearance(ctx))
    return {
        "status": "ok",
        "edge_id": response.created_id,
        "source_id": req.source_id,
        "target_id": req.target_id,
    }


@router.post("/entities")
def create_entity(
    req: EntityCreateRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> dict[str, Any]:
    """Create an entity node in the knowledge graph."""
    args: dict[str, Any] = {
        "entity_type": req.entity_type,
        "name": req.name,
        "properties": dict(req.properties),
    }
    if req.entity_id is not None:
        args["entity_id"] = req.entity_id
    cmd = Command(
        operation=Operation.ENTITY_CREATE,
        args=args,
        target_type="entity",
        requested_by="api:entity",
    )
    response = _execute_command(cmd, clearance=request_clearance(ctx))
    return {
        "status": "ok",
        "node_id": response.created_id,
        "entity_type": req.entity_type,
        "name": req.name,
    }


@router.post("/documents")
def create_document(
    body: dict[str, Any],
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> dict[str, Any]:
    """Write a document to the document store for FTS retrieval.

    Accepts: ``{"doc_id": "...", "content": "...", "metadata": {...},
    "sensitivity": "..."}``

    An optional ``sensitivity`` labels the stored document, under the rules of
    ``POST /evidence`` and MCP ``save_memory``: the key needs that clearance
    itself (403 otherwise), and an unknown label, or a raw
    ``data_classification`` in ``metadata``, is a 422. Nothing is written in
    either case.

    A ``doc_id`` that already exists is refused (409) when the stored
    document's label is above the key's clearance, or above the label this
    write carries: a re-put replaces the row, so it must not destroy or
    downgrade content the caller may not read.
    """
    doc_id = body.get("doc_id")
    content = body.get("content", "")
    # ``or {}`` not ``.get(..., {})``: an explicit ``"metadata": null`` is a
    # shape real clients send, and every consumer below wants a mapping.
    metadata = body.get("metadata") or {}
    sensitivity = body.get("sensitivity")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=422, detail="metadata must be an object")
    if sensitivity is not None and not isinstance(sensitivity, str):
        raise HTTPException(status_code=422, detail="sensitivity must be a string")
    metadata = stamp_request_sensitivity(metadata, sensitivity, ctx=ctx)
    if isinstance(doc_id, str) and doc_id:
        refuse_document_overwrite(
            get_registry().knowledge.document_store,
            doc_id,
            ctx=ctx,
            metadata=metadata,
        )
    command = build_evidence_ingest_command(
        doc_id=doc_id,
        content=content,
        metadata=metadata,
        requested_by="api:create-document",
        idempotency_key=body.get("idempotency_key"),
    )
    response = _execute_command(command, clearance=request_clearance(ctx))
    stored_id = response.created_id or command.target_id
    if (
        response.status == CommandStatus.DUPLICATE
        and stored_id is not None
        and get_registry().knowledge.document_store.get(stored_id) is None
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate mutation references missing document: {stored_id}",
        )
    return {"status": "ok", "doc_id": stored_id}


@router.post("/feedback", response_model=CommandResponse)
def record_feedback(
    req: FeedbackRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> CommandResponse:
    """Record feedback on a trace or precedent."""
    args: dict[str, object] = {"target_id": req.target_id, "rating": req.rating}
    if req.comment:
        args["comment"] = req.comment
    if req.pack_id:
        args["pack_id"] = req.pack_id
    cmd = Command(
        operation=Operation.FEEDBACK_RECORD,
        args=args,
        target_id=req.target_id,
        requested_by="api:feedback",
    )
    return _execute_command(cmd, clearance=request_clearance(ctx))


@router.post("/packs/{pack_id}/feedback", response_model=PackFeedbackResponse)
def pack_feedback(pack_id: str, req: PackFeedbackRequest) -> PackFeedbackResponse:
    """Record element-level feedback on a specific context pack.

    Routes through :func:`trellis.feedback.recording.record_feedback`,
    which appends the durable ``pack_feedback.jsonl`` row and emits the
    authoritative ``FEEDBACK_RECORDED`` event to the operational
    EventLog. The response surfaces whether that event reached the log
    (``event_log_in_sync``) so callers can detect soft-failed emissions
    rather than silently treating a JSONL-only write as fully recorded.
    """
    registry = get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        raise HTTPException(
            status_code=500,
            detail="stores_dir is not configured; cannot record pack feedback",
        )

    # Shared with the MCP ``record_feedback`` tool: one mapping of the
    # element-level surface onto PackFeedback, so the two agent-facing
    # surfaces cannot disagree about what identical inputs mean. In
    # particular ``success`` is derived from ``rating`` when the caller
    # omits it, and ``items_served`` is left empty rather than unioned
    # from the cited ids (see :meth:`PackFeedback.from_agent_signal`).
    feedback = PackFeedback.from_agent_signal(
        run_id=req.target_id or pack_id,
        success=req.success,
        rating=req.rating,
        helpful_item_ids=req.helpful_item_ids,
        unhelpful_item_ids=req.unhelpful_item_ids,
        followed_advisory_ids=req.followed_advisory_ids,
        pack_id=pack_id,
        notes=req.comment,
    )
    result = record_pack_feedback(
        feedback,
        log_dir=feedback_log_dir(stores_dir),
        event_log=registry.operational.event_log,
        outcome_store=registry.operational.outcome_store,
        pack_id=pack_id,
    )
    return PackFeedbackResponse(
        pack_id=pack_id,
        feedback_id=result.feedback_id,
        feedback="positive" if feedback.succeeded else "negative",
        event_log_in_sync=result.event_log_in_sync,
        event_log_emitted=result.event_log_emitted,
        event_log_skipped_as_duplicate=result.event_log_skipped_as_duplicate,
    )
