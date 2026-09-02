"""Pack schema for Trellis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import TimestampedModel, VersionedModel, utc_now
from trellis.core.ids import generate_ulid
from trellis.schemas.advisory import Advisory


class PackItem(VersionedModel):
    """A single item included in a context pack.

    ``injected_advisory_ids`` (Unit C1, foundation for D1 axis C tightening)
    records which advisories — by ``Advisory.advisory_id`` — influenced this
    item's inclusion or ranking in the assembled pack. Empty list when no
    advisory matched this item. Currently populated for advisories whose
    ``entity_id`` equals the item's ``item_id`` (ENTITY / ANTI_PATTERN
    categories); other advisory categories are pack-scoped, not item-scoped.
    Lets downstream analyzers join ``advisory_id -> outcome`` per-item rather
    than relying on the coarser domain-scope proxy.
    """

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
    injected_advisory_ids: list[str] = Field(default_factory=list)
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
    reason: str  # dedup, structural_filter, max_items, token_budget, content_floor
    strategy_source: str | None = None

    @classmethod
    def from_pack_item(
        cls,
        item: PackItem,
        reason: str,
        *,
        strategy_source: str | None = None,
    ) -> RejectedItem:
        """Build the row for ``item`` being rejected under ``reason``.

        **The single constructor.** Every gate in the pipeline rejects a
        :class:`PackItem`, and every one of them wants the same four fields
        copied off it — so this is the only place that copying is written.
        It lives on the schema rather than on
        :class:`~trellis.retrieve.pack_builder.PackBuilder` because the
        content floor (:mod:`trellis.retrieve.excerpts`) is a gate too, and
        ``excerpts`` is imported *by* ``pack_builder``; a helper on the
        builder would be unreachable from there without a cycle.

        Eleven of the twelve ``item_type`` / ``relevance_score`` mutants
        across the six hand-built copies this replaces survived the
        **full** suite — the same count #456 measured over the retrieval
        subset alone, so widening the selection caught nothing extra. Only
        ``dedup``'s ``existing.relevance_score`` died. Nothing *branches*
        on either field, which is why six independent chances to swap,
        constant-fold or mistype them were each invisible; but both are
        serialised into ``PACK_ASSEMBLED.payload["rejected_items"]`` and
        rendered as the *Type* and *Relevance* columns of the Memory
        Explorer's "Rejected items" table, so a wrong value reached an
        operator as fact rather than going unread. One constructor is one
        thing to pin.

        ``strategy_source`` defaults to the item's own. Pass it only for a
        gate that runs before ``_promote_strategy_source`` has stamped the
        item, where the caller knows the axis and the item does not.
        """
        return cls(
            item_id=item.item_id,
            item_type=item.item_type,
            relevance_score=item.relevance_score,
            reason=reason,
            strategy_source=strategy_source or item.strategy_source,
        )


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
    #: Request-scoped identifier for the unit of work this pack was served
    #: to. Carried into ``PACK_ASSEMBLED`` telemetry so the learning join
    #: (:mod:`trellis.learning.pack_observations`) can attribute a promoted
    #: precedent to the runs that supported it. ``None`` when the caller has
    #: no run identity — the join then keeps its ``"unknown-run"`` bucket
    #: rather than borrowing ``session_id``, which is a coarser unit.
    run_id: str | None = None
    #: Canonical intent bucket for this pack, normalized by
    #: :func:`trellis.learning.scoring.normalize_intent_family`. Callers that
    #: already know their family (phase-driven workflows) pass it through;
    #: otherwise PackBuilder derives it from ``intent``.
    intent_family: str | None = None
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
    #: See :attr:`Pack.run_id` / :attr:`Pack.intent_family` — same
    #: request-scoped attribution, carried on the sectioned pack kind so
    #: both telemetry payloads describe a pack the same way.
    run_id: str | None = None
    intent_family: str | None = None
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
