"""Reviewable corrections to a stored item: validation, ids and event readers.

An agent that finds a served item wrong or outdated can say what is right with
MCP ``propose_correction``. The proposal is a **record, not a write**: nothing
is superseded, nothing new becomes servable and no pack changes until a
reviewer runs ``trellis curate corrections apply``, which stores the corrected
text as a new memory through the governed ``evidence.ingest`` path and stamps
the target superseded through :mod:`trellis.mcp.supersession`, the path
``save_memory(supersedes=...)`` uses.

**Where the proposed text lives.** Only in the ``CORRECTION_PROPOSED`` event's
payload, the way a precedent candidate lives only in its
``PRECEDENT_CANDIDATE_MINED`` event. The event log is Operational Plane: no
search strategy reads it and no pack is built from it, so a proposal cannot be
served. The payload carries the target's ``data_classification`` sensitivity
(``sensitivity``) so a reader of the event log can hold a proposal to the label
of the item it would change.

A proposal's status is derived, never stored: ``pending`` until a
``CORRECTION_APPLIED`` or ``CORRECTION_REJECTED`` event names its id.

No model decides anything here. :class:`CorrectionValidator` is the seam where
an automatic validator could later choose apply or reject; nothing implements
or calls it yet.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.hashing import content_hash
from trellis.core.reserved_doc_ids import is_trace_summary_doc_id
from trellis.mutate.sensitivity_floor import visible_document
from trellis.retrieve.lifecycle import declared_successor, is_archived
from trellis.schemas.classification import Sensitivity, sensitivity_of
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.registry import StoreRegistry

#: The write-surface label of MCP ``propose_correction``.
PROPOSE_CORRECTION_SURFACE = "mcp:propose_correction"

#: The write-surface label of ``trellis curate corrections apply`` / ``reject``.
CORRECTIONS_CLI_SURFACE = "cli:curate-corrections"

#: Prefix of every proposal id.
PROPOSAL_ID_PREFIX = "corr-"

#: Upper bound on ``reason``. ``corrected_content`` has none, as
#: ``save_memory``'s ``content`` has none.
MAX_REASON_CHARS = 2000

CorrectionStatus = Literal["pending", "applied", "rejected"]
CORRECTION_STATUSES: tuple[CorrectionStatus, ...] = ("pending", "applied", "rejected")

CorrectionDecision = Literal["applied", "rejected"]

#: Refusal reasons, one per deterministic check, in the order they run.
REFUSAL_REASONS: tuple[str, ...] = (
    "blank_field",
    "reason_too_long",
    "target_missing",
    "target_chunk",
    "target_archived",
    "target_superseded",
    "content_identical",
    "content_near_identical",
    "secret_detected",
)


@dataclass(frozen=True)
class CorrectionRefusal:
    """Why a correction is refused; nothing was written."""

    reason: str
    field: str
    message: str

    def row(self) -> dict[str, str]:
        """The rejection row ``record_write_rejection`` carries."""
        return {"kind": "value", "loc": self.field, "msg": self.message}


@dataclass(frozen=True)
class CorrectionProposal:
    """One proposal, read back from its ``CORRECTION_PROPOSED`` event."""

    proposal_id: str
    target_id: str
    corrected_content: str
    reason: str
    proposed_by: str
    content_digest: str
    content_length: int
    sensitivity: str
    pack_id: str | None
    evidence_uri: str | None
    proposed_at: str
    status: CorrectionStatus
    resolution: dict[str, Any] | None

    def summary(self) -> dict[str, Any]:
        """The fields ``list`` shows: everything but the proposed text."""
        return {
            "proposal_id": self.proposal_id,
            "target_id": self.target_id,
            "status": self.status,
            "reason": self.reason,
            "proposed_by": self.proposed_by,
            "proposed_at": self.proposed_at,
            "content_length": self.content_length,
            "sensitivity": self.sensitivity,
            "pack_id": self.pack_id,
        }

    def detail(self) -> dict[str, Any]:
        """Everything, the proposed text and any resolution included."""
        return {
            **self.summary(),
            "content_digest": self.content_digest,
            "evidence_uri": self.evidence_uri,
            "corrected_content": self.corrected_content,
            "resolution": self.resolution,
        }


class CorrectionValidator(Protocol):
    """Seam for an automatic reviewer that could decide apply or reject.

    Not implemented and not called anywhere: today a human runs ``trellis
    curate corrections apply`` or ``reject``. ``None`` means no opinion.
    """

    def decide(
        self, proposal: CorrectionProposal, target: Mapping[str, Any]
    ) -> CorrectionDecision | None: ...


def proposal_id_for(target_id: str, digest: str) -> str:
    """The proposal id for ``(target, content digest)``; a repeat derives the same."""
    raw = f"{target_id}\x00{digest}".encode()
    return PROPOSAL_ID_PREFIX + hashlib.sha256(raw).hexdigest()[:26]


def _refused(reason: str, field: str, message: str) -> CorrectionRefusal:
    return CorrectionRefusal(reason, field, f"{message}; nothing was written")


def correction_refusal(
    registry: StoreRegistry,
    *,
    item_id: str,
    corrected_content: str,
    reason: str,
    clearance: Sensitivity,
) -> tuple[CorrectionRefusal | None, dict[str, Any] | None]:
    """Run every deterministic check; return the first refusal, else the target.

    A target above ``clearance`` is answered exactly as a missing one. A
    trace-summary id is answered the same way too, before any lookup, so the
    answer does not depend on whether such a row exists.
    """
    refusal = _field_refusal(item_id, corrected_content, reason)
    if refusal is not None:
        return refusal, None
    doc = None
    if not is_trace_summary_doc_id(item_id):
        doc = visible_document(
            registry.knowledge.document_store, item_id, clearance=clearance
        )
    if doc is None:
        return (
            _refused(
                "target_missing",
                "item_id",
                f"item_id does not reference an existing document: {item_id}",
            ),
            None,
        )
    refusal = (
        _lifecycle_refusal(item_id, doc.get("metadata") or {})
        or _content_refusal(str(doc.get("content") or ""), corrected_content)
        or _secret_refusal(corrected_content, reason)
    )
    if refusal is not None:
        return refusal, None
    return None, doc


def _field_refusal(
    item_id: str, corrected_content: str, reason: str
) -> CorrectionRefusal | None:
    for field, value in (
        ("item_id", item_id),
        ("corrected_content", corrected_content),
        ("reason", reason),
    ):
        if not value or not value.strip():
            return _refused("blank_field", field, f"{field} must not be blank")
    if len(reason) > MAX_REASON_CHARS:
        return _refused(
            "reason_too_long",
            "reason",
            f"reason is {len(reason)} characters, more than {MAX_REASON_CHARS}",
        )
    return None


def _lifecycle_refusal(
    item_id: str, metadata: Mapping[str, Any]
) -> CorrectionRefusal | None:
    """Refuse a target the supersede path could not stamp (``_ineligibility``)."""
    chunk_count = metadata.get("chunk_count")
    if isinstance(chunk_count, int) and chunk_count > 0:
        return _refused(
            "target_chunk",
            "item_id",
            f"{item_id} is part of a chunked document, which a correction "
            "cannot supersede",
        )
    if is_archived(dict(metadata)):
        return _refused("target_archived", "item_id", f"{item_id} is archived")
    successor = declared_successor(dict(metadata))
    if successor is not None:
        return _refused(
            "target_superseded",
            "item_id",
            f"{item_id} is already superseded by {successor}",
        )
    return None


def _content_refusal(target_text: str, corrected: str) -> CorrectionRefusal | None:
    """Refuse a correction identical or near-identical to its target.

    Near-identical is ``save_memory``'s fuzzy-dedup test: a
    :class:`MinHashIndex` at its default threshold.
    """
    if content_hash(corrected) == content_hash(target_text):
        return _refused(
            "content_identical",
            "corrected_content",
            "corrected_content is identical to the target",
        )
    index = MinHashIndex()
    index.add("target", target_text)
    match = index.find_duplicate(corrected)
    if match is not None:
        return _refused(
            "content_near_identical",
            "corrected_content",
            f"corrected_content is a near-duplicate ({match[1]:.0%}) of the target",
        )
    return None


def _secret_refusal(corrected: str, reason: str) -> CorrectionRefusal | None:
    """Refuse a secret-shaped value, with the scan session capture runs on memories.

    Only the class names reach the message, never the matched text.
    """
    from trellis_workers.session_capture.secret_scan import scan  # noqa: PLC0415

    for field, text in (("corrected_content", corrected), ("reason", reason)):
        classes = scan(text)
        if classes:
            return _refused(
                "secret_detected",
                field,
                f"{field} matches the secret scan ({', '.join(sorted(classes))})",
            )
    return None


def proposal_payload(
    *,
    proposal_id: str,
    target_id: str,
    target_metadata: Mapping[str, Any] | None,
    corrected_content: str,
    reason: str,
    proposed_by: str,
    pack_id: str | None,
    evidence_uri: str | None,
) -> dict[str, Any]:
    """The ``CORRECTION_PROPOSED`` payload; the proposal inherits the target's label."""
    payload: dict[str, Any] = {
        "proposal_id": proposal_id,
        "target_id": target_id,
        "reason": reason,
        "proposed_by": proposed_by,
        "content_digest": content_hash(corrected_content),
        "content_length": len(corrected_content),
        "sensitivity": sensitivity_of(target_metadata),
        "corrected_content": corrected_content,
    }
    if pack_id:
        payload["pack_id"] = pack_id
    if evidence_uri:
        payload["evidence_uri"] = evidence_uri
    return payload


def _resolution_event(event_log: EventLog, proposal_id: str) -> Event | None:
    rows = [
        e
        for event_type in (EventType.CORRECTION_APPLIED, EventType.CORRECTION_REJECTED)
        for e in event_log.get_events(
            event_type=event_type, entity_id=proposal_id, limit=1
        )
    ]
    return min(rows, key=lambda e: e.occurred_at) if rows else None


def _from_event(event_log: EventLog, event: Event) -> CorrectionProposal:
    payload = event.payload
    resolved = _resolution_event(event_log, str(payload["proposal_id"]))
    status: CorrectionStatus = "pending"
    resolution: dict[str, Any] | None = None
    if resolved is not None:
        status = (
            "applied"
            if resolved.event_type == EventType.CORRECTION_APPLIED
            else "rejected"
        )
        resolution = {**resolved.payload, "at": resolved.occurred_at.isoformat()}
    return CorrectionProposal(
        proposal_id=str(payload["proposal_id"]),
        target_id=str(payload["target_id"]),
        corrected_content=str(payload.get("corrected_content", "")),
        reason=str(payload.get("reason", "")),
        proposed_by=str(payload.get("proposed_by", "")),
        content_digest=str(payload.get("content_digest", "")),
        content_length=int(payload.get("content_length", 0)),
        sensitivity=str(payload.get("sensitivity", "internal")),
        pack_id=payload.get("pack_id"),
        evidence_uri=payload.get("evidence_uri"),
        proposed_at=event.occurred_at.isoformat(),
        status=status,
        resolution=resolution,
    )


def find_proposal(event_log: EventLog, proposal_id: str) -> CorrectionProposal | None:
    """The proposal ``proposal_id`` names, with its derived status, or ``None``."""
    rows = event_log.get_events(
        event_type=EventType.CORRECTION_PROPOSED, entity_id=proposal_id, limit=1
    )
    return _from_event(event_log, rows[0]) if rows else None


def list_proposals(
    event_log: EventLog,
    *,
    status: CorrectionStatus | None = None,
    limit: int = 1000,
) -> list[CorrectionProposal]:
    """Proposals, newest first, optionally only those with ``status``."""
    rows = event_log.get_events(
        event_type=EventType.CORRECTION_PROPOSED, order="desc", limit=limit
    )
    proposals = [_from_event(event_log, e) for e in rows]
    return [p for p in proposals if status is None or p.status == status]
