"""``execute_mutation``, the governed-mutation tool.

Includes its operation and target resolution.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import ValidationError

from trellis.auth import SCOPE_ADMIN, SCOPE_MUTATE
from trellis.core.error_sanitize import describe_validation_error
from trellis.errors import TrellisError
from trellis.mcp.app import mcp
from trellis.mcp.auth import (
    caller_has_scope,
    current_clearance,
    current_principal_id,
    trellis_scope,
)
from trellis.mcp.boundary import (
    _raise_internal,
    _raise_invalid_params,
    _raise_policy_denied,
    _result_message,
)
from trellis.mcp.wiring import _get_registry, _stamp_trace_project
from trellis.mutate import (
    Command,
    Operation,
    build_curate_executor,
    build_evidence_ingest_command_from_args,
    redirect_unreachable_settings_update,
    requires_admin_scope,
)
from trellis.mutate.evidence_pointer_keys import evidence_ingest_pointer_refusal
from trellis.schemas.trace import Trace

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Macro Tool 12: execute_mutation
# ---------------------------------------------------------------------------


def _resolve_operation(operation: str) -> Any:
    """Resolve an ``operation`` string to an :class:`Operation` enum member.

    Accepts both the wire form (``"link.create"``) and the screaming-snake
    name (``"LINK_CREATE"``). Returns ``None`` when the string matches
    neither, leaving error reporting to the caller.

    The two ``except`` clauses below are intentionally tight: the wire
    form falls through ``ValueError`` to try the enum-name form, and the
    enum-name miss returns ``None`` so the caller can raise a single
    INVALID_PARAMS with the offending string and the registry context.
    Replacing either with a raise here would force the caller to mask
    the legitimate "try other form" flow.
    """
    from trellis.mutate.commands import Operation  # noqa: PLC0415

    try:
        return Operation(operation)
    except ValueError:
        # GUARD: tried the wire form ("link.create"); fall through and
        # try the enum-name form ("LINK_CREATE") below.
        pass
    try:
        return Operation[operation]
    except KeyError:
        # GUARD: neither wire form nor enum name matched. Caller
        # surfaces an INVALID_PARAMS MCPError with the offending string.
        return None


def _resolve_execute_mutation_target(
    op: Any, args: dict[str, Any]
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Derive ``Command.target_id``/``target_type`` for the operations whose
    audit-event ``entity_id`` would otherwise read as ``None`` through this
    MCP entry point, plus (``trace.ingest`` only) the project-stamped args.

    ``MutationExecutor`` reads ``Command.target_id``, never
    ``args["target_id"]`` -- so the CLI and REST routes that copy a path
    or query parameter onto ``Command.target_id`` themselves
    (``trellis_cli.curate.demote_precedent``/``reinstate_precedent``,
    ``trellis_api.routes.curate.demote``/``reinstate``) stay correct,
    while ``execute_mutation``, which forwards ``args`` unchanged into
    ``Command(...)``, silently left ``target_id`` -- and the audited
    ``entity_id`` -- ``None`` unless special-cased here.

    Scoped to ``trace.ingest`` (stamp + target, as in ``save_experience``)
    and ``precedent.demote``/``precedent.reinstate`` (``args["target_id"]``
    names the audited precedent directly) -- not generalized to every
    operation whose ``args`` happens to carry a ``target_id``-shaped key,
    since ``link.create``/``entity.merge`` carry one there with a
    different meaning.
    """
    if op is Operation.TRACE_INGEST:
        # A trace that does not validate goes on as sent and untargeted,
        # for the handler to refuse.
        try:
            trace = Trace.model_validate(args.get("trace"))
        except ValidationError:
            return None, None, args
        stamped = {**args, "trace": _stamp_trace_project(trace)}
        return trace.trace_id, "trace", stamped
    if op in (Operation.PRECEDENT_DEMOTE, Operation.PRECEDENT_REINSTATE):
        candidate = args.get("target_id")
        if isinstance(candidate, str) and candidate:
            return candidate, "precedent", args
    return None, None, args


def _refuse_evidence_pointer_keys(command: Command) -> None:
    """Refuse an ``evidence.ingest`` whose metadata would withhold or redirect an item.

    A caller's ``lifecycle`` or ``evidence_ref`` can reach an item other than the
    document; only the restore form and an unchanged ``evidence_ref`` pass
    (:mod:`trellis.mutate.evidence_pointer_keys`), judged on the built command so
    the document id is the one it will write. Refused before anything is
    written, with the tool's ordinary pre-flight ``INVALID_PARAMS``.
    """
    if command.operation is not Operation.EVIDENCE_INGEST:
        return
    refusal = evidence_ingest_pointer_refusal(
        command,
        document_store=_get_registry().knowledge.document_store,
        clearance=current_clearance(),
    )
    if refusal is not None:
        _raise_invalid_params(
            refusal.message,
            data={
                "field": f"args.evidence.metadata.{refusal.key}",
                "operation": str(command.operation),
            },
        )


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def execute_mutation(
    operation: str,
    args: dict[str, Any],
    idempotency_key: str | None = None,
    actor: str | None = None,
) -> str:
    """Execute a governed mutation through the ``MutationExecutor``.

    Provides MCP-surface parity with the REST ``/api/v1/commands/batch``
    endpoint for operator scripting. Wraps a single command in the same
    five-stage pipeline (validate → policy → idempotency → execute →
    emit), so policy gates and audit events apply identically. One
    difference: a ``trace.ingest`` trace is stamped with this server's
    project first and the command targets it (``target_type="trace"``),
    as in ``save_experience``.

    A ``mutate``-scoped caller may issue any operation outside
    :data:`~trellis.mutate.NON_DESTRUCTIVE_OPERATIONS` (merges,
    redactions, retention prune, alias rebinds, link/label removal,
    schedule/settings/classify-rule writes) only with the ``admin``
    scope — refused with ``POLICY_DENIED`` before a ``Command`` is ever
    built. ``requested_by`` is derived from the authenticated principal
    (:func:`~trellis.mcp.auth.current_principal_id`) when one is present
    — ``mcp:<key id>`` — never from the caller-supplied ``actor``; with
    no principal to derive from (stdio, or enforcement disabled) current
    behaviour is unchanged and ``actor`` passes through as before. The
    claimed ``actor`` rides along separately as
    ``metadata["requested_by_claim"]`` whenever it is overridden. This
    tool has no caller-settable ``metadata`` parameter: ``Command.metadata``
    here is always this server's own ``{"requested_by_claim": ...}`` or
    ``{}``, never merged with caller input, so there is no
    ``trellis.mutate.reject_reserved_metadata`` call site here the way
    REST ``/commands/batch`` needs one for its per-item ``metadata`` body
    field -- running that guard on this function's own stamp would
    reject its own legitimate ``requested_by_claim`` key.

    Args:
        operation: Operation name. Accepts the wire value
            (e.g. ``"link.create"``) or the enum key
            (e.g. ``"LINK_CREATE"``).
        args: Operation-specific argument map. Required keys depend on
            the operation — see ``OperationRegistry`` in
            ``trellis.mutate.commands``.
        idempotency_key: Optional dedup key. Repeat submissions with the
            same key return ``status="duplicate"`` without re-executing.
        actor: Optional audit identifier for the submitter. Defaults to
            ``"mcp:execute_mutation"`` when not supplied.

    Returns:
        A JSON object string with fields ``status``, ``command_id``,
        ``operation``, ``message``, and (on success) ``created_id``.
        The executor's own non-success statuses (``rejected``, ``failed``,
        ``duplicate``) are still returned in the JSON body — those are
        structured outcomes, not transport-layer errors.

    Raises:
        MCPError: With ``INVALID_PARAMS`` for pre-flight argument
            issues (empty operation, unknown enum, non-dict args,
            ``Command`` construction failure, an ``evidence.ingest`` whose
            ``evidence.metadata`` sets a ``lifecycle`` other than
            ``{"state": "current"}`` or an ``evidence_ref`` the document does
            not already hold), ``POLICY_DENIED`` for a
            destructive operation without the ``admin`` scope, and
            ``INTERNAL_ERROR`` for unexpected executor-side crashes.
    """
    from trellis.mutate.commands import Command  # noqa: PLC0415

    if not operation or not operation.strip():
        _raise_invalid_params(
            "operation must not be empty",
            data={"field": "operation"},
        )

    op = _resolve_operation(operation)
    if op is None:
        _raise_invalid_params(
            f"unknown operation: {operation}",
            data={"field": "operation", "value": operation},
        )

    if requires_admin_scope(op) and not caller_has_scope(SCOPE_ADMIN):
        logger.warning(
            "execute_mutation_scope_refused",
            tool="execute_mutation",
            required_scope=SCOPE_ADMIN,
            operation=str(op),
        )
        _raise_policy_denied(
            f"operation {op} is destructive and requires the admin scope",
            data={"operation": str(op)},
        )

    if not isinstance(args, dict):
        _raise_invalid_params(
            "args must be a dict",
            data={"field": "args", "type": type(args).__name__},
        )

    requested_by = actor.strip() if actor and actor.strip() else "mcp:execute_mutation"
    requested_by_claim: str | None = None
    principal = current_principal_id()
    if principal is not None:
        requested_by_claim = requested_by
        requested_by = f"mcp:{principal}"

    # Stamped before ingest, as in save_experience: a stored trace is
    # immutable. Targeted as there too: the target is what an
    # ``entity_type: trace`` policy matches and the audit event names.
    # See _resolve_execute_mutation_target's docstring for why
    # precedent.demote/precedent.reinstate are also special-cased here.
    target_id, target_type, args = _resolve_execute_mutation_target(op, args)

    command_metadata = (
        {"requested_by_claim": requested_by_claim}
        if requested_by_claim is not None
        else {}
    )
    try:
        if op is Operation.EVIDENCE_INGEST and isinstance(args.get("evidence"), dict):
            command = build_evidence_ingest_command_from_args(
                args,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                command_metadata=command_metadata,
            )
        else:
            command = Command(
                operation=op,
                target_id=target_id,
                target_type=target_type,
                args=dict(args),
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                metadata=command_metadata,
            )
    except Exception as exc:
        _raise_invalid_params(
            f"invalid command: {describe_validation_error(exc)}",
            data={"operation": str(op), "error_class": type(exc).__name__},
        )

    _refuse_evidence_pointer_keys(command)
    try:
        executor = build_curate_executor(_get_registry(), clearance=current_clearance())
        result = executor.execute(command)
    except Exception as exc:
        logger.exception("execute_mutation_failed", operation=str(op))
        # A TrellisError keeps its text, which Trellis wrote. Any other
        # exception is named by its type: its text can be a driver's. The
        # log above and the chained cause keep it for the operator.
        detail = exc if isinstance(exc, TrellisError) else type(exc).__name__
        _raise_internal(
            f"execution failed: {detail}",
            cause=exc,
            data={
                "command_id": command.command_id,
                "operation": str(op),
                "error_class": type(exc).__name__,
            },
        )

    # A no-op for every operation but settings.update, which this
    # executor never registers a handler for; see the function's own
    # docstring for why it rewrites the message, not the status.
    result = redirect_unreachable_settings_update(result)

    response: dict[str, Any] = {
        "status": result.status.value,
        "command_id": result.command_id,
        "operation": str(result.operation),
        "message": _result_message(result),
    }
    if result.created_id is not None:
        response["created_id"] = result.created_id
    if result.warnings:
        response["warnings"] = list(result.warnings)
    return json.dumps(response)
