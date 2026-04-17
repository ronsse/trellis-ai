"""Entity schema for Trellis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from trellis.core.base import TimestampedModel, TrellisModel, VersionedModel, utc_now
from trellis.core.ids import generate_ulid
from trellis.schemas.enums import NodeRole


class EntitySource(VersionedModel):
    """Origin information for an entity."""

    origin: str
    detail: str | None = None
    trace_id: str | None = None


class GenerationSpec(TrellisModel):
    """Provenance record for a curated node.

    A curated node is a synthesized/derived graph node produced by a named
    generator (e.g., precedent promotion worker, community detection
    algorithm, domain rollup summarizer). ``GenerationSpec`` records which
    generator produced the node, when, and from which inputs — enough to
    deterministically regenerate the node later or audit how it came to be.

    Required when ``node_role == NodeRole.CURATED``; must be ``None`` for
    ``STRUCTURAL`` and ``SEMANTIC`` nodes.
    """

    generator_name: str
    generator_version: str
    generated_at: datetime = Field(default_factory=utc_now)
    source_node_ids: list[str] = Field(default_factory=list)
    source_trace_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class Entity(TimestampedModel, VersionedModel):
    """A named entity in the experience graph.

    Entities carry a ``node_role`` describing whether they are structural
    plumbing, semantic ground-truth, or curated synthesis. See the
    ``NodeRole`` enum and ``docs/agent-guide/modeling-guide.md`` for the
    three-role taxonomy.

    ``entity_type`` is an **open string**. The ``EntityType`` enum in
    ``schemas/enums.py`` lists well-known agent-centric values (service,
    team, concept, ...); domain-specific integrations (e.g., Unity
    Catalog tables, dbt models) pass their own strings and are accepted
    verbatim by the storage layer. Do not add domain-specific types to
    the core enum — define them in the consumer package instead.
    """

    entity_id: str = Field(default_factory=generate_ulid)
    entity_type: str
    name: str
    properties: dict = Field(default_factory=dict)
    source: EntitySource | None = None
    metadata: dict = Field(default_factory=dict)
    node_role: NodeRole = NodeRole.SEMANTIC
    generation_spec: GenerationSpec | None = None

    @model_validator(mode="after")
    def _validate_generation_spec(self) -> Entity:
        """Enforce: generation_spec iff node_role == CURATED."""
        if self.node_role == NodeRole.CURATED and self.generation_spec is None:
            msg = (
                "generation_spec is required when node_role is CURATED "
                "(identify which generator produced the node)"
            )
            raise ValueError(msg)
        if self.node_role != NodeRole.CURATED and self.generation_spec is not None:
            msg = (
                "generation_spec must be None unless node_role is CURATED "
                f"(got node_role={self.node_role.value})"
            )
            raise ValueError(msg)
        return self


class EntityAlias(TimestampedModel, VersionedModel):
    """Cross-system identifier bound to a canonical entity."""

    alias_id: str = Field(default_factory=generate_ulid)
    entity_id: str
    source_system: str
    raw_id: str
    raw_name: str | None = None
    match_confidence: float = 1.0
    is_primary: bool = False
