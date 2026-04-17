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
    from trellis_workers.enrichment.service import EnrichmentService

logger = structlog.get_logger(__name__)


class LLMFacetClassifier:
    """Wraps :class:`EnrichmentService` to produce faceted classification.

    Maps the existing enrichment output into the faceted tag format:

    - ``auto_tags`` → ``domain`` facet
    - ``auto_class`` → ``content_type`` facet
    - ``auto_importance`` / ``auto_summary`` → preserved as ``_auto_*`` keys

    **Enrichment-only** — LLM calls are non-deterministic, unbounded cost,
    and non-reproducible on replay.  Never used in ingestion mode.
    """

    def __init__(self, enrichment_service: EnrichmentService) -> None:
        self._enrichment = enrichment_service

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
