"""Tests for response formatters."""

from __future__ import annotations

from typing import Any

from trellis.retrieve.formatters import (
    format_advisories_as_markdown,
    format_entities_as_markdown,
    format_entity_as_markdown,
    format_fetched_items_as_markdown,
    format_file_context_as_markdown,
    format_index_line,
    format_lessons_as_markdown,
    format_pack_as_index_markdown,
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


def test_format_pack_omits_relevance_score():
    """RRF-fused scores are ordinal, not calibrated — the pack must not render
    them as a "relevance: X.XX" decimal (it read as low-confidence on every
    item). Order conveys ranking; the id/excerpt still render."""
    items = [
        {
            "item_id": "top",
            "item_type": "document",
            "excerpt": "most relevant",
            "relevance_score": 0.0164,
        },
        {
            "item_id": "next",
            "item_type": "entity",
            "excerpt": "less relevant",
            "relevance_score": 0.0161,
        },
    ]
    result = format_pack_as_markdown(items, "q", max_tokens=2000)
    assert "relevance:" not in result
    assert "0.02" not in result
    assert "top" in result
    assert "next" in result
    # Order preserved: the higher-ranked item appears first.
    assert result.index("`top`") < result.index("`next`")


def test_format_sectioned_pack_omits_relevance_score():
    sections = [
        {
            "name": "Domain",
            "items": [
                {
                    "item_id": "s1",
                    "item_type": "document",
                    "excerpt": "body",
                    "relevance_score": 0.0164,
                }
            ],
        }
    ]
    result = format_sectioned_pack_as_markdown(sections, "q", max_tokens=2000)
    assert "0.02" not in result
    assert "`s1`" in result
    assert "body" in result


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
        result = format_sectioned_pack_as_markdown(sections, "plan orders pipeline")
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
        assert "Entity X appears in 82% of successful packs" in result
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


class TestAdvisoryEvidenceIsRenderedOnce:
    """#392 — the formatter must not repeat what the message already says.

    Every ``AdvisoryGenerator`` analysis writes its sample size and effect
    into ``message``; the formatter appended a second ``(n=..., effect=...)``
    on top, so every rendered line printed the same two figures twice. The
    defect is visible in output and was latent only because production has
    served zero sectioned packs — the one surface that renders advisories.
    """

    @staticmethod
    def _generator_shaped(message: str, **evidence: float) -> Advisory:
        return Advisory(
            category=AdvisoryCategory.APPROACH,
            confidence=0.21,
            message=message,
            evidence=AdvisoryEvidence(
                sample_size=int(evidence.get("sample_size", 5)),
                success_rate_with=evidence.get("success_rate_with", 0.6),
                success_rate_without=evidence.get("success_rate_without", 0.0),
                effect_size=evidence.get("effect_size", 0.6),
            ),
            scope="global",
        )

    def test_embedded_evidence_appears_exactly_once(self) -> None:
        """Verbatim production message shape (n and effect inside the text)."""
        message = (
            "Packs using the 'graph' strategy succeeded 60% of the time"
            " vs 0% without (n=5, effect=+60%)."
        )
        result = format_advisories_as_markdown([self._generator_shaped(message)])

        assert result.count("(n=5, effect=+60%)") == 1
        assert result.count("n=5") == 1
        assert result.count("effect=+60%") == 1

    def test_no_evidence_suffix_is_appended_to_any_message(self) -> None:
        """Even a message carrying no numbers gets no formatter-made suffix.

        Pinned against a *different* evidence block from the one above, so
        the assertion cannot be satisfied by a constant: were the suffix
        restored it would read ``(n=31, effect=-25%)`` here.
        """
        advisory = self._generator_shaped(
            "Prefer the deterministic extractor",
            sample_size=31,
            effect_size=-0.25,
        )

        result = format_advisories_as_markdown([advisory])

        assert result.count("Prefer the deterministic extractor") == 1
        assert "n=31" not in result
        assert "effect=" not in result

    def test_structured_evidence_is_still_reachable_on_the_object(self) -> None:
        """What the suffix removal does *not* cost.

        The numbers ride ``Advisory.evidence``, which ``POST /api/v1/packs``
        serialises in full — the markdown surface is not the only place a
        consumer can read them.
        """
        advisory = self._generator_shaped("anything", sample_size=31)
        assert advisory.evidence.sample_size == 31

    def test_every_advisory_renders_on_its_own_line(self) -> None:
        """Two advisories, two lines, each with one message and one id."""
        advisories = [
            self._generator_shaped("First finding (n=5, effect=+60%)."),
            self._generator_shaped("Second finding (n=9, effect=-30%)."),
        ]

        lines = [
            line
            for line in format_advisories_as_markdown(advisories).splitlines()
            if line.startswith(("1.", "2."))
        ]

        assert len(lines) == 2
        assert lines[0].count("(n=5, effect=+60%)") == 1
        assert lines[1].count("(n=9, effect=-30%)") == 1
        assert advisories[0].advisory_id in lines[0]
        assert advisories[1].advisory_id in lines[1]


# ---------------------------------------------------------------------------
# Progressive disclosure — index lines, index packs, batch fetch (#305)
# ---------------------------------------------------------------------------


class TestFormatIndexLine:
    def test_prefers_metadata_title_over_excerpt(self) -> None:
        line = format_index_line(
            {
                "item_id": "doc-1",
                "item_type": "document",
                "excerpt": "Body prose that must not appear.",
                "metadata": {"title": "Postgres failover runbook"},
                "estimated_tokens": 420,
            }
        )
        assert "Postgres failover runbook" in line
        assert "Body prose" not in line

    def test_falls_back_through_capture_title_and_name(self) -> None:
        for key in ("capture_title", "name"):
            line = format_index_line(
                {
                    "item_id": "e-1",
                    "item_type": "entity",
                    "excerpt": "ignored",
                    "metadata": {key: f"From {key}"},
                }
            )
            assert f"From {key}" in line

    def test_falls_back_to_excerpt_when_no_title(self) -> None:
        line = format_index_line(
            {
                "item_id": "doc-2",
                "item_type": "document",
                "excerpt": "A short excerpt stands in for a missing title.",
            }
        )
        assert "A short excerpt stands in for a missing title." in line

    def test_excerpt_label_stops_at_the_label_budget(self) -> None:
        # The label is a char budget, not a first-sentence rule: a short
        # excerpt survives whole, a long one is cut at a boundary.
        line = format_index_line(
            {
                "item_id": "doc-2b",
                "item_type": "document",
                "excerpt": (
                    "The opening sentence runs long enough to fill the label "
                    "budget on its own. Everything after it is body prose the "
                    "index must never carry."
                ),
            }
        )
        assert "The opening sentence" in line
        assert "body prose the index must never carry" not in line

    def test_blank_title_falls_through_to_excerpt(self) -> None:
        line = format_index_line(
            {
                "item_id": "doc-3",
                "item_type": "document",
                "excerpt": "Real label.",
                "metadata": {"title": "   "},
            }
        )
        assert "Real label." in line

    def test_stays_one_line_when_excerpt_is_multiline(self) -> None:
        line = format_index_line(
            {
                "item_id": "doc-4",
                "item_type": "document",
                "excerpt": "alpha\nbeta\n\ngamma",
            }
        )
        assert "\n" not in line

    def test_renders_read_cost_when_estimated(self) -> None:
        assert "~250 tok" in format_index_line(
            {"item_id": "d", "item_type": "document", "estimated_tokens": 250}
        )

    def test_omits_read_cost_when_absent_or_zero(self) -> None:
        for item in (
            {"item_id": "d", "item_type": "document"},
            {"item_id": "d", "item_type": "document", "estimated_tokens": 0},
            {"item_id": "d", "item_type": "document", "estimated_tokens": None},
        ):
            assert "tok" not in format_index_line(item)

    def test_item_id_is_copy_pastable(self) -> None:
        line = format_index_line({"item_id": "01ABCDEF", "item_type": "document"})
        assert "`01ABCDEF`" in line

    def test_long_label_is_truncated(self) -> None:
        line = format_index_line(
            {
                "item_id": "d",
                "item_type": "document",
                "excerpt": "word " * 200,
            }
        )
        assert len(line) < 160


class TestFormatPackAsIndexMarkdown:
    @staticmethod
    def _items(count: int) -> list[dict[str, Any]]:
        return [
            {
                "item_id": f"doc-{i}",
                "item_type": "document",
                "excerpt": "SECRET BODY PROSE " * 40,
                "metadata": {"title": f"Title {i}"},
                "estimated_tokens": 180,
            }
            for i in range(count)
        ]

    def test_carries_no_excerpt_bodies(self) -> None:
        result = format_pack_as_index_markdown(self._items(3), "deploy")
        assert "SECRET BODY PROSE" not in result
        for i in range(3):
            assert f"`doc-{i}`" in result
            assert f"Title {i}" in result

    def test_surfaces_pack_id_and_both_follow_up_calls(self) -> None:
        result = format_pack_as_index_markdown(
            self._items(2), "deploy", pack_id="pack-42"
        )
        assert "**pack_id:** `pack-42`" in result
        assert "get_items(" in result
        assert "record_feedback(" in result

    def test_omits_follow_up_footer_without_pack_id(self) -> None:
        result = format_pack_as_index_markdown(self._items(2), "deploy")
        assert "pack_id" not in result
        assert "get_items(" not in result

    def test_indexes_far_more_items_than_the_full_pack_rendering(self) -> None:
        items = self._items(40)
        index = format_pack_as_index_markdown(items, "deploy", max_tokens=500)
        full = format_pack_as_markdown(items, "deploy", max_tokens=500)
        assert index.count("- `doc-") > full.count("## [document]")

    def test_reports_omitted_items_when_budget_runs_out(self) -> None:
        result = format_pack_as_index_markdown(self._items(40), "deploy", max_tokens=60)
        assert "more items omitted" in result
        # An omission notice on its own is not a passing render — the
        # agent still has to come away with ids it can fetch.
        assert result.count("- `doc-") > 0

    def test_renders_one_id_even_when_nothing_fits(self) -> None:
        # An index with no id is a dead end. One line is ~15 tokens.
        result = format_pack_as_index_markdown(
            self._items(5),
            "how do I fail over the pgbouncer sidecar safely",
            max_tokens=30,
            pack_id="PK1",
        )
        assert "- `doc-0`" in result
        assert "*[4 more items omitted]*" in result

    def test_empty_items_render_header_only(self) -> None:
        result = format_pack_as_index_markdown([], "deploy")
        assert "# Context index for: deploy" in result
        assert "- `" not in result


class TestFormatFetchedItems:
    @staticmethod
    def _item(item_id: str, body: str, kind: str = "document") -> dict[str, Any]:
        return {"item_id": item_id, "kind": kind, "body": body}

    def test_renders_full_bodies_with_ids(self) -> None:
        result, served, omitted = format_fetched_items_as_markdown(
            [self._item("a", "alpha body"), self._item("b", "beta body", "entity")]
        )
        assert "alpha body" in result
        assert "beta body" in result
        assert "## [entity] `b`" in result
        assert served == ["a", "b"]
        assert omitted == []

    def test_over_budget_items_are_omitted_whole_not_truncated(self) -> None:
        big = "B" * 8000
        result, served, omitted = format_fetched_items_as_markdown(
            [self._item("small", "tiny"), self._item("big", big)],
            max_tokens=100,
        )
        assert served == ["small"]
        assert omitted == ["big"]
        assert big[:200] not in result
        assert "re-fetch with a larger max_tokens: `big`" in result

    def test_not_found_ids_are_always_listed(self) -> None:
        result, served, _ = format_fetched_items_as_markdown(
            [self._item("a", "alpha")], not_found=["ghost"]
        )
        assert "not found: `ghost`" in result
        assert served == ["a"]

    def test_nothing_is_served_when_nothing_fits(self) -> None:
        # No trimmed-prefix fallback: a half-body the agent cannot tell is
        # half, recorded as fully served, is worse than an empty response
        # naming the ids to re-fetch.
        result, served, omitted = format_fetched_items_as_markdown(
            [self._item("a", "A" * 5000), self._item("b", "B" * 5000)],
            max_tokens=40,
        )
        assert served == []
        assert omitted == ["a", "b"]
        assert "AAA" not in result
        assert "re-fetch with a larger max_tokens: `a`, `b`" in result

    def test_surfaces_pack_id_when_given(self) -> None:
        result, _, _ = format_fetched_items_as_markdown(
            [self._item("a", "alpha")], pack_id="pack-9"
        )
        assert "**pack_id:** `pack-9`" in result

    def test_empty_input_renders_header_only(self) -> None:
        result, served, omitted = format_fetched_items_as_markdown([])
        assert "# Fetched items" in result
        assert served == []
        assert omitted == []


class TestFormatEntityBlock:
    """The one entity block ``get_graph`` and ``get_items`` both render."""

    @staticmethod
    def _node(doc_ids: list[str]) -> dict[str, Any]:
        return {
            "node_id": "svc-api",
            "node_type": "service",
            "properties": {"name": "API Gateway", "owner": "platform"},
            "document_ids": doc_ids,
        }

    def test_renders_name_type_and_properties(self) -> None:
        result = format_entity_as_markdown(self._node([]))
        assert result.startswith("**API Gateway** (service)")
        assert "- **owner**: platform" in result
        assert "Evidence documents" not in result

    def test_evidence_pointers_share_the_graph_cap(self) -> None:
        # Same cap as the subgraph root block — one definition, so a
        # fetched entity and a traversed one cannot disagree.
        result = format_entity_as_markdown(self._node([f"d{i}" for i in range(14)]))
        assert result.count("`d") == 10
        assert "(+4 more)" in result

    def test_falls_back_to_node_id_without_a_name(self) -> None:
        result = format_entity_as_markdown({"node_id": "n1", "properties": {}})
        assert "**n1** (unknown)" in result


class TestFormatSubgraphDocPointers:
    @staticmethod
    def _entity(doc_ids: list[str] | None = None) -> dict[str, Any]:
        entity: dict[str, Any] = {
            "node_id": "n1",
            "node_type": "service",
            "properties": {"name": "API Gateway"},
        }
        if doc_ids is not None:
            entity["document_ids"] = doc_ids
        return entity

    def test_root_evidence_pointers_are_rendered(self) -> None:
        entity = self._entity(["doc-a", "doc-b"])
        result = format_subgraph_as_markdown(entity, {"nodes": [entity], "edges": []})
        assert "## Evidence documents (2)" in result
        assert "`doc-a`" in result
        assert "`doc-b`" in result

    def test_no_evidence_section_when_node_has_no_doc_links(self) -> None:
        entity = self._entity()
        result = format_subgraph_as_markdown(entity, {"nodes": [entity], "edges": []})
        assert "Evidence documents" not in result

    def test_empty_doc_ids_render_no_section(self) -> None:
        entity = self._entity([])
        result = format_subgraph_as_markdown(entity, {"nodes": [entity], "edges": []})
        assert "Evidence documents" not in result

    def test_root_pointer_list_is_capped_and_reports_the_remainder(self) -> None:
        entity = self._entity([f"doc-{i}" for i in range(14)])
        result = format_subgraph_as_markdown(
            entity, {"nodes": [entity], "edges": []}, max_tokens=4000
        )
        assert "## Evidence documents (14)" in result
        assert "`doc-9`" in result
        assert "`doc-10`" not in result
        assert "4 more omitted" in result

    def test_neighbor_pointers_ride_inline(self) -> None:
        entity = self._entity()
        neighbor = {
            "node_id": "n2",
            "node_type": "service",
            "properties": {"name": "Auth"},
            "document_ids": ["doc-x"],
        }
        result = format_subgraph_as_markdown(
            entity, {"nodes": [entity, neighbor], "edges": []}
        )
        assert "**Auth** (service) — docs: `doc-x`" in result

    def test_neighbor_pointers_are_capped_with_a_count(self) -> None:
        entity = self._entity()
        neighbor = {
            "node_id": "n2",
            "node_type": "service",
            "properties": {"name": "Auth"},
            "document_ids": [f"d{i}" for i in range(6)],
        }
        result = format_subgraph_as_markdown(
            entity, {"nodes": [entity, neighbor], "edges": []}
        )
        assert "`d2`" in result
        assert "`d3`" not in result
        assert "(+3)" in result

    def test_neighbor_without_doc_links_has_no_suffix(self) -> None:
        entity = self._entity()
        neighbor = {
            "node_id": "n2",
            "node_type": "service",
            "properties": {"name": "Auth"},
        }
        result = format_subgraph_as_markdown(
            entity, {"nodes": [entity, neighbor], "edges": []}
        )
        assert "**Auth** (service)" in result
        assert "docs:" not in result


class TestFormatFileContext:
    def test_empty_result(self):
        empty = format_file_context_as_markdown({"paths": []})
        assert empty == "No file paths queried."

    def test_path_without_context(self):
        result = format_file_context_as_markdown(
            {
                "paths": [
                    {
                        "path": "notes/foo.md",
                        "documents": [],
                        "entities": [],
                        "newest_item_at": None,
                    }
                ]
            }
        )
        assert "## notes/foo.md" in result
        assert "No stored context for this path." in result

    def test_documents_and_entities_rendered_with_timestamps(self):
        result = format_file_context_as_markdown(
            {
                "paths": [
                    {
                        "path": "notes/foo.md",
                        "documents": [
                            {
                                "doc_id": "corpus:vault:abc",
                                "source_path": "notes/foo.md",
                                "title": "Foo Notes",
                                "excerpt": "Gotcha about cold starts.",
                                "created_at": "2026-08-01T00:00:00+00:00",
                                "updated_at": "2026-08-14T10:00:00+00:00",
                            }
                        ],
                        "entities": [
                            {
                                "entity_id": "ent-1",
                                "name": "Cold Start",
                                "entity_type": "concept",
                                "description": "API cold-start latency",
                                "created_at": "2026-08-02T00:00:00+00:00",
                                "updated_at": "2026-08-03T00:00:00+00:00",
                            }
                        ],
                        "newest_item_at": "2026-08-14T10:00:00+00:00",
                    }
                ]
            }
        )
        assert "Newest memory: 2026-08-14T10:00:00+00:00" in result
        assert "**Foo Notes** `corpus:vault:abc`" in result
        assert "updated 2026-08-14T10:00:00+00:00" in result
        assert "Gotcha about cold starts." in result
        assert "**Cold Start** (concept) `ent-1`" in result
        assert "API cold-start latency" in result

    def test_respects_token_budget(self):
        entities = [
            {
                "entity_id": f"ent-{i}",
                "name": f"Entity {i}",
                "entity_type": "concept",
                "description": "x" * 200,
                "updated_at": "2026-08-03T00:00:00+00:00",
            }
            for i in range(50)
        ]
        result = format_file_context_as_markdown(
            {
                "paths": [
                    {
                        "path": "notes/foo.md",
                        "documents": [],
                        "entities": entities,
                        "newest_item_at": "2026-08-03T00:00:00+00:00",
                    }
                ]
            },
            max_tokens=100,
        )
        assert len(result) <= 100 * 4

    def test_truncated_graph_scan_is_flagged(self):
        """ "No entities" and "couldn't look" must not read the same."""
        entry = {
            "path": "notes/foo.md",
            "documents": [],
            "entities": [],
            "newest_item_at": None,
        }
        assert "may be incomplete" in format_file_context_as_markdown(
            {"paths": [entry], "graph_scan_truncated": True}
        )
        assert "may be incomplete" not in format_file_context_as_markdown(
            {"paths": [entry], "graph_scan_truncated": False}
        )
