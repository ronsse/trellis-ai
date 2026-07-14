"""Tests for PackBuilder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.core.hashing import content_hash
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


def _make_strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    """Create a mock strategy returning given items."""
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _item(item_id: str, score: float, excerpt: str = "text") -> PackItem:
    return PackItem(
        item_id=item_id, item_type="document", excerpt=excerpt, relevance_score=score
    )


class TestPackBuilder:
    def test_build_with_no_strategies(self) -> None:
        builder = PackBuilder()
        pack = builder.build("test query")
        assert pack.intent == "test query"
        assert pack.items == []
        assert pack.retrieval_report.queries_run == 0

    def test_build_with_single_strategy(self) -> None:
        s = _make_strategy("keyword", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("search")
        assert len(pack.items) == 2
        assert pack.items[0].item_id == "d1"
        assert pack.retrieval_report.queries_run == 1
        assert "keyword" in pack.retrieval_report.strategies_used

    def test_build_with_multiple_strategies(self) -> None:
        s1 = _make_strategy("keyword", [_item("d1", 0.9)])
        s2 = _make_strategy("semantic", [_item("v1", 0.85)])
        builder = PackBuilder(strategies=[s1, s2])
        pack = builder.build("search")
        assert len(pack.items) == 2
        assert pack.retrieval_report.queries_run == 2

    def test_deduplication_keeps_highest_score(self) -> None:
        s1 = _make_strategy("keyword", [_item("d1", 0.7)])
        s2 = _make_strategy("semantic", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s1, s2])
        pack = builder.build("search")
        assert len(pack.items) == 1
        assert pack.items[0].relevance_score == 0.9

    def test_sorted_by_relevance(self) -> None:
        s = _make_strategy("kw", [_item("a", 0.3), _item("b", 0.9), _item("c", 0.6)])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        scores = [item.relevance_score for item in pack.items]
        assert scores == sorted(scores, reverse=True)

    def test_budget_max_items(self) -> None:
        items = [_item(f"d{i}", 1.0 - i * 0.1) for i in range(10)]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q", budget=PackBudget(max_items=3, max_tokens=100000))
        assert len(pack.items) == 3

    def test_budget_max_tokens(self) -> None:
        # Each item has 100 chars => ~26 tokens (100//4+1)
        items = [_item(f"d{i}", 1.0 - i * 0.01, excerpt="x" * 100) for i in range(20)]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        # Budget of 100 tokens, each item ~26 tokens, so ~3-4 items fit
        pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=100))
        assert len(pack.items) < 20

    def test_domain_and_agent_id(self) -> None:
        builder = PackBuilder()
        pack = builder.build("q", domain="platform", agent_id="agent-1")
        assert pack.domain == "platform"
        assert pack.agent_id == "agent-1"

    def test_retrieval_report(self) -> None:
        s1 = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.8)])
        s2 = _make_strategy("sem", [_item("v1", 0.7)])
        builder = PackBuilder(strategies=[s1, s2])
        pack = builder.build("q")
        assert pack.retrieval_report.candidates_found == 3
        assert pack.retrieval_report.items_selected == 3
        assert pack.retrieval_report.strategies_used == ["kw", "sem"]

    def test_add_strategy(self) -> None:
        builder = PackBuilder()
        s = _make_strategy("kw", [_item("d1", 0.5)])
        builder.add_strategy(s)
        pack = builder.build("q")
        assert len(pack.items) == 1

    def test_strategy_failure_continues(self) -> None:
        good = _make_strategy("kw", [_item("d1", 0.9)])
        bad = _make_strategy("bad", [])
        bad.search.side_effect = RuntimeError("oops")
        builder = PackBuilder(strategies=[bad, good])
        pack = builder.build("q")
        assert len(pack.items) == 1  # good strategy still works

    def test_filters_passed_to_strategies(self) -> None:
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q", filters={"domain": "platform"})
        call_kwargs = s.search.call_args
        assert call_kwargs[1]["filters"] == {"domain": "platform"}

    def test_pack_has_assembled_at(self) -> None:
        builder = PackBuilder()
        pack = builder.build("q")
        assert pack.assembled_at is not None

    def test_budget_preserved_in_pack(self) -> None:
        budget = PackBudget(max_items=5, max_tokens=1000)
        builder = PackBuilder()
        pack = builder.build("q", budget=budget)
        assert pack.budget.max_items == 5
        assert pack.budget.max_tokens == 1000

    def test_tag_filters_passed_to_strategies(self) -> None:
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build(
            "q",
            tag_filters={
                "domain": {"in": ["data-pipeline"]},
                "signal_quality": {"in": ["high", "standard"]},
            },
        )
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert "content_tags" in filters
        assert filters["content_tags"]["domain"] == {"in": ["data-pipeline"]}
        assert filters["content_tags"]["signal_quality"] == {
            "in": ["high", "standard"],
        }

    def test_tag_filters_merged_with_existing_filters(self) -> None:
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build(
            "q",
            filters={"category": "tutorial"},
            tag_filters={"domain": {"in": ["api"]}},
        )
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert filters["category"] == "tutorial"
        assert filters["content_tags"]["domain"] == {"in": ["api"]}

    def test_default_noise_exclusion(self) -> None:
        """Noise excluded by default via ``{"not_in": ["noise"]}`` —
        robust to a future ``signal_quality`` value being added (the
        old enumerated allowlist would silently miss it)."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q", tag_filters={"domain": {"in": ["api"]}})
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert filters["content_tags"]["signal_quality"] == {
            "not_in": ["noise"],
        }

    def test_explicit_signal_quality_overrides_default(self) -> None:
        """When signal_quality is explicitly provided, no default exclusion."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q", tag_filters={"signal_quality": {"in": ["noise"]}})
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert filters["content_tags"]["signal_quality"] == {"in": ["noise"]}

    def test_no_tag_filters_no_content_tags_in_filters(self) -> None:
        """When tag_filters is None, no content_tags key added to filters."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q")
        call_kwargs = s.search.call_args
        assert call_kwargs[1]["filters"] is None

    def test_domain_param_translated_to_strategy_filters(self) -> None:
        """``domain=`` alone (no explicit filters) reaches strategies as
        both the scalar hint and the content_tags facet (#262)."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q", domain="infra")
        filters = s.search.call_args[1]["filters"]
        assert filters["domain"] == "infra"
        assert filters["content_tags"]["domain"] == {"in": ["infra"]}

    def test_domain_param_does_not_override_caller_filters(self) -> None:
        """An explicit caller-supplied domain filter wins over the param."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build(
            "q",
            domain="infra",
            filters={"domain": "explicit"},
            tag_filters={"domain": {"in": ["explicit"]}},
        )
        filters = s.search.call_args[1]["filters"]
        assert filters["domain"] == "explicit"
        assert filters["content_tags"]["domain"] == {"in": ["explicit"]}


class _FakeGraphStore:
    """Minimal GraphStore stand-in returning a fixed node list."""

    def __init__(self, nodes: list[dict[str, object]]) -> None:
        self._nodes = nodes

    def query(
        self,
        *,
        node_type: str | None = None,
        properties: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        del node_type, properties, limit
        return list(self._nodes)


class TestBuildDomainScopeGraphAxis:
    """`build(domain=...)` enforces the #254 default-pass contract on the
    graph axis: explicit mismatch excluded, domain-less passes (#262)."""

    def _nodes(self) -> list[dict[str, object]]:
        return [
            {
                "node_id": "n-mismatch",
                "node_type": "concept",
                "node_role": "semantic",
                "properties": {"name": "mismatch", "domain": "data"},
            },
            {
                "node_id": "n-plain",
                "node_type": "concept",
                "node_role": "semantic",
                "properties": {"name": "plain"},
            },
            {
                "node_id": "n-match",
                "node_type": "concept",
                "node_role": "semantic",
                "properties": {"name": "match", "domain": "infra"},
            },
        ]

    def test_mismatched_domain_node_excluded_domainless_passes(self) -> None:
        from trellis.retrieve.strategies import GraphSearch

        builder = PackBuilder(strategies=[GraphSearch(_FakeGraphStore(self._nodes()))])
        pack = builder.build("q", domain="infra")
        ids = {item.item_id for item in pack.items}
        assert "n-mismatch" not in ids
        assert "n-plain" in ids
        assert "n-match" in ids

    def test_no_domain_returns_everything(self) -> None:
        from trellis.retrieve.strategies import GraphSearch

        builder = PackBuilder(strategies=[GraphSearch(_FakeGraphStore(self._nodes()))])
        pack = builder.build("q")
        ids = {item.item_id for item in pack.items}
        assert ids == {"n-mismatch", "n-plain", "n-match"}

    def test_facet_domain_mismatch_excluded(self) -> None:
        """The ``properties.content_tags.domain`` facet location is honored
        on the graph axis too."""
        from trellis.retrieve.strategies import GraphSearch

        nodes: list[dict[str, object]] = [
            {
                "node_id": "n-facet-mismatch",
                "node_type": "concept",
                "node_role": "semantic",
                "properties": {
                    "name": "facet mismatch",
                    "content_tags": {"domain": ["data"]},
                },
            },
            {
                "node_id": "n-facet-match",
                "node_type": "concept",
                "node_role": "semantic",
                "properties": {
                    "name": "facet match",
                    "content_tags": {"domain": ["infra"]},
                },
            },
        ]
        builder = PackBuilder(strategies=[GraphSearch(_FakeGraphStore(nodes))])
        pack = builder.build("q", domain="infra")
        ids = {item.item_id for item in pack.items}
        assert "n-facet-mismatch" not in ids
        assert "n-facet-match" in ids

    def test_selected_items_gain_observability_fields(self) -> None:
        s = _make_strategy(
            "kw",
            [
                _item("d1", 0.9, excerpt="x" * 40),
                _item("d2", 0.7, excerpt="y" * 20),
            ],
        )
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")

        assert [item.rank for item in pack.items] == [1, 2]
        assert [item.included for item in pack.items] == [True, True]
        assert pack.items[0].selection_reason == "selected_by_relevance"
        assert pack.items[0].score_breakdown == {"relevance_score": 0.9}
        assert pack.items[0].estimated_tokens == 11
        assert pack.items[1].estimated_tokens == 6


class TestBuildSectioned:
    """Tests for PackBuilder.build_sectioned()."""

    def test_produces_sections(self) -> None:
        """Each SectionRequest produces a corresponding PackSection."""
        items = [
            _item("doc1", 0.9, "ownership rules"),
            _item("code1", 0.8, "SELECT * FROM table"),
        ]
        # Tag items with metadata for tier mapping
        items[0] = items[0].model_copy(
            update={
                "metadata": {
                    "content_tags": {
                        "content_type": "documentation",
                        "scope": "org",
                    }
                }
            }
        )
        items[1] = items[1].model_copy(
            update={
                "item_type": "entity",
                "metadata": {"content_tags": {"content_type": "code"}},
            }
        )

        strategy = _make_strategy("test", items)
        builder = PackBuilder(strategies=[strategy])
        pack = builder.build_sectioned(
            intent="test",
            sections=[
                SectionRequest(
                    name="domain", retrieval_affinities=["domain_knowledge"]
                ),
                SectionRequest(name="tactical", retrieval_affinities=["reference"]),
            ],
        )
        assert len(pack.sections) == 2
        section_names = [s.name for s in pack.sections]
        assert "domain" in section_names
        assert "tactical" in section_names

    def test_per_section_budget(self) -> None:
        """Each section respects its own max_items."""
        items = [_item(f"item_{i}", 0.9 - i * 0.05, "x" * 100) for i in range(10)]
        strategy = _make_strategy("test", items)
        builder = PackBuilder(strategies=[strategy])
        pack = builder.build_sectioned(
            intent="test",
            sections=[SectionRequest(name="all", max_items=3, max_tokens=50000)],
        )
        assert len(pack.sections[0].items) <= 3

    def test_cross_section_dedup(self) -> None:
        """Item in multiple sections kept only in highest-scoring one."""
        item = _item("shared", 0.9, "shared content")
        item = item.model_copy(
            update={
                "metadata": {
                    "content_tags": {
                        "content_type": "pattern",
                        "scope": "org",
                        "retrieval_affinity": [
                            "domain_knowledge",
                            "technical_pattern",
                        ],
                    }
                }
            }
        )
        strategy = _make_strategy("test", [item])
        builder = PackBuilder(strategies=[strategy])
        pack = builder.build_sectioned(
            intent="test",
            sections=[
                SectionRequest(
                    name="domain", retrieval_affinities=["domain_knowledge"]
                ),
                SectionRequest(
                    name="patterns", retrieval_affinities=["technical_pattern"]
                ),
            ],
        )
        # Item should appear in exactly one section
        total = sum(len(s.items) for s in pack.sections)
        assert total == 1

    def test_empty_section_when_no_matches(self) -> None:
        """A section with no matching items produces an empty PackSection."""
        items = [_item("code1", 0.9, "python code")]
        items[0] = items[0].model_copy(update={"item_type": "entity"})
        strategy = _make_strategy("test", items)
        builder = PackBuilder(strategies=[strategy])
        pack = builder.build_sectioned(
            intent="test",
            sections=[
                SectionRequest(name="traces", retrieval_affinities=["operational"]),
            ],
        )
        assert pack.sections[0].items == []

    def test_selection_reason_includes_section_name(self) -> None:
        """Selected items are annotated with the section they belong to."""
        items = [_item("doc1", 0.9, "content")]
        strategy = _make_strategy("test", items)
        builder = PackBuilder(strategies=[strategy])
        pack = builder.build_sectioned(
            intent="test",
            sections=[SectionRequest(name="mySection")],
        )
        for item in pack.sections[0].items:
            assert "mySection" in (item.selection_reason or "")


def _role_item(item_id: str, score: float, node_role: str) -> PackItem:
    """PackItem with a node_role stamped into metadata."""
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt="entity excerpt",
        relevance_score=score,
        metadata={"node_role": node_role},
    )


class TestStructuralFiltering:
    """PackBuilder drops structural items by default."""

    def test_structural_items_dropped_by_default(self) -> None:
        items = [
            _role_item("e1", 0.9, "semantic"),
            _role_item("col1", 0.85, "structural"),
            _role_item("cluster1", 0.8, "curated"),
        ]
        builder = PackBuilder(strategies=[_make_strategy("graph", items)])
        pack = builder.build("q")
        item_ids = [i.item_id for i in pack.items]
        assert "col1" not in item_ids
        assert "e1" in item_ids
        assert "cluster1" in item_ids

    def test_include_structural_opt_in(self) -> None:
        items = [
            _role_item("e1", 0.9, "semantic"),
            _role_item("col1", 0.85, "structural"),
        ]
        builder = PackBuilder(strategies=[_make_strategy("graph", items)])
        pack = builder.build("q", include_structural=True)
        item_ids = [i.item_id for i in pack.items]
        assert "col1" in item_ids
        assert "e1" in item_ids

    def test_include_structural_propagated_to_strategy_filters(self) -> None:
        """Filter must carry include_structural into per-strategy filters."""
        strategy = _make_strategy("graph", [])
        builder = PackBuilder(strategies=[strategy])
        builder.build("q", include_structural=True)
        # Inspect the filters kwarg passed to the strategy
        call_kwargs = strategy.search.call_args.kwargs
        assert call_kwargs["filters"]["include_structural"] is True

    def test_default_strategy_filters_exclude_structural_flag(self) -> None:
        strategy = _make_strategy("graph", [])
        builder = PackBuilder(strategies=[strategy])
        builder.build("q")
        call_kwargs = strategy.search.call_args.kwargs
        # Default path should not inject include_structural (keeps behaviour
        # for strategies that don't understand the flag).
        if call_kwargs["filters"] is not None:
            assert "include_structural" not in call_kwargs["filters"]

    def test_sectioned_build_drops_structural(self) -> None:
        items = [
            _role_item("e1", 0.9, "semantic"),
            _role_item("col1", 0.85, "structural"),
        ]
        strategy = _make_strategy("graph", items)
        builder = PackBuilder(strategies=[strategy])
        pack = builder.build_sectioned(
            intent="q",
            sections=[SectionRequest(name="all")],
        )
        all_ids = [i.item_id for s in pack.sections for i in s.items]
        assert "col1" not in all_ids
        assert "e1" in all_ids


# ---------------------------------------------------------------------------
# Decision trail tests (Phase 1: pack observability)
# ---------------------------------------------------------------------------


def _strategy_item(
    item_id: str, score: float, strategy: str, excerpt: str = "text"
) -> PackItem:
    """Create a PackItem with source_strategy in metadata."""
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=excerpt,
        relevance_score=score,
        metadata={"source_strategy": strategy},
    )


class TestStrategySourcePromotion:
    """strategy_source is promoted from metadata to the first-class field."""

    def test_strategy_source_promoted_from_metadata(self) -> None:
        s = _make_strategy("kw", [_strategy_item("d1", 0.9, "keyword")])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        assert pack.items[0].strategy_source == "keyword"

    def test_explicit_strategy_source_not_overridden(self) -> None:
        item = PackItem(
            item_id="d1",
            item_type="document",
            excerpt="text",
            relevance_score=0.9,
            strategy_source="custom",
            metadata={"source_strategy": "keyword"},
        )
        s = _make_strategy("kw", [item])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        assert pack.items[0].strategy_source == "custom"

    def test_no_metadata_source_strategy_leaves_none(self) -> None:
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        assert pack.items[0].strategy_source is None


class TestRejectionTracking:
    """Rejected items are tracked with reasons at each filtering stage."""

    def test_dedup_rejection_tracked(self) -> None:
        s1 = _make_strategy("kw", [_strategy_item("d1", 0.7, "keyword")])
        s2 = _make_strategy("sem", [_strategy_item("d1", 0.9, "semantic")])
        builder = PackBuilder(strategies=[s1, s2])
        pack = builder.build("q")

        rejected = pack.retrieval_report.rejected_items
        assert len(rejected) == 1
        assert rejected[0].item_id == "d1"
        assert rejected[0].reason == "dedup"
        assert rejected[0].relevance_score == 0.7

    def test_structural_filter_rejection_tracked(self) -> None:
        items = [
            _role_item("e1", 0.9, "semantic"),
            _role_item("col1", 0.85, "structural"),
        ]
        builder = PackBuilder(strategies=[_make_strategy("graph", items)])
        pack = builder.build("q")

        rejected = pack.retrieval_report.rejected_items
        structural_rejected = [r for r in rejected if r.reason == "structural_filter"]
        assert len(structural_rejected) == 1
        assert structural_rejected[0].item_id == "col1"

    def test_max_items_rejection_tracked(self) -> None:
        items = [_item(f"d{i}", 1.0 - i * 0.1) for i in range(5)]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q", budget=PackBudget(max_items=2, max_tokens=100000))

        rejected = pack.retrieval_report.rejected_items
        max_items_rejected = [r for r in rejected if r.reason == "max_items"]
        assert len(max_items_rejected) == 3
        # Rejected items are the lowest-scoring ones
        rejected_ids = {r.item_id for r in max_items_rejected}
        assert "d2" in rejected_ids
        assert "d3" in rejected_ids
        assert "d4" in rejected_ids

    def test_token_budget_rejection_tracked(self) -> None:
        # Each item ~26 tokens (100 chars). Budget=50 tokens → 1 item fits.
        items = [
            _item("d0", 0.9, excerpt="x" * 100),
            _item("d1", 0.8, excerpt="y" * 100),
            _item("d2", 0.7, excerpt="z" * 100),
        ]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=50))

        rejected = pack.retrieval_report.rejected_items
        token_rejected = [r for r in rejected if r.reason == "token_budget"]
        assert len(token_rejected) == 2
        assert token_rejected[0].item_id == "d1"
        assert token_rejected[1].item_id == "d2"

    def test_rejection_carries_strategy_source(self) -> None:
        s1 = _make_strategy("kw", [_strategy_item("d1", 0.7, "keyword")])
        s2 = _make_strategy("sem", [_strategy_item("d1", 0.9, "semantic")])
        builder = PackBuilder(strategies=[s1, s2])
        pack = builder.build("q")

        rejected = pack.retrieval_report.rejected_items
        assert rejected[0].strategy_source == "keyword"


class TestBudgetTrace:
    """Budget consumption trace records running token totals."""

    def test_budget_trace_records_all_items(self) -> None:
        items = [
            _item("d0", 0.9, excerpt="x" * 40),  # 11 tokens
            _item("d1", 0.8, excerpt="y" * 80),  # 21 tokens
        ]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")

        trace = pack.retrieval_report.budget_trace
        assert len(trace) == 2
        assert trace[0].item_id == "d0"
        assert trace[0].item_tokens == 11
        assert trace[0].running_total == 11
        assert trace[0].included is True
        assert trace[1].item_id == "d1"
        assert trace[1].running_total == 32  # 11 + 21
        assert trace[1].included is True

    def test_budget_trace_marks_excluded_items(self) -> None:
        items = [
            _item("d0", 0.9, excerpt="x" * 40),  # 11 tokens
            _item("d1", 0.8, excerpt="y" * 200),  # 51 tokens
        ]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=30))

        trace = pack.retrieval_report.budget_trace
        assert len(trace) == 2
        assert trace[0].included is True
        assert trace[1].included is False
        # Running total stays at last included item's total
        assert trace[1].running_total == 11

    def test_budget_trace_empty_when_no_items(self) -> None:
        builder = PackBuilder()
        pack = builder.build("q")
        assert pack.retrieval_report.budget_trace == []


# ---------------------------------------------------------------------------
# Advisory delivery tests (Phase 3)
# ---------------------------------------------------------------------------


def _make_advisory(
    *,
    scope: str = "global",
    category: AdvisoryCategory = AdvisoryCategory.ENTITY,
    confidence: float = 0.8,
    entity_id: str | None = None,
    advisory_id: str | None = None,
) -> Advisory:
    kwargs: dict[str, object] = {
        "category": category,
        "confidence": confidence,
        "message": f"Test advisory ({category.value})",
        "evidence": AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        ),
        "scope": scope,
        "entity_id": entity_id,
    }
    if advisory_id is not None:
        kwargs["advisory_id"] = advisory_id
    return Advisory(**kwargs)  # type: ignore[arg-type]


class TestAdvisoryDelivery:
    """PackBuilder attaches matching advisories to packs."""

    def test_no_advisory_store_returns_empty(self) -> None:
        builder = PackBuilder()
        pack = builder.build("q")
        assert pack.advisories == []

    def test_advisories_attached_to_flat_pack(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_make_advisory(scope="global"))
        store.put(_make_advisory(scope="platform"))

        builder = PackBuilder(advisory_store=store)
        pack = builder.build("q", domain="platform")
        # Should get both global and platform-scoped advisories
        assert len(pack.advisories) == 2

    def test_advisories_filtered_by_domain(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_make_advisory(scope="global"))
        store.put(_make_advisory(scope="data"))

        builder = PackBuilder(advisory_store=store)
        pack = builder.build("q", domain="platform")
        # "data" scope should not match "platform" domain
        assert len(pack.advisories) == 1
        assert pack.advisories[0].scope == "global"

    def test_advisories_attached_to_sectioned_pack(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_make_advisory(scope="global"))

        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
        )
        assert len(pack.advisories) == 1

    def test_no_domain_matches_global_only(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_make_advisory(scope="global"))
        store.put(_make_advisory(scope="platform"))

        builder = PackBuilder(advisory_store=store)
        pack = builder.build("q")  # no domain
        # Only global advisories match when no domain specified
        assert len(pack.advisories) == 1
        assert pack.advisories[0].scope == "global"

    def test_low_confidence_advisories_suppressed(self, tmp_path: Path) -> None:
        """Advisories below _ADVISORY_MIN_CONFIDENCE are not delivered."""
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_make_advisory(scope="global", confidence=0.05))
        store.put(_make_advisory(scope="global", confidence=0.5))

        builder = PackBuilder(advisory_store=store)
        pack = builder.build("q")
        # Only the 0.5 confidence advisory should be delivered
        assert len(pack.advisories) == 1
        assert pack.advisories[0].confidence == 0.5


# ---------------------------------------------------------------------------
# Unit C1 — per-item advisory provenance (foundation for axis C tightening)
# ---------------------------------------------------------------------------


class TestAdvisoryProvenance:
    """``PackItem.injected_advisory_ids`` records per-item advisory influence.

    Foundation for D1 (axis C semantic tightening): downstream analyzers
    can join ``advisory_id -> outcome`` per-item instead of using the
    coarser domain-scope proxy. An advisory with ``entity_id`` set
    influences the ``PackItem`` whose ``item_id`` matches; advisories
    without ``entity_id`` are pack-scoped and never stamp items.
    """

    def test_default_field_is_empty_list(self) -> None:
        """No advisories configured → field stays empty (preserves prior behavior)."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        assert all(item.injected_advisory_ids == [] for item in pack.items)

    def test_advisory_without_entity_id_does_not_stamp_items(
        self, tmp_path: Path
    ) -> None:
        """Pack-scoped advisories (APPROACH/SCOPE/QUERY) leave items untouched."""
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_make_advisory(scope="global", category=AdvisoryCategory.APPROACH))
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build("q")
        assert len(pack.advisories) == 1
        assert pack.items[0].injected_advisory_ids == []

    def test_advisory_with_entity_id_stamps_matching_item(self, tmp_path: Path) -> None:
        """When ``advisory.entity_id == item.item_id``, the advisory ID is stamped."""
        store = AdvisoryStore(tmp_path / "adv.json")
        advisory = _make_advisory(
            scope="global",
            category=AdvisoryCategory.ENTITY,
            entity_id="d1",
        )
        store.put(advisory)
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build("q")

        d1 = next(i for i in pack.items if i.item_id == "d1")
        d2 = next(i for i in pack.items if i.item_id == "d2")
        assert d1.injected_advisory_ids == [advisory.advisory_id]
        assert d2.injected_advisory_ids == []

    def test_anti_pattern_advisory_stamps_matching_item(self, tmp_path: Path) -> None:
        """ANTI_PATTERN advisories also carry ``entity_id`` and influence items."""
        store = AdvisoryStore(tmp_path / "adv.json")
        advisory = _make_advisory(
            scope="global",
            category=AdvisoryCategory.ANTI_PATTERN,
            entity_id="d1",
        )
        store.put(advisory)
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build("q")
        assert pack.items[0].injected_advisory_ids == [advisory.advisory_id]

    def test_multiple_advisories_target_same_item(self, tmp_path: Path) -> None:
        """Each matching advisory appends its ID; ordering matches iteration."""
        store = AdvisoryStore(tmp_path / "adv.json")
        a1 = _make_advisory(
            scope="global",
            category=AdvisoryCategory.ENTITY,
            entity_id="d1",
            advisory_id="adv-1",
        )
        a2 = _make_advisory(
            scope="global",
            category=AdvisoryCategory.ANTI_PATTERN,
            entity_id="d1",
            advisory_id="adv-2",
        )
        store.put(a1)
        store.put(a2)
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build("q")
        # Both advisory IDs present; order is stable but not guaranteed
        # (advisory_store sort order is implementation-detail).
        assert set(pack.items[0].injected_advisory_ids) == {"adv-1", "adv-2"}

    def test_no_advisories_configured_leaves_field_empty(self) -> None:
        """No advisory store at all → field stays empty (default-empty contract)."""
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s])  # no advisory_store
        pack = builder.build("q")
        assert pack.advisories == []
        assert pack.items[0].injected_advisory_ids == []

    def test_advisory_for_nonexistent_item_no_op(self, tmp_path: Path) -> None:
        """Advisory targeting an item_id absent from the pack does not error."""
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(
            _make_advisory(
                scope="global",
                category=AdvisoryCategory.ENTITY,
                entity_id="absent-id",
            )
        )
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build("q")
        assert pack.items[0].injected_advisory_ids == []

    def test_sectioned_pack_stamps_per_item_advisory_provenance(
        self, tmp_path: Path
    ) -> None:
        """``build_sectioned`` propagates advisory provenance into each section."""
        store = AdvisoryStore(tmp_path / "adv.json")
        advisory = _make_advisory(
            scope="global",
            category=AdvisoryCategory.ENTITY,
            entity_id="d1",
        )
        store.put(advisory)
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], advisory_store=store)
        pack = builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
        )
        assert len(pack.advisories) == 1
        # Find the stamped item across all sections.
        d1_items = [i for s in pack.sections for i in s.items if i.item_id == "d1"]
        d2_items = [i for s in pack.sections for i in s.items if i.item_id == "d2"]
        assert len(d1_items) == 1
        assert d1_items[0].injected_advisory_ids == [advisory.advisory_id]
        assert all(i.injected_advisory_ids == [] for i in d2_items)

    def test_pack_assembled_telemetry_includes_per_item_advisory_ids(
        self, tmp_path: Path
    ) -> None:
        """``PACK_ASSEMBLED`` payload surfaces per-item advisory IDs for analyzers."""
        store = AdvisoryStore(tmp_path / "adv.json")
        advisory = _make_advisory(
            scope="global",
            category=AdvisoryCategory.ENTITY,
            entity_id="d1",
        )
        store.put(advisory)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
            builder = PackBuilder(
                strategies=[s], advisory_store=store, event_log=event_log
            )
            builder.build("q")
            events = event_log.get_events(limit=10)
            assert len(events) == 1
            payload = events[0].payload
            injected = payload["injected_items"]
            by_id = {row["item_id"]: row for row in injected}
            assert by_id["d1"]["injected_advisory_ids"] == [advisory.advisory_id]
            assert by_id["d2"]["injected_advisory_ids"] == []
        finally:
            event_log.close()


# ---------------------------------------------------------------------------
# Session-aware dedup tests
# ---------------------------------------------------------------------------


@pytest.fixture
def session_event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "session_events.db")
    yield log
    log.close()


class TestSessionDedup:
    """Items recently served in the same session are excluded."""

    def test_no_session_id_no_dedup(self, session_event_log: SQLiteEventLog) -> None:
        """Without session_id, repeated builds return the same items."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        first = builder.build("q")
        second = builder.build("q")
        assert {i.item_id for i in first.items} == {"d1", "d2"}
        assert {i.item_id for i in second.items} == {"d1", "d2"}

    def test_session_id_excludes_previously_served_items(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """Second build in same session drops items served in first build."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        first = builder.build("q", session_id="sess-A")
        assert {i.item_id for i in first.items} == {"d1", "d2"}

        second = builder.build("q", session_id="sess-A")
        assert second.items == []

    def test_session_dedup_tracks_rejections(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """Session-deduped items appear in RejectedItem list with reason."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build("q", session_id="sess-B")
        second = builder.build("q", session_id="sess-B")
        reasons = {r.reason for r in second.retrieval_report.rejected_items}
        assert "session_dedup" in reasons
        deduped_ids = {
            r.item_id
            for r in second.retrieval_report.rejected_items
            if r.reason == "session_dedup"
        }
        assert deduped_ids == {"d1", "d2"}

    def test_different_sessions_isolated(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """Items served to session A are still available for session B."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build("q", session_id="sess-A")
        other = builder.build("q", session_id="sess-B")
        assert {i.item_id for i in other.items} == {"d1", "d2"}

    def test_session_id_recorded_on_pack(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """session_id is recorded on the returned Pack."""
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        pack = builder.build("q", session_id="sess-C")
        assert pack.session_id == "sess-C"

    def test_session_id_in_pack_assembled_event(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """PACK_ASSEMBLED payload carries session_id for downstream dedup."""
        from trellis.stores.base.event_log import EventType

        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build("q", session_id="sess-D")
        events = session_event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert len(events) == 1
        assert events[0].payload.get("session_id") == "sess-D"

    def test_no_event_log_no_dedup(self) -> None:
        """Without an event log, session_id is recorded but no dedup runs."""
        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s])  # no event_log
        first = builder.build("q", session_id="sess-E")
        second = builder.build("q", session_id="sess-E")
        assert len(first.items) == 1
        assert len(second.items) == 1  # no dedup without event log

    def test_sectioned_session_dedup(self, session_event_log: SQLiteEventLog) -> None:
        """Sectioned pack also drops items served in previous session builds."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        first = builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
            session_id="sess-F",
        )
        assert len(first.all_items) == 2

        second = builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
            session_id="sess-F",
        )
        assert second.all_items == []

    def test_sectioned_session_id_recorded(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """SectionedPack carries session_id and emits it on telemetry."""
        from trellis.stores.base.event_log import EventType

        s = _make_strategy("kw", [_item("d1", 0.9)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        pack = builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
            session_id="sess-G",
        )
        assert pack.session_id == "sess-G"
        events = session_event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert events[0].payload.get("session_id") == "sess-G"

    def test_flat_build_dedups_against_sectioned_build(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """A flat build sees items served by a prior sectioned build."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
            session_id="sess-H",
        )
        flat = builder.build("q", session_id="sess-H")
        assert flat.items == []


# ---------------------------------------------------------------------------
# Issue #258 — session-delta packs: content-hash re-serve, refresh, bounds
# ---------------------------------------------------------------------------


def _emit_served_event(
    log: SQLiteEventLog,
    *,
    session_id: str,
    item_id: str,
    excerpt: str,
    occurred_at: datetime | None = None,
    with_hash: bool = True,
) -> None:
    """Append a synthetic ``PACK_ASSEMBLED`` served-item event.

    ``with_hash=False`` produces a *thin* payload (no ``injected_item_hashes``)
    matching events emitted before issue #258, so the id-only suppression
    path can be exercised. ``occurred_at`` lets a test place the event
    inside or outside the dedup window deterministically.
    """
    payload: dict[str, object] = {
        "session_id": session_id,
        "injected_item_ids": [item_id],
    }
    if with_hash:
        payload["injected_item_hashes"] = {item_id: content_hash(excerpt)}
    event = Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=f"pack-{item_id}",
        entity_type="pack",
        payload=payload,
    )
    if occurred_at is not None:
        event = event.model_copy(update={"occurred_at": occurred_at})
    log.append(event)


class TestSessionDeltaPacks:
    """Content-hash re-serve rule, ``refresh`` bypass, and window bounds."""

    def test_same_id_same_content_suppressed(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """(a) Same id + unchanged content within window → suppressed."""
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="v1")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        first = builder.build("q", session_id="sess-same")
        assert {i.item_id for i in first.items} == {"d1"}
        # Second call: identical content → still suppressed.
        second = builder.build("q", session_id="sess-same")
        assert second.items == []

    def test_same_id_changed_content_reserved(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """(b) Same id + CHANGED content → eligible for re-serving."""
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="original")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        first = builder.build("q", session_id="sess-change")
        assert {i.item_id for i in first.items} == {"d1"}

        # Same id, superseded content — content hash no longer matches.
        s.search.return_value = [_item("d1", 0.9, excerpt="superseded content")]
        second = builder.build("q", session_id="sess-change")
        assert [i.item_id for i in second.items] == ["d1"]
        assert second.items[0].excerpt == "superseded content"

    def test_thin_event_suppressed_by_id(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """(c) Historical event without hashes → suppressed by id, as before."""
        _emit_served_event(
            session_event_log,
            session_id="sess-thin",
            item_id="d1",
            excerpt="whatever",
            with_hash=False,
        )
        # Candidate has *different* content, but the thin event carries no
        # hash, so we cannot tell it changed → suppress by id (no KeyError).
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="different now")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        pack = builder.build("q", session_id="sess-thin")
        assert pack.items == []

    def test_refresh_bypasses_session_dedup(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """refresh=True re-serves previously-served items for that call only."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build("q", session_id="sess-refresh")

        # Default path still suppresses.
        deduped = builder.build("q", session_id="sess-refresh")
        assert deduped.items == []

        # refresh=True bypasses the served-set subtraction.
        refreshed = builder.build("q", session_id="sess-refresh", refresh=True)
        assert {i.item_id for i in refreshed.items} == {"d1", "d2"}

        # And it is scoped to that call only — the next default call dedups.
        after = builder.build("q", session_id="sess-refresh")
        assert after.items == []

    def test_refresh_bypasses_sectioned_session_dedup(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """refresh=True also bypasses dedup on the sectioned path."""
        s = _make_strategy("kw", [_item("d1", 0.9), _item("d2", 0.7)])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build_sectioned(
            "q", sections=[SectionRequest(name="all")], session_id="sess-sec-r"
        )
        deduped = builder.build_sectioned(
            "q", sections=[SectionRequest(name="all")], session_id="sess-sec-r"
        )
        assert deduped.all_items == []
        refreshed = builder.build_sectioned(
            "q",
            sections=[SectionRequest(name="all")],
            session_id="sess-sec-r",
            refresh=True,
        )
        assert {i.item_id for i in refreshed.all_items} == {"d1", "d2"}

    def test_sectioned_changed_content_reserved(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """Content-hash re-serve rule applies to the sectioned path too."""
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="original")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build_sectioned(
            "q", sections=[SectionRequest(name="all")], session_id="sess-sec-c"
        )
        s.search.return_value = [_item("d1", 0.9, excerpt="new content")]
        second = builder.build_sectioned(
            "q", sections=[SectionRequest(name="all")], session_id="sess-sec-c"
        )
        assert [i.item_id for i in second.all_items] == ["d1"]

    def test_flat_payload_carries_item_hashes(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """Flat PACK_ASSEMBLED payload records per-item content hashes."""
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="hash me")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build("q", session_id="sess-flat-h")
        events = session_event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        hashes = events[0].payload.get("injected_item_hashes")
        assert hashes == {"d1": content_hash("hash me")}

    def test_sectioned_payload_carries_item_hashes(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """Sectioned PACK_ASSEMBLED payload records per-item content hashes."""
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="sec hash")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        builder.build_sectioned(
            "q", sections=[SectionRequest(name="all")], session_id="sess-sec-h"
        )
        events = session_event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        hashes = events[0].payload.get("injected_item_hashes")
        assert hashes == {"d1": content_hash("sec hash")}

    def test_event_outside_time_window_not_suppressed(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """An event older than the window is not consulted → item re-served."""
        old = datetime.now(UTC) - timedelta(minutes=120)
        _emit_served_event(
            session_event_log,
            session_id="sess-win",
            item_id="d1",
            excerpt="text",
            occurred_at=old,
        )
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="text")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        # 60-minute window (default) excludes the 120-minute-old serve.
        pack = builder.build(
            "q", session_id="sess-win", session_dedup_window_minutes=60
        )
        assert {i.item_id for i in pack.items} == {"d1"}

    def test_event_inside_time_window_suppressed(
        self, session_event_log: SQLiteEventLog
    ) -> None:
        """An event inside the window is consulted → item suppressed."""
        recent = datetime.now(UTC) - timedelta(minutes=30)
        _emit_served_event(
            session_event_log,
            session_id="sess-win2",
            item_id="d1",
            excerpt="text",
            occurred_at=recent,
        )
        s = _make_strategy("kw", [_item("d1", 0.9, excerpt="text")])
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        pack = builder.build(
            "q", session_id="sess-win2", session_dedup_window_minutes=60
        )
        assert pack.items == []

    def test_event_beyond_count_limit_not_suppressed(
        self,
        session_event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Serves older than the event-count cap fall out of the served-set."""
        monkeypatch.setattr(
            "trellis.retrieve.pack_builder.DEFAULT_SESSION_DEDUP_EVENT_LIMIT", 1
        )
        base = datetime.now(UTC) - timedelta(minutes=5)
        # Oldest serve (d1) — will be dropped by the count cap.
        _emit_served_event(
            session_event_log,
            session_id="sess-cap",
            item_id="d1",
            excerpt="text",
            occurred_at=base,
        )
        # Newest serve (d2) — the only event the cap of 1 keeps.
        _emit_served_event(
            session_event_log,
            session_id="sess-cap",
            item_id="d2",
            excerpt="text",
            occurred_at=base + timedelta(minutes=1),
        )
        s = _make_strategy(
            "kw", [_item("d1", 0.9, excerpt="text"), _item("d2", 0.7, excerpt="text")]
        )
        builder = PackBuilder(strategies=[s], event_log=session_event_log)
        pack = builder.build("q", session_id="sess-cap")
        # d2 (newest, within cap) suppressed; d1 (beyond cap) re-served.
        assert {i.item_id for i in pack.items} == {"d1"}


# ---------------------------------------------------------------------------
# Gap 3.2 — Semantic / fuzzy dedup via MinHash/LSH
# ---------------------------------------------------------------------------


class TestSemanticDedup:
    """PackBuilder collapses near-duplicate excerpts that survived
    exact-item_id dedup (mirrored schemas, cross-system clones)."""

    _LONG_EXCERPT = (
        "Deploying to production requires running the full migration suite, "
        "validating the schema against the staging database, and confirming "
        "that all downstream consumers have updated their clients. "
    )

    def _near_duplicate(self, base: str) -> str:
        # Tiny perturbation: whitespace + punctuation changes. MinHash with
        # a 0.85 Jaccard threshold should catch this easily.
        return base.replace(". ", ".  ").replace(",", " ,")

    def test_disabled_by_default(self) -> None:
        from trellis.retrieve.pack_builder import SemanticDedupConfig  # noqa: F401

        s = _make_strategy(
            "kw",
            [
                _item("a", 0.9, excerpt=self._LONG_EXCERPT),
                _item("b", 0.8, excerpt=self._LONG_EXCERPT),
            ],
        )
        builder = PackBuilder(strategies=[s])  # no config → disabled
        pack = builder.build("q")
        # Both items pass through (distinct item_ids, same excerpt).
        assert {i.item_id for i in pack.items} == {"a", "b"}

    def test_collapses_near_duplicate_excerpts(self) -> None:
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        dup = self._near_duplicate(self._LONG_EXCERPT)
        s = _make_strategy(
            "kw",
            [
                _item("winner", 0.95, excerpt=self._LONG_EXCERPT),
                _item("loser", 0.5, excerpt=dup),
            ],
        )
        builder = PackBuilder(
            strategies=[s],
            semantic_dedup=SemanticDedupConfig(),
        )
        pack = builder.build("q")

        # Higher-scoring wins, loser rejected with reason=semantic_dedup.
        assert [i.item_id for i in pack.items] == ["winner"]
        reasons = {r.reason for r in pack.retrieval_report.rejected_items}
        assert "semantic_dedup" in reasons

    def test_preserves_distinct_excerpts(self) -> None:
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        s = _make_strategy(
            "kw",
            [
                _item("a", 0.9, excerpt="Migration guide for v2 to v3 upgrades"),
                _item(
                    "b",
                    0.8,
                    excerpt="Rollback procedure when a release fails validation",
                ),
            ],
        )
        builder = PackBuilder(
            strategies=[s],
            semantic_dedup=SemanticDedupConfig(),
        )
        pack = builder.build("q")
        assert {i.item_id for i in pack.items} == {"a", "b"}

    def test_short_excerpts_below_entropy_pass_through(self) -> None:
        """MinHash's entropy filter refuses to dedup trivially short text."""
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        # min_shingles=5 with shingle_size=3 means text needs 8+ chars
        s = _make_strategy(
            "kw",
            [
                _item("a", 0.9, excerpt="TBD"),
                _item("b", 0.8, excerpt="TBD"),
            ],
        )
        builder = PackBuilder(
            strategies=[s],
            semantic_dedup=SemanticDedupConfig(),
        )
        pack = builder.build("q")
        # Both kept — entropy filter prevents a false-positive dedup.
        assert {i.item_id for i in pack.items} == {"a", "b"}

    def test_keeps_highest_score_on_cluster(self) -> None:
        """When 3+ items are near-duplicates, the highest-scoring wins."""
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        base = self._LONG_EXCERPT
        s = _make_strategy(
            "kw",
            [
                _item("low", 0.3, excerpt=base),
                _item("top", 0.99, excerpt=self._near_duplicate(base)),
                _item("mid", 0.7, excerpt=base + " "),
            ],
        )
        builder = PackBuilder(
            strategies=[s],
            semantic_dedup=SemanticDedupConfig(),
        )
        pack = builder.build("q")
        assert [i.item_id for i in pack.items] == ["top"]
        rejected_ids = {r.item_id for r in pack.retrieval_report.rejected_items}
        assert "low" in rejected_ids
        assert "mid" in rejected_ids

    def test_threshold_loosening_catches_more(self) -> None:
        """Lowering threshold dedups items a stricter threshold would keep."""
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        # Roughly 70% overlap — not enough at 0.85 but enough at 0.5.
        a_text = "Production deploys run the migration suite and validate schemas"
        b_text = "Production deploys execute migration scripts and confirm schemas"

        strict = PackBuilder(
            strategies=[
                _make_strategy(
                    "kw",
                    [_item("a", 0.9, excerpt=a_text), _item("b", 0.8, excerpt=b_text)],
                )
            ],
            semantic_dedup=SemanticDedupConfig(threshold=0.85),
        )
        loose = PackBuilder(
            strategies=[
                _make_strategy(
                    "kw",
                    [_item("a", 0.9, excerpt=a_text), _item("b", 0.8, excerpt=b_text)],
                )
            ],
            semantic_dedup=SemanticDedupConfig(threshold=0.3),
        )

        strict_ids = {i.item_id for i in strict.build("q").items}
        loose_ids = {i.item_id for i in loose.build("q").items}
        # Strict may or may not catch this specific pair depending on
        # shingle overlap — what matters is that loose catches at least
        # as much as strict.
        assert loose_ids.issubset(strict_ids)

    def test_telemetry_records_semantic_dedup_state(self, tmp_path: Path) -> None:
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        event_log = SQLiteEventLog(tmp_path / "events.db")
        s = _make_strategy(
            "kw",
            [
                _item("a", 0.9, excerpt=self._LONG_EXCERPT),
                _item("b", 0.5, excerpt=self._near_duplicate(self._LONG_EXCERPT)),
            ],
        )
        builder = PackBuilder(
            strategies=[s],
            event_log=event_log,
            semantic_dedup=SemanticDedupConfig(),
        )
        builder.build("q")

        from trellis.stores.base.event_log import EventType

        events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["semantic_dedup_enabled"] is True
        assert payload["semantic_dedup_rejected"] >= 1

    def test_telemetry_off_when_disabled(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        s = _make_strategy("kw", [_item("a", 0.9, excerpt=self._LONG_EXCERPT)])
        builder = PackBuilder(strategies=[s], event_log=event_log)
        builder.build("q")

        from trellis.stores.base.event_log import EventType

        events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
        assert events[0].payload["semantic_dedup_enabled"] is False
        assert events[0].payload["semantic_dedup_rejected"] == 0

    def test_sectioned_path_also_applies_dedup(self) -> None:
        from trellis.retrieve.pack_builder import SemanticDedupConfig

        dup = self._near_duplicate(self._LONG_EXCERPT)
        s = _make_strategy(
            "kw",
            [
                _item("winner", 0.9, excerpt=self._LONG_EXCERPT),
                _item("loser", 0.5, excerpt=dup),
            ],
        )
        builder = PackBuilder(
            strategies=[s],
            semantic_dedup=SemanticDedupConfig(),
        )
        pack = builder.build_sectioned("q", sections=[SectionRequest(name="all")])
        section_items = {i.item_id for i in pack.sections[0].items}
        assert section_items == {"winner"}


class TestEvaluatorHook:
    """Optional pack-quality evaluator hook on PackBuilder."""

    def test_no_evaluator_is_default(self) -> None:
        s = _make_strategy("s", [_item("a", 0.5)])
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        assert "quality_report" not in pack.metadata

    def test_evaluator_result_attaches_to_pack_metadata(self) -> None:
        from trellis.retrieve.evaluate import (
            EvaluationScenario,
            evaluate_pack,
        )
        from trellis.schemas.pack import Pack

        def evaluator(pack: Pack):
            return evaluate_pack(
                pack,
                EvaluationScenario(
                    name="fixture",
                    intent=pack.intent,
                    required_coverage=["widget"],
                ),
            )

        s = _make_strategy("s", [_item("a", 0.6, excerpt="widget guide")])
        builder = PackBuilder(strategies=[s], evaluator=evaluator)
        pack = builder.build("q")
        quality = pack.metadata["quality_report"]
        assert quality["scenario_name"] == "fixture"
        assert quality["dimensions"]["completeness"] == 1.0

    def test_evaluator_returning_none_leaves_metadata_unchanged(self) -> None:
        s = _make_strategy("s", [_item("a", 0.5)])
        builder = PackBuilder(strategies=[s], evaluator=lambda _pack: None)
        pack = builder.build("q")
        assert "quality_report" not in pack.metadata

    def test_evaluator_exception_is_swallowed(self) -> None:
        def boom(_pack):
            msg = "evaluator blew up"
            raise RuntimeError(msg)

        s = _make_strategy("s", [_item("a", 0.5)])
        builder = PackBuilder(strategies=[s], evaluator=boom)
        pack = builder.build("q")
        assert "quality_report" not in pack.metadata
        assert len(pack.items) == 1

    def test_evaluator_emits_pack_quality_scored_event(self, tmp_path) -> None:
        from trellis.retrieve.evaluate import (
            EvaluationScenario,
            evaluate_pack,
        )
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:

            def evaluator(pack):
                return evaluate_pack(
                    pack,
                    EvaluationScenario(
                        name="fx",
                        intent=pack.intent,
                        required_coverage=["widget"],
                    ),
                )

            s = _make_strategy("s", [_item("a", 0.5, excerpt="widget")])
            builder = PackBuilder(
                strategies=[s], evaluator=evaluator, event_log=event_log
            )
            pack = builder.build("q")
            events = event_log.get_events(
                event_type=EventType.PACK_QUALITY_SCORED, limit=10
            )
            assert len(events) == 1
            payload = events[0].payload
            assert payload["pack_id"] == pack.pack_id
            assert payload["scenario_name"] == "fx"
            assert payload["dimensions"]["completeness"] == 1.0
            assert "weighted_score" in payload
        finally:
            event_log.close()

    def test_no_event_when_evaluator_returns_none(self, tmp_path) -> None:
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy("s", [_item("a", 0.5)])
            builder = PackBuilder(
                strategies=[s],
                evaluator=lambda _p: None,
                event_log=event_log,
            )
            builder.build("q")
            events = event_log.get_events(
                event_type=EventType.PACK_QUALITY_SCORED, limit=10
            )
            assert events == []
        finally:
            event_log.close()


class _FixedTokenCounter:
    """Test fixture: reports a constant token count per call."""

    def __init__(self, value: int, name: str = "fixed") -> None:
        self._value = value
        self.name = name

    def count(self, _text: str) -> int:
        return self._value


class _MultiplierTokenCounter:
    """Test fixture: scales the heuristic by a multiplier."""

    def __init__(self, multiplier: float, name: str = "multiplier") -> None:
        self._multiplier = multiplier
        self.name = name

    def count(self, text: str) -> int:
        return int((len(text) // 4 + 1) * self._multiplier)


class TestPackBuilderTokenBudget:
    """Gap 3.1 — pluggable token counter + safety margin + validator telemetry."""

    def test_default_counter_preserves_prior_behavior(self) -> None:
        items = [_item(f"d{i}", 1.0 - i * 0.01, excerpt="x" * 100) for i in range(20)]
        s = _make_strategy("kw", items)
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=100))
        # Each item is 26 tokens (100//4+1), so 3 fit in a budget of 100.
        assert len(pack.items) == 3

    def test_custom_counter_used_for_budget(self) -> None:
        items = [_item(f"d{i}", 1.0 - i * 0.01, excerpt="x" * 100) for i in range(20)]
        s = _make_strategy("kw", items)
        # With a counter that reports 10 tokens per item, 10 items fit in 100.
        builder = PackBuilder(strategies=[s], token_counter=_FixedTokenCounter(10))
        pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=100))
        assert len(pack.items) == 10

    def test_safety_margin_shrinks_effective_budget(self) -> None:
        items = [_item(f"d{i}", 1.0 - i * 0.01, excerpt="x" * 100) for i in range(20)]
        s = _make_strategy("kw", items)
        # 20% margin on 100 tokens → effective 80. Fixed counter 10 → 8 fit.
        builder = PackBuilder(
            strategies=[s],
            token_counter=_FixedTokenCounter(10),
            token_budget_safety_margin=0.2,
        )
        pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=100))
        assert len(pack.items) == 8

    def test_invalid_safety_margin_raises(self) -> None:
        with pytest.raises(ValueError, match="safety_margin"):
            PackBuilder(token_budget_safety_margin=1.0)
        with pytest.raises(ValueError, match="safety_margin"):
            PackBuilder(token_budget_safety_margin=-0.1)

    def test_telemetry_emits_counter_name_and_margin(self, tmp_path: Path) -> None:
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy("kw", [_item("a", 0.9, excerpt="x" * 100)])
            builder = PackBuilder(
                strategies=[s],
                event_log=event_log,
                token_counter=_FixedTokenCounter(10, name="fixed_10"),
                token_budget_safety_margin=0.1,
            )
            builder.build("q", budget=PackBudget(max_items=10, max_tokens=100))
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["token_counter"] == "fixed_10"  # noqa: S105
            assert payload["token_budget_safety_margin"] == 0.1
            # 100 * 0.1 = 10 reserved, effective 90.
            assert payload["token_budget_effective"] == 90
            # One selected item, counter returns 10.
            assert payload["token_total_estimated"] == 10
        finally:
            event_log.close()

    def test_validator_emits_delta_when_set(self, tmp_path: Path) -> None:
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            # Primary counter under-counts (5 per item); validator reports 8.
            s = _make_strategy(
                "kw",
                [_item(f"d{i}", 1.0 - i * 0.01, excerpt="x" * 100) for i in range(3)],
            )
            builder = PackBuilder(
                strategies=[s],
                event_log=event_log,
                token_counter=_FixedTokenCounter(5, name="under"),
                token_budget_validator=_FixedTokenCounter(8, name="real"),
            )
            builder.build("q", budget=PackBudget(max_items=10, max_tokens=100))
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            payload = events[0].payload
            assert payload["token_counter"] == "under"  # noqa: S105
            assert payload["token_counter_validator"] == "real"  # noqa: S105
            # 3 items selected → estimated 15, validated 24.
            assert payload["token_total_estimated"] == 15
            assert payload["token_total_validated"] == 24
            assert payload["token_count_delta"] == 9
            assert payload["token_count_delta_pct"] == pytest.approx(0.6)
        finally:
            event_log.close()

    def test_validator_failure_does_not_break_assembly(self, tmp_path: Path) -> None:
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        class _BrokenValidator:
            name = "broken"

            def count(self, _text: str) -> int:
                msg = "tokenizer exploded"
                raise RuntimeError(msg)

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy("kw", [_item("a", 0.9, excerpt="x" * 100)])
            builder = PackBuilder(
                strategies=[s],
                event_log=event_log,
                token_budget_validator=_BrokenValidator(),
            )
            pack = builder.build("q", budget=PackBudget(max_items=10, max_tokens=100))
            assert len(pack.items) == 1
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            payload = events[0].payload
            # Validator fields absent, but primary telemetry still landed.
            assert "token_total_validated" not in payload
            assert payload["token_counter"] == "heuristic_4cpt"  # noqa: S105
        finally:
            event_log.close()

    def test_effective_budget_floor_of_one(self) -> None:
        # 99% margin on a small budget would round to zero; we floor at 1.
        builder = PackBuilder(token_budget_safety_margin=0.99)
        assert builder._effective_token_budget(2) == 1

    def test_annotate_uses_custom_counter(self) -> None:
        items = [_item("a", 0.9, excerpt="x" * 100)]
        s = _make_strategy("kw", items)
        builder = PackBuilder(
            strategies=[s], token_counter=_FixedTokenCounter(42, name="fx")
        )
        pack = builder.build("q", budget=PackBudget(max_items=5, max_tokens=1000))
        assert pack.items[0].estimated_tokens == 42

    def test_sectioned_telemetry_includes_token_fields(self, tmp_path: Path) -> None:
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy("kw", [_item(f"d{i}", 1.0 - i * 0.01) for i in range(3)])
            builder = PackBuilder(
                strategies=[s],
                event_log=event_log,
                token_counter=_MultiplierTokenCounter(1.0, name="mx"),
                token_budget_safety_margin=0.1,
            )
            builder.build_sectioned(
                "q",
                sections=[
                    SectionRequest(name="a", max_items=5, max_tokens=200),
                    SectionRequest(name="b", max_items=5, max_tokens=300),
                ],
            )
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["token_counter"] == "mx"  # noqa: S105
            assert payload["token_budget_safety_margin"] == 0.1
            # Aggregate max_tokens across sections = 500, effective = 450.
            assert payload["token_budget_effective"] == 450
            assert "token_total_estimated" in payload
        finally:
            event_log.close()


# ---------------------------------------------------------------------------
# Meta-Activity filter (Item 6 Phase 2)
# ---------------------------------------------------------------------------


def _meta_activity_item(
    item_id: str, *, agent_id: str = "trellis_meta_analyzer"
) -> PackItem:
    """Build a PackItem matching what GraphSearch emits for a meta-Activity.

    Two metadata signals matter for ``PackBuilder._is_meta_activity``:
    ``node_type == "Activity"`` and ``agent_id`` starts with
    ``trellis_meta_``. The rest is unobserved.
    """
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt="meta activity",
        relevance_score=0.5,
        metadata={
            "source_strategy": "graph",
            "node_type": "Activity",
            "agent_id": agent_id,
            "analyzer_name": "test",
        },
    )


def _regular_activity_item(item_id: str) -> PackItem:
    """A user-authored Activity that must NOT trigger the meta filter."""
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt="real activity",
        relevance_score=0.5,
        metadata={
            "source_strategy": "graph",
            "node_type": "Activity",
            "agent_id": "human-analyst-1",
        },
    )


class TestMetaActivityFilter:
    def test_default_excludes_meta_activity(self) -> None:
        """include_meta=False (default) drops meta-Activity items."""
        s = _make_strategy(
            "graph",
            [_meta_activity_item("meta-1"), _item("d1", 0.9)],
        )
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        item_ids = [item.item_id for item in pack.items]
        assert "meta-1" not in item_ids
        assert "d1" in item_ids

    def test_opt_in_includes_meta_activity(self) -> None:
        """include_meta=True surfaces meta-Activity items."""
        s = _make_strategy(
            "graph",
            [_meta_activity_item("meta-1"), _item("d1", 0.9)],
        )
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q", include_meta=True)
        item_ids = [item.item_id for item in pack.items]
        assert "meta-1" in item_ids
        assert "d1" in item_ids

    def test_filter_is_agent_id_specific_not_blanket_activity_drop(self) -> None:
        """User-authored Activities (non-synthetic agent_id) must remain."""
        s = _make_strategy(
            "graph",
            [
                _meta_activity_item("meta-1"),
                _regular_activity_item("real-1"),
            ],
        )
        builder = PackBuilder(strategies=[s])
        pack = builder.build("q")
        item_ids = [item.item_id for item in pack.items]
        assert "real-1" in item_ids
        assert "meta-1" not in item_ids

    def test_meta_filtered_count_emitted_in_event(self, tmp_path: Path) -> None:
        """PACK_ASSEMBLED payload includes ``meta_filtered_count``."""
        from trellis.stores.base.event_log import EventType

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy(
                "graph",
                [
                    _meta_activity_item("meta-1"),
                    _meta_activity_item("meta-2"),
                    _item("d1", 0.9),
                ],
            )
            builder = PackBuilder(strategies=[s], event_log=event_log)
            builder.build("q")
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            assert len(events) == 1
            assert events[0].payload["meta_filtered_count"] == 2
        finally:
            event_log.close()

    def test_meta_filtered_count_zero_when_opt_in(self, tmp_path: Path) -> None:
        """include_meta=True yields ``meta_filtered_count == 0`` (filter no-op)."""
        from trellis.stores.base.event_log import EventType

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy(
                "graph",
                [_meta_activity_item("meta-1"), _item("d1", 0.9)],
            )
            builder = PackBuilder(strategies=[s], event_log=event_log)
            builder.build("q", include_meta=True)
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            assert events[0].payload["meta_filtered_count"] == 0
        finally:
            event_log.close()

    def test_meta_filtered_count_in_sectioned_telemetry(self, tmp_path: Path) -> None:
        """``build_sectioned`` also propagates ``meta_filtered_count``."""
        from trellis.stores.base.event_log import EventType

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            s = _make_strategy(
                "graph",
                [_meta_activity_item("meta-1"), _item("d1", 0.9)],
            )
            builder = PackBuilder(strategies=[s], event_log=event_log)
            builder.build_sectioned(
                "q",
                sections=[SectionRequest(name="default", max_items=5, max_tokens=200)],
            )
            events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
            assert len(events) == 1
            assert events[0].payload["meta_filtered_count"] == 1
        finally:
            event_log.close()

    def test_is_meta_activity_requires_both_signals(self) -> None:
        """Helper requires node_type==Activity AND agent_id prefix match."""
        # node_type=Activity but agent_id not synthetic — keep.
        non_meta = _regular_activity_item("real-1")
        assert PackBuilder._is_meta_activity(non_meta) is False

        # synthetic agent_id but wrong node_type — keep (defensive).
        wrong_type = PackItem(
            item_id="x",
            item_type="entity",
            excerpt="",
            relevance_score=0.5,
            metadata={
                "node_type": "Document",
                "agent_id": "trellis_meta_analyzer",
            },
        )
        assert PackBuilder._is_meta_activity(wrong_type) is False

        # Both signals present — drop.
        meta = _meta_activity_item("meta-1")
        assert PackBuilder._is_meta_activity(meta) is True

    def test_is_meta_activity_handles_empty_metadata(self) -> None:
        """An item with empty metadata is never a meta-Activity."""
        item = PackItem(
            item_id="x",
            item_type="document",
            excerpt="",
            relevance_score=0.5,
            metadata={},
        )
        assert PackBuilder._is_meta_activity(item) is False
