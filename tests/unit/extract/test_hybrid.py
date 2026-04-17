"""Tests for HybridJSONExtractor — deterministic-first + LLM residue."""

from __future__ import annotations

from typing import Any, ClassVar

from trellis.extract.base import ExtractorTier
from trellis.extract.context import ExtractionContext
from trellis.extract.hybrid import HybridJSONExtractor
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)


class FakeExtractor:
    """Programmable fake implementing the Extractor Protocol.

    Produces a preconfigured :class:`ExtractionResult` and records the
    inputs it was called with.  Used to isolate HybridJSONExtractor
    behavior from the real deterministic/LLM implementations.
    """

    supported_sources: ClassVar[list[str]] = ["test"]

    def __init__(
        self,
        *,
        name: str,
        tier: ExtractorTier,
        result: ExtractionResult,
        version: str = "0.0.0",
    ) -> None:
        self.name = name
        self.tier = tier
        self.version = version
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        self.calls.append(
            {
                "raw_input": raw_input,
                "source_hint": source_hint,
                "context": context,
            }
        )
        return self._result


def _result(
    *,
    name: str = "fake",
    tier: ExtractorTier = ExtractorTier.DETERMINISTIC,
    entities: list[EntityDraft] | None = None,
    edges: list[EdgeDraft] | None = None,
    confidence: float = 1.0,
    residue: Any | None = None,
    llm_calls: int = 0,
    tokens_used: int = 0,
) -> ExtractionResult:
    return ExtractionResult(
        entities=entities or [],
        edges=edges or [],
        extractor_used=name,
        tier=tier.value,
        llm_calls=llm_calls,
        tokens_used=tokens_used,
        overall_confidence=confidence,
        unparsed_residue=residue,
        provenance=ExtractionProvenance(
            extractor_name=name,
            extractor_version="0.0.0",
            source_hint=None,
        ),
    )


def _ent(
    name: str,
    *,
    entity_type: str = "person",
    entity_id: str | None = None,
    confidence: float = 1.0,
) -> EntityDraft:
    return EntityDraft(
        entity_id=entity_id,
        entity_type=entity_type,
        name=name,
        confidence=confidence,
    )


def _edge(
    source_id: str,
    target_id: str,
    *,
    edge_kind: str = "mentions",
    confidence: float = 1.0,
) -> EdgeDraft:
    return EdgeDraft(
        source_id=source_id,
        target_id=target_id,
        edge_kind=edge_kind,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Short-circuit: deterministic wins fully
# ---------------------------------------------------------------------------


class TestDeterministicShortCircuit:
    async def test_high_confidence_no_residue_skips_llm(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(
                entities=[_ent("A", entity_id="ent-a")],
                confidence=1.0,
                residue=None,
            ),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(entities=[_ent("Z")], confidence=0.9),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        result = await ext.extract(
            "hi",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert llm.calls == []  # LLM never called
        assert len(result.entities) == 1
        assert result.entities[0].name == "A"
        # Composite identity is preserved
        assert result.extractor_used == "hybrid(det+llm)"
        assert result.tier == ExtractorTier.HYBRID.value

    async def test_threshold_boundary(self) -> None:
        """Exactly at threshold with no residue → no LLM call."""
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(entities=[_ent("A")], confidence=0.7, residue=None),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm, confidence_threshold=0.7)
        await ext.extract("hi", context=ExtractionContext(allow_llm_fallback=True))
        assert llm.calls == []


# ---------------------------------------------------------------------------
# LLM fires for residue
# ---------------------------------------------------------------------------


class TestLLMForResidue:
    async def test_residue_triggers_llm(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(
                entities=[_ent("A", entity_id="ent-a")],
                confidence=0.5,
                residue={"text": "leftover", "unmatched_mentions": ["ghost"]},
            ),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(
                name="llm",
                tier=ExtractorTier.LLM,
                entities=[_ent("Ghost", entity_id="ent-ghost")],
                confidence=0.8,
                llm_calls=1,
                tokens_used=42,
            ),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        result = await ext.extract(
            {"doc_id": "mem-1", "text": "hi @alice and @ghost"},
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert len(llm.calls) == 1
        # Residue preserved and doc_id merged from raw_input
        llm_input = llm.calls[0]["raw_input"]
        assert llm_input["text"] == "leftover"
        assert llm_input["doc_id"] == "mem-1"
        assert llm_input["unmatched_mentions"] == ["ghost"]
        # Both contributions merged
        names = {e.name for e in result.entities}
        assert names == {"A", "Ghost"}
        assert result.llm_calls == 1
        assert result.tokens_used == 42
        # Confidence = min of both stages (0.5, 0.8) = 0.5
        assert result.overall_confidence == 0.5

    async def test_low_confidence_triggers_llm(self) -> None:
        """Confidence below threshold fires LLM even when residue is None."""
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(entities=[_ent("A")], confidence=0.5, residue=None),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(entities=[_ent("B")], confidence=0.9, llm_calls=1),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm, confidence_threshold=0.7)
        result = await ext.extract(
            "text",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert len(llm.calls) == 1
        assert {e.name for e in result.entities} == {"A", "B"}

    async def test_residue_string_passed_to_llm(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(confidence=0.0, residue="unclaimed text"),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(llm_calls=1),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        await ext.extract(
            {"doc_id": "m-1", "text": "original"},
            context=ExtractionContext(allow_llm_fallback=True),
        )
        # String residue promoted to dict with doc_id
        assert llm.calls[0]["raw_input"] == {
            "doc_id": "m-1",
            "text": "unclaimed text",
        }


# ---------------------------------------------------------------------------
# Budget gates
# ---------------------------------------------------------------------------


class TestBudgetGates:
    async def test_allow_llm_fallback_false_skips_llm(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(confidence=0.0, residue="text"),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        result = await ext.extract(
            "text",
            context=ExtractionContext(allow_llm_fallback=False),
        )
        assert llm.calls == []
        assert result.llm_calls == 0
        assert result.extractor_used == "hybrid(det+llm)"

    async def test_max_llm_calls_zero_skips_llm(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(confidence=0.0, residue="text"),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        await ext.extract(
            "text",
            context=ExtractionContext(allow_llm_fallback=True, max_llm_calls=0),
        )
        assert llm.calls == []

    async def test_no_context_skips_llm(self) -> None:
        """No context = no explicit opt-in = no LLM call."""
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(confidence=0.0, residue="text"),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        await ext.extract("text")  # no context passed
        assert llm.calls == []


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    async def test_entity_dedup_by_type_and_id(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(
                entities=[_ent("Alice", entity_type="person", entity_id="ent-a")],
                confidence=0.5,
                residue="x",
            ),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(
                entities=[
                    _ent(
                        "Alice Smith",  # different name, same id
                        entity_type="person",
                        entity_id="ent-a",
                    )
                ],
                confidence=0.9,
                llm_calls=1,
            ),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        result = await ext.extract(
            "text",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert len(result.entities) == 1
        # Deterministic wins
        assert result.entities[0].name == "Alice"

    async def test_entity_dedup_by_type_and_name_when_no_id(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(
                entities=[_ent("Alice", entity_type="person")],  # no id
                confidence=0.5,
                residue="x",
            ),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(
                entities=[_ent("Alice", entity_type="person")],
                confidence=0.9,
                llm_calls=1,
            ),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        result = await ext.extract(
            "text",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert len(result.entities) == 1

    async def test_edge_dedup_by_triple(self) -> None:
        det = FakeExtractor(
            name="det",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(
                edges=[_edge("a", "b", edge_kind="owns")],
                confidence=0.5,
                residue="x",
            ),
        )
        llm = FakeExtractor(
            name="llm",
            tier=ExtractorTier.LLM,
            result=_result(
                edges=[
                    _edge("a", "b", edge_kind="owns"),  # duplicate
                    _edge("a", "c", edge_kind="owns"),  # new
                ],
                confidence=0.9,
                llm_calls=1,
            ),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        result = await ext.extract(
            "text",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert len(result.edges) == 2
        assert {(e.source_id, e.target_id) for e in result.edges} == {
            ("a", "b"),
            ("a", "c"),
        }


# ---------------------------------------------------------------------------
# Confidence math + provenance
# ---------------------------------------------------------------------------


class TestResultMetadata:
    async def test_composite_provenance(self) -> None:
        det = FakeExtractor(
            name="rules",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(entities=[_ent("A")], confidence=0.5, residue="x"),
        )
        llm = FakeExtractor(
            name="gpt",
            tier=ExtractorTier.LLM,
            result=_result(entities=[_ent("B")], confidence=0.8, llm_calls=1),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm, version="9.9")
        result = await ext.extract(
            "t",
            source_hint="save_memory",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert result.extractor_used == "hybrid(rules+gpt)"
        assert result.provenance.extractor_name == "hybrid(rules+gpt)"
        assert result.provenance.extractor_version == "9.9"
        assert result.provenance.source_hint == "save_memory"
        assert result.tier == ExtractorTier.HYBRID.value

    async def test_confidence_min_when_both_contribute(self) -> None:
        det = FakeExtractor(
            name="d",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(entities=[_ent("A")], confidence=0.6, residue="x"),
        )
        llm = FakeExtractor(
            name="l",
            tier=ExtractorTier.LLM,
            result=_result(entities=[_ent("B")], confidence=0.9, llm_calls=1),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        r = await ext.extract("t", context=ExtractionContext(allow_llm_fallback=True))
        assert r.overall_confidence == 0.6

    async def test_confidence_from_llm_only_when_det_empty(self) -> None:
        det = FakeExtractor(
            name="d",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(confidence=0.0, residue="x"),
        )
        llm = FakeExtractor(
            name="l",
            tier=ExtractorTier.LLM,
            result=_result(entities=[_ent("B")], confidence=0.8, llm_calls=1),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        r = await ext.extract("t", context=ExtractionContext(allow_llm_fallback=True))
        assert r.overall_confidence == 0.8

    async def test_tokens_and_llm_calls_summed(self) -> None:
        det = FakeExtractor(
            name="d",
            tier=ExtractorTier.DETERMINISTIC,
            result=_result(confidence=0.0, residue="x", tokens_used=10),
        )
        llm = FakeExtractor(
            name="l",
            tier=ExtractorTier.LLM,
            result=_result(
                entities=[_ent("B")],
                confidence=0.8,
                llm_calls=1,
                tokens_used=42,
            ),
        )
        ext = HybridJSONExtractor(deterministic=det, llm=llm)
        r = await ext.extract("t", context=ExtractionContext(allow_llm_fallback=True))
        assert r.llm_calls == 1
        assert r.tokens_used == 52
