"""Tests for ExtractorRegistry."""

from __future__ import annotations

from typing import Any

from trellis.extract.base import ExtractorTier
from trellis.extract.context import ExtractionContext
from trellis.extract.registry import ExtractorRegistry
from trellis.schemas.extraction import ExtractionProvenance, ExtractionResult


def _make_extractor(
    name: str,
    tier: ExtractorTier,
    sources: list[str],
) -> Any:
    class _E:
        def __init__(self) -> None:
            self.name = name
            self.tier = tier
            self.supported_sources = sources
            self.version = "1.0.0"

        async def extract(
            self,
            raw_input: Any,
            *,
            source_hint: str | None = None,
            context: ExtractionContext | None = None,
        ) -> ExtractionResult:
            return ExtractionResult(
                extractor_used=self.name,
                tier=self.tier.value,
                provenance=ExtractionProvenance(extractor_name=self.name),
            )

    return _E()


class TestExtractorRegistry:
    def test_register_and_get(self) -> None:
        reg = ExtractorRegistry()
        ex = _make_extractor("dbt", ExtractorTier.DETERMINISTIC, ["dbt-manifest"])
        reg.register(ex)
        assert reg.get("dbt") is ex
        assert reg.get("missing") is None

    def test_candidates_for_source(self) -> None:
        reg = ExtractorRegistry()
        a = _make_extractor("a", ExtractorTier.DETERMINISTIC, ["shared"])
        b = _make_extractor("b", ExtractorTier.LLM, ["shared"])
        c = _make_extractor("c", ExtractorTier.DETERMINISTIC, ["other"])
        reg.register(a)
        reg.register(b)
        reg.register(c)
        assert reg.candidates_for("shared") == [a, b]
        assert reg.candidates_for("other") == [c]
        assert reg.candidates_for("missing") == []

    def test_candidates_for_none_returns_all(self) -> None:
        reg = ExtractorRegistry()
        a = _make_extractor("a", ExtractorTier.DETERMINISTIC, ["s1"])
        b = _make_extractor("b", ExtractorTier.LLM, ["s2"])
        reg.register(a)
        reg.register(b)
        assert set(reg.candidates_for(None)) == {a, b}

    def test_re_register_moves_to_end(self) -> None:
        reg = ExtractorRegistry()
        a = _make_extractor("a", ExtractorTier.DETERMINISTIC, ["shared"])
        b = _make_extractor("b", ExtractorTier.DETERMINISTIC, ["shared"])
        reg.register(a)
        reg.register(b)
        # Now re-register a new extractor with the same name as `a`; it
        # should replace `a` and end up after `b` in the source bucket.
        a2 = _make_extractor("a", ExtractorTier.DETERMINISTIC, ["shared"])
        reg.register(a2)
        assert reg.candidates_for("shared") == [b, a2]
        assert reg.get("a") is a2

    def test_by_tier(self) -> None:
        reg = ExtractorRegistry()
        a = _make_extractor("a", ExtractorTier.DETERMINISTIC, ["s"])
        b = _make_extractor("b", ExtractorTier.LLM, ["s"])
        c = _make_extractor("c", ExtractorTier.DETERMINISTIC, ["t"])
        reg.register(a)
        reg.register(b)
        reg.register(c)
        assert set(reg.by_tier(ExtractorTier.DETERMINISTIC)) == {a, c}
        assert reg.by_tier(ExtractorTier.LLM) == [b]

    def test_names(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("a", ExtractorTier.DETERMINISTIC, ["s"]))
        reg.register(_make_extractor("b", ExtractorTier.LLM, ["s"]))
        assert reg.names() == ["a", "b"]
