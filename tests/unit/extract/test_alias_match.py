"""Tests for AliasMatchExtractor — deterministic mention resolution."""

from __future__ import annotations

import re

import pytest

from trellis.extract.alias_match import AliasMatchExtractor
from trellis.extract.base import ExtractorTier


def _make_resolver(mapping: dict[str, list[str]]):
    """Build a resolver that looks up mentions in a dict."""

    def resolver(alias: str) -> list[str]:
        return list(mapping.get(alias, []))

    return resolver


class TestExtractorContract:
    async def test_tier_and_metadata(self) -> None:
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({}))
        assert ext.tier == ExtractorTier.DETERMINISTIC
        assert ext.supported_sources == ["save_memory"]
        assert ext.name == "alias_match"
        assert ext.version == "0.1.0"

    async def test_custom_name_sources_version(self) -> None:
        ext = AliasMatchExtractor(
            "custom",
            alias_resolver=_make_resolver({}),
            supported_sources=["memo", "save_memory"],
            version="2.0",
        )
        assert ext.name == "custom"
        assert ext.supported_sources == ["memo", "save_memory"]
        assert ext.version == "2.0"


class TestInputShapes:
    async def test_string_input_no_edges_emitted(self) -> None:
        """Plain string has no doc_id → matches found but no edges."""
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract("hello @alice")
        assert result.edges == []
        assert result.entities == []
        assert result.overall_confidence == 1.0  # matched, just no edge target
        assert result.unparsed_residue is None

    async def test_dict_input_with_doc_id_emits_edge(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract({"doc_id": "mem-1", "text": "hi @alice"})
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.source_id == "mem-1"
        assert edge.target_id == "ent-alice"
        assert edge.edge_kind == "mentions"
        assert edge.confidence == 1.0

    async def test_dict_input_without_doc_id_acts_like_string(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract({"text": "hi @alice"})
        assert result.edges == []

    async def test_invalid_type_raises(self) -> None:
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({}))
        with pytest.raises(TypeError, match="str or dict"):
            await ext.extract(42)

    async def test_non_string_text_raises(self) -> None:
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({}))
        with pytest.raises(TypeError, match="'text' field"):
            await ext.extract({"text": 42, "doc_id": "mem-1"})

    async def test_non_string_doc_id_raises(self) -> None:
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({}))
        with pytest.raises(TypeError, match="'doc_id' must be a string"):
            await ext.extract({"text": "x", "doc_id": 42})


class TestResolution:
    async def test_single_match_emits_edge(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract({"doc_id": "d1", "text": "ping @alice"})
        assert len(result.edges) == 1
        assert result.edges[0].target_id == "ent-alice"
        assert result.overall_confidence == 1.0
        assert result.unparsed_residue is None

    async def test_multiple_mentions_all_resolved(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"], "bob": ["ent-bob"]}),
        )
        result = await ext.extract(
            {"doc_id": "d1", "text": "@alice pinged @bob about it"}
        )
        assert {e.target_id for e in result.edges} == {"ent-alice", "ent-bob"}
        assert result.overall_confidence == 1.0

    async def test_duplicate_mentions_deduplicated(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract(
            {"doc_id": "d1", "text": "@alice thanks @alice again"}
        )
        assert len(result.edges) == 1
        assert result.overall_confidence == 1.0

    async def test_unknown_mention_goes_to_residue(self) -> None:
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({}))
        result = await ext.extract({"doc_id": "d1", "text": "hey @ghost"})
        assert result.edges == []
        assert result.overall_confidence == 0.0
        assert isinstance(result.unparsed_residue, dict)
        assert result.unparsed_residue["unmatched_mentions"] == ["ghost"]
        assert result.unparsed_residue["text"] == "hey @ghost"

    async def test_ambiguous_mention_skips_silently(self) -> None:
        """Two+ candidates => extractor refuses to guess, adds to residue."""
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-a1", "ent-a2"]}),
        )
        result = await ext.extract({"doc_id": "d1", "text": "@alice here"})
        assert result.edges == []
        assert result.overall_confidence == 0.0
        assert result.unparsed_residue["unmatched_mentions"] == ["alice"]

    async def test_partial_resolution_keeps_text_and_unmatched(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract({"doc_id": "d1", "text": "@alice and @ghost"})
        assert len(result.edges) == 1
        assert result.edges[0].target_id == "ent-alice"
        assert result.overall_confidence == 0.5
        assert result.unparsed_residue == {
            "text": "@alice and @ghost",
            "unmatched_mentions": ["ghost"],
        }

    async def test_no_mentions_no_result_full_text_residue(self) -> None:
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({"alice": ["ent-a"]}))
        result = await ext.extract({"doc_id": "d1", "text": "nothing to see"})
        assert result.edges == []
        assert result.overall_confidence == 0.0
        assert result.unparsed_residue == "nothing to see"


class TestCustomization:
    async def test_custom_edge_kind(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
            edge_kind="references",
        )
        result = await ext.extract({"doc_id": "d1", "text": "@alice"})
        assert result.edges[0].edge_kind == "references"

    async def test_custom_mention_pattern(self) -> None:
        """Override the default @word pattern with a hashtag-style one."""
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"topic": ["ent-topic"]}),
            mention_pattern=re.compile(r"#([A-Za-z0-9_]+)"),
        )
        result = await ext.extract({"doc_id": "d1", "text": "about #topic indeed"})
        assert len(result.edges) == 1
        assert result.edges[0].target_id == "ent-topic"


class TestResultMetadata:
    async def test_carries_tier_and_provenance(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({}),
            version="1.2.3",
        )
        result = await ext.extract("", source_hint="save_memory")
        assert result.extractor_used == "alias_match"
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        assert result.provenance.extractor_name == "alias_match"
        assert result.provenance.extractor_version == "1.2.3"
        assert result.provenance.source_hint == "save_memory"
        assert result.llm_calls == 0
        assert result.tokens_used == 0


class TestSourceDocumentNode:
    """The ``mentions`` edge needs a source node, or ``LinkCreateHandler``'s
    FK pre-flight rejects it — see ``TestGovernedWritePath`` in
    ``test_save_memory.py``."""

    async def test_document_node_precedes_the_edges(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        result = await ext.extract({"doc_id": "mem-1", "text": "hi @alice"})

        assert len(result.entities) == 1
        doc = result.entities[0]
        assert doc.entity_id == "mem-1"
        assert doc.entity_type == "CreativeWork"  # canonical for "document"
        assert result.edges[0].source_id == doc.entity_id
        assert result.edges[0].allow_dangling is False

    async def test_no_document_node_without_a_resolved_mention(self) -> None:
        """Nothing to anchor — a memory that links to nothing needs no node."""
        ext = AliasMatchExtractor(alias_resolver=_make_resolver({}))
        result = await ext.extract({"doc_id": "mem-1", "text": "hey @ghost"})

        assert result.entities == []

    async def test_document_node_name_is_the_first_line_truncated(self) -> None:
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"alice": ["ent-alice"]}),
        )
        text = "\n  \n" + "word " * 40 + "\n@alice"
        result = await ext.extract({"doc_id": "mem-1", "text": text})

        name = result.entities[0].name
        assert len(name) == 80
        assert name.endswith("…")
        assert name.startswith("word word word")

    async def test_document_node_falls_back_to_the_id_when_text_is_blank(
        self,
    ) -> None:
        """Defensive branch — only reachable through a custom pattern,
        but a nameless node is unreadable in every graph view."""
        ext = AliasMatchExtractor(
            alias_resolver=_make_resolver({"": ["ent-alice"]}),
            mention_pattern=re.compile(r"^()"),
        )
        result = await ext.extract({"doc_id": "mem-1", "text": "   "})

        assert result.entities[0].name == "mem-1"


class TestResolverCallEconomy:
    """A resolution can cost a bounded full-graph scan, so the extractor
    must not pay for the same token twice inside one document — while the
    occurrence-level accounting ``_summarize`` reads stays untouched."""

    @staticmethod
    def _counting_resolver(mapping: dict[str, list[str]]) -> tuple:
        calls: list[str] = []

        def resolver(alias: str) -> list[str]:
            calls.append(alias)
            return list(mapping.get(alias, []))

        return resolver, calls

    async def test_repeated_mention_resolves_once(self) -> None:
        resolver, calls = self._counting_resolver({"alice": ["ent-alice"]})
        ext = AliasMatchExtractor(alias_resolver=resolver)

        result = await ext.extract(
            {"doc_id": "d1", "text": "@alice ping @alice again @alice"}
        )

        assert calls == ["alice"]
        assert len(result.edges) == 1

    async def test_unresolvable_repeat_also_resolves_once(self) -> None:
        """The case the production corpus is made of: 118 of 119 mention
        occurrences match nothing, so minting can never retire them — the
        scan is only removed by not repeating it."""
        resolver, calls = self._counting_resolver({})
        ext = AliasMatchExtractor(alias_resolver=resolver)

        result = await ext.extract(
            {"doc_id": "d1", "text": "a@gmail.com b@gmail.com c@gmail.com"}
        )

        assert calls == ["gmail"]
        # Occurrence-level accounting is unchanged: three occurrences,
        # three residue entries, one resolver call.
        assert result.unparsed_residue["unmatched_mentions"] == [
            "gmail",
            "gmail",
            "gmail",
        ]

    async def test_distinct_tokens_each_resolve_once(self) -> None:
        resolver, calls = self._counting_resolver({"alice": ["ent-alice"]})
        ext = AliasMatchExtractor(alias_resolver=resolver)

        await ext.extract({"doc_id": "d1", "text": "@alice @bob @alice @bob"})

        assert calls == ["alice", "bob"]

    async def test_case_variants_are_not_merged_by_the_cache(self) -> None:
        """Collapsing case is the resolver's rule, not the cache's — a
        cache that merged them would silently apply a matching rule the
        injected resolver never agreed to."""
        resolver, calls = self._counting_resolver({})
        ext = AliasMatchExtractor(alias_resolver=resolver)

        await ext.extract({"doc_id": "d1", "text": "@Alice and @alice"})

        assert calls == ["Alice", "alice"]

    async def test_cache_does_not_survive_the_call(self) -> None:
        """Scoped to one document, so a mention that resolves to nothing
        now can resolve on the next document once its entity exists."""
        mapping: dict[str, list[str]] = {}
        resolver, calls = self._counting_resolver(mapping)
        ext = AliasMatchExtractor(alias_resolver=resolver)

        first = await ext.extract({"doc_id": "d1", "text": "@alice"})
        assert first.edges == []

        mapping["alice"] = ["ent-alice"]
        second = await ext.extract({"doc_id": "d2", "text": "@alice"})

        assert calls == ["alice", "alice"]
        assert [e.target_id for e in second.edges] == ["ent-alice"]

    async def test_partial_resolution_summary_unchanged_by_caching(self) -> None:
        """Pin the whole ``_summarize`` output for a document with both a
        repeated hit and a repeated miss — this is the assertion that
        fails if the cache ever starts deduplicating the counts."""
        resolver, calls = self._counting_resolver({"alice": ["ent-alice"]})
        ext = AliasMatchExtractor(alias_resolver=resolver)

        text = "@alice @ghost @alice @ghost"
        result = await ext.extract({"doc_id": "d1", "text": text})

        assert calls == ["alice", "ghost"]
        assert result.overall_confidence == 0.5
        assert result.unparsed_residue == {
            "text": text,
            "unmatched_mentions": ["ghost", "ghost"],
        }
