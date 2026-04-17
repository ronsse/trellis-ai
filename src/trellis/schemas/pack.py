"""Pack schema for Trellis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import TimestampedModel, VersionedModel, utc_now
from trellis.core.ids import generate_ulid
from trellis.schemas.advisory import Advisory


class PackItem(VersionedModel):
    """A single item included in a context pack."""

    item_id: str
    item_type: str  # trace, evidence, precedent, entity
    excerpt: str = ""
    relevance_score: float = 0.0
    included: bool = True
    rank: int | None = None
    selection_reason: str | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    estimated_tokens: int | None = None
    strategy_source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PackBudget(VersionedModel):
    """Budget constraints for a context pack."""

    max_items: int = 50
    max_tokens: int = 8000


class RejectedItem(VersionedModel):
    """An item that was considered but excluded from the pack."""

    item_id: str
    item_type: str
    relevance_score: float = 0.0
    reason: str  # dedup, structural_filter, max_items, token_budget
    strategy_source: str | None = None


class BudgetStep(VersionedModel):
    """One step in the budget consumption trace."""

    item_id: str
    item_tokens: int
    running_total: int
    included: bool


class RetrievalReport(VersionedModel):
    """Report on how pack items were retrieved."""

    queries_run: int = 0
    candidates_found: int = 0
    items_selected: int = 0
    duration_ms: int = 0
    strategies_used: list[str] = Field(default_factory=list)
    rejected_items: list[RejectedItem] = Field(default_factory=list)
    budget_trace: list[BudgetStep] = Field(default_factory=list)


class Pack(TimestampedModel, VersionedModel):
    """A context pack assembled for an agent or workflow."""

    pack_id: str = Field(default_factory=generate_ulid)
    intent: str
    items: list[PackItem] = Field(default_factory=list)
    retrieval_report: RetrievalReport = Field(default_factory=RetrievalReport)
    policies_applied: list[str] = Field(default_factory=list)
    budget: PackBudget = Field(default_factory=PackBudget)
    domain: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    skill_id: str | None = None
    target_entity_ids: list[str] = Field(default_factory=list)
    advisories: list[Advisory] = Field(default_factory=list)
    assembled_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sectioned pack models — tiered context retrieval
# ---------------------------------------------------------------------------


class SectionRequest(VersionedModel):
    """Request for one section of a sectioned pack.

    Each section targets a specific kind of knowledge (domain conventions,
    technical patterns, entity metadata, execution traces) with its own
    budget and filtering criteria. Applications define which sections each
    agent phase needs; the PackBuilder fills them independently.
    """

    name: str
    retrieval_affinities: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    max_tokens: int = 2000
    max_items: int = 10


class PackSection(VersionedModel):
    """One section of a sectioned pack, independently budgeted."""

    name: str
    items: list[PackItem] = Field(default_factory=list)
    retrieval_report: RetrievalReport = Field(default_factory=RetrievalReport)
    budget: PackBudget = Field(
        default_factory=lambda: PackBudget(max_items=10, max_tokens=2000)
    )


class SectionedPack(TimestampedModel, VersionedModel):
    """A context pack organized into independently budgeted sections.

    Each section targets a different retrieval tier (objective, strategic,
    tactical, reflective) with its own items, budget, and retrieval report.
    Sections are assembled from a shared candidate pool but budgeted and
    deduplicated independently.
    """

    pack_id: str = Field(default_factory=generate_ulid)
    intent: str
    sections: list[PackSection] = Field(default_factory=list)
    domain: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    advisories: list[Advisory] = Field(default_factory=list)
    assembled_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_items(self) -> int:
        """Total items across all sections."""
        return sum(len(s.items) for s in self.sections)

    @property
    def total_tokens(self) -> int:
        """Estimated total tokens across all sections."""
        return sum(
            item.estimated_tokens or (len(item.excerpt) // 4 + 1)
            for s in self.sections
            for item in s.items
        )

    @property
    def all_items(self) -> list[PackItem]:
        """Flatten all section items into a single list."""
        return [item for s in self.sections for item in s.items]
