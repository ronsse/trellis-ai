"""The ``correction.propose``, ``.apply_start`` and ``.resolve`` handlers.

All three are event-only, like ``precedent.candidate``: none writes a document,
graph or vector row, so none can change what a pack serves. Applying a
correction's text is a separate ``evidence.ingest`` plus the supersede stamp,
run by ``trellis curate corrections apply`` before it resolves the proposal.
"""

from __future__ import annotations

from typing import Any

from trellis.core.hashing import content_hash
from trellis.errors import IdempotencyError, ValidationError
from trellis.feedback.corrections import find_proposal, proposal_id_for
from trellis.mutate.commands import Command
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

#: The ``source`` both handlers emit under.
CORRECTION_EVENT_SOURCE = "mutation_executor"


def _invalid(msg: str, code: str) -> ValidationError:
    return ValidationError(msg, errors=[msg], code=code)


class CorrectionProposeHandler:
    """Record one proposal as a single ``CORRECTION_PROPOSED`` event.

    ``args["proposal"]`` is the payload
    :func:`~trellis.feedback.corrections.proposal_payload` builds. Its id must
    be the one ``(target_id, digest of corrected_content)`` derives, so a
    caller cannot file text under another proposal's id. A proposal already
    recorded is a duplicate.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["proposal"]
        if not isinstance(raw, dict):
            msg = "correction.propose proposal must be an object"
            raise _invalid(msg, "invalid")
        proposal: dict[str, Any] = dict(raw)
        content = proposal.get("corrected_content")
        target_id = proposal.get("target_id")
        if not isinstance(content, str) or not isinstance(target_id, str):
            msg = "correction.propose needs string target_id and corrected_content"
            raise _invalid(msg, "invalid")
        digest = content_hash(content)
        if proposal.get("content_digest") != digest:
            msg = "correction.propose content_digest does not match the content"
            raise _invalid(msg, "digest_mismatch")
        proposal_id = proposal_id_for(target_id, digest)
        if proposal.get("proposal_id") != proposal_id:
            msg = "correction.propose proposal_id does not match target and content"
            raise _invalid(msg, "proposal_id_mismatch")
        event_log = self._registry.operational.event_log
        if find_proposal(event_log, proposal_id) is not None:
            raise IdempotencyError(idempotency_key=proposal_id)
        event_log.emit(
            EventType.CORRECTION_PROPOSED,
            source=CORRECTION_EVENT_SOURCE,
            entity_id=proposal_id,
            entity_type="correction",
            payload=proposal,
        )
        return proposal_id, "correction"


class CorrectionApplyStartHandler:
    """Record the id an ``apply`` is about to write, as ``CORRECTION_APPLY_STARTED``.

    ``target_id`` is the proposal id and ``args["successor_id"]`` the fresh
    document id the corrected text will be stored under. The event is what
    makes that document the proposal's own successor
    (``trellis curate corrections``); only a pending proposal can start one.
    Starting again is allowed: an ``apply`` that stopped before its write left
    an event naming no document, and the next ``apply`` records a new one.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        proposal_id = command.target_id or ""
        successor_id = command.args["successor_id"]
        if not isinstance(successor_id, str) or not successor_id:
            msg = "correction.apply_start successor_id must be a non-empty string"
            raise _invalid(msg, "invalid")
        event_log = self._registry.operational.event_log
        proposal = find_proposal(event_log, proposal_id)
        if proposal is None:
            msg = f"no correction proposal {proposal_id}"
            raise _invalid(msg, "proposal_missing")
        if proposal.status != "pending":
            raise IdempotencyError(idempotency_key=f"{proposal_id}:resolved")
        event_log.emit(
            EventType.CORRECTION_APPLY_STARTED,
            source=CORRECTION_EVENT_SOURCE,
            entity_id=proposal_id,
            entity_type="correction",
            payload={
                "proposal_id": proposal_id,
                "target_id": proposal.target_id,
                "successor_id": successor_id,
            },
        )
        return proposal_id, "correction"


class CorrectionResolveHandler:
    """Record a reviewer's verdict as ``CORRECTION_APPLIED`` or ``_REJECTED``.

    ``target_id`` is the proposal id. A proposal not on record is refused; one
    already resolved is a duplicate, so it is resolved exactly once.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        proposal_id = command.target_id or ""
        decision = command.args["decision"]
        reason = command.args["reason"]
        if decision not in ("applied", "rejected"):
            msg = "correction.resolve decision must be 'applied' or 'rejected'"
            raise _invalid(msg, "invalid_decision")
        if not isinstance(reason, str) or not reason.strip():
            msg = "correction.resolve reason must not be blank"
            raise _invalid(msg, "invalid")
        event_log = self._registry.operational.event_log
        proposal = find_proposal(event_log, proposal_id)
        if proposal is None:
            msg = f"no correction proposal {proposal_id}"
            raise _invalid(msg, "proposal_missing")
        if proposal.status != "pending":
            raise IdempotencyError(idempotency_key=f"{proposal_id}:resolved")
        payload: dict[str, Any] = {
            "proposal_id": proposal_id,
            "target_id": proposal.target_id,
            "decision": decision,
            "reason": reason,
        }
        successor_id = command.args.get("successor_id")
        if decision == "applied":
            if not isinstance(successor_id, str) or not successor_id:
                msg = "correction.resolve applied needs the successor_id"
                raise _invalid(msg, "invalid")
            payload["successor_id"] = successor_id
        event_type = (
            EventType.CORRECTION_APPLIED
            if decision == "applied"
            else EventType.CORRECTION_REJECTED
        )
        event_log.emit(
            event_type,
            source=CORRECTION_EVENT_SOURCE,
            entity_id=proposal_id,
            entity_type="correction",
            payload=payload,
        )
        return proposal_id, "correction"
