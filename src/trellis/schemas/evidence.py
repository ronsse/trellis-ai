"""Evidence schema for Trellis."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from trellis.core.base import TimestampedModel, VersionedModel
from trellis.core.hashing import content_hash
from trellis.core.ids import generate_ulid
from trellis.schemas.enums import EvidenceType


class AttachmentRef(VersionedModel):
    """Reference linking evidence to a target object."""

    target_id: str
    target_type: str  # trace, entity, precedent


class Evidence(TimestampedModel, VersionedModel):
    """A piece of evidence supporting traces, precedents, or entities."""

    evidence_id: str = Field(default_factory=generate_ulid)
    evidence_type: EvidenceType
    content: str | None = None
    uri: str | None = None
    content_hash: str = ""
    source_origin: str  # trace, manual, ingestion
    source_trace_id: str | None = None
    attached_to: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _compute_hash(self) -> Evidence:
        if self.content and not self.content_hash:
            self.content_hash = content_hash(self.content)
        return self
