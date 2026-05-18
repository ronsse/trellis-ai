"""Extraction schemas — drafts emitted by the tiered extraction pipeline.

Extractors (see ``trellis.extract``) convert raw input into ``ExtractionResult``
objects containing ``EntityDraft`` and ``EdgeDraft`` records.  Drafts are
domain-agnostic staging shapes — the CLI/API layer converts them into
``Command`` objects and routes them through ``MutationExecutor`` for
governed creation.  Drafts themselves never touch a store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from trellis.core.base import TrellisModel, utc_now
from trellis.schemas.entity import GenerationSpec
from trellis.schemas.enums import NodeRole
from trellis.schemas.well_known import validate_entity_type_not_anti_pattern


class EntityDraft(TrellisModel):
    """An entity candidate produced by an extractor.

    Field names mirror :class:`~trellis.schemas.entity.Entity` so the
    draft-to-command conversion is a direct field copy.  ``entity_id`` is
    optional: omit to let the alias/ID system assign one at create-time.

    ``allow_structural_leaf`` is the per-draft opt-in for the regulated-
    column exception documented in ``adr-source-modeling-discipline.md``
    (Track G).  When ``False`` (default), constructing a draft whose
    ``entity_type`` is on the anti-pattern blocklist
    (``Column`` / ``column`` / ``TableColumn`` / ``table_column``) emits a
    ``structlog`` WARNING; with ``True`` it emits an INFO acknowledging
    the deliberate opt-in.  Soft enforcement only — never raises.
    """

    entity_id: str | None = None
    entity_type: str
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    node_role: NodeRole = NodeRole.SEMANTIC
    generation_spec: GenerationSpec | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    allow_structural_leaf: bool = False

    @model_validator(mode="after")
    def _warn_on_anti_pattern_entity_type(self) -> EntityDraft:
        """Emit advisory warning / opt-in ack for anti-pattern entity types.

        Soft enforcement only — never raises.  See
        :func:`~trellis.schemas.well_known.validate_entity_type_not_anti_pattern`
        for the exact behaviour.
        """
        validate_entity_type_not_anti_pattern(
            self.entity_type,
            allow_structural_leaf=self.allow_structural_leaf,
        )
        return self


class EdgeDraft(TrellisModel):
    """An edge candidate produced by an extractor.

    ``source_id`` / ``target_id`` may either be final entity IDs (when the
    extractor knows them) or references to ``EntityDraft.entity_id`` values
    produced in the same result (when the extractor is emitting fresh
    entities whose IDs will be assigned downstream).  The CLI/API layer is
    responsible for resolving draft-local references before issuing
    ``LINK_CREATE`` commands.

    ``allow_dangling`` opts the edge out of pre-flight FK validation in
    ``LinkCreateHandler``.  Use it for extractors that legitimately emit
    edges whose targets live outside the current batch — e.g. dbt
    ``depends_on`` references that span manifests, or OpenLineage events
    streamed in chunks where a job's dataset was extracted in a prior
    batch.  Default ``False`` keeps strict FK semantics for in-batch
    edges.
    """

    source_id: str
    target_id: str
    edge_kind: str
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    allow_dangling: bool = False


class ExtractionProvenance(TrellisModel):
    """Provenance for an extraction run.

    Distinct from :class:`~trellis.schemas.entity.GenerationSpec`, which
    records provenance for a single curated node.  ``ExtractionProvenance``
    describes the extraction run as a whole — which extractor touched which
    input at what time — and sits on the enclosing ``ExtractionResult``.
    """

    extractor_name: str
    extractor_version: str = "0.0.0"
    source_hint: str | None = None
    raw_input_hash: str | None = None
    extracted_at: datetime = Field(default_factory=utc_now)


class ExtractionResult(TrellisModel):
    """Output of an :class:`~trellis.extract.base.Extractor`.

    Carries the extracted drafts plus full cost / confidence / provenance
    telemetry so the dispatcher and downstream effectiveness analysis can
    reason about extraction quality without re-running the extractor.
    """

    entities: list[EntityDraft] = Field(default_factory=list)
    edges: list[EdgeDraft] = Field(default_factory=list)
    extractor_used: str
    tier: str  # ExtractorTier value — avoids circular import with extract module
    llm_calls: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    overall_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: ExtractionProvenance
    unparsed_residue: Any | None = None
