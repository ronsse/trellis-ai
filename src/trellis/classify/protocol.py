"""Classifier protocol and data types for the classification pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from trellis.schemas.classification import ContentTags, RetrievalAffinity

if TYPE_CHECKING:
    from trellis.schemas.classification import (
        ClassifierMode,
        ContentType,
        Scope,
        SignalQuality,
    )


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
    #: Which pipeline mode produced this result — set by
    #: :class:`~trellis.classify.pipeline.ClassifierPipeline` on every call.
    #: ``None`` only on default-constructed instances in tests; production
    #: code always carries a value. Propagated to
    #: :attr:`ContentTags.classified_mode` via :meth:`to_content_tags`.
    mode: ClassifierMode | None = None

    @property
    def min_confidence(self) -> float:
        """Minimum confidence across all facets."""
        if self.confidence_per_facet:
            return min(self.confidence_per_facet.values())
        return 1.0

    def to_content_tags(self) -> ContentTags:
        """Convert merged classification into a ContentTags schema object.

        Facet keys the schema does not name are routed into
        :attr:`ContentTags.custom` rather than dropped. A classifier that emits
        a key nobody models is making a claim about the document; silently
        discarding it loses the claim *and* hides the mismatch, which is how
        the enrichment path went unnoticed while producing nothing usable.
        Keys prefixed with ``_`` are excluded — classifiers use those as
        out-of-band channels for scores and prose (``_auto_importance`` /
        ``_auto_summary``), which are not tags.
        """
        domain = self.tags.get("domain", [])
        content_type_values = self.tags.get("content_type", [])
        scope_values = self.tags.get("scope", [])
        signal_quality_values = self.tags.get("signal_quality", [])
        retrieval_affinity_values = self.tags.get("retrieval_affinity", [])
        custom = {
            key: [str(v) for v in values]
            for key, values in self.tags.items()
            if key not in _MODELLED_FACETS and not key.startswith("_")
        }

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
            custom=custom,
            classified_by=self.classified_by,
            classified_at=datetime.now(UTC),
            classified_mode=self.mode,
        )


#: Facet keys :meth:`MergedClassification.to_content_tags` maps onto typed
#: :class:`~trellis.schemas.classification.ContentTags` fields. Anything else a
#: classifier emits lands in ``custom``.
_MODELLED_FACETS: frozenset[str] = frozenset(
    {"domain", "content_type", "scope", "signal_quality", "retrieval_affinity"}
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
