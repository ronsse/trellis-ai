"""Ingest routes -- traces, evidence, vectors, and bulk (entities+edges+aliases)."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException

from trellis.classify.ingest import classify_metadata_on_write
from trellis.core.ids import generate_ulid
from trellis.extract.trace_ingest_hook import run_trace_extraction
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandStatus,
    Operation,
)
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis.schemas.evidence import Evidence
from trellis.schemas.trace import Trace
from trellis_api.app import get_registry
from trellis_wire.dtos import (
    BulkAliasItem,
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
    """Ingest a trace through the governed mutation pipeline."""
    try:
        trace = Trace.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid trace: {exc}") from exc

    registry = get_registry()
    executor = build_curate_executor(registry)
    result = executor.execute(
        Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": trace},
            target_id=trace.trace_id,
            target_type="trace",
            requested_by="api:ingest-trace",
        )
    )
    if result.status == CommandStatus.FAILED:
        # TraceStore.append raises StoreError on duplicate trace_id; the
        # handler propagates that as a FAILED status. 409 is the closest fit.
        raise HTTPException(status_code=409, detail=result.message)

    # Feature-flagged post-ingest trace->graph extraction
    # (TRELLIS_ENABLE_TRACE_EXTRACTION=1). Runs after the trace is durably
    # stored; fail-soft inside the hook so it never fails the request.
    run_trace_extraction(registry, trace, requested_by="api:ingest-trace")

    return IngestResponse(trace_id=result.created_id or trace.trace_id)


@router.post("/evidence", response_model=IngestResponse)
def ingest_evidence(body: dict[str, Any]) -> IngestResponse:
    """Ingest evidence."""
    try:
        evidence = Evidence.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid evidence: {exc}") from exc

    registry = get_registry()
    evidence_metadata: dict[str, Any] = {
        "evidence_type": evidence.evidence_type,
        "source_origin": evidence.source_origin,
    }
    # Classify-on-write (see classify_metadata_on_write). ``source_origin`` is
    # a provenance label ("trace"/"manual"/"ingestion"), not a source system,
    # so no classification context is derived from it.
    evidence_metadata = classify_metadata_on_write(
        evidence_metadata, evidence.content or "", doc_id=evidence.evidence_id
    )
    registry.knowledge.document_store.put(
        doc_id=evidence.evidence_id,
        content=evidence.content or "",
        metadata=evidence_metadata,
    )

    # Feature-flagged embedding (TRELLIS_ENABLE_EMBED_ON_INGEST=1). The hook
    # skips content-less evidence and never fails the ingest.
    run_embed_on_ingest(
        registry,
        evidence.evidence_id,
        evidence.content or "",
        evidence_metadata,
        source="api:ingest-evidence",
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
    vector_store = getattr(registry.knowledge, "vector_store", None)
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
            "allow_dangling": item.allow_dangling,
        },
        target_id=item.source_id,
        target_type="entity",
        idempotency_key=item.idempotency_key,
        requested_by=requested_by,
    )


def _alias_command(item: BulkAliasItem, requested_by: str) -> Command:
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
    """
    registry = get_registry()
    executor = build_curate_executor(registry)

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
        result = executor.execute(_alias_command(alias, req.requested_by))
        _record_status(response.aliases, result.status)
        response.aliases.results.append(
            BulkItemResult(
                status=result.status.value,
                id=result.created_id,
                name=f"{alias.source_system}:{alias.raw_id}",
                message=result.message,
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
