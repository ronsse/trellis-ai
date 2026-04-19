"""Client-side extraction route.

``POST /api/v1/extract/drafts`` is the submission point for client
extractor packages — Unity Catalog readers, dbt syncs, custom
domain extractors.  See the plan in TODO.md (Client Boundary Phase 1,
Step 4).

Pipeline:

1. Client constructs an :class:`trellis_wire.ExtractionBatch` locally.
2. POSTs it here (optionally with an ``Idempotency-Key`` header).
3. Route translates wire drafts → core :class:`ExtractionResult` via
   :func:`trellis.wire.translate.extraction_batch_to_core_result`.
4. :func:`trellis.extract.commands.result_to_batch` converts the
   result to a :class:`CommandBatch` (same bridge the CLI uses).
5. :class:`MutationExecutor` runs the batch.  All mutations flow
   through the governed pipeline — never direct store writes.

The ``requested_by`` field on the audit trail takes the form
``"{extractor_name}@{extractor_version}"`` so downstream
effectiveness analysis can attribute drafts to a specific client
release.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Header

from trellis.extract.commands import result_to_batch
from trellis.mutate.commands import CommandStatus
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.wire.translate import (
    batch_strategy_to_core,
    extraction_batch_to_core_result,
)
from trellis_api.app import get_registry
from trellis_wire import (
    CommandResponse,
    DraftSubmissionRequest,
    DraftSubmissionResult,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/extract/drafts", response_model=DraftSubmissionResult)
def submit_drafts(
    req: DraftSubmissionRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DraftSubmissionResult:
    """Submit a client-extracted draft batch for governed ingestion.

    The ``Idempotency-Key`` header overrides
    ``req.batch.idempotency_key`` when both are set — the header is
    closer to the transport layer and more visible on the client
    side, so it wins.  When neither is present the submission is
    still processed, but without deduplication.
    """
    batch = req.batch
    effective_key = idempotency_key or batch.idempotency_key
    extractor_id = f"{batch.extractor_name}@{batch.extractor_version}"
    requested_by = req.requested_by or extractor_id

    registry = get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(event_log=registry.event_log, handlers=handlers)

    # Wire batch → core ExtractionResult → CommandBatch.  The bridge
    # is the same one CLI ingest and MCP save_memory use, so the
    # "drafts never touch a store" invariant has one enforcement
    # point.
    core_result = extraction_batch_to_core_result(batch)
    command_batch = result_to_batch(
        core_result,
        requested_by=requested_by,
        strategy=batch_strategy_to_core(req.strategy),
    )

    # Stamp the idempotency key on every command in the batch so
    # replays dedupe at the MutationExecutor layer.  Scoping to
    # ``{key}:{i}`` keeps each command's key distinct while still
    # tying it to the submission.  Core Command models are mutable
    # (TrellisModel doesn't set frozen=True).
    if effective_key:
        for i, cmd in enumerate(command_batch.commands):
            cmd.idempotency_key = f"{effective_key}:{i}"

    results = executor.execute_batch(command_batch)

    logger.info(
        "extract_drafts_submitted",
        extractor=extractor_id,
        source=batch.source,
        tier=batch.tier.value,
        entities=len(batch.entities),
        edges=len(batch.edges),
        idempotency_key=effective_key,
        succeeded=sum(1 for r in results if r.status == CommandStatus.SUCCESS),
    )

    return DraftSubmissionResult(
        batch_id=command_batch.batch_id,
        extractor=extractor_id,
        strategy=command_batch.strategy.value,
        idempotency_key=effective_key,
        entities_submitted=len(batch.entities),
        edges_submitted=len(batch.edges),
        executed=len(results),
        succeeded=sum(1 for r in results if r.status == CommandStatus.SUCCESS),
        failed=sum(1 for r in results if r.status == CommandStatus.FAILED),
        rejected=sum(1 for r in results if r.status == CommandStatus.REJECTED),
        duplicates=sum(1 for r in results if r.status == CommandStatus.DUPLICATE),
        results=[
            CommandResponse(
                status=r.status.value,
                command_id=r.command_id,
                operation=r.operation,
                message=r.message,
                created_id=r.created_id,
            )
            for r in results
        ],
    )
