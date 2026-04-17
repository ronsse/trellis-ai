"""Tests for the Extractor Protocol, tier enum, and error types."""

from __future__ import annotations

from typing import Any, ClassVar

from trellis.extract.base import (
    Extractor,
    ExtractorTier,
    NoExtractorAvailableError,
)
from trellis.extract.context import ExtractionContext
from trellis.schemas.extraction import ExtractionProvenance, ExtractionResult


class _StubExtractor:
    """Minimal class that satisfies the Extractor Protocol via duck typing."""

    name = "stub"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["stub-source"]
    version = "0.1.0"

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
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )


class TestExtractorTier:
    def test_members(self) -> None:
        assert ExtractorTier.DETERMINISTIC.value == "deterministic"
        assert ExtractorTier.HYBRID.value == "hybrid"
        assert ExtractorTier.LLM.value == "llm"


class TestExtractorProtocol:
    def test_stub_is_runtime_checkable_extractor(self) -> None:
        assert isinstance(_StubExtractor(), Extractor)

    async def test_stub_returns_extraction_result(self) -> None:
        extractor = _StubExtractor()
        result = await extractor.extract({}, source_hint="stub-source")
        assert result.extractor_used == "stub"
        assert result.tier == "deterministic"
        assert result.provenance.extractor_name == "stub"
        assert result.provenance.source_hint == "stub-source"


class TestNoExtractorAvailableError:
    def test_carries_fields(self) -> None:
        exc = NoExtractorAvailableError("unknown", "no match")
        assert exc.source_hint == "unknown"
        assert exc.reason == "no match"
        assert "unknown" in str(exc)
        assert "no match" in str(exc)


class TestExtractionContext:
    def test_defaults(self) -> None:
        ctx = ExtractionContext()
        assert ctx.allow_llm_fallback is False
        assert ctx.max_llm_calls == 5
        assert ctx.max_tokens == 8000
        assert ctx.prefer_tier is None
        assert ctx.domain is None
        assert ctx.source_system is None

    def test_prefer_tier_accepts_enum(self) -> None:
        ctx = ExtractionContext(prefer_tier=ExtractorTier.LLM)
        assert ctx.prefer_tier == ExtractorTier.LLM
