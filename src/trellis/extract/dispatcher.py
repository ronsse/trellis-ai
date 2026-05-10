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
from trellis.extract.validators import ValidationFinding
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.extract.base import Extractor
    from trellis.extract.registry import ExtractorRegistry
    from trellis.extract.validators import ExtractionValidator
    from trellis.schemas.extraction import ExtractionResult
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_TIER_PRIORITY: list[ExtractorTier] = [
    ExtractorTier.DETERMINISTIC,
    ExtractorTier.HYBRID,
    ExtractorTier.LLM,
]

#: Reasons surfaced on ``EXTRACTOR_FALLBACK`` events. Kept as string
#: constants rather than an enum to match the open-string convention used
#: by rejection-reason fields elsewhere (pack rejected items, etc.).
FALLBACK_REASON_PREFER_TIER = "prefer_tier_override"
FALLBACK_REASON_EMPTY_RESULT = "empty_result"


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
        validators: list[ExtractionValidator] | None = None,
    ) -> None:
        self._registry = registry
        self._event_log = event_log
        # Validators run AFTER extraction but BEFORE the result is returned.
        # See adr-extraction-validation.md §5.3 for the enforcement contract:
        # any finding causes the dispatcher to quarantine entities/edges into
        # ``unparsed_residue["rejected_by_validators"]`` and emit
        # ``EXTRACTION_REJECTED`` — no Commands flow downstream.
        self._validators: list[ExtractionValidator] = list(validators or [])

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
        extractor, natural_tier = self._select(source_hint, ctx)
        if extractor is None:
            raise NoExtractorAvailableError(
                source_hint=source_hint,
                reason=self._no_match_reason(source_hint, ctx),
            )

        # Fallback signal #1: ``prefer_tier`` forced a selection below the
        # natural priority order. Fire before running so we also capture
        # cases where the chosen extractor raises.
        if natural_tier is not None and extractor.tier != natural_tier:
            self._emit_fallback(
                source_hint=source_hint,
                chosen_extractor=extractor.name,
                chosen_tier=extractor.tier.value,
                skipped_tier=natural_tier.value,
                reason=FALLBACK_REASON_PREFER_TIER,
            )

        result = await extractor.extract(
            raw_input,
            source_hint=source_hint,
            context=ctx,
        )

        # Fallback signal #2: the chosen extractor produced no drafts.
        # Emitted BEFORE the validator pass because this is the
        # graduation-tracking lens ("deterministic silently failed for
        # this source_hint — consider promoting to a higher tier") and
        # must fire even when the validator pass also rejects. Two events
        # for one shape: EXTRACTOR_FALLBACK serves analyze_extractor_fallbacks
        # (tier-promotion decisions); EXTRACTION_REJECTED serves
        # analyze_extraction_validation (corpus-quality decisions). Per
        # adr-extraction-validation.md §6.2, both consumers want the
        # data in their own framing.
        if not result.entities and not result.edges:
            self._emit_fallback(
                source_hint=source_hint,
                chosen_extractor=extractor.name,
                chosen_tier=extractor.tier.value,
                skipped_tier=None,
                reason=FALLBACK_REASON_EMPTY_RESULT,
            )

        # Validation pass — enforcement, not signal-only. When any validator
        # returns findings the dispatcher quarantines the original drafts
        # into ``unparsed_residue["rejected_by_validators"]``, emits
        # EXTRACTION_REJECTED, and returns an empty result so no Commands
        # flow downstream. EXTRACTION_DISPATCHED is deliberately skipped —
        # rejection is the canonical record. EXTRACTOR_FALLBACK still fires
        # above (graduation lens; different consumer).
        # See adr-extraction-validation.md §5.3 + §6.2.
        if self._validators:
            findings = self._collect_findings(result, source_hint=source_hint)
            if findings:
                return self._reject_extraction(
                    result,
                    findings=findings,
                    source_hint=source_hint,
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
    ) -> tuple[Extractor | None, ExtractorTier | None]:
        """Pick an extractor and report the natural-priority tier.

        Returns ``(chosen_extractor, natural_tier)``. ``natural_tier`` is the
        tier that *would* have been selected under default priority ordering
        with the current ``allow_llm_fallback`` gate — i.e. the highest-
        priority tier among candidates, ignoring ``prefer_tier``. Callers
        compare ``chosen_extractor.tier`` against ``natural_tier`` to detect
        a ``prefer_tier`` override for fallback telemetry.
        """
        candidates = self._registry.candidates_for(source_hint)
        if not candidates:
            return None, None

        natural_tier = self._natural_tier(candidates, ctx)

        # Explicit tier preference short-circuits the priority order.
        if ctx.prefer_tier is not None:
            if ctx.prefer_tier == ExtractorTier.LLM and not ctx.allow_llm_fallback:
                return None, natural_tier
            for candidate in candidates:
                if candidate.tier == ctx.prefer_tier:
                    return candidate, natural_tier
            return None, natural_tier

        # Normal priority-ordered selection.
        for tier in _TIER_PRIORITY:
            if tier == ExtractorTier.LLM and not ctx.allow_llm_fallback:
                continue
            for candidate in candidates:
                if candidate.tier == tier:
                    return candidate, natural_tier
        return None, natural_tier

    @staticmethod
    def _natural_tier(
        candidates: list[Extractor],
        ctx: ExtractionContext,
    ) -> ExtractorTier | None:
        """Highest-priority tier among candidates respecting the LLM gate."""
        for tier in _TIER_PRIORITY:
            if tier == ExtractorTier.LLM and not ctx.allow_llm_fallback:
                continue
            if any(c.tier == tier for c in candidates):
                return tier
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

    def _emit_fallback(
        self,
        *,
        source_hint: str | None,
        chosen_extractor: str,
        chosen_tier: str,
        skipped_tier: str | None,
        reason: str,
    ) -> None:
        """Fire EXTRACTOR_FALLBACK with a small payload. Fail-soft."""
        if self._event_log is None:
            return
        try:
            self._event_log.emit(
                EventType.EXTRACTOR_FALLBACK,
                source="extraction_dispatcher",
                payload={
                    "source_hint": source_hint,
                    "chosen_extractor": chosen_extractor,
                    "chosen_tier": chosen_tier,
                    "skipped_tier": skipped_tier,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception(
                "extractor_fallback_emit_failed",
                source_hint=source_hint,
                reason=reason,
            )

    def _collect_findings(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None,
    ) -> list[ValidationFinding]:
        """Run every wired validator and gather findings."""
        all_findings: list[ValidationFinding] = []
        for validator in self._validators:
            try:
                findings = validator.validate(result, source_hint=source_hint)
            except Exception:
                # Validators must be cheap and total. A buggy validator
                # should not silently corrupt the dispatch — log and treat
                # as a finding so the operator notices.
                logger.exception(
                    "validator_raised",
                    validator=getattr(validator, "name", validator.__class__.__name__),
                    source_hint=source_hint,
                )
                all_findings.append(
                    ValidationFinding(
                        validator_name=getattr(
                            validator, "name", validator.__class__.__name__
                        ),
                        code="validator_error",
                        message="validator raised; treating as a rejection",
                    )
                )
                continue
            all_findings.extend(findings)
        return all_findings

    def _reject_extraction(
        self,
        result: ExtractionResult,
        *,
        findings: list[ValidationFinding],
        source_hint: str | None,
    ) -> ExtractionResult:
        """Quarantine the rejected drafts + emit EXTRACTION_REJECTED.

        Returns a fresh :class:`ExtractionResult` whose ``entities`` and
        ``edges`` are empty so ``result_to_batch()`` produces no Commands.
        The original drafts are preserved under
        ``unparsed_residue["rejected_by_validators"]`` for forensics.
        """
        from trellis.schemas.extraction import ExtractionResult  # noqa: PLC0415

        existing_residue = result.unparsed_residue
        residue_base: dict[str, Any] = (
            dict(existing_residue) if isinstance(existing_residue, dict) else {}
        )
        if not isinstance(existing_residue, dict) and existing_residue is not None:
            # Preserve any non-dict residue under a stable key so we never
            # lose signal — even if the extractor's residue shape is
            # opaque to us.
            residue_base["original_residue"] = existing_residue
        residue_base["rejected_by_validators"] = {
            "entities": [draft.model_dump(mode="json") for draft in result.entities],
            "edges": [draft.model_dump(mode="json") for draft in result.edges],
            "findings": [finding.__dict__ for finding in findings],
        }

        self._emit_extraction_rejected(
            source_hint=source_hint,
            extractor_used=result.extractor_used,
            findings=findings,
        )
        return ExtractionResult(
            entities=[],
            edges=[],
            extractor_used=result.extractor_used,
            tier=result.tier,
            llm_calls=result.llm_calls,
            tokens_used=result.tokens_used,
            overall_confidence=result.overall_confidence,
            provenance=result.provenance,
            unparsed_residue=residue_base,
        )

    def _emit_extraction_rejected(
        self,
        *,
        source_hint: str | None,
        extractor_used: str,
        findings: list[ValidationFinding],
    ) -> None:
        """Fire EXTRACTION_REJECTED with the validator findings. Fail-soft."""
        if self._event_log is None:
            return
        try:
            self._event_log.emit(
                EventType.EXTRACTION_REJECTED,
                source="extraction_dispatcher",
                payload={
                    "source_hint": source_hint,
                    "extractor_used": extractor_used,
                    "findings": [finding.__dict__ for finding in findings],
                },
            )
        except Exception:
            logger.exception(
                "extraction_rejected_emit_failed",
                source_hint=source_hint,
                extractor_used=extractor_used,
            )
