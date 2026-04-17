"""Tests for response formatters."""

from __future__ import annotations

from trellis.retrieve.formatters import (
    format_advisories_as_markdown,
    format_entities_as_markdown,
    format_lessons_as_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
    format_subgraph_as_markdown,
    format_traces_as_markdown,
)
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence


def test_format_pack_empty():
    result = format_pack_as_markdown([], "test intent")
    assert "test intent" in result


def test_format_pack_with_items():
    items = [
        {
            "item_id": "id1",
            "item_type": "document",
            "excerpt": "hello world",
            "relevance_score": 0.9,
        },
        {
            "item_id": "id2",
            "item_type": "entity",
            "excerpt": "test entity",
            "relevance_score": 0.5,
        },
    ]
    result = format_pack_as_markdown(items, "test search", max_tokens=2000)
    assert "test search" in result
    assert "hello world" in result
    assert "document" in result


def test_format_pack_surfaces_pack_id():
    items = [
        {
            "item_id": "long_id_abcdef123456789",
            "item_type": "document",
            "excerpt": "hello",
            "relevance_score": 0.5,
        }
    ]
    result = format_pack_as_markdown(
        items, "intent", max_tokens=2000, pack_id="pack_abc"
    )
    assert "pack_abc" in result
    assert "long_id_abcdef123456789" in result  # full id, not truncated
    assert "record_feedback" in result


def test_format_pack_respects_token_budget():
    items = [
        {
            "item_id": f"id{i}",
            "item_type": "doc",
            "excerpt": "x" * 500,
            "relevance_score": 0.5,
        }
        for i in range(20)
    ]
    result = format_pack_as_markdown(items, "test", max_tokens=200)
    assert "omitted" in result


def test_format_traces_empty():
    assert "No traces" in format_traces_as_markdown([])


def test_format_traces_with_data():
    traces = [
        {
            "intent": "deploy service",
            "outcome": "success",
            "domain": "platform",
            "created_at": "2026-01-15T00:00:00",
        },
    ]
    result = format_traces_as_markdown(traces)
    assert "deploy service" in result
    assert "success" in result


def test_format_entities_empty():
    assert "No entities" in format_entities_as_markdown([])


def test_format_entities_with_data():
    entities = [
        {
            "node_id": "n1",
            "node_type": "concept",
            "properties": {"name": "Redis", "description": "Cache layer"},
        },
    ]
    result = format_entities_as_markdown(entities)
    assert "Redis" in result
    assert "concept" in result


def test_format_lessons_empty():
    assert "No lessons" in format_lessons_as_markdown([])


def test_format_lessons_with_data():
    lessons = [
        {
            "title": "Always check locks",
            "description": "Deadlocks are bad",
            "domain": "platform",
        },
    ]
    result = format_lessons_as_markdown(lessons)
    assert "Always check locks" in result
    assert "Deadlocks" in result


def test_format_subgraph():
    entity = {
        "node_id": "n1",
        "node_type": "service",
        "properties": {"name": "API Gateway"},
    }
    subgraph = {
        "nodes": [
            entity,
            {
                "node_id": "n2",
                "node_type": "service",
                "properties": {"name": "Auth"},
            },
        ],
        "edges": [
            {
                "source_id": "n1",
                "target_id": "n2",
                "edge_type": "depends_on",
            },
        ],
    }
    result = format_subgraph_as_markdown(entity, subgraph)
    assert "API Gateway" in result
    assert "depends_on" in result
    assert "Auth" in result


class TestFormatSectionedPack:
    def test_empty_sections(self) -> None:
        result = format_sectioned_pack_as_markdown([], "test intent")
        assert "test intent" in result

    def test_sections_with_items(self) -> None:
        sections = [
            {
                "name": "Domain Knowledge",
                "items": [
                    {
                        "item_id": "doc1",
                        "item_type": "document",
                        "excerpt": "ownership rules",
                        "relevance_score": 0.9,
                    },
                ],
            },
            {
                "name": "Patterns",
                "items": [
                    {
                        "item_id": "pat1",
                        "item_type": "pattern",
                        "excerpt": "dedup with ROW_NUMBER",
                        "relevance_score": 0.8,
                    },
                ],
            },
        ]
        result = format_sectioned_pack_as_markdown(sections, "plan sportsbook pipeline")
        assert "## Domain Knowledge" in result
        assert "## Patterns" in result
        assert "ownership rules" in result
        assert "dedup with ROW_NUMBER" in result

    def test_empty_section_omitted(self) -> None:
        sections = [
            {"name": "Empty", "items": []},
            {
                "name": "HasContent",
                "items": [
                    {
                        "item_id": "x",
                        "item_type": "doc",
                        "excerpt": "content",
                        "relevance_score": 0.5,
                    }
                ],
            },
        ]
        result = format_sectioned_pack_as_markdown(sections, "test")
        assert "## Empty" not in result
        assert "## HasContent" in result

    def test_respects_token_budget(self) -> None:
        sections = [
            {
                "name": "Big",
                "items": [
                    {
                        "item_id": f"item_{i}",
                        "item_type": "doc",
                        "excerpt": "x" * 500,
                        "relevance_score": 0.5,
                    }
                    for i in range(20)
                ],
            },
        ]
        result = format_sectioned_pack_as_markdown(sections, "test", max_tokens=200)
        assert "omitted" in result

    def test_pack_id_surfaced_when_provided(self) -> None:
        sections = [
            {
                "name": "S",
                "items": [
                    {
                        "item_id": "my_full_item_id_01ABC",
                        "item_type": "doc",
                        "excerpt": "content",
                        "relevance_score": 0.5,
                    }
                ],
            }
        ]
        result = format_sectioned_pack_as_markdown(
            sections, "intent", pack_id="pack_01HXYZ"
        )
        # Pack ID visible near the top
        assert "pack_01HXYZ" in result
        # Citation footer present
        assert "record_feedback" in result
        # Full item_id visible (no 40-char truncation)
        assert "my_full_item_id_01ABC" in result

    def test_pack_id_omitted_when_absent(self) -> None:
        sections = [
            {
                "name": "S",
                "items": [
                    {
                        "item_id": "x",
                        "item_type": "doc",
                        "excerpt": "content",
                        "relevance_score": 0.5,
                    }
                ],
            }
        ]
        result = format_sectioned_pack_as_markdown(sections, "intent")
        assert "pack_id" not in result
        assert "record_feedback" not in result


class TestFormatAdvisories:
    def test_empty_advisories(self) -> None:
        result = format_advisories_as_markdown([])
        assert result == ""

    def test_renders_advisories(self) -> None:
        adv = Advisory(
            category=AdvisoryCategory.ENTITY,
            confidence=0.85,
            message="Entity X appears in 82% of successful packs",
            evidence=AdvisoryEvidence(
                sample_size=47,
                success_rate_with=0.82,
                success_rate_without=0.34,
                effect_size=0.48,
            ),
            scope="platform",
        )
        result = format_advisories_as_markdown([adv])
        assert "## Advisories" in result
        assert "entity" in result.lower()
        assert "n=47" in result
        assert "Entity X" in result
        # advisory_id is surfaced so agents can cite it in feedback
        assert adv.advisory_id in result

    def test_multiple_advisories(self) -> None:
        advs = [
            Advisory(
                category=AdvisoryCategory.APPROACH,
                confidence=0.7,
                message="Validate schema first",
                evidence=AdvisoryEvidence(
                    sample_size=20,
                    success_rate_with=0.8,
                    success_rate_without=0.3,
                    effect_size=0.5,
                ),
                scope="global",
            ),
            Advisory(
                category=AdvisoryCategory.ANTI_PATTERN,
                confidence=0.6,
                message="Skipping dry-run correlated with failure",
                evidence=AdvisoryEvidence(
                    sample_size=15,
                    success_rate_with=0.3,
                    success_rate_without=0.7,
                    effect_size=-0.4,
                ),
                scope="global",
            ),
        ]
        result = format_advisories_as_markdown(advs)
        assert "1." in result
        assert "2." in result
        assert "approach" in result.lower()
        assert "anti_pattern" in result.lower()
