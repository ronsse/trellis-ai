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
    ReservedMetadataKeyError,
    build_curate_executor,
    build_evidence_ingest_command_from_args,
    redirect_unreachable_settings_update,
    reject_reserved_metadata,
    requires_admin_scope,
)
from trellis.mutate.evidence_pointer_keys import evidence_ingest_pointer_refusal
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
from trellis_api.routes._principal import request_clearance, resolve_requested_by
from trellis_api.routes._results import command_response
from trellis_wire.dtos import (
    BatchCommandBodyError,
    BatchCommandItem,
    BatchCommandRequest,
    BatchCommandResponse,
    BatchOperationNameError,
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


def _build_command(
    item: BatchCommandItem,
    operation: Operation,
    requested_by: str,
    requested_by_claim: str | None,
) -> Command:
    """Build one batch item's :class:`Command`.

    *item.metadata* is caller-supplied and passes through
    :func:`~trellis.mutate.reject_reserved_metadata` first -- which
    raises :class:`~trellis.mutate.ReservedMetadataKeyError` for a
    planted ``requested_by_claim``/``key_id``/``key_name``/
    ``write_provenance``/``handler_warnings`` -- *before* the
    server-derived ``requested_by_claim`` override below is applied, so
    the guard only ever sees what the caller actually sent.
    """
    metadata = reject_reserved_metadata(item.metadata)
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
        400: {
            "description": (
                "A command's own ``metadata``, or the batch's own "
                "``metadata``, named a key reserved for server-side audit "
                "stamping (``requested_by_claim``, ``key_id``, "
                "``key_name``, ``write_provenance``, "
                "``handler_warnings``); ``detail`` names the key."
            )
        },
        403: {
            "description": (
                "The batch names an operation outside "
                "NON_DESTRUCTIVE_OPERATIONS and the caller's key lacks "
                "the admin scope."
            )
        },
        422: {
            "description": (
                "A pydantic body-validation failure (BatchCommandBodyError -- "
                "``detail`` is a list of field errors), or a command names "
                "an operation outside the Operation enum "
                "(BatchOperationNameError -- ``detail`` is a plain string "
                "naming the bad value; see ``_parse_operation``), or an "
                "``evidence.ingest`` command's ``evidence.metadata`` carries a "
                '``lifecycle`` that is not ``{"state": "current"}`` or an '
                "``evidence_ref`` the document does not already hold "
                "(``detail`` is a plain string; the whole batch is refused and "
                "nothing is written)."
            ),
            "model": BatchCommandBodyError | BatchOperationNameError,
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
    :func:`trellis_api.routes._principal.resolve_requested_by`); the
    body's own claim rides along as each command's
    ``metadata["requested_by_claim"]``.

    A command's own ``metadata`` is otherwise free-form decoration, with
    one exception: it may not name a key reserved for server-side audit
    stamping (``requested_by_claim``, ``key_id``, ``key_name``,
    ``write_provenance``, ``handler_warnings`` --
    :data:`~trellis.mutate.RESERVED_COMMAND_METADATA_KEYS`). The batch's
    own ``metadata`` (``CommandBatch.metadata``, distinct from any one
    command's) carries the same restriction, even though nothing reads it
    yet. Either one naming a reserved key refuses the whole batch with
    400, naming the key, before any command executes.

    An ``evidence.ingest`` command's own ``evidence.metadata`` may not use
    ``lifecycle`` or ``evidence_ref`` to withhold or redirect an item: the pack
    builder withholds by id, so a ``lifecycle`` that says ``superseded`` would
    withhold every item under the document's id (a graph node's included) and an
    ``evidence_ref`` the item whose successor it names. A ``lifecycle`` is
    accepted only as ``{"state": "current"}``, with any other field null or at
    its default, which is how an operator restores a superseded document (a set
    ``deprecation_reason`` would let a caller forge a redaction tombstone), and
    an ``evidence_ref`` only when it equals the value the document the command
    writes already holds, so a round-tripped metadata bag keeps working.
    Anything else refuses the whole batch with 422, before any command executes
    and whatever the strategy, the way an unknown operation does; the message
    names the governed writer. A stored document above the key's clearance
    reads as absent for the comparison.
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

    executor = build_curate_executor(get_registry(), clearance=request_clearance(ctx))

    requested_by, requested_by_claim = resolve_requested_by(
        ctx, req.requested_by, claim_was_set="requested_by" in req.model_fields_set
    )

    # Build Command objects from the request items. A command's own
    # metadata is caller-supplied, so _build_command rejects a reserved
    # key (requested_by_claim/key_id/key_name/write_provenance/
    # handler_warnings) before this server's own stamp is merged onto it
    # -- see reject_reserved_metadata's docstring for why a planted one
    # is a forgery, not harmless decoration.
    try:
        commands = [
            _build_command(item, operation, requested_by, requested_by_claim)
            for item, operation in zip(req.commands, operations, strict=True)
        ]
        # The batch's own metadata, not any one command's. Nothing reads
        # CommandBatch.metadata today, but it is caller-supplied the same
        # way each command's own metadata is, so it gets the same guard
        # rather than an exemption that would need revisiting the day a
        # handler starts reading it.
        batch_metadata = reject_reserved_metadata(req.metadata)
    except ReservedMetadataKeyError as exc:
        # str(exc) is ReservedMetadataKeyError's own message -- built from
        # RESERVED_COMMAND_METADATA_KEYS, not a second copy of this prose.
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # A caller's ``lifecycle`` or ``evidence_ref`` on an ``evidence.ingest`` can
    # withhold or redirect an item other than the document, so a command that
    # carries one outside the restore form is refused the way an unknown
    # operation is: the whole batch, before any command executes, whatever the
    # strategy. Judged on the built commands, so the document id checked is the
    # one the command writes. See trellis.mutate.evidence_pointer_keys.
    document_store = get_registry().knowledge.document_store
    clearance = request_clearance(ctx)
    for command in commands:
        if command.operation is not Operation.EVIDENCE_INGEST:
            continue
        refusal = evidence_ingest_pointer_refusal(
            command, document_store=document_store, clearance=clearance
        )
        if refusal is not None:
            raise HTTPException(status_code=422, detail=refusal.message)

    batch = CommandBatch(
        commands=commands,
        strategy=BatchStrategy(req.strategy),
        requested_by=requested_by,
        metadata=batch_metadata,
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
