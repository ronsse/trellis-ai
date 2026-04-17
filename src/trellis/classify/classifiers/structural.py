"""StructuralClassifier — classifies content by structural patterns."""

from __future__ import annotations

import re
from pathlib import Path

from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
)

_CODE_FENCE = re.compile(r"```\w+\n")
_CODE_DEF = re.compile(r"^(def |class |import |from )", re.MULTILINE)
_NUMBERED_STEPS = re.compile(r"^\d+\.\s", re.MULTILINE)
_ERROR_KEYWORDS = re.compile(r"(traceback|exception|error|stack trace)", re.IGNORECASE)
_FIX_KEYWORDS = re.compile(r"(fix|resolve|solution|root cause|fixed by)", re.IGNORECASE)
_CONFIG_EXTENSIONS = frozenset((".yaml", ".yml", ".toml", ".ini", ".env", ".json"))
_LOW_QUALITY_THRESHOLD = 50
_MIN_PROCEDURE_LINES = 3


class StructuralClassifier:
    """Classify by structural signals: code fences, lists, error patterns."""

    @property
    def name(self) -> str:
        return "structural"

    @property
    def allowed_modes(self) -> frozenset[str]:
        from trellis.classify.protocol import BOTH_MODES  # noqa: PLC0415

        return BOTH_MODES

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        tags: dict[str, list[str]] = {}
        stripped = content.strip()

        # Signal quality from length
        if len(stripped) < _LOW_QUALITY_THRESHOLD:
            tags["signal_quality"] = ["low"]

        # Error resolution: error keywords AND fix keywords
        if _ERROR_KEYWORDS.search(content) and _FIX_KEYWORDS.search(content):
            tags["content_type"] = ["error-resolution"]
            tags["retrieval_affinity"] = ["operational"]

        # Procedure: numbered steps with enough lines
        elif (
            _NUMBERED_STEPS.search(content)
            and content.count("\n") > _MIN_PROCEDURE_LINES
        ):
            tags["content_type"] = ["procedure"]
            tags["retrieval_affinity"] = ["technical_pattern"]

        elif _CODE_FENCE.search(content) or _CODE_DEF.search(content):
            tags["content_type"] = ["code"]
            tags["retrieval_affinity"] = ["technical_pattern", "reference"]

        # Configuration from file path
        if context and context.file_path:
            ext = Path(context.file_path).suffix.lower()
            if ext in _CONFIG_EXTENSIONS:
                tags["content_type"] = ["configuration"]
                tags["retrieval_affinity"] = ["reference"]

        return ClassificationResult(
            tags=tags,
            confidence=0.95 if tags else 0.3,
            classifier_name=self.name,
            needs_llm_review=not bool(tags),
        )
