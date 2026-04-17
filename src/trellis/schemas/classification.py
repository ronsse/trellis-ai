"""Classification schemas for the tagging layer."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from trellis.core.base import TrellisModel

# Controlled vocabularies for single-label facets
ContentType = Literal[
    "pattern",
    "decision",
    "error-resolution",
    "discovery",
    "procedure",
    "constraint",
    "configuration",
    "code",
    "documentation",
]

Scope = Literal["universal", "org", "project", "ephemeral"]

SignalQuality = Literal["high", "standard", "low", "noise"]

RetrievalAffinity = Literal[
    "domain_knowledge",
    "technical_pattern",
    "operational",
    "reference",
]


class ContentTags(TrellisModel):
    """Classification tags attached to any stored item.

    Five orthogonal facets:
    - domain: multi-label, what area of knowledge (extensible, no controlled vocabulary)
    - content_type: single-label, what shape of information
    - scope: single-label, how broadly applicable
    - signal_quality: single-label, computed, should this be retrieved at all
    - retrieval_affinity: multi-label, which retrieval tier(s)
      this content is best suited for
    """

    domain: list[str] = Field(default_factory=list)
    content_type: ContentType | None = None
    scope: Scope | None = None
    signal_quality: SignalQuality = "standard"
    retrieval_affinity: list[RetrievalAffinity] = Field(default_factory=list)
    custom: dict[str, list[str]] = Field(default_factory=dict)
    classified_by: list[str] = Field(default_factory=list)
    classification_version: str = "2"
