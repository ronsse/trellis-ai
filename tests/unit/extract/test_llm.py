"""Tests for LLMExtractor — tier=LLM JSON extraction."""

from __future__ import annotations

import json

import pytest

from trellis.extract.base import ExtractorTier
from trellis.extract.context import ExtractionContext
from trellis.extract.llm import LLMExtractor
from trellis.llm.types import LLMResponse, Message, TokenUsage
from trellis.schemas.enums import NodeRole


class FakeLLMClient:
    """Programmable fake implementing the :class:`LLMClient` protocol.

    Records calls for assertions and returns a preconfigured
    ``LLMResponse``.  ``response_text`` may be overridden per-call via
    ``next_response``.
    """

    def __init__(
        self,
        response_text: str = '{"entities": [], "edges": []}',
        usage: TokenUsage | None = None,
        model: str = "fake-model",
    ) -> None:
        self.response_text = response_text
        self.usage = usage
        self.model = model
        self.calls: list[dict] = []
        self.next_response: str | None = None

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
        text = (
            self.next_response if self.next_response is not None else self.response_text
        )
        self.next_response = None
        return LLMResponse(content=text, model=model or self.model, usage=self.usage)


# ---------------------------------------------------------------------------
# Contract / config
# ---------------------------------------------------------------------------


class TestExtractorContract:
    async def test_tier_and_defaults(self) -> None:
        ext = LLMExtractor(llm_client=FakeLLMClient())
        assert ext.tier == ExtractorTier.LLM
        assert ext.name == "llm_extractor"
        assert ext.supported_sources == []
        assert ext.version == "0.1.0"

    async def test_custom_config(self) -> None:
        ext = LLMExtractor(
            "custom",
            llm_client=FakeLLMClient(),
            entity_type_hints=["person"],
            edge_kind_hints=["owns"],
            model="gpt-x",
            temperature=0.1,
            max_tokens=500,
            supported_sources=["free-text"],
            version="2.0",
        )
        assert ext.name == "custom"
        assert ext.supported_sources == ["free-text"]
        assert ext.version == "2.0"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_parses_clean_json(self) -> None:
        payload = {
            "entities": [
                {
                    "entity_id": "alice",
                    "entity_type": "person",
                    "name": "Alice",
                    "properties": {"team": "platform"},
                    "confidence": 0.9,
                },
                {
                    "entity_id": None,
                    "entity_type": "pipeline",
                    "name": "orders",
                    "confidence": 0.7,
                },
            ],
            "edges": [
                {
                    "source_id": "alice",
                    "target_id": "orders",
                    "edge_kind": "deployed",
                    "confidence": 0.8,
                }
            ],
        }
        fake = FakeLLMClient(
            response_text=json.dumps(payload),
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )
        ext = LLMExtractor(llm_client=fake)

        result = await ext.extract("Alice deployed orders pipeline")

        assert len(result.entities) == 2
        assert result.entities[0].entity_id == "alice"
        assert result.entities[0].name == "Alice"
        assert result.entities[0].properties == {"team": "platform"}
        assert result.entities[0].node_role == NodeRole.SEMANTIC
        assert result.entities[1].entity_id is None  # null in JSON → None
        assert len(result.edges) == 1
        assert result.edges[0].edge_kind == "deployed"
        assert result.llm_calls == 1
        assert result.tokens_used == 150
        assert 0.7 <= result.overall_confidence <= 0.9
        assert result.unparsed_residue is None

    async def test_usage_absent_tokens_zero(self) -> None:
        fake = FakeLLMClient(
            response_text=(
                '{"entities": [{"entity_type": "x", "name": "y", '
                '"confidence": 0.8}], "edges": []}'
            ),
            usage=None,
        )
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("text")
        assert result.tokens_used == 0
        assert result.llm_calls == 1

    async def test_passes_model_and_params_to_llm(self) -> None:
        fake = FakeLLMClient()
        ext = LLMExtractor(
            llm_client=fake,
            model="gpt-5",
            temperature=0.05,
            max_tokens=777,
        )
        await ext.extract("hi")
        call = fake.calls[0]
        assert call["model"] == "gpt-5"
        assert call["temperature"] == 0.05
        assert call["max_tokens"] == 777

    async def test_prompt_receives_hints_and_domain(self) -> None:
        fake = FakeLLMClient()
        ext = LLMExtractor(
            llm_client=fake,
            entity_type_hints=["person", "system"],
            edge_kind_hints=["owns"],
        )
        await ext.extract(
            "text",
            context=ExtractionContext(
                allow_llm_fallback=True,
                domain="data_eng",
                source_system="dbt",
            ),
        )
        user_msg = fake.calls[0]["messages"][1].content
        assert "Prefer these entity types: person, system" in user_msg
        assert "Prefer these edge kinds: owns" in user_msg
        assert "Domain: data_eng" in user_msg
        assert "Source system: dbt" in user_msg


# ---------------------------------------------------------------------------
# Tolerant JSON parsing
# ---------------------------------------------------------------------------


class TestJsonTolerance:
    async def test_strips_code_fences(self) -> None:
        fake = FakeLLMClient(
            response_text=(
                "```json\n"
                '{"entities": [{"entity_type":"p","name":"A","confidence":0.9}], '
                '"edges": []}\n'
                "```"
            )
        )
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert len(result.entities) == 1
        assert result.entities[0].name == "A"

    async def test_strips_bare_code_fences(self) -> None:
        fake = FakeLLMClient(response_text='```\n{"entities": [], "edges": []}\n```')
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert result.entities == []
        assert result.unparsed_residue is None or result.unparsed_residue == "x"
        # Parse succeeded even though no drafts were produced — confidence reflects that
        assert result.overall_confidence == 0.0

    async def test_recovers_from_prose_wrapper(self) -> None:
        """Some LLMs emit prose around the JSON despite instructions."""
        fake = FakeLLMClient(
            response_text=(
                "Sure, here's the JSON:\n"
                '{"entities": [{"entity_type":"p","name":"B","confidence":0.8}], '
                '"edges": []}\n'
                "Let me know if you need more."
            )
        )
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert len(result.entities) == 1
        assert result.entities[0].name == "B"

    async def test_bare_array_lifted_to_entities(self) -> None:
        fake = FakeLLMClient(
            response_text='[{"entity_type":"p","name":"C","confidence":0.9}]'
        )
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert len(result.entities) == 1
        assert result.entities[0].name == "C"

    async def test_malformed_json_surfaces_residue(self) -> None:
        fake = FakeLLMClient(response_text="not json at all")
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert result.entities == []
        assert result.edges == []
        assert result.unparsed_residue == "not json at all"
        assert result.overall_confidence == 0.0
        assert result.llm_calls == 1  # call still happened

    async def test_empty_response_surfaces_residue(self) -> None:
        fake = FakeLLMClient(response_text="")
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert result.entities == []
        assert result.unparsed_residue == ""
        assert result.overall_confidence == 0.0


# ---------------------------------------------------------------------------
# Robustness against bad LLM output
# ---------------------------------------------------------------------------


class TestDraftRobustness:
    async def test_skips_entity_without_name(self) -> None:
        payload = {
            "entities": [
                {"entity_type": "p", "name": "Good", "confidence": 0.9},
                {"entity_type": "p"},  # missing name
                {"name": "no-type", "confidence": 0.9},  # missing type
                {"entity_type": "p", "name": "", "confidence": 0.9},  # blank name
            ],
            "edges": [],
        }
        fake = FakeLLMClient(response_text=json.dumps(payload))
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert len(result.entities) == 1
        assert result.entities[0].name == "Good"

    async def test_skips_edge_with_missing_fields(self) -> None:
        payload = {
            "entities": [
                {"entity_type": "p", "name": "A", "confidence": 0.9},
                {"entity_type": "p", "name": "B", "confidence": 0.9},
            ],
            "edges": [
                {
                    "source_id": "A",
                    "target_id": "B",
                    "edge_kind": "e",
                    "confidence": 0.9,
                },
                {"source_id": "A", "target_id": "B"},  # no edge_kind
                {"source_id": "", "target_id": "B", "edge_kind": "e"},  # blank source
            ],
        }
        fake = FakeLLMClient(response_text=json.dumps(payload))
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert len(result.edges) == 1

    async def test_confidence_out_of_range_clamped(self) -> None:
        payload = {
            "entities": [
                {"entity_type": "p", "name": "X", "confidence": 1.5},
                {"entity_type": "p", "name": "Y", "confidence": -0.3},
                {"entity_type": "p", "name": "Z", "confidence": "not-a-number"},
            ],
            "edges": [],
        }
        fake = FakeLLMClient(response_text=json.dumps(payload))
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        confs = [e.confidence for e in result.entities]
        assert confs == [1.0, 0.0, 0.5]  # clamped / defaulted

    async def test_properties_not_dict_defaulted(self) -> None:
        payload = {
            "entities": [
                {
                    "entity_type": "p",
                    "name": "X",
                    "properties": "not-a-dict",
                    "confidence": 0.9,
                }
            ],
            "edges": [],
        }
        fake = FakeLLMClient(response_text=json.dumps(payload))
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert result.entities[0].properties == {}

    async def test_entities_not_list_treated_as_empty(self) -> None:
        payload = {"entities": "oops", "edges": "oops"}
        fake = FakeLLMClient(response_text=json.dumps(payload))
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract("x")
        assert result.entities == []
        assert result.edges == []


# ---------------------------------------------------------------------------
# Budget + context
# ---------------------------------------------------------------------------


class TestBudget:
    async def test_zero_budget_short_circuits(self) -> None:
        fake = FakeLLMClient()
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract(
            "some text",
            context=ExtractionContext(allow_llm_fallback=True, max_llm_calls=0),
        )
        assert fake.calls == []  # no LLM call
        assert result.llm_calls == 0
        assert result.tokens_used == 0
        assert result.overall_confidence == 0.0
        assert result.unparsed_residue == "some text"


# ---------------------------------------------------------------------------
# Input shape handling
# ---------------------------------------------------------------------------


class TestInputShapes:
    async def test_dict_input(self) -> None:
        fake = FakeLLMClient(
            response_text=(
                '{"entities": [{"entity_type":"p","name":"A",'
                '"confidence":0.9}], "edges": []}'
            )
        )
        ext = LLMExtractor(llm_client=fake)
        result = await ext.extract({"doc_id": "mem-1", "text": "hi"})
        user_msg = fake.calls[0]["messages"][1].content
        assert "hi" in user_msg
        assert len(result.entities) == 1

    async def test_rejects_non_str_text(self) -> None:
        ext = LLMExtractor(llm_client=FakeLLMClient())
        with pytest.raises(TypeError, match="'text' field"):
            await ext.extract({"text": 42})

    async def test_rejects_non_str_doc_id(self) -> None:
        ext = LLMExtractor(llm_client=FakeLLMClient())
        with pytest.raises(TypeError, match="'doc_id' must be"):
            await ext.extract({"text": "x", "doc_id": 42})

    async def test_rejects_invalid_type(self) -> None:
        ext = LLMExtractor(llm_client=FakeLLMClient())
        with pytest.raises(TypeError, match="str or dict"):
            await ext.extract(123)


# ---------------------------------------------------------------------------
# Result metadata
# ---------------------------------------------------------------------------


class TestResultMetadata:
    async def test_provenance(self) -> None:
        ext = LLMExtractor(
            "custom-llm",
            llm_client=FakeLLMClient(),
            version="2.3.4",
        )
        result = await ext.extract("x", source_hint="free-text")
        assert result.extractor_used == "custom-llm"
        assert result.tier == ExtractorTier.LLM.value
        assert result.provenance.extractor_name == "custom-llm"
        assert result.provenance.extractor_version == "2.3.4"
        assert result.provenance.source_hint == "free-text"
