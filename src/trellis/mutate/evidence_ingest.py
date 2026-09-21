"""Prepare document creation commands for governed evidence ingestion."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, cast

from trellis.core.ids import generate_ulid
from trellis.mutate.commands import Command, Operation

EvidenceEmbedMode = Literal["none", "soft", "strict"]


def _stable_document_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"evidence:{digest[:32]}"


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_evidence_ingest_command(
    *,
    content: str,
    requested_by: str,
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
    uri: str | None = None,
    preserve_updated_at: bool = False,
    created_at: str | None = None,
    embed_mode: EvidenceEmbedMode = "soft",
    idempotency_key: str | None = None,
    derive_idempotency: bool = True,
    command_metadata: dict[str, Any] | None = None,
) -> Command:
    """Build a fully identified ``evidence.ingest`` command before submission.

    A caller key wins unchanged and also makes an omitted document id stable
    across transport retries. Keyless mode is reserved for state-based repair
    loops, where a successful historical event must not suppress a missing
    vector row's reconstruction.
    """
    resolved_id = doc_id or (
        _stable_document_id(idempotency_key) if idempotency_key else generate_ulid()
    )
    evidence: dict[str, Any] = {
        "doc_id": resolved_id,
        "content": content,
        "metadata": dict(metadata or {}),
        "preserve_updated_at": preserve_updated_at,
        "embed_mode": embed_mode,
    }
    if uri is not None:
        evidence["uri"] = uri
    if created_at is not None:
        evidence["created_at"] = created_at

    resolved_key = idempotency_key
    if resolved_key is None and derive_idempotency:
        payload = {
            "op": Operation.EVIDENCE_INGEST,
            "op_version": 1,
            "document_id": resolved_id,
            "content_digest": hashlib.sha256(content.encode()).hexdigest(),
            "metadata_digest": _canonical_digest(evidence["metadata"]),
            "uri": uri,
            "embed_mode": embed_mode,
            "preserve_updated_at": preserve_updated_at,
            "created_at": created_at,
        }
        resolved_key = f"evidence.ingest:{_canonical_digest(payload)}"

    return Command(
        operation=Operation.EVIDENCE_INGEST,
        target_id=resolved_id,
        target_type="document",
        args={"evidence": evidence},
        requested_by=requested_by,
        idempotency_key=resolved_key,
        metadata=dict(command_metadata or {}),
    )


def build_evidence_ingest_command_from_args(
    args: dict[str, Any],
    *,
    requested_by: str,
    target_id: str | None = None,
    idempotency_key: str | None = None,
    command_metadata: dict[str, Any] | None = None,
) -> Command:
    """Prepare the generic mutation surface's nested evidence payload."""
    raw = args.get("evidence")
    evidence = raw if isinstance(raw, dict) else {}
    raw_content = evidence.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    metadata = evidence.get("metadata")
    raw_mode = evidence.get("embed_mode", "soft")
    command = build_evidence_ingest_command(
        doc_id=evidence.get("doc_id") or target_id,
        content=content,
        uri=evidence.get("uri"),
        metadata=metadata if isinstance(metadata, dict) else None,
        preserve_updated_at=evidence.get("preserve_updated_at", False),
        created_at=evidence.get("created_at"),
        embed_mode=cast("EvidenceEmbedMode", raw_mode),
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        command_metadata=command_metadata,
    )
    prepared = command.args["evidence"]
    if "content" in evidence and not isinstance(raw_content, str):
        prepared["content"] = raw_content
    if metadata is not None and not isinstance(metadata, dict):
        prepared["metadata"] = metadata
    return command


__all__ = [
    "EvidenceEmbedMode",
    "build_evidence_ingest_command",
    "build_evidence_ingest_command_from_args",
]
