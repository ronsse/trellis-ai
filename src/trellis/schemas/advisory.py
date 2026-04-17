"""Advisory schema for Trellis.

Advisories are deterministic, evidence-backed suggestions generated from
outcome data.  They are computed by the :class:`AdvisoryGenerator` and
delivered alongside context packs so agents know *what to do differently*
based on past successes and failures.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from trellis.core.base import TimestampedModel, VersionedModel
from trellis.core.ids import generate_ulid


class AdvisoryCategory(StrEnum):
    """Category of advice an advisory provides."""

    APPROACH = "approach"  # "Agents that validated schema first succeeded 82%"
    SCOPE = "scope"  # "Narrowing to 3 entities improved success by 40%"
    ENTITY = "entity"  # "Entity X in 80% of successful traces"
    ANTI_PATTERN = "anti_pattern"  # "Skipping validation → 3x failure rate"
    QUERY = "query"  # "Include 'deployment' in your context query"


class AdvisoryEvidence(VersionedModel):
    """Statistical backing for an advisory."""

    sample_size: int
    success_rate_with: float
    success_rate_without: float
    effect_size: float  # success_rate_with - success_rate_without
    representative_trace_ids: list[str] = Field(default_factory=list)


class Advisory(TimestampedModel, VersionedModel):
    """A single actionable suggestion for an agent.

    Advisories are generated deterministically from outcome data — never
    by an LLM at read time.  Each carries its statistical evidence so
    the consuming agent can weight it appropriately.
    """

    advisory_id: str = Field(default_factory=generate_ulid)
    category: AdvisoryCategory
    confidence: float  # 0.0-1.0, derived from sample size + effect size
    message: str  # Human/agent-readable suggestion
    evidence: AdvisoryEvidence
    scope: str  # domain, intent pattern, or entity type
    entity_id: str | None = None  # for ENTITY advisories
    metadata: dict[str, Any] = Field(default_factory=dict)
