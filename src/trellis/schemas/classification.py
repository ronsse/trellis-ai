"""Classification schemas for the tagging layer.

See ``docs/design/adr-tag-vocabulary-split.md`` for the decision record behind
the split between ``ContentTags`` (retrieval-shaping, flexible) and the
first-class policy-relevant schemas ``DataClassification`` and ``Lifecycle``
defined here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

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

RESERVED_NAMESPACES: frozenset[str] = frozenset(
    {
        "sensitivity",
        "regulatory",
        "lifecycle",
        "jurisdiction",
        "authority",
        "retention",
        "redaction",
    }
)

_NAMESPACE_GUIDANCE: dict[str, str] = {
    "sensitivity": "use DataClassification.sensitivity",
    "regulatory": "use DataClassification.regulatory_tags",
    "lifecycle": "use Lifecycle.state",
    "jurisdiction": "use DataClassification.jurisdiction",
    "authority": (
        "authority is derived from graph position (e.g., canonical ADR folders, "
        "sign-off edges) — do not tag it directly"
    ),
    "retention": (
        "retention is expressed via Policy (PolicyType.RETENTION), not content tags"
    ),
    "redaction": (
        "redaction is expressed via Policy (PolicyType.REDACTION), not content tags"
    ),
}

_ReservedField = Literal["custom key", "domain value"]


def _reserved_name_for(value: str) -> str | None:
    if ":" in value:
        prefix, _, _ = value.partition(":")
        return prefix if prefix in RESERVED_NAMESPACES else None
    return value if value in RESERVED_NAMESPACES else None


def _format_reservation_error(
    value: str, reserved: str, field: _ReservedField
) -> str:
    guidance = _NAMESPACE_GUIDANCE[reserved]
    return (
        f"{field}={value!r} uses reserved namespace {reserved!r}. "
        f"{guidance}. See docs/design/adr-tag-vocabulary-split.md."
    )


class ContentTags(TrellisModel):
    """Classification tags attached to any stored item.

    Five orthogonal facets plus an escape hatch:
    - domain: multi-label, what area of knowledge (extensible, no controlled vocabulary)
    - content_type: single-label, what shape of information
    - scope: single-label, how broadly applicable
    - signal_quality: single-label, computed, should this be retrieved at all
    - retrieval_affinity: multi-label, which retrieval tier(s)
      this content is best suited for
    - custom: free-form dict for long-tail tagging that does not warrant a
      first-class facet

    Policy-relevant dimensions (sensitivity, regulatory, lifecycle, jurisdiction,
    authority, retention, redaction) are reserved and cannot appear in ``custom``
    keys or ``domain`` values. Use ``DataClassification`` / ``Lifecycle`` or the
    policy system instead. See ``docs/design/adr-tag-vocabulary-split.md``.
    """

    domain: list[str] = Field(default_factory=list)
    content_type: ContentType | None = None
    scope: Scope | None = None
    signal_quality: SignalQuality = "standard"
    retrieval_affinity: list[RetrievalAffinity] = Field(default_factory=list)
    custom: dict[str, list[str]] = Field(default_factory=dict)
    classified_by: list[str] = Field(default_factory=list)
    classification_version: str = "2"
    #: When this tag set was last (re)computed. Populated by classifiers via
    #: :meth:`MergedClassification.to_content_tags` and by reclassification
    #: passes (see :mod:`trellis.classify.refresh`). Closes Gap 1.1: without
    #: a stamp, retrieval can't tell a stale ingest-time tag from a fresh
    #: re-evaluation. ``None`` means "never stamped" (legacy items pre-1.1
    #: fix or hand-edited metadata).
    classified_at: datetime | None = None

    @model_validator(mode="after")
    def _reject_reserved_namespaces(self) -> ContentTags:
        for key in self.custom:
            reserved = _reserved_name_for(key)
            if reserved is not None:
                raise ValueError(
                    _format_reservation_error(key, reserved, "custom key")
                )
        for value in self.domain:
            reserved = _reserved_name_for(value)
            if reserved is not None:
                raise ValueError(
                    _format_reservation_error(value, reserved, "domain value")
                )
        return self


Sensitivity = Literal["public", "internal", "confidential", "restricted"]

LifecycleState = Literal[
    "draft",
    "current",
    "deprecated",
    "superseded",
    "archived",
]


class DataClassification(TrellisModel):
    """Access-policy-relevant classification.

    Separate from ``ContentTags`` because the dimensions below gate *access*
    and *compliance*, not retrieval ranking. Policy code is typed against this
    schema; it must never reach into ``ContentTags.custom`` for these values.

    Defined so the shape is stable before any consumer ships;
    no classifier populates it and no policy gate enforces it yet.
    See ``docs/design/adr-tag-vocabulary-split.md``.
    """

    sensitivity: Sensitivity = "internal"
    regulatory_tags: list[str] = Field(default_factory=list)
    jurisdiction: list[str] = Field(default_factory=list)
    classified_by: list[str] = Field(default_factory=list)
    classification_version: str = "1"


class Lifecycle(TrellisModel):
    """Temporal validity state of content.

    Separate from ``ContentTags.signal_quality`` because ``signal_quality``
    captures "should this be retrieved at all" (low / noise / standard / high)
    while lifecycle captures "is this current, deprecated, or superseded".
    A ``signal_quality="high"`` document can still be ``state="deprecated"``.

    Defined so the shape is stable before any consumer ships;
    no classifier populates it and no policy gate enforces it yet.
    See ``docs/design/adr-tag-vocabulary-split.md``.
    """

    state: LifecycleState = "current"
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    superseded_by: str | None = None
    deprecation_reason: str | None = None
    classification_version: str = "1"
