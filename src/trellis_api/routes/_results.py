"""The one projection of :class:`CommandResult` onto the wire.

Three routes (``curate``, ``extract``, ``mutations``) each hand-wrote the
same five-field copy, and all three dropped ``CommandResult.warnings`` --
so ``Enforcement.WARN``, whose entire contract is "allow, but say so",
said nothing to any REST caller. Consolidating is not the fix on its own;
it reduces the number of places the projection can go wrong from three to
one, and the rule in
``tests/unit/api/test_command_response_rule.py`` keeps it there.

``message`` is sanitized here rather than at each call site (#829
follow-up). ``curate.py``'s own caller pre-filters FAILED/REJECTED into a
sanitized ``HTTPException`` before this projection ever sees them, but
``extract.py`` and ``mutations.py`` hand every result -- FAILED and
REJECTED included -- straight to this projection, same as the bulk-ingest
per-item results in ``ingest.py`` sanitize their own ``BulkItemResult``.
``sanitize_error_message`` passes clean text through unchanged, so a
SUCCESS/DUPLICATE message is unaffected.
"""

from __future__ import annotations

from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate import CommandResult
from trellis_wire.dtos import CommandResponse

__all__ = ["command_response"]


def command_response(result: CommandResult) -> CommandResponse:
    """Project a :class:`CommandResult` onto its wire DTO."""
    return CommandResponse(
        status=result.status.value,
        command_id=result.command_id,
        operation=result.operation,
        message=sanitize_error_message(result.message),
        created_id=result.created_id,
        warnings=list(result.warnings),
    )
