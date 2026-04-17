"""SourceSystemClassifier — maps source system and file paths to tags."""

from __future__ import annotations

from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
)

_SOURCE_DOMAIN_MAP: dict[str, list[str]] = {
    "dbt": ["data-pipeline"],
    "unity_catalog": ["data-pipeline"],
    "git": [],
    "obsidian": ["documentation"],
    "openlineage": ["data-pipeline", "observability"],
}


_SOURCE_AFFINITY_MAP: dict[str, list[str]] = {
    "unity_catalog": ["reference"],
    "dbt": ["technical_pattern", "reference"],
}


def _infer_affinity(context: ClassificationContext) -> list[str]:
    """Infer retrieval affinity from source system, file path, and entity type."""
    affinity = list(_SOURCE_AFFINITY_MAP.get(context.source_system, []))

    if not affinity and context.source_system == "git" and context.file_path:
        if context.file_path.endswith(".md"):
            affinity = ["domain_knowledge"]
        elif context.file_path.endswith((".py", ".sql")):
            affinity = ["technical_pattern"]

    if (
        context.entity_type
        and "trace" in context.entity_type
        and "operational" not in affinity
    ):
        affinity.append("operational")

    return affinity


class SourceSystemClassifier:
    """Classify based on source system and file path context."""

    @property
    def name(self) -> str:
        return "source_system"

    @property
    def allowed_modes(self) -> frozenset[str]:
        from trellis.classify.protocol import BOTH_MODES  # noqa: PLC0415

        return BOTH_MODES

    def classify(
        self,
        content: str,  # noqa: ARG002
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        if not context:
            return ClassificationResult(
                tags={},
                confidence=0.3,
                classifier_name=self.name,
                needs_llm_review=True,
            )

        tags: dict[str, list[str]] = {}
        has_signal = False

        # Source system -> domain
        if context.source_system:
            domains = _SOURCE_DOMAIN_MAP.get(context.source_system, [])
            if domains:
                tags["domain"] = list(domains)
                has_signal = True

        # File path -> content_type and domain
        if context.file_path:
            fp = context.file_path
            if "/tests/" in fp or fp.startswith("test_") or "/test_" in fp:
                tags.setdefault("domain", [])
                if "testing" not in tags["domain"]:
                    tags["domain"].append("testing")
                tags["content_type"] = ["code"]
                has_signal = True
            if "/docs/" in fp or fp.endswith(".md"):
                tags["content_type"] = ["documentation"]
                has_signal = True

        # Retrieval affinity from source system, file path, entity type
        affinity = _infer_affinity(context)
        if affinity:
            tags["retrieval_affinity"] = affinity
            has_signal = True

        return ClassificationResult(
            tags=tags,
            confidence=0.9 if has_signal else 0.3,
            classifier_name=self.name,
            needs_llm_review=not has_signal,
        )
