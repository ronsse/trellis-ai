"""LLMFacetClassifier — wraps EnrichmentService for faceted tag output."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog
    from trellis_workers.enrichment.service import EnrichmentService

logger = structlog.get_logger(__name__)

#: Sentinel ``degraded_to`` value emitted in ``CLASSIFICATION_DEGRADED``
#: events when this classifier falls back to ``needs_llm_review=True``.
#: Stable string so analyzers can group / filter by degradation mode
#: without inspecting code.
_DEGRADED_TO_NEEDS_REVIEW = "needs_llm_review"

#: Slug recorded in ``payload.upstream_failure_kind`` when the only
#: signal available is ``EnrichmentResult.success=False`` with no
#: further structure. Generic on purpose: callers who emit their own
#: ``EXTRACTION_FAILED`` event upstream still join via timestamp +
#: ``subject_entity_id``, so we don't need to mirror their failure_kind
#: here. Documented on :attr:`EventType.CLASSIFICATION_DEGRADED`.
_UPSTREAM_ENRICHMENT_FAILURE = "enrichment_failure"


class LLMFacetClassifier:
    """Wraps :class:`EnrichmentService` to produce faceted classification.

    Maps the existing enrichment output into the faceted tag format:

    - ``auto_tags`` → ``domain`` facet
    - ``auto_class`` → ``content_type`` facet
    - ``auto_importance`` / ``auto_summary`` → preserved as ``_auto_*`` keys

    **Enrichment-only** — LLM calls are non-deterministic, unbounded cost,
    and non-reproducible on replay.  Never used in ingestion mode.

    When the wrapped :class:`EnrichmentService` returns a
    ``success=False`` result, the classifier degrades to a
    ``needs_llm_review=True`` sentinel rather than raising. If an
    ``event_log`` is supplied, that degradation also emits a
    :attr:`~trellis.stores.base.event_log.EventType.CLASSIFICATION_DEGRADED`
    event so downstream analyzers can correlate the degradation with the
    upstream ``EXTRACTION_FAILED`` event ``EnrichmentService`` will emit
    once wired (timestamp + ``subject_entity_id`` join). The event_log
    parameter is optional — when ``None`` the emit is a no-op, matching
    the optional-event-log pattern used elsewhere in the codebase.
    """

    def __init__(
        self,
        enrichment_service: EnrichmentService,
        *,
        event_log: EventLog | None = None,
    ) -> None:
        self._enrichment = enrichment_service
        self._event_log = event_log

    @property
    def name(self) -> str:
        return "llm_facet"

    @property
    def allowed_modes(self) -> frozenset[str]:
        from trellis.classify.protocol import ENRICHMENT_ONLY  # noqa: PLC0415

        return ENRICHMENT_ONLY

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        """Synchronous wrapper — runs the async classify in an event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures  # noqa: PLC0415

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self.classify_async(content, context=context),
                )
                return future.result()
        return asyncio.run(self.classify_async(content, context=context))

    async def classify_async(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        """Async classification via EnrichmentService."""
        title = context.title if context else ""
        result = await self._enrichment.enrich(content, title=title)

        if not result.success:
            logger.warning("llm_classification_failed", error=result.error)
            self._emit_degraded(
                context=context,
                upstream_failure_kind=_UPSTREAM_ENRICHMENT_FAILURE,
            )
            return ClassificationResult(
                tags={},
                confidence=0.0,
                classifier_name=self.name,
                needs_llm_review=True,
            )

        tags: dict[str, Any] = {}

        if result.auto_tags:
            tags["domain"] = result.auto_tags

        if result.auto_class:
            tags["content_type"] = [result.auto_class]

        # Preserve importance and summary as special keys for downstream use
        if result.auto_importance:
            tags["_auto_importance"] = [result.auto_importance]
        if result.auto_summary:
            tags["_auto_summary"] = [result.auto_summary]

        confidence = min(result.tag_confidence, result.class_confidence)

        return ClassificationResult(
            tags=tags,
            confidence=confidence,
            classifier_name=self.name,
        )

    def _emit_degraded(
        self,
        *,
        context: ClassificationContext | None,
        upstream_failure_kind: str,
    ) -> None:
        """Emit ``CLASSIFICATION_DEGRADED`` when wired, else no-op.

        Mirrors the optional-event-log pattern: when ``self._event_log``
        is ``None`` the helper short-circuits, so callers never have to
        special-case "wired vs. unwired". A broken event log must not
        break the classifier's degradation contract — emission errors
        are logged and swallowed (the warning log + sentinel result is
        already the user-visible signal).
        """
        if self._event_log is None:
            return
        from trellis.stores.base.event_log import EventType  # noqa: PLC0415

        subject_entity_id = context.node_id if context else None
        payload: dict[str, Any] = {
            "classifier_id": self.name,
            "upstream_failure_kind": upstream_failure_kind,
            "subject_entity_id": subject_entity_id,
            "degraded_to": _DEGRADED_TO_NEEDS_REVIEW,
        }
        try:
            self._event_log.emit(
                EventType.CLASSIFICATION_DEGRADED,
                source=self.name,
                entity_id=subject_entity_id,
                payload=payload,
            )
        # GRACEFUL-DEGRADATION: a broken event log must not break the
        # classifier's degradation contract. The caller still gets the
        # sentinel ClassificationResult; the warning log already
        # captured the failure.
        except Exception:
            logger.exception(
                "classification_degraded_emit_failed",
                classifier_id=self.name,
                upstream_failure_kind=upstream_failure_kind,
            )
