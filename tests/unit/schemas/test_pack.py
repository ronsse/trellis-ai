"""Tests for Pack schema."""

from __future__ import annotations

from trellis.schemas import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    BudgetStep,
    Pack,
    PackBudget,
    PackItem,
    RejectedItem,
    RetrievalReport,
)
from trellis.schemas.pack import PackSection, SectionedPack, SectionRequest


class TestPack:
    """Tests for Pack model."""

    def test_pack_with_items(self) -> None:
        items = [
            PackItem(
                item_id="tr_1",
                item_type="trace",
                excerpt="did X",
                relevance_score=0.95,
            ),
            PackItem(item_id="ev_1", item_type="evidence", excerpt="doc Y"),
        ]
        p = Pack(intent="debug auth failure", items=items)
        assert len(p.pack_id) == 26
        assert p.intent == "debug auth failure"
        assert len(p.items) == 2
        assert p.items[0].relevance_score == 0.95

    def test_pack_with_budget(self) -> None:
        budget = PackBudget(max_items=10, max_tokens=4000)
        p = Pack(intent="summarize sprint", budget=budget)
        assert p.budget.max_items == 10
        assert p.budget.max_tokens == 4000

    def test_pack_with_retrieval_report(self) -> None:
        report = RetrievalReport(
            queries_run=3,
            candidates_found=42,
            items_selected=8,
            duration_ms=150,
            strategies_used=["semantic", "keyword"],
        )
        p = Pack(
            intent="find related precedents",
            retrieval_report=report,
            domain="infrastructure",
            agent_id="agent_007",
            policies_applied=["pol_1"],
        )
        assert p.retrieval_report.queries_run == 3
        assert p.retrieval_report.items_selected == 8
        assert p.domain == "infrastructure"
        assert p.policies_applied == ["pol_1"]

    def test_pack_default_budget(self) -> None:
        p = Pack(intent="test defaults")
        assert p.budget.max_items == 50
        assert p.budget.max_tokens == 8000
        assert p.retrieval_report.queries_run == 0

    def test_pack_item_observability_fields(self) -> None:
        item = PackItem(
            item_id="doc_1",
            item_type="document",
            excerpt="table lineage context",
            relevance_score=0.88,
            included=True,
            rank=1,
            selection_reason="selected_by_relevance",
            score_breakdown={"relevance_score": 0.88},
            estimated_tokens=5,
        )
        assert item.included is True
        assert item.rank == 1
        assert item.selection_reason == "selected_by_relevance"
        assert item.score_breakdown == {"relevance_score": 0.88}
        assert item.estimated_tokens == 5

    def test_pack_item_strategy_source(self) -> None:
        item = PackItem(
            item_id="d1",
            item_type="document",
            strategy_source="keyword",
        )
        assert item.strategy_source == "keyword"

    def test_pack_item_strategy_source_defaults_none(self) -> None:
        item = PackItem(item_id="d1", item_type="document")
        assert item.strategy_source is None

    def test_pack_advisories_default_empty(self) -> None:
        p = Pack(intent="test")
        assert p.advisories == []

    def test_pack_with_advisories(self) -> None:
        adv = Advisory(
            category=AdvisoryCategory.ENTITY,
            confidence=0.8,
            message="Include entity X",
            evidence=AdvisoryEvidence(
                sample_size=10,
                success_rate_with=0.9,
                success_rate_without=0.4,
                effect_size=0.5,
            ),
            scope="global",
        )
        p = Pack(intent="test", advisories=[adv])
        assert len(p.advisories) == 1
        assert p.advisories[0].category == AdvisoryCategory.ENTITY

    def test_pack_supports_skill_and_target_entities(self) -> None:
        p = Pack(
            intent="investigate dbt model",
            skill_id="dbt-triage",
            target_entity_ids=["ent_orders", "ent_customers"],
        )
        assert p.skill_id == "dbt-triage"
        assert p.target_entity_ids == ["ent_orders", "ent_customers"]


class TestSectionRequest:
    def test_defaults(self) -> None:
        sr = SectionRequest(name="domain")
        assert sr.name == "domain"
        assert sr.retrieval_affinities == []
        assert sr.content_types == []
        assert sr.max_tokens == 2000
        assert sr.max_items == 10

    def test_with_filters(self) -> None:
        sr = SectionRequest(
            name="tactical",
            retrieval_affinities=["reference", "operational"],
            content_types=["code", "error-resolution"],
            scopes=["project"],
            entity_ids=["uc://table1"],
            max_tokens=3000,
            max_items=15,
        )
        assert sr.retrieval_affinities == ["reference", "operational"]
        assert sr.entity_ids == ["uc://table1"]


class TestPackSection:
    def test_with_items(self) -> None:
        items = [PackItem(item_id="x", item_type="doc", excerpt="hello")]
        section = PackSection(name="domain", items=items)
        assert section.name == "domain"
        assert len(section.items) == 1

    def test_defaults(self) -> None:
        section = PackSection(name="empty")
        assert section.items == []
        assert section.budget.max_items == 10
        assert section.budget.max_tokens == 2000


class TestSectionedPack:
    def test_total_items(self) -> None:
        sp = SectionedPack(
            intent="test",
            sections=[
                PackSection(
                    name="a",
                    items=[PackItem(item_id="1", item_type="doc", excerpt="x" * 40)],
                ),
                PackSection(
                    name="b",
                    items=[
                        PackItem(item_id="2", item_type="doc", excerpt="y" * 80),
                        PackItem(item_id="3", item_type="doc", excerpt="z" * 20),
                    ],
                ),
            ],
        )
        assert sp.total_items == 3

    def test_total_tokens(self) -> None:
        sp = SectionedPack(
            intent="test",
            sections=[
                PackSection(
                    name="a",
                    items=[PackItem(item_id="1", item_type="doc", excerpt="x" * 40)],
                ),
            ],
        )
        assert sp.total_tokens == 11  # 40 // 4 + 1

    def test_all_items_flattens(self) -> None:
        sp = SectionedPack(
            intent="test",
            sections=[
                PackSection(name="a", items=[PackItem(item_id="1", item_type="doc")]),
                PackSection(name="b", items=[PackItem(item_id="2", item_type="doc")]),
            ],
        )
        ids = [item.item_id for item in sp.all_items]
        assert ids == ["1", "2"]

    def test_empty_sections(self) -> None:
        sp = SectionedPack(intent="test")
        assert sp.total_items == 0
        assert sp.total_tokens == 0
        assert sp.all_items == []

    def test_sectioned_pack_advisories_default_empty(self) -> None:
        sp = SectionedPack(intent="test")
        assert sp.advisories == []


class TestRejectedItem:
    def test_basic(self) -> None:
        r = RejectedItem(
            item_id="d1",
            item_type="document",
            relevance_score=0.7,
            reason="dedup",
            strategy_source="keyword",
        )
        assert r.item_id == "d1"
        assert r.reason == "dedup"
        assert r.strategy_source == "keyword"

    def test_defaults(self) -> None:
        r = RejectedItem(item_id="d1", item_type="doc", reason="max_items")
        assert r.relevance_score == 0.0
        assert r.strategy_source is None


class TestBudgetStep:
    def test_basic(self) -> None:
        b = BudgetStep(
            item_id="d1",
            item_tokens=25,
            running_total=25,
            included=True,
        )
        assert b.item_id == "d1"
        assert b.item_tokens == 25
        assert b.running_total == 25
        assert b.included is True


class TestRetrievalReportDecisionTrail:
    def test_rejected_items_field(self) -> None:
        r = RetrievalReport(
            rejected_items=[
                RejectedItem(item_id="d1", item_type="doc", reason="dedup"),
                RejectedItem(item_id="d2", item_type="doc", reason="token_budget"),
            ]
        )
        assert len(r.rejected_items) == 2
        assert r.rejected_items[0].reason == "dedup"

    def test_budget_trace_field(self) -> None:
        r = RetrievalReport(
            budget_trace=[
                BudgetStep(
                    item_id="d1", item_tokens=10, running_total=10, included=True
                ),
                BudgetStep(
                    item_id="d2", item_tokens=50, running_total=10, included=False
                ),
            ]
        )
        assert len(r.budget_trace) == 2
        assert r.budget_trace[0].included is True
        assert r.budget_trace[1].included is False

    def test_defaults_empty(self) -> None:
        r = RetrievalReport()
        assert r.rejected_items == []
        assert r.budget_trace == []
