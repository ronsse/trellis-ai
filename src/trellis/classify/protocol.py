"""Classifier protocol and data types for the classification pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from trellis.schemas.classification import ContentTags, RetrievalAffinity

if TYPE_CHECKING:
    from trellis.schemas.classification import ContentType, Scope, SignalQuality


@dataclass
class ClassificationContext:
    """Contextual hints available to classifiers."""

    title: str = ""
    source_system: str = ""
    file_path: str = ""
    entity_type: str = ""
    node_id: str = ""
    existing_tags: ContentTags | None = None
    existing_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassificationResult:
    """Output of a single classifier."""

    tags: dict[str, list[str]] = field(default_factory=dict)
    confidence: float = 1.0
    classifier_name: str = ""
    needs_llm_review: bool = False


@dataclass
class MergedClassification:
    """Merged output from multiple classifiers."""

    tags: dict[str, list[str]] = field(default_factory=dict)
    confidence_per_facet: dict[str, float] = field(default_factory=dict)
    results: list[ClassificationResult] = field(default_factory=list)
    classified_by: list[str] = field(default_factory=list)

    @property
    def min_confidence(self) -> float:
        """Minimum confidence across all facets."""
        if self.confidence_per_facet:
            return min(self.confidence_per_facet.values())
        return 1.0

    def to_content_tags(self) -> ContentTags:
        """Convert merged classification into a ContentTags schema object."""
        domain = self.tags.get("domain", [])
        content_type_values = self.tags.get("content_type", [])
        scope_values = self.tags.get("scope", [])
        signal_quality_values = self.tags.get("signal_quality", [])
        retrieval_affinity_values = self.tags.get("retrieval_affinity", [])

        return ContentTags(
            domain=domain,
            content_type=cast("ContentType", content_type_values[0])
            if content_type_values
            else None,
            scope=cast("Scope", scope_values[0]) if scope_values else None,
            signal_quality=cast("SignalQuality", signal_quality_values[0])
            if signal_quality_values
            else "standard",
            retrieval_affinity=[
                cast("RetrievalAffinity", v) for v in retrieval_affinity_values
            ],
            classified_by=self.classified_by,
        )


# Canonical mode sets — import these instead of redefining per-classifier.
BOTH_MODES: frozenset[str] = frozenset({"ingestion", "enrichment"})
ENRICHMENT_ONLY: frozenset[str] = frozenset({"enrichment"})


@runtime_checkable
class Classifier(Protocol):
    """Protocol for a single classifier in the pipeline."""

    @property
    def name(self) -> str:
        """Classifier name for audit trail."""
        ...

    @property
    def allowed_modes(self) -> frozenset[str]:
        """Modes in which this classifier may run.

        Returns a frozenset containing ``"ingestion"``, ``"enrichment"``, or
        both.  The ``ClassifierPipeline`` rejects classifiers whose
        ``allowed_modes`` do not include the pipeline's active mode.

        Defaults to ``frozenset({"ingestion", "enrichment"})`` so existing
        classifiers that do not override this property work in both modes.
        """
        ...

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        """Classify content and return tagged result."""
        ...
