"""``propose_correction``: record a reviewable correction to a stored item."""

from __future__ import annotations

import threading

import structlog

from trellis.auth import SCOPE_INGEST
from trellis.core.hashing import content_hash
from trellis.feedback.corrections import (
    PROPOSE_CORRECTION_SURFACE,
    correction_refusal,
    find_proposal,
    proposal_id_for,
    proposal_payload,
)
from trellis.mcp.app import mcp
from trellis.mcp.auth import current_clearance, current_principal_id, trellis_scope
from trellis.mcp.boundary import (
    _raise_internal,
    _raise_invalid_params,
    _raise_mutation_failed,
    _result_message,
)
from trellis.mcp.rejections import _record_boundary_rejection
from trellis.mcp.wiring import _get_registry
from trellis.mutate import Command, CommandStatus, Operation, build_correction_executor

logger = structlog.get_logger(__name__)

#: Serializes the duplicate check and the write, as ``save_memory`` does.
_propose_lock = threading.Lock()

#: The line every accepted or repeated proposal ends with.
PENDING_LINE = "The correction is pending review; the item is unchanged."


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def propose_correction(
    item_id: str,
    corrected_content: str,
    reason: str,
    pack_id: str = "",
    evidence_uri: str | None = None,
) -> str:
    """Propose a correction to a stored item you found wrong or outdated.

    Records a proposal for a reviewer, nothing more: the item keeps being
    served unchanged, the corrected text is not served, and no pack changes
    until a reviewer applies it, which stores the text as a new memory that
    supersedes the item.

    Refused, with nothing written, when a field is blank or holds a NUL byte,
    ``reason`` is over 2000 characters or ``pack_id`` / ``evidence_uri`` over
    2048, the item does not exist or you cannot read it, it is part of a
    chunked document, it is archived or already superseded, the text is
    identical or near-identical to the item, or ``corrected_content``,
    ``reason`` or ``evidence_uri`` matches the secret scan. Proposing the same
    text for the same item again returns the same proposal id.

    Args:
        item_id: Id of the stored document to correct (the backticked id in a
            pack).
        corrected_content: The full text the item should say instead.
        reason: Why the item is wrong or outdated.
        pack_id: Optional id of the pack that served the item.
        evidence_uri: Optional link to what shows the correction is right.
    """
    registry = _get_registry()
    clearance = current_clearance()
    refusal, target = correction_refusal(
        registry,
        item_id=item_id,
        corrected_content=corrected_content,
        reason=reason,
        clearance=clearance,
        pack_id=pack_id or None,
        evidence_uri=evidence_uri or None,
    )
    if refusal is not None:
        _record_boundary_rejection(
            tool="propose_correction",
            rejections=[refusal.row()],
            payload_chars=len(corrected_content or ""),
        )
        _raise_invalid_params(
            refusal.message,
            data={"field": refusal.field, "reason": refusal.reason},
        )

    if target is None:
        _raise_internal("correction target vanished between check and write")

    proposal_id = proposal_id_for(item_id, content_hash(corrected_content))
    payload = proposal_payload(
        proposal_id=proposal_id,
        target_id=item_id,
        target_metadata=target.get("metadata"),
        corrected_content=corrected_content,
        reason=reason,
        proposed_by=current_principal_id() or "local",
        pack_id=pack_id or None,
        evidence_uri=evidence_uri or None,
    )
    command = Command(
        operation=Operation.CORRECTION_PROPOSE,
        target_id=proposal_id,
        target_type="correction",
        args={"proposal": payload},
        requested_by=PROPOSE_CORRECTION_SURFACE,
    )
    with _propose_lock:
        result = build_correction_executor(registry, clearance=clearance).execute(
            command
        )
    if result.status is CommandStatus.DUPLICATE:
        existing = find_proposal(registry.operational.event_log, proposal_id)
        status = existing.status if existing is not None else "pending"
        if status == "pending":
            return f"Correction `{proposal_id}` was already proposed. {PENDING_LINE}"
        return (
            f"Correction `{proposal_id}` was already proposed and has been "
            f"{status}; nothing was written."
        )
    if result.status is not CommandStatus.SUCCESS:
        logger.warning(
            "propose_correction_not_recorded",
            status=result.status.value,
            proposal_id=proposal_id,
        )
        _raise_mutation_failed(
            f"correction proposal not recorded: {_result_message(result)}",
            data={"status": result.status.value, "command_id": result.command_id},
        )
    return f"Proposed correction `{proposal_id}` for `{item_id}`. {PENDING_LINE}"
