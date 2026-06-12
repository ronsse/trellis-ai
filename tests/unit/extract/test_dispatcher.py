"""Tests for ExtractionDispatcher routing + telemetry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.extract.base import ExtractorTier, NoExtractorAvailableError
from trellis.extract.context import ExtractionContext
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.extract.validators import (
    DraftLocalReferenceValidator,
    EmptyResultValidator,
    OrphanProvenanceValidator,
    ValidationFinding,
    default_validators,
)
from trellis.schemas.entity import GenerationSpec
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


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


def _custom_extractor(
    name: str,
    tier: ExtractorTier,
    sources: list[str],
    *,
    entities: list[EntityDraft] | None = None,
    edges: list[EdgeDraft] | None = None,
    residue: Any | None = None,
) -> Any:
    """Test extractor that emits a fully-controlled result payload."""

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
                entities=list(entities or []),
                edges=list(edges or []),
                extractor_used=self.name,
                tier=self.tier.value,
                provenance=ExtractionProvenance(
                    extractor_name=self.name,
                    source_hint=source_hint,
                ),
                unparsed_residue=residue,
            )

    return _E()


class TestExtractionValidatorEnforcement:
    """ADR §5.3 — when validators fire, the dispatcher rejects the extraction:
    quarantine drafts in residue, emit EXTRACTION_REJECTED, return empty."""

    async def test_no_validators_no_change(self, event_log: SQLiteEventLog) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        d = ExtractionDispatcher(reg, event_log=event_log)
        result = await d.dispatch({}, source_hint="s")
        assert len(result.entities) == 1
        assert event_log.count(event_type=EventType.EXTRACTION_REJECTED) == 0
        # EXTRACTION_DISPATCHED still fires on the happy path.
        assert event_log.count(event_type=EventType.EXTRACTION_DISPATCHED) == 1

    async def test_findings_quarantine_drafts_in_residue(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        # Extractor emits an entity AND an orphan edge — the orphan-edge
        # validator will fire.
        reg.register(
            _custom_extractor(
                "det",
                ExtractorTier.DETERMINISTIC,
                ["s"],
                entities=[EntityDraft(entity_id="ent_a", entity_type="x", name="a")],
                edges=[
                    EdgeDraft(
                        source_id="ent_a",
                        target_id="ent_missing",
                        edge_kind="related_to",
                    )
                ],
            )
        )
        d = ExtractionDispatcher(
            reg,
            event_log=event_log,
            validators=[DraftLocalReferenceValidator()],
        )
        result = await d.dispatch({}, source_hint="s")

        # Empty drafts — no Commands flow downstream.
        assert result.entities == []
        assert result.edges == []
        # Original signal is preserved in residue under the named key.
        assert isinstance(result.unparsed_residue, dict)
        rejected = result.unparsed_residue["rejected_by_validators"]
        assert len(rejected["entities"]) == 1
        assert len(rejected["edges"]) == 1
        assert rejected["entities"][0]["entity_id"] == "ent_a"
        assert rejected["edges"][0]["target_id"] == "ent_missing"
        assert len(rejected["findings"]) >= 1
        assert any(f["code"] == "orphan_edge" for f in rejected["findings"])

    async def test_emits_extraction_rejected_event(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _custom_extractor(
                "det",
                ExtractorTier.DETERMINISTIC,
                ["s"],
                entities=[],
                edges=[],
            )
        )
        d = ExtractionDispatcher(
            reg,
            event_log=event_log,
            validators=[EmptyResultValidator()],
        )
        await d.dispatch({}, source_hint="s")

        events = event_log.get_events(
            event_type=EventType.EXTRACTION_REJECTED, limit=10
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.source == "extraction_dispatcher"
        assert evt.payload["source_hint"] == "s"
        assert evt.payload["extractor_used"] == "det"
        codes = [f["code"] for f in evt.payload["findings"]]
        assert "empty_result" in codes
        # EXTRACTION_DISPATCHED is suppressed — rejection is the canonical event.
        assert event_log.count(event_type=EventType.EXTRACTION_DISPATCHED) == 0
        # EXTRACTOR_FALLBACK still fires for the empty-result case — different
        # consumer (graduation tracking via analyze_extractor_fallbacks) than
        # EXTRACTION_REJECTED (validation tracking). Both lenses want the
        # data; per adr-extraction-validation.md §6.2.
        fallback_events = event_log.get_events(
            event_type=EventType.EXTRACTOR_FALLBACK, limit=10
        )
        assert len(fallback_events) == 1
        assert fallback_events[0].payload["reason"] == "empty_result"

    async def test_curated_without_provenance_rejected(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _custom_extractor(
                "det",
                ExtractorTier.DETERMINISTIC,
                ["s"],
                entities=[
                    EntityDraft(
                        entity_id="cur_1",
                        entity_type="precedent",
                        name="bad",
                        node_role=NodeRole.CURATED,
                    )
                ],
            )
        )
        d = ExtractionDispatcher(
            reg,
            event_log=event_log,
            validators=[OrphanProvenanceValidator()],
        )
        result = await d.dispatch({}, source_hint="s")
        assert result.entities == []
        events = event_log.get_events(
            event_type=EventType.EXTRACTION_REJECTED, limit=10
        )
        assert len(events) == 1
        codes = [f["code"] for f in events[0].payload["findings"]]
        assert "missing_generation_spec" in codes

    async def test_curated_with_provenance_passes(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _custom_extractor(
                "det",
                ExtractorTier.DETERMINISTIC,
                ["s"],
                entities=[
                    EntityDraft(
                        entity_id="cur_1",
                        entity_type="precedent",
                        name="ok",
                        node_role=NodeRole.CURATED,
                        generation_spec=GenerationSpec(
                            generator_name="rollup",
                            generator_version="1.0.0",
                        ),
                    )
                ],
            )
        )
        d = ExtractionDispatcher(
            reg,
            event_log=event_log,
            validators=default_validators(),
        )
        result = await d.dispatch({}, source_hint="s")
        assert len(result.entities) == 1
        assert event_log.count(event_type=EventType.EXTRACTION_REJECTED) == 0

    async def test_preserves_existing_residue_on_rejection(
        self, event_log: SQLiteEventLog
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _custom_extractor(
                "det",
                ExtractorTier.DETERMINISTIC,
                ["s"],
                entities=[],
                edges=[],
                residue={"prior_signal": "carried-through"},
            )
        )
        d = ExtractionDispatcher(
            reg,
            event_log=event_log,
            validators=[EmptyResultValidator()],
        )
        result = await d.dispatch({}, source_hint="s")
        assert isinstance(result.unparsed_residue, dict)
        assert result.unparsed_residue["prior_signal"] == "carried-through"
        assert "rejected_by_validators" in result.unparsed_residue

    async def test_validator_exception_treated_as_rejection(
        self, event_log: SQLiteEventLog
    ) -> None:
        class _Boom:
            name = "boom"

            def validate(
                self,
                result: ExtractionResult,
                *,
                source_hint: str | None = None,
            ) -> list[ValidationFinding]:
                msg = "boom"
                raise RuntimeError(msg)

        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        d = ExtractionDispatcher(reg, event_log=event_log, validators=[_Boom()])
        result = await d.dispatch({}, source_hint="s")
        assert result.entities == []
        events = event_log.get_events(
            event_type=EventType.EXTRACTION_REJECTED, limit=10
        )
        assert len(events) == 1
        codes = [f["code"] for f in events[0].payload["findings"]]
        assert "validator_error" in codes
