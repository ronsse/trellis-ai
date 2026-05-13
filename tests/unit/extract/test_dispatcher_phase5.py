"""C2 Phase 5 — telemetry-failure tests for `trellis.extract.dispatcher`.

Pins the 3 GRACEFUL-DEGRADATION sites in
``src/trellis/extract/dispatcher.py``:

* L277 — ``_emit_fallback`` event_log.emit raises → dispatch still
  returns a result; failure logged via ``logger.exception``.
* L295 — validator raises inside ``_collect_findings`` → converted into
  a synthetic ``validator_error`` finding that forces EXTRACTION_REJECTED.
* L385 — ``_emit_extraction_rejected`` emit raises → rejected result
  still returned (empty entities/edges); failure logged.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.extract.base import ExtractorTier
from trellis.extract.context import ExtractionContext
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.extract.validators import ValidationFinding
from trellis.schemas.extraction import (
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.BoundLogger,
        processors=saved.get("processors", []),
    )
    try:
        with capture_logs() as cap:
            yield cap
    finally:
        structlog.configure(**saved)


def _events_with_key(cap: list[dict], event_key: str) -> list[dict]:
    return [e for e in cap if e.get("event") == event_key]


class _BoomEventLog:
    """EventLog whose ``emit`` always raises."""

    def emit(self, *_args, **_kwargs):
        msg = "event log down"
        raise RuntimeError(msg)


def _make_extractor(
    name: str,
    tier: ExtractorTier,
    sources: list[str],
    *,
    entities: int = 0,
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
            drafts = [
                EntityDraft(entity_type="stub", name=f"{name}-{i}")
                for i in range(entities)
            ]
            return ExtractionResult(
                entities=drafts,
                edges=[],
                extractor_used=self.name,
                tier=self.tier.value,
                provenance=ExtractionProvenance(
                    extractor_name=self.name,
                    source_hint=source_hint,
                ),
            )

    return _E()


class TestEmitFallbackFailureGraceful:
    """L277 — EXTRACTOR_FALLBACK emit failure must not derail dispatch.

    Invokes ``_emit_fallback`` directly to isolate the L277 except site:
    other emit calls (e.g. EXTRACTION_DISPATCHED at L257) are out of
    scope for this phase and would otherwise mask the test.
    """

    def test_emit_fallback_swallows_event_log_failure(
        self,
        log_output: list[dict],
    ) -> None:
        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        d = ExtractionDispatcher(reg, event_log=_BoomEventLog())

        # Primary op: helper returns cleanly despite the boom.
        d._emit_fallback(
            source_hint="s",
            chosen_extractor="det",
            chosen_tier="deterministic",
            skipped_tier="hybrid",
            reason="prefer_tier_override",
        )

        events = _events_with_key(log_output, "extractor_fallback_emit_failed")
        assert events, log_output
        assert events[0].get("log_level") == "error"


class TestValidatorRaisesGraceful:
    """L295 — buggy validator → synthetic ``validator_error`` finding."""

    async def test_validator_exception_becomes_finding_and_logs(
        self,
        log_output: list[dict],
    ) -> None:
        class _BrokenValidator:
            name = "broken"

            def validate(self, result, *, source_hint):
                msg = "validator exploded"
                raise RuntimeError(msg)

        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        d = ExtractionDispatcher(reg, validators=[_BrokenValidator()])

        # Primary op: dispatch returns a rejected (empty) result. The
        # buggy validator is converted into a finding rather than
        # silently passing the drafts through.
        result = await d.dispatch({}, source_hint="s")
        assert result.entities == []
        assert result.edges == []
        # The rejection residue carries the synthetic finding.
        assert "rejected_by_validators" in (result.unparsed_residue or {})
        findings = result.unparsed_residue["rejected_by_validators"]["findings"]
        assert any(f.get("code") == "validator_error" for f in findings)

        events = _events_with_key(log_output, "validator_raised")
        assert events, log_output
        assert events[0].get("log_level") == "error"


class TestEmitExtractionRejectedFailureGraceful:
    """L385 — EXTRACTION_REJECTED emit failure must not undo rejection."""

    async def test_rejection_returned_despite_emit_failure(
        self,
        log_output: list[dict],
    ) -> None:
        class _AlwaysRejectValidator:
            name = "reject"

            def validate(self, result, *, source_hint):
                return [
                    ValidationFinding(
                        validator_name="reject",
                        code="forced",
                        message="always rejects",
                    )
                ]

        reg = ExtractorRegistry()
        reg.register(
            _make_extractor("det", ExtractorTier.DETERMINISTIC, ["s"], entities=1)
        )
        d = ExtractionDispatcher(
            reg,
            event_log=_BoomEventLog(),
            validators=[_AlwaysRejectValidator()],
        )

        # Primary op: rejection still happens (empty result returned to
        # the caller) even though the EXTRACTION_REJECTED emit raised.
        result = await d.dispatch({}, source_hint="s")
        assert result.entities == []
        assert result.edges == []

        events = _events_with_key(log_output, "extraction_rejected_emit_failed")
        assert events, log_output
        assert events[0].get("log_level") == "error"
