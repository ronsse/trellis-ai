"""Ingest routes -- traces, evidence, vectors, and bulk (entities+edges+aliases)."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException

from trellis.core.ids import generate_ulid
from trellis.errors import StoreError
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.schemas.evidence import Evidence
from trellis.schemas.trace import Trace
from trellis_api.app import get_registry
from trellis_api.models import (
    BulkEdgeItem,
    BulkEntityItem,
    BulkGroupResult,
    BulkIngestRequest,
    BulkIngestResponse,
    BulkItemResult,
    IngestResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/traces", response_model=IngestResponse)
def ingest_trace(body: dict[str, Any]) -> IngestResponse:
    """Ingest a trace."""
    try:
        trace = Trace.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid trace: {exc}") from exc

    registry = get_registry()
    try:
        trace_id = registry.trace_store.append(trace)
    except StoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return IngestResponse(trace_id=trace_id)


@router.post("/evidence", response_model=IngestResponse)
def ingest_evidence(body: dict[str, Any]) -> IngestResponse:
    """Ingest evidence."""
    try:
        evidence = Evidence.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid evidence: {exc}") from exc

    registry = get_registry()
    registry.document_store.put(
        doc_id=evidence.evidence_id,
        content=evidence.content or "",
        metadata={
            "evidence_type": evidence.evidence_type,
            "source_origin": evidence.source_origin,
        },
    )

    return IngestResponse(evidence_id=evidence.evidence_id)


# ── Vector batch upsert ─────────────────────────────────────────────────


@router.post("/vectors")
def upsert_vectors(body: dict[str, Any]) -> dict[str, Any]:
    """Batch upsert vectors into the vector store.

    Body: ``{"vectors": [{"item_id": "...", "vector": [...], "metadata": {...}}, ...]}``
    """
    vectors = body.get("vectors", [])
    if not isinstance(vectors, list):
        raise HTTPException(status_code=422, detail="'vectors' must be a list")

    registry = get_registry()
    vector_store = getattr(registry, "vector_store", None)
    if vector_store is None:
        raise HTTPException(status_code=501, detail="Vector store not configured")

    upserted = 0
    errors = 0
    for item in vectors:
        item_id = item.get("item_id")
        vector = item.get("vector")
        metadata = item.get("metadata", {})
        if not item_id or not vector:
            errors += 1
            continue
        try:
            vector_store.upsert(item_id=item_id, vector=vector, metadata=metadata)
            upserted += 1
        except Exception:
            errors += 1

    return {"status": "ok", "upserted": upserted, "errors": errors}


# ── Bulk ingest (entities + edges + aliases in one request) ─────────────


def _entity_command(item: BulkEntityItem, requested_by: str) -> Command:
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
        requested_by=requested_by,
    )


def _edge_command(item: BulkEdgeItem, requested_by: str) -> Command:
    return Command(
        operation=Operation.LINK_CREATE,
        args={
            "source_id": item.source_id,
            "target_id": item.target_id,
            "edge_kind": item.edge_kind,
            "properties": dict(item.properties),
        },
        target_id=item.source_id,
        target_type="entity",
        idempotency_key=item.idempotency_key,
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


@router.post("/ingest/bulk", response_model=BulkIngestResponse)
def ingest_bulk(req: BulkIngestRequest) -> BulkIngestResponse:
    """Bulk ingest entities, edges, and aliases in one request.

    Entities and edges flow through the governed mutation pipeline
    (audit events, per-item idempotency). Aliases route directly to the
    graph store -- there is no alias mutation operation yet; the graph
    store handles alias versioning (SCD Type 2) natively.

    Strategies:

    - ``continue_on_error`` *(default)* -- run every item, report per-item
      status. Suited for backfill where partial success is acceptable.
    - ``stop_on_error`` -- halt at the first FAILED/REJECTED result and
      skip remaining items, including later groups.
    - ``sequential`` -- behaves like ``continue_on_error`` (errors don't
      halt); kept for consistency with ``/commands/batch``.

    Processing order is entities → edges → aliases (downstream groups
    reference entities, so entities must land first).
    """
    registry = get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(event_log=registry.event_log, handlers=handlers)

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
        entity_cmd = _entity_command(entity, req.requested_by)
        entity_result = executor.execute(entity_cmd)
        _record_status(response.entities, entity_result.status)
        response.entities.results.append(
            BulkItemResult(
                status=entity_result.status.value,
                id=entity_result.created_id,
                name=entity.name,
                message=entity_result.message,
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
        edge_cmd = _edge_command(edge, req.requested_by)
        edge_result = executor.execute(edge_cmd)
        _record_status(response.edges, edge_result.status)
        response.edges.results.append(
            BulkItemResult(
                status=edge_result.status.value,
                id=edge_result.created_id,
                name=f"{edge.source_id}->{edge.target_id}",
                message=edge_result.message,
            )
        )
        if req.strategy == BatchStrategy.STOP_ON_ERROR and _is_terminal_failure(
            edge_result.status
        ):
            halted = True

    # -- Aliases (direct graph store; no alias mutation operation exists) --
    # Unlike entities/edges which flow through MutationExecutor and only
    # halt on FAILED/REJECTED (not DUPLICATE), aliases have no CommandStatus
    # distinction. Any exception halts under stop_on_error. This is
    # intentional: the graph store's upsert_alias is idempotent (SCD Type 2
    # versioning), so genuine failures indicate a real problem worth halting.
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
        try:
            alias_id = registry.graph_store.upsert_alias(
                entity_id=alias.entity_id,
                source_system=alias.source_system,
                raw_id=alias.raw_id,
                raw_name=alias.raw_name,
                match_confidence=alias.match_confidence,
                is_primary=alias.is_primary,
            )
            response.aliases.succeeded += 1
            response.aliases.results.append(
                BulkItemResult(
                    status="success",
                    id=alias_id,
                    name=f"{alias.source_system}:{alias.raw_id}",
                    message=f"Alias bound to entity {alias.entity_id}",
                )
            )
        except Exception as exc:
            logger.warning(
                "bulk_alias_failed",
                entity_id=alias.entity_id,
                source_system=alias.source_system,
                raw_id=alias.raw_id,
                error=str(exc),
            )
            response.aliases.failed += 1
            response.aliases.results.append(
                BulkItemResult(
                    status="failed",
                    name=f"{alias.source_system}:{alias.raw_id}",
                    message=str(exc),
                )
            )
            if req.strategy == BatchStrategy.STOP_ON_ERROR:
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
