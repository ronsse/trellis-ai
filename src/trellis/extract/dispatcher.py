"""ExtractionDispatcher — routes raw input to the right extractor.

Routing priority (when multiple candidates match):

1. If ``context.prefer_tier`` is set, only extractors at that tier are
   considered.
2. Otherwise, priority is DETERMINISTIC > HYBRID > LLM.
3. LLM-tier extractors are only eligible when ``context.allow_llm_fallback``
   is true (explicit opt-in gate).
4. Within a tier, the first candidate registered for the ``source_hint``
   wins.  ``source_hint=None`` considers all registered extractors at each
   tier.

On dispatch, an ``EXTRACTION_DISPATCHED`` event is emitted to the event log
(when one is configured) with cost / tier / confidence telemetry so
effectiveness analysis can reason about extractions without re-running
them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.extract.base import ExtractorTier, NoExtractorAvailableError
from trellis.extract.context import ExtractionContext
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.extract.base import Extractor
    from trellis.extract.registry import ExtractorRegistry
    from trellis.schemas.extraction import ExtractionResult
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_TIER_PRIORITY: list[ExtractorTier] = [
    ExtractorTier.DETERMINISTIC,
    ExtractorTier.HYBRID,
    ExtractorTier.LLM,
]


class ExtractionDispatcher:
    """Routes raw input to the right :class:`Extractor`.

    The dispatcher itself is stateless — it holds references to the
    registry and the (optional) event log but owns no mutable state.
    """

    def __init__(
        self,
        registry: ExtractorRegistry,
        *,
        event_log: EventLog | None = None,
    ) -> None:
        self._registry = registry
        self._event_log = event_log

    async def dispatch(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        """Pick an extractor, run it, emit telemetry, return the result.

        Raises :class:`NoExtractorAvailableError` when no registered extractor
        matches the hint under the given context (e.g. only LLM-tier
        candidates are registered but ``allow_llm_fallback=False``).
        """
        ctx = context or ExtractionContext()
        extractor = self._select(source_hint, ctx)
        if extractor is None:
            raise NoExtractorAvailableError(
                source_hint=source_hint,
                reason=self._no_match_reason(source_hint, ctx),
            )

        result = await extractor.extract(
            raw_input,
            source_hint=source_hint,
            context=ctx,
        )

        self._emit(result, source_hint=source_hint)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _select(
        self,
        source_hint: str | None,
        ctx: ExtractionContext,
    ) -> Extractor | None:
        candidates = self._registry.candidates_for(source_hint)
        if not candidates:
            return None

        # Explicit tier preference short-circuits the priority order.
        if ctx.prefer_tier is not None:
            if ctx.prefer_tier == ExtractorTier.LLM and not ctx.allow_llm_fallback:
                return None
            for candidate in candidates:
                if candidate.tier == ctx.prefer_tier:
                    return candidate
            return None

        # Normal priority-ordered selection.
        for tier in _TIER_PRIORITY:
            if tier == ExtractorTier.LLM and not ctx.allow_llm_fallback:
                continue
            for candidate in candidates:
                if candidate.tier == tier:
                    return candidate
        return None

    def _no_match_reason(
        self,
        source_hint: str | None,
        ctx: ExtractionContext,
    ) -> str:
        candidates = self._registry.candidates_for(source_hint)
        if not candidates:
            return "no registered extractors match"
        if ctx.prefer_tier is not None:
            available_tiers = sorted({c.tier.value for c in candidates})
            return (
                f"no extractor at preferred tier={ctx.prefer_tier.value} "
                f"(available: {available_tiers})"
            )
        # Only LLM candidates but LLM disabled.
        if all(c.tier == ExtractorTier.LLM for c in candidates) and not (
            ctx.allow_llm_fallback
        ):
            return "only LLM-tier extractors match; set allow_llm_fallback=True"
        return "no eligible extractor after applying context gates"

    def _emit(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None,
    ) -> None:
        if self._event_log is None:
            return
        self._event_log.emit(
            EventType.EXTRACTION_DISPATCHED,
            source="extraction_dispatcher",
            payload={
                "extractor_used": result.extractor_used,
                "tier": result.tier,
                "source_hint": source_hint,
                "entities": len(result.entities),
                "edges": len(result.edges),
                "llm_calls": result.llm_calls,
                "tokens_used": result.tokens_used,
                "overall_confidence": result.overall_confidence,
            },
        )
