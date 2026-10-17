"""Mutation routes -- batch command execution through the governed pipeline."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

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

logger = structlog.get_logger(__name__)

router = APIRouter()


def _parse_operation(value: str) -> Operation:
    """Resolve a wire operation string to :class:`Operation`, or answer 422.

    ``Operation(value)`` raises :class:`ValueError` on an unknown wire
    string, which previously reached the catch-all handler and answered
    500 -- a caller typo looked like a server fault. Same shape as
    ``routes/explore.py``'s ``_parse_event_type``: a 422 naming the bad
    value and the valid vocabulary.
    """
    try:
        return Operation(value)
    except ValueError:
        valid = ", ".join(sorted(op.value for op in Operation))
        raise HTTPException(
            status_code=422,
            detail=f"Unknown operation '{value}'. Valid values: {valid}",
        ) from None


def _refused_operations(operations: list[Operation]) -> list[str]:
    """Wire values in *operations* that require the ``admin`` scope,
    deduplicated and sorted for a stable, readable 403 body."""
    return sorted({op.value for op in operations if requires_admin_scope(op)})


def _resolve_requested_by(ctx: AuthContext, claimed: str) -> tuple[str, str | None]:
    """The audit ``requested_by`` for this batch, and the body's own claim.

    When a principal is authenticated (a real key, or the legacy shared
    secret), the audit label is derived from it -- ``api:<key name or
    id>`` -- never from the caller-chosen *claimed* body value, so a
    careless or hostile body cannot forge who the audit trail blames.
    With no principal (auth mode ``off``, or ``optional`` with no
    credential presented) there is nothing to derive from, so current
    behaviour is unchanged: *claimed* passes through verbatim and the
    second element is ``None``.
    """
    principal = ctx.key_id or ctx.name
    if principal is None:
        return claimed, None
    return f"api:{principal}", claimed


def _build_command(
    item: BatchCommandItem,
    operation: Operation,
    requested_by: str,
    requested_by_claim: str | None,
) -> Command:
    metadata = dict(item.metadata)
    if requested_by_claim is not None:
        metadata["requested_by_claim"] = requested_by_claim
    if operation is Operation.EVIDENCE_INGEST and isinstance(
        item.args.get("evidence"), dict
    ):
        return build_evidence_ingest_command_from_args(
            item.args,
            requested_by=requested_by,
            target_id=item.target_id,
            idempotency_key=item.idempotency_key,
            command_metadata=metadata,
        )
    return Command(
        operation=operation,
        target_id=item.target_id,
        target_type=item.target_type,
        args=item.args,
        idempotency_key=item.idempotency_key,
        metadata=metadata,
        requested_by=requested_by,
    )


@router.post(
    "/commands/batch",
    response_model=BatchCommandResponse,
    responses={
        403: {
            "description": (
                "The batch names an operation outside "
                "NON_DESTRUCTIVE_OPERATIONS and the caller's key lacks "
                "the admin scope."
            )
        },
        422: {
            "description": (
                "A pydantic body-validation failure, or a command names "
                "an operation outside the Operation enum."
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        },
    },
)
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
    the caller's key carries the ``admin`` scope. ``requested_by`` is
    derived from the authenticated principal when one is present (see
    :func:`_resolve_requested_by`); the body's own claim rides along as
    each command's ``metadata["requested_by_claim"]``.
    """
    operations = [_parse_operation(item.operation) for item in req.commands]

    refused = _refused_operations(operations)
    if refused and not scopes_satisfy(ctx.scopes, SCOPE_ADMIN):
        logger.warning(
            "commands_batch_scope_refused",
            route="/commands/batch",
            required_scope=SCOPE_ADMIN,
            operations=refused,
        )
        raise HTTPException(
            status_code=403,
            detail=(f"operations require the admin scope: {', '.join(refused)}"),
        )

    executor = build_curate_executor(get_registry())

    requested_by, requested_by_claim = _resolve_requested_by(ctx, req.requested_by)

    # Build Command objects from the request items
    commands = [
        _build_command(item, operation, requested_by, requested_by_claim)
        for item, operation in zip(req.commands, operations, strict=True)
    ]

    batch = CommandBatch(
        commands=commands,
        strategy=BatchStrategy(req.strategy),
        requested_by=requested_by,
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
