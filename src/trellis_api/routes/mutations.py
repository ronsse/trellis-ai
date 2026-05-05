"""Mutation routes -- batch command execution through the governed pipeline."""

from __future__ import annotations

from fastapi import APIRouter

from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis_api.app import get_registry
from trellis_api.models import (
    BatchCommandRequest,
    BatchCommandResponse,
    CommandResponse,
)

router = APIRouter()


@router.post("/commands/batch", response_model=BatchCommandResponse)
def execute_batch(req: BatchCommandRequest) -> BatchCommandResponse:
    """Execute a batch of mutation commands through the governed pipeline.

    Supported strategies:

    - ``sequential`` — execute all, never stop early.
    - ``stop_on_error`` — halt on the first ``FAILED`` or ``REJECTED``.
    - ``continue_on_error`` — execute all, include errors in results.
    """
    registry = get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log, handlers=handlers
    )

    # Build Command objects from the request items
    commands = [
        Command(
            operation=Operation(item.operation),
            target_id=item.target_id,
            target_type=item.target_type,
            args=item.args,
            idempotency_key=item.idempotency_key,
            metadata=item.metadata,
            requested_by=req.requested_by,
        )
        for item in req.commands
    ]

    batch = CommandBatch(
        commands=commands,
        strategy=BatchStrategy(req.strategy),
        requested_by=req.requested_by,
        metadata=req.metadata,
    )

    results = executor.execute_batch(batch)

    return BatchCommandResponse(
        batch_id=batch.batch_id,
        strategy=batch.strategy.value,
        total=len(req.commands),
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
