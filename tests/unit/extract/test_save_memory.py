"""Tests for build_save_memory_extractor — the save_memory pipeline factory."""

from __future__ import annotations

from trellis.extract.alias_match import AliasMatchExtractor
from trellis.extract.base import ExtractorTier
from trellis.extract.context import ExtractionContext
from trellis.extract.hybrid import HybridJSONExtractor
from trellis.extract.llm import LLMExtractor
from trellis.extract.prompts.extraction import MEMORY_EXTRACTION_V1
from trellis.extract.save_memory import build_save_memory_extractor
from trellis.llm.types import LLMResponse, Message, TokenUsage


class _FakeLLMClient:
    """Minimal LLMClient stub that returns a preconfigured response."""

    def __init__(self, response_text: str = '{"entities": [], "edges": []}') -> None:
        self.response_text = response_text
        self.calls: list[dict] = []

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "model": model,
            }
        )
        return LLMResponse(
            content=self.response_text,
            model=model,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


def _resolver(mapping: dict[str, list[str]]):
    def lookup(alias: str) -> list[str]:
        return list(mapping.get(alias, []))

    return lookup


class TestFactoryStructure:
    def test_returns_hybrid_with_alias_and_llm_stages(self) -> None:
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({}),
            llm_client=_FakeLLMClient(),
        )
        assert isinstance(ext, HybridJSONExtractor)
        assert ext.tier == ExtractorTier.HYBRID
        assert ext.supported_sources == ["save_memory"]
        assert ext.name == "save_memory"

    def test_inner_deterministic_is_alias_match(self) -> None:
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({}),
            llm_client=_FakeLLMClient(),
        )
        assert isinstance(ext._deterministic, AliasMatchExtractor)
        assert ext._deterministic.tier == ExtractorTier.DETERMINISTIC
        assert ext._deterministic.supported_sources == ["save_memory"]

    def test_inner_llm_uses_memory_prompt(self) -> None:
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({}),
            llm_client=_FakeLLMClient(),
        )
        assert isinstance(ext._llm, LLMExtractor)
        assert ext._llm._prompt is MEMORY_EXTRACTION_V1
        assert ext._llm.tier == ExtractorTier.LLM
        assert ext._llm.supported_sources == ["save_memory"]

    def test_custom_name_and_version(self) -> None:
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({}),
            llm_client=_FakeLLMClient(),
            name="custom_memory",
            version="9.9",
        )
        assert ext.name == "custom_memory"
        assert ext.version == "9.9"

    def test_model_and_max_tokens_passed_to_llm(self) -> None:
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({}),
            llm_client=_FakeLLMClient(),
            model="fast-tiny",
            max_tokens=123,
        )
        assert ext._llm._model == "fast-tiny"
        assert ext._llm._max_tokens == 123


class TestEndToEnd:
    async def test_fully_resolved_skips_llm(self) -> None:
        """All mentions resolve → deterministic wins, LLM never called."""
        llm = _FakeLLMClient()
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({"alice": ["ent-alice"]}),
            llm_client=llm,
        )
        result = await ext.extract(
            {"doc_id": "mem-1", "text": "status from @alice"},
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert llm.calls == []
        assert len(result.edges) == 1
        assert result.edges[0].source_id == "mem-1"
        assert result.edges[0].target_id == "ent-alice"
        assert result.edges[0].edge_kind == "mentions"
        assert result.llm_calls == 0
        # Provenance names the composite
        assert result.extractor_used.startswith("hybrid(alias_match+llm_memory)")

    async def test_partial_match_fires_llm_with_residue(self) -> None:
        """Unresolved mention → LLM stage sees the residue."""
        llm = _FakeLLMClient(
            response_text=(
                '{"entities": ['
                '{"entity_type":"person","name":"Ghost","confidence":0.9}'
                '], "edges": []}'
            )
        )
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({"alice": ["ent-alice"]}),
            llm_client=llm,
        )
        result = await ext.extract(
            {"doc_id": "mem-1", "text": "@alice pinged @ghost"},
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert len(llm.calls) == 1
        # Hybrid merges edges (from alias) + entities (from LLM)
        assert {e.target_id for e in result.edges} == {"ent-alice"}
        assert {e.name for e in result.entities} == {"Ghost"}
        assert result.llm_calls == 1

    async def test_no_context_skips_llm_stage(self) -> None:
        """Without explicit opt-in, the LLM stage never fires."""
        llm = _FakeLLMClient()
        ext = build_save_memory_extractor(
            alias_resolver=_resolver({}),
            llm_client=llm,
        )
        result = await ext.extract(
            {"doc_id": "mem-1", "text": "untagged text"},
            # no context → LLM gate blocks
        )
        assert llm.calls == []
        assert result.llm_calls == 0
