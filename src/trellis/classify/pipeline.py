"""ClassifierPipeline — unified classification for ingestion and enrichment."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import structlog

from trellis.classify.protocol import (
    BOTH_MODES,
    ClassificationContext,
    ClassificationResult,
    Classifier,
    MergedClassification,
)

if TYPE_CHECKING:
    from trellis.schemas.classification import ClassifierMode

logger = structlog.get_logger(__name__)


class ClassifierPipeline:
    """Runs classifiers in order, merges results.

    Two modes controlled by configuration:

    - **Ingestion** (``llm_classifier=None``): deterministic only.
    - **Enrichment** (``llm_classifier=<impl>``): deterministic + LLM.

    Classifiers that declare ``allowed_modes`` incompatible with the active
    pipeline mode are rejected at construction time.
    """

    def __init__(
        self,
        classifiers: list[Classifier],
        llm_classifier: Classifier | None = None,
        llm_threshold: float = 0.7,
    ) -> None:
        self._llm_classifier = llm_classifier
        self._llm_threshold = llm_threshold
        active = self.mode
        self._classifiers = self._validate_classifiers(classifiers, active)

    @property
    def mode(self) -> ClassifierMode:
        """Return ``'enrichment'`` if LLM is configured, else ``'ingestion'``."""
        return cast(
            "ClassifierMode",
            "enrichment" if self._llm_classifier else "ingestion",
        )

    @staticmethod
    def _validate_classifiers(
        classifiers: list[Classifier], active_mode: str
    ) -> list[Classifier]:
        """Reject classifiers whose ``allowed_modes`` excludes the active mode."""
        validated: list[Classifier] = []
        for c in classifiers:
            modes = getattr(c, "allowed_modes", BOTH_MODES)
            if active_mode not in modes:
                msg = (
                    f"Classifier {c.name!r} does not support mode "
                    f"{active_mode!r} (allowed: {sorted(modes)}). "
                    f"See adr-deferred-cognition.md for classifier-mode policy."
                )
                raise ValueError(msg)
            validated.append(c)
        return validated

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> MergedClassification:
        """Run all classifiers and merge results."""
        results = [c.classify(content, context=context) for c in self._classifiers]
        merged = self._merge(results)

        if self._llm_classifier and self._should_use_llm(merged):
            llm_result = self._llm_classifier.classify(content, context=context)
            merged = self._merge_llm(merged, llm_result)
            logger.debug(
                "llm_classifier_fired",
                classifier=self._llm_classifier.name,
                confidence=llm_result.confidence,
            )

        merged.mode = self.mode
        return merged

    def _merge(self, results: list[ClassificationResult]) -> MergedClassification:
        """Merge multiple classifier results. Higher confidence wins per facet."""
        tags: dict[str, list[str]] = {}
        confidence_per_facet: dict[str, float] = {}
        classified_by: list[str] = []

        for result in results:
            if not result.tags:
                continue
            classified_by.append(result.classifier_name)
            for facet, values in result.tags.items():
                existing_conf = confidence_per_facet.get(facet, 0.0)
                if result.confidence >= existing_conf:
                    tags[facet] = values
                    confidence_per_facet[facet] = result.confidence

        return MergedClassification(
            tags=tags,
            confidence_per_facet=confidence_per_facet,
            results=results,
            classified_by=classified_by,
        )

    def _merge_llm(
        self, merged: MergedClassification, llm_result: ClassificationResult
    ) -> MergedClassification:
        """Merge LLM result into existing merged classification.

        LLM can fill missing facets and override low-confidence facets.
        """
        new_tags = dict(merged.tags)
        new_conf = dict(merged.confidence_per_facet)
        new_classified_by = list(merged.classified_by)

        if llm_result.classifier_name not in new_classified_by:
            new_classified_by.append(llm_result.classifier_name)

        for facet, values in llm_result.tags.items():
            existing_conf = new_conf.get(facet, 0.0)
            if llm_result.confidence >= existing_conf:
                new_tags[facet] = values
                new_conf[facet] = llm_result.confidence

        return MergedClassification(
            tags=new_tags,
            confidence_per_facet=new_conf,
            results=[*merged.results, llm_result],
            classified_by=new_classified_by,
        )

    def _should_use_llm(self, merged: MergedClassification) -> bool:
        """LLM fires when confidence is below threshold or review flagged."""
        if any(r.needs_llm_review for r in merged.results):
            return True
        return merged.min_confidence < self._llm_threshold
