"""Classification schemas for the tagging layer.

See ``docs/design/adr-tag-vocabulary-split.md`` for the decision record behind
the split between ``ContentTags`` (retrieval-shaping, flexible) and the
first-class policy-relevant schemas ``DataClassification`` and ``Lifecycle``
defined here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

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

#: The ``ContentType`` vocabulary as a set, for callers that have to *test*
#: membership rather than annotate with it — chiefly
#: :mod:`trellis.schemas.document_metadata`, which uses it to tell a real
#: content-type facet from the foreign values that used to share the key.
CONTENT_TYPE_VALUES: frozenset[str] = frozenset(get_args(ContentType))

Scope = Literal["universal", "org", "project", "ephemeral"]

SignalQuality = Literal["high", "standard", "low", "noise"]

RetrievalAffinity = Literal[
    "domain_knowledge",
    "technical_pattern",
    "operational",
    "reference",
]

ClassifierMode = Literal["ingestion", "enrichment"]

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


def _format_reservation_error(value: str, reserved: str, field: _ReservedField) -> str:
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
    #: Which pipeline mode produced this tag set — ``"ingestion"`` (deterministic
    #: classifiers only) or ``"enrichment"`` (deterministic + LLM fallback).
    #: Populated by :meth:`MergedClassification.to_content_tags`. Closes Gap 1.2:
    #: without it, callers can't tell whether a stored tag set came from a
    #: deterministic-only pass or an LLM-augmented one, so reclassification
    #: comparisons (and audit of cross-mode drift) are impossible. ``None``
    #: means "never stamped" — same legacy / hand-edit story as
    #: :attr:`classified_at`.
    classified_mode: ClassifierMode | None = None
    #: When the *importance score* embedded in this item's metadata
    #: (``metadata["auto_importance"]``) was last computed. Distinct from
    #: :attr:`classified_at` because importance can refresh on a different
    #: cadence (e.g., re-derived from refreshed tags via
    #: :func:`~trellis.classify.importance.compute_importance`, or re-scored
    #: by the enrichment worker). Closes Gap 3.5: without it, retrieval has
    #: no way to tell a stale 6-month-old high score from a fresh one and
    #: cannot apply read-time decay safely. ``None`` means "never stamped"
    #: — same legacy / hand-edit story as :attr:`classified_at`. The
    #: read-path guardrail in :func:`trellis.retrieve.strategies._apply_importance`
    #: raises ``ValueError`` if ``auto_importance`` is set but this stamp is
    #: missing — every writer of ``auto_importance`` MUST also stamp here.
    #: See ``docs/design/adr-importance-score-freshness.md``.
    importance_scored_at: datetime | None = None

    @model_validator(mode="after")
    def _reject_reserved_namespaces(self) -> ContentTags:
        for key in self.custom:
            reserved = _reserved_name_for(key)
            if reserved is not None:
                raise ValueError(_format_reservation_error(key, reserved, "custom key"))
        for value in self.domain:
            reserved = _reserved_name_for(value)
            if reserved is not None:
                raise ValueError(
                    _format_reservation_error(value, reserved, "domain value")
                )
        return self


#: Document-metadata key the shadow tag record is stored under.
#: Lives here, beside the model it names, so both the writer
#: (:mod:`trellis.classify.shadow`) and the serving-boundary filter
#: (:mod:`trellis.retrieve.servable`) can key off one definition without
#: either package importing the other.
SHADOW_TAGS_KEY = "content_tags_shadow"


class ShadowTags(TrellisModel):
    """LLM-derived tags recorded *beside* :class:`ContentTags`, never in place of it.

    The shadow record is the precondition for the ``DETERMINISTIC > LOCAL >
    FRONTIER`` ladder (``docs/PRD.md`` §6): the deterministic layer cannot
    inherit a vocabulary the LLM has never been observed producing. Persisting
    LLM output under a separate key lets a corpus accrue with **zero effect on
    retrieval** — nothing overwrites the live tags, so no pack ranking moves
    while the evidence builds up. See ``#321``.

    **Every facet is an open string, deliberately.** :attr:`ContentTags.content_type`
    is a closed nine-value ``Literal``; the enrichment path's vocabulary
    (:data:`trellis_workers.enrichment.service.DEFAULT_CLASSIFICATIONS` —
    ``reference``, ``research``, ``notes``, ``project``, …) overlaps it in
    exactly one value, ``documentation``. Coercing LLM output into
    ``ContentTags`` therefore raises ``ValidationError`` for almost every real
    classification — and even where it validated it would silently discard the
    disagreement. That disagreement is the measurement: Phase 2 mines it. So a
    shadow record stores what the model actually said, verbatim.

    **Reserved namespaces are not rejected here**, unlike
    :meth:`ContentTags._reject_reserved_namespaces`. A shadow record is an
    *observation* of what a model proposed, not an assertion the system adopts;
    refusing to record a reserved proposal would lose the very signal an
    operator needs to see. Enforcement lives at the promotion gate instead —
    :func:`trellis.learning.tag_evolution.analyze_tag_keyword_candidates`
    refuses to surface a candidate whose tag is a reserved name.
    """

    domain: list[str] = Field(default_factory=list)
    content_type: str | None = None
    scope: str | None = None
    signal_quality: str | None = None
    retrieval_affinity: list[str] = Field(default_factory=list)
    custom: dict[str, list[str]] = Field(default_factory=dict)
    #: Names of the classifiers that produced this record (the ``Classifier``
    #: protocol's ``name``, e.g. ``"llm_facet"``).
    classified_by: list[str] = Field(default_factory=list)
    #: When the shadow pass ran. Drives the "already shadowed / stale" scan in
    #: :func:`trellis.classify.shadow.shadow_classify_stale`, exactly as
    #: :attr:`ContentTags.classified_at` drives the live refresh scan.
    classified_at: datetime | None = None
    #: Identifier of the model or tier that produced the record (e.g.
    #: ``"hermes3:8b"``). A label for grouping agreement stats by model — the
    #: same string that rides ``MemoryOpJudgedPayload.model_id``.
    model_id: str = ""
    #: The producing classifier's own confidence, in ``[0.0, 1.0]``.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    shadow_version: str = "1"

    @property
    def has_tags(self) -> bool:
        """``True`` when this record carries at least one actual tag.

        Provenance alone is not signal: a record can be well-formed —
        ``classified_by``, ``classified_at``, ``model_id``, ``confidence`` all
        populated — and still say nothing about the document. Writers use this
        to tell "the model classified it" from "the model returned, and
        classified nothing", which are different facts and are counted
        differently.
        """
        return bool(
            self.domain
            or self.content_type
            or self.scope
            or self.signal_quality
            or self.retrieval_affinity
            or self.custom
        )


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
