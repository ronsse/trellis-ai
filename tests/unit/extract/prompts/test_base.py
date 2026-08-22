"""Tests for PromptTemplate + render()."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from trellis.extract.prompts import (
    ENTITY_EXTRACTION_V1,
    MEMORY_EXTRACTION_V1,
    PromptTemplate,
    render,
)


class TestPromptTemplate:
    def test_is_frozen(self) -> None:
        t = PromptTemplate(name="t", version="1", system="s", user_template="{text}")
        with pytest.raises(FrozenInstanceError):
            t.name = "mutated"  # type: ignore[misc]

    def test_fields_preserved(self) -> None:
        t = PromptTemplate(name="t", version="2.1", system="sys", user_template="u")
        assert t.name == "t"
        assert t.version == "2.1"
        assert t.system == "sys"
        assert t.user_template == "u"


class TestRender:
    def test_text_only_produces_two_messages(self) -> None:
        t = PromptTemplate(
            name="t", version="1", system="SYS", user_template="prefix {text}"
        )
        msgs = render(t, text="hello")
        assert len(msgs) == 2
        assert msgs[0].role == "system"
        assert msgs[0].content == "SYS"
        assert msgs[1].role == "user"
        assert msgs[1].content == "prefix hello"

    def test_type_hints_render_as_line(self) -> None:
        t = PromptTemplate(
            name="t",
            version="1",
            system="",
            user_template="{type_hints}\n{text}",
        )
        msgs = render(t, text="x", entity_type_hints=["person", "system"])
        assert "Prefer these entity types: person, system" in msgs[1].content

    def test_empty_hints_collapse_to_empty_string(self) -> None:
        t = PromptTemplate(
            name="t",
            version="1",
            system="",
            user_template="[{type_hints}][{edge_hints}][{domain_line}][{source_line}]-{text}",
        )
        msgs = render(t, text="x")
        assert msgs[1].content == "[][][][]-x"

    def test_none_hints_collapse(self) -> None:
        t = PromptTemplate(
            name="t",
            version="1",
            system="",
            user_template="[{type_hints}][{edge_hints}]-{text}",
        )
        msgs = render(t, text="y", entity_type_hints=None, edge_kind_hints=None)
        assert msgs[1].content == "[][]-y"

    def test_edge_kind_hints_render(self) -> None:
        t = PromptTemplate(
            name="t",
            version="1",
            system="",
            user_template="{edge_hints}\n{text}",
        )
        msgs = render(t, text="x", edge_kind_hints=["owns", "derived_from"])
        assert "Prefer these edge kinds: owns, derived_from" in msgs[1].content

    def test_domain_and_source_render(self) -> None:
        t = PromptTemplate(
            name="t",
            version="1",
            system="",
            user_template="{domain_line}\n{source_line}\n{text}",
        )
        msgs = render(t, text="x", domain="data_eng", source_system="dbt")
        assert "Domain: data_eng" in msgs[1].content
        assert "Source system: dbt" in msgs[1].content


class TestShippedTemplates:
    def test_entity_extraction_renders(self) -> None:
        msgs = render(
            ENTITY_EXTRACTION_V1,
            text="Alice deployed the orders pipeline last Tuesday.",
            entity_type_hints=["person", "pipeline"],
            edge_kind_hints=["deployed"],
            domain="data_eng",
        )
        assert msgs[0].role == "system"
        assert "entity_type" in msgs[0].content  # schema in system prompt
        assert "Text:" in msgs[1].content
        assert "Alice deployed" in msgs[1].content
        assert "Prefer these entity types: person, pipeline" in msgs[1].content
        assert "Prefer these edge kinds: deployed" in msgs[1].content
        assert "Domain: data_eng" in msgs[1].content

    def test_memory_extraction_renders(self) -> None:
        msgs = render(
            MEMORY_EXTRACTION_V1,
            text="domain_c_sessions owner is alice",
            entity_type_hints=["person", "dataset"],
            domain="data_eng",
        )
        assert msgs[0].role == "system"
        assert "memories" in msgs[0].content
        assert "Memory:" in msgs[1].content
        assert "domain_c_sessions" in msgs[1].content

    def test_entity_extraction_schema_mentions_json(self) -> None:
        """Sanity: system prompt documents JSON output."""
        assert "JSON" in ENTITY_EXTRACTION_V1.system
        assert "entities" in ENTITY_EXTRACTION_V1.system
        assert "edges" in ENTITY_EXTRACTION_V1.system

    def test_memory_extraction_no_edges_by_design(self) -> None:
        """Memory template asks for empty edges list."""
        assert '"edges": []' in MEMORY_EXTRACTION_V1.system

    def test_template_identity(self) -> None:
        """Shipped templates have stable name+version — consumers can pin."""
        assert ENTITY_EXTRACTION_V1.name == "entity_extraction"
        assert ENTITY_EXTRACTION_V1.version == "1.0"
        assert MEMORY_EXTRACTION_V1.name == "memory_extraction"
        # 1.1: participant/frame-preservation rules (#299/#300).
        # 1.2: skip discipline + anti-meta guard (#311).
        assert MEMORY_EXTRACTION_V1.version == "1.2"

    def test_memory_extraction_frame_rules_present(self) -> None:
        """1.1 rules: never mint speakers; mentions carry no ownership."""
        assert "NEVER extract the text's speakers" in MEMORY_EXTRACTION_V1.system
        assert "MENTIONS, nothing stronger" in MEMORY_EXTRACTION_V1.system

    def test_memory_extraction_skip_criteria_present(self) -> None:
        """1.2: explicit skip criteria for operational noise (#311)."""
        system = " ".join(MEMORY_EXTRACTION_V1.system.split())
        assert "Skip discipline" in system
        assert "status check that found nothing" in system
        assert "dependency install or build that completed cleanly" in system
        assert "bare file or directory listing" in system
        assert "restatement of a finding" in system
        assert "research or a search that found nothing" in system

    def test_memory_extraction_skips_are_silent(self) -> None:
        """1.2: a skip is the empty JSON, never a prose explanation."""
        # Normalize line wrapping — the clause matters, not the reflow.
        system = " ".join(MEMORY_EXTRACTION_V1.system.split())
        assert "never explain the skip in prose" in system
        # The skip target is the same empty shape as the no-entities case,
        # so a skipping model cannot produce a stored artifact. Assert it in
        # skip-block context — the bare literal also matches the long-standing
        # no-entities sentence, so it would pass without the skip block.
        assert 'Extract NOTHING (return {"entities": [], "edges": []})' in system
        assert 'If skipping, return {"entities": [], "edges": []} and nothing' in system

    def test_memory_extraction_anti_meta_guard_present(self) -> None:
        """1.2: record the work, never the recording process itself."""
        system = " ".join(MEMORY_EXTRACTION_V1.system.split())
        assert "learned, built, or fixed" in system
        assert "NEVER what you or the recording process are doing" in system
        # The guard is about the recording process, not the topic: a memory
        # whose subject IS an extraction pipeline must still be extracted.
        assert "ordinary subject matter" in system
