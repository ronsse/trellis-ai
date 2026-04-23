"""Tests for PackBuilder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
from trellis.stores.advisory_store import AdvisoryStore
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
                "domain": ["data-pipeline"],
                "signal_quality": ["high", "standard"],
            },
        )
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert "content_tags" in filters
        assert filters["content_tags"]["domain"] == ["data-pipeline"]
        assert filters["content_tags"]["signal_quality"] == ["high", "standard"]

    def test_tag_filters_merged_with_existing_filters(self) -> None:
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build(
            "q",
            filters={"category": "tutorial"},
            tag_filters={"domain": ["api"]},
        )
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert filters["category"] == "tutorial"
        assert filters["content_tags"]["domain"] == ["api"]

    def test_default_noise_exclusion(self) -> None:
        """Noise should be excluded by default with no signal_quality filter."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q", tag_filters={"domain": ["api"]})
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert "noise" not in filters["content_tags"]["signal_quality"]
        assert "high" in filters["content_tags"]["signal_quality"]
        assert "standard" in filters["content_tags"]["signal_quality"]
        assert "low" in filters["content_tags"]["signal_quality"]

    def test_explicit_signal_quality_overrides_default(self) -> None:
        """When signal_quality is explicitly provided, no default exclusion."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q", tag_filters={"signal_quality": ["noise"]})
        call_kwargs = s.search.call_args
        filters = call_kwargs[1]["filters"]
        assert filters["content_tags"]["signal_quality"] == ["noise"]

    def test_no_tag_filters_no_content_tags_in_filters(self) -> None:
        """When tag_filters is None, no content_tags key added to filters."""
        s = _make_strategy("kw", [])
        builder = PackBuilder(strategies=[s])
        builder.build("q")
        call_kwargs = s.search.call_args
        assert call_kwargs[1]["filters"] is None

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
) -> Advisory:
    return Advisory(
        category=category,
        confidence=confidence,
        message=f"Test advisory ({category.value})",
        evidence=AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        ),
        scope=scope,
    )


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
        builder = PackBuilder(
            strategies=[s], token_counter=_FixedTokenCounter(10)
        )
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
            events = event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED, limit=10
            )
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
            events = event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED, limit=10
            )
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
            pack = builder.build(
                "q", budget=PackBudget(max_items=10, max_tokens=100)
            )
            assert len(pack.items) == 1
            events = event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED, limit=10
            )
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
            s = _make_strategy(
                "kw", [_item(f"d{i}", 1.0 - i * 0.01) for i in range(3)]
            )
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
            events = event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED, limit=10
            )
            assert len(events) == 1
            payload = events[0].payload
            assert payload["token_counter"] == "mx"  # noqa: S105
            assert payload["token_budget_safety_margin"] == 0.1
            # Aggregate max_tokens across sections = 500, effective = 450.
            assert payload["token_budget_effective"] == 450
            assert "token_total_estimated" in payload
        finally:
            event_log.close()
