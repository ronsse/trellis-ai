"""Tests for ExtractionDispatcher routing + telemetry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.extract.base import ExtractorTier, NoExtractorAvailableError
from trellis.extract.context import ExtractionContext
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.schemas.extraction import (
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.stores.event_log import EventType, SQLiteEventLog


def _make_extractor(
    name: str,
    tier: ExtractorTier,
    sources: list[str],
    *,
    entities: int = 0,
    edges: int = 0,
    llm_calls: int = 0,
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
            draft_entities = [
                EntityDraft(entity_type="stub", name=f"{name}-{i}")
                for i in range(entities)
            ]
            return ExtractionResult(
                entities=draft_entities,
                edges=[],
                extractor_used=self.name,
                tier=self.tier.value,
                llm_calls=llm_calls,
                provenance=ExtractionProvenance(
                    extractor_name=self.name,
                    source_hint=source_hint,
                ),
            )

    return _E()


@pytest.fixture
def event_log(tmp_path: Path) -> SQLiteEventLog:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log  # type: ignore[misc]
    log.close()


class TestRoutingPriority:
    async def test_deterministic_preferred_over_llm(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"]))
        reg.register(_make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"]))
        d = ExtractionDispatcher(reg)
        # Even with LLM allowed, deterministic wins on tier priority.
        result = await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert result.extractor_used == "det"

    async def test_hybrid_preferred_over_llm(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"]))
        reg.register(_make_extractor("hyb", ExtractorTier.HYBRID, ["s"]))
        d = ExtractionDispatcher(reg)
        result = await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert result.extractor_used == "hyb"

    async def test_llm_gated_off_by_default(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"]))
        d = ExtractionDispatcher(reg)
        with pytest.raises(NoExtractorAvailableError) as exc:
            await d.dispatch({}, source_hint="s")
        assert "allow_llm_fallback" in exc.value.reason

    async def test_llm_allowed_when_opted_in(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"]))
        d = ExtractionDispatcher(reg)
        result = await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        assert result.extractor_used == "llm"

    async def test_prefer_tier_overrides_priority(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"]))
        reg.register(_make_extractor("hyb", ExtractorTier.HYBRID, ["s"]))
        d = ExtractionDispatcher(reg)
        result = await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(prefer_tier=ExtractorTier.HYBRID),
        )
        assert result.extractor_used == "hyb"

    async def test_prefer_tier_llm_still_gated(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"]))
        d = ExtractionDispatcher(reg)
        with pytest.raises(NoExtractorAvailableError):
            await d.dispatch(
                {},
                source_hint="s",
                context=ExtractionContext(prefer_tier=ExtractorTier.LLM),
            )

    async def test_no_candidates_for_hint(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("det", ExtractorTier.DETERMINISTIC, ["other"]))
        d = ExtractionDispatcher(reg)
        with pytest.raises(NoExtractorAvailableError) as exc:
            await d.dispatch({}, source_hint="missing")
        assert "no registered extractors" in exc.value.reason

    async def test_null_hint_picks_highest_priority(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s1"]))
        reg.register(_make_extractor("det", ExtractorTier.DETERMINISTIC, ["s2"]))
        d = ExtractionDispatcher(reg)
        result = await d.dispatch({}, source_hint=None)
        assert result.extractor_used == "det"


class TestTelemetry:
    async def test_emits_extraction_dispatched(
        self,
        event_log: SQLiteEventLog,
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor(
                "det",
                ExtractorTier.DETERMINISTIC,
                ["s"],
                llm_calls=0,
            )
        )
        d = ExtractionDispatcher(reg, event_log=event_log)
        await d.dispatch({}, source_hint="s")

        events = event_log.get_events(event_type=EventType.EXTRACTION_DISPATCHED)
        assert len(events) == 1
        evt = events[0]
        assert evt.source == "extraction_dispatcher"
        assert evt.payload["extractor_used"] == "det"
        assert evt.payload["tier"] == "deterministic"
        assert evt.payload["source_hint"] == "s"
        assert evt.payload["llm_calls"] == 0

    async def test_no_emit_without_event_log(self) -> None:
        reg = ExtractorRegistry()
        reg.register(_make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"]))
        d = ExtractionDispatcher(reg)
        # Should not raise.
        await d.dispatch({}, source_hint="s")

    async def test_no_event_on_no_match(
        self,
        event_log: SQLiteEventLog,
    ) -> None:
        reg = ExtractorRegistry()
        d = ExtractionDispatcher(reg, event_log=event_log)
        with pytest.raises(NoExtractorAvailableError):
            await d.dispatch({}, source_hint="s")
        assert event_log.count(event_type=EventType.EXTRACTION_DISPATCHED) == 0


class TestFallbackTelemetry:
    """EXTRACTOR_FALLBACK event emission (Gap 4.3)."""

    async def test_no_fallback_on_natural_priority(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"], entities=1))
        d = ExtractionDispatcher(reg, event_log=event_log)
        await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(allow_llm_fallback=True),
        )
        # det was at natural priority tier → no fallback
        assert event_log.count(event_type=EventType.EXTRACTOR_FALLBACK) == 0

    async def test_prefer_tier_override_emits_fallback(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"], entities=1))
        d = ExtractionDispatcher(reg, event_log=event_log)
        await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(
                allow_llm_fallback=True,
                prefer_tier=ExtractorTier.LLM,
            ),
        )
        events = event_log.get_events(event_type=EventType.EXTRACTOR_FALLBACK, limit=10)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["reason"] == "prefer_tier_override"
        assert payload["chosen_tier"] == "llm"
        assert payload["skipped_tier"] == "deterministic"
        assert payload["chosen_extractor"] == "llm"
        assert payload["source_hint"] == "s"

    async def test_empty_result_emits_fallback(self, event_log: SQLiteEventLog) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=0)
        )
        d = ExtractionDispatcher(reg, event_log=event_log)
        await d.dispatch({}, source_hint="s")
        events = event_log.get_events(event_type=EventType.EXTRACTOR_FALLBACK, limit=10)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["reason"] == "empty_result"
        assert payload["chosen_tier"] == "deterministic"
        assert payload["skipped_tier"] is None

    async def test_no_event_log_swallows_fallback(self) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=0)
        )
        d = ExtractionDispatcher(reg)  # no event_log
        # Should not raise
        result = await d.dispatch({}, source_hint="s")
        assert result.extractor_used == "det"

    async def test_prefer_tier_and_empty_result_both_emit(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        reg.register(_make_extractor("llm", ExtractorTier.LLM, ["s"], entities=0))
        d = ExtractionDispatcher(reg, event_log=event_log)
        await d.dispatch(
            {},
            source_hint="s",
            context=ExtractionContext(
                allow_llm_fallback=True,
                prefer_tier=ExtractorTier.LLM,
            ),
        )
        events = event_log.get_events(event_type=EventType.EXTRACTOR_FALLBACK, limit=10)
        reasons = sorted(e.payload["reason"] for e in events)
        assert reasons == ["empty_result", "prefer_tier_override"]
