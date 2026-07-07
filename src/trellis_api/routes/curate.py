"""Curate routes -- promote, link, label, feedback, entity creation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback as record_pack_feedback
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
)
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis_api.app import get_registry
from trellis_wire.dtos import (
    CommandResponse,
    EntityCreateRequest,
    FeedbackRequest,
    LinkRequest,
    PackFeedbackRequest,
    PackFeedbackResponse,
    PromoteRequest,
)

router = APIRouter()


def _execute_command(cmd: Command) -> CommandResponse:
    """Execute a command through the mutation pipeline."""
    result = build_curate_executor(get_registry()).execute(cmd)
    # Both FAILED (unexpected handler errors) and REJECTED (handler-raised
    # ValidationError, post-Variant A') are 400-class outcomes from the
    # API caller's perspective. See adr-extraction-validation.md §5.5.
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(status_code=400, detail=result.message)
    return CommandResponse(
        status=result.status.value,
        command_id=result.command_id,
        operation=result.operation,
        message=result.message,
        created_id=result.created_id,
    )


@router.post("/precedents", response_model=CommandResponse)
def promote(req: PromoteRequest) -> CommandResponse:
    """Promote a trace to a precedent."""
    cmd = Command(
        operation=Operation.PRECEDENT_PROMOTE,
        args={
            "trace_id": req.trace_id,
            "title": req.title,
            "description": req.description,
        },
        target_id=req.trace_id,
        target_type="trace",
        requested_by=req.requested_by,
    )
    return _execute_command(cmd)


@router.post("/links")
def create_link(req: LinkRequest) -> dict[str, Any]:
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
    response = _execute_command(cmd)
    return {
        "status": "ok",
        "edge_id": response.created_id,
        "source_id": req.source_id,
        "target_id": req.target_id,
    }


@router.post("/entities")
def create_entity(req: EntityCreateRequest) -> dict[str, Any]:
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
    response = _execute_command(cmd)
    return {
        "status": "ok",
        "node_id": response.created_id,
        "entity_type": req.entity_type,
        "name": req.name,
    }


@router.post("/documents")
def create_document(body: dict[str, Any]) -> dict[str, Any]:
    """Write a document to the document store for FTS retrieval.

    Accepts: ``{"doc_id": "...", "content": "...", "metadata": {...}}``
    """
    registry = get_registry()
    doc_id = body.get("doc_id")
    content = body.get("content", "")
    metadata = body.get("metadata", {})
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    stored_id = registry.knowledge.document_store.put(
        doc_id=doc_id, content=content, metadata=metadata
    )
    # Feature-flagged embedding (TRELLIS_ENABLE_EMBED_ON_INGEST=1) so
    # SemanticSearch can retrieve the document. Fail-soft inside the hook.
    run_embed_on_ingest(
        registry, stored_id, content, metadata, source="api:create-document"
    )
    return {"status": "ok", "doc_id": stored_id}


@router.post("/feedback", response_model=CommandResponse)
def record_feedback(req: FeedbackRequest) -> CommandResponse:
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
    return _execute_command(cmd)


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

    # Map the element-level surface onto PackFeedback. helpful_item_ids
    # become items_referenced (the positive signal to_event_payload
    # promotes to helpful_item_ids); items_served is the union of cited
    # items. The stronger "actively unhelpful" and "advisory followed"
    # signals are not part of the served/referenced model, so they ride
    # along in metadata where the fitness loops can read them.
    helpful = list(req.helpful_item_ids)
    unhelpful = list(req.unhelpful_item_ids)
    items_served = list(dict.fromkeys([*helpful, *unhelpful]))
    metadata: dict[str, Any] = {}
    if unhelpful:
        metadata["unhelpful_item_ids"] = unhelpful
    if req.followed_advisory_ids:
        metadata["followed_advisory_ids"] = list(req.followed_advisory_ids)
    if req.rating is not None:
        metadata["rating"] = req.rating
    if req.comment:
        metadata["notes"] = req.comment

    feedback = PackFeedback(
        run_id=req.target_id or pack_id,
        phase="feedback",
        intent="",
        outcome="success" if req.success else "failure",
        items_served=items_served,
        items_referenced=helpful,
        metadata=metadata,
    )
    result = record_pack_feedback(
        feedback,
        log_dir=stores_dir / "feedback",
        event_log=registry.operational.event_log,
        pack_id=pack_id,
    )
    return PackFeedbackResponse(
        pack_id=pack_id,
        feedback_id=result.feedback_id,
        feedback="positive" if req.success else "negative",
        event_log_in_sync=result.event_log_in_sync,
        event_log_emitted=result.event_log_emitted,
        event_log_skipped_as_duplicate=result.event_log_skipped_as_duplicate,
    )
