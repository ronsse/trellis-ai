"""Curate routes -- promote, link, label, feedback, entity creation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.stores.base.event_log import EventType
from trellis_api.app import get_registry
from trellis_api.models import (
    CommandResponse,
    EntityCreateRequest,
    FeedbackRequest,
    LinkRequest,
    PromoteRequest,
)

router = APIRouter()


def _execute_command(cmd: Command) -> CommandResponse:
    """Execute a command through the mutation pipeline."""
    registry = get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(event_log=registry.event_log, handlers=handlers)
    result = executor.execute(cmd)
    if result.status == CommandStatus.FAILED:
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
        },
        target_id=req.source_id,
        target_type="entity",
        requested_by="api",
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
        requested_by="api",
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
    stored_id = registry.document_store.put(
        doc_id=doc_id, content=content, metadata=metadata
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
        requested_by="api",
    )
    return _execute_command(cmd)


@router.post("/packs/{pack_id}/feedback")
def pack_feedback(
    pack_id: str,
    success: bool = Query(..., description="Whether the context was helpful"),
    notes: str | None = Query(None, description="Optional notes"),
) -> dict[str, Any]:
    """Record feedback on a specific context pack."""
    registry = get_registry()
    registry.event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="api",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "pack_id": pack_id,
            "success": success,
            "notes": notes or "",
            "rating": 1.0 if success else 0.0,
        },
    )
    label = "positive" if success else "negative"
    return {"status": "ok", "pack_id": pack_id, "feedback": label}
