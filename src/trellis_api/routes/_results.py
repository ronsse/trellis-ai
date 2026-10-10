"""The one projection of :class:`CommandResult` onto the wire.

Three routes (``curate``, ``extract``, ``mutations``) each hand-wrote the
same five-field copy, and all three dropped ``CommandResult.warnings`` --
so ``Enforcement.WARN``, whose entire contract is "allow, but say so",
said nothing to any REST caller. Consolidating is not the fix on its own;
it reduces the number of places the projection can go wrong from three to
one, and the rule in
``tests/unit/api/test_command_response_rule.py`` keeps it there.

``message`` is sanitized here rather than at each call site (#829
follow-up), but only when ``status`` is FAILED or REJECTED. ``curate.py``'s
own caller pre-filters FAILED/REJECTED into a sanitized ``HTTPException``
before this projection ever sees them, but ``extract.py`` and
``mutations.py`` hand every result -- FAILED and REJECTED included --
straight to this projection, same as the bulk-ingest per-item results in
``ingest.py`` sanitize their own ``BulkItemResult``. A SUCCESS or
DUPLICATE message only restates the caller's own request -- a name, id,
title or idempotency key -- so it is returned verbatim; wrapping it in
``sanitize_error_message`` unconditionally would replace an ordinary long
name, an email-named entity, or a digest idempotency key with the
suppression marker, for no protective value.
"""

from __future__ import annotations

from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate import CommandResult
from trellis.mutate.commands import CommandStatus
from trellis_wire.dtos import CommandResponse

__all__ = ["command_response"]

_SANITIZED_STATUSES = frozenset({CommandStatus.FAILED, CommandStatus.REJECTED})


def command_response(result: CommandResult) -> CommandResponse:
    """Project a :class:`CommandResult` onto its wire DTO."""
    message = result.message
    if result.status in _SANITIZED_STATUSES:
        message = sanitize_error_message(message)
    return CommandResponse(
        status=result.status.value,
        command_id=result.command_id,
        operation=result.operation,
        message=message,
        created_id=result.created_id,
        warnings=list(result.warnings),
    )
