"""Mutation routes -- batch command execution through the governed pipeline."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from trellis.auth import SCOPE_ADMIN, scopes_satisfy
from trellis.mutate import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
    build_curate_executor,
    build_evidence_ingest_command_from_args,
    redirect_unreachable_settings_update,
    requires_admin_scope,
)
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
from trellis_api.routes._results import command_response
from trellis_wire.dtos import (
    BatchCommandItem,
    BatchCommandRequest,
    BatchCommandResponse,
)

router = APIRouter()


def _refused_operations(req: BatchCommandRequest) -> list[str]:
    """Operations in *req* that require the ``admin`` scope, deduplicated
    and sorted for a stable, readable 403 body."""
    return sorted(
        {
            item.operation
            for item in req.commands
            if requires_admin_scope(Operation(item.operation))
        }
    )


def _build_command(item: BatchCommandItem, requested_by: str) -> Command:
    operation = Operation(item.operation)
    if operation is Operation.EVIDENCE_INGEST and isinstance(
        item.args.get("evidence"), dict
    ):
        return build_evidence_ingest_command_from_args(
            item.args,
            requested_by=requested_by,
            target_id=item.target_id,
            idempotency_key=item.idempotency_key,
            command_metadata=item.metadata,
        )
    return Command(
        operation=operation,
        target_id=item.target_id,
        target_type=item.target_type,
        args=item.args,
        idempotency_key=item.idempotency_key,
        metadata=item.metadata,
        requested_by=requested_by,
    )


@router.post("/commands/batch", response_model=BatchCommandResponse)
def execute_batch(
    req: BatchCommandRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> BatchCommandResponse:
    """Execute a batch of mutation commands through the governed pipeline.

    Supported strategies:

    - ``sequential`` — execute all, never stop early.
    - ``stop_on_error`` — halt on the first ``FAILED`` or ``REJECTED``.
    - ``continue_on_error`` — execute all, include errors in results.

    A batch containing any operation outside
    :data:`~trellis.mutate.NON_DESTRUCTIVE_OPERATIONS` (merges, redactions,
    retention prune, alias rebinds, link/label removal, schedule/settings/
    classify-rule writes) is refused whole, with no command executed, unless
    the caller's key carries the ``admin`` scope. ``requested_by`` stays a
    caller-chosen audit label, unverified against the credential, exactly
    as it already was.
    """
    refused = _refused_operations(req)
    if refused and not scopes_satisfy(ctx.scopes, SCOPE_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"operations require the admin scope: {', '.join(refused)}"),
        )

    executor = build_curate_executor(get_registry())

    # Build Command objects from the request items
    commands = [_build_command(item, req.requested_by) for item in req.commands]

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
        # Counts above are computed before this redirect: it only ever
        # rewrites a FAILED message, never the status, so it cannot move a
        # result between buckets. See its docstring for why status stays
        # FAILED rather than becoming REJECTED.
        results=[
            command_response(redirect_unreachable_settings_update(r)) for r in results
        ],
    )
