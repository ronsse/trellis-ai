"""Define a domain-specific classifier that tags content at ingest time.

STATUS: PREVIEW — examples are in flux while parallel work lands. Expect
breaking changes before the next minor release.

Classifiers conform to the Classifier Protocol (`trellis.classify.protocol`)
— any object with `name`, `allowed_modes`, and `classify()` qualifies. The
ClassifierPipeline merges results from all registered classifiers into the
ContentTags attached to each item.

This example tags items as belonging to specific *teams* by scanning for
team owner-codes in the content. Drop in real patterns: PII detection,
PCI-scope flagging, language detection, sensitivity tiers, etc.

Run:
    python examples/custom_classifier.py
"""

from __future__ import annotations

import re

from trellis.classify.protocol import (
    BOTH_MODES,
    ClassificationContext,
    ClassificationResult,
)


# Map team codes -> domain tags. In a real system, this might come from a
# config file or an internal directory service.
_TEAM_PATTERNS: dict[str, str] = {
    "TEAM-PAY": "payments",
    "TEAM-ORD": "orders",
    "TEAM-INF": "infrastructure",
}

_TOKEN_RE = re.compile(r"\b(TEAM-[A-Z]{3})\b")


class TeamOwnershipClassifier:
    """Tags content with the domain of the owning team, based on TEAM-XXX
    tokens that show up in the content (e.g. in commit messages, PR
    descriptions, runbook headers).
    """

    @property
    def name(self) -> str:
        return "team-ownership"

    @property
    def allowed_modes(self) -> frozenset[str]:
        # Cheap regex scan — safe to run inline at ingestion time.
        return BOTH_MODES

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        matches = _TOKEN_RE.findall(content or "")
        domains = sorted({_TEAM_PATTERNS[m] for m in matches if m in _TEAM_PATTERNS})

        if not domains:
            # No signal — return empty tags with low confidence so other
            # classifiers (or LLM fallback) can take over.
            return ClassificationResult(
                tags={}, confidence=0.0, classifier_name=self.name
            )

        return ClassificationResult(
            tags={"domain": domains},
            confidence=0.95,
            classifier_name=self.name,
        )


def main() -> None:
    classifier = TeamOwnershipClassifier()

    samples = [
        "Fix retry budget in payments client. Owner: TEAM-PAY.",
        "Refactor checkout flow — coordinate with TEAM-ORD and TEAM-PAY.",
        "Random unrelated note with no owner mentioned.",
    ]

    for s in samples:
        result = classifier.classify(s)
        print(f"text:       {s}")
        print(f"  tags:     {result.tags}")
        print(f"  conf:     {result.confidence}")
        print()

    # To wire this into ingestion, append it to the classifier list passed
    # to ClassifierPipeline. See src/trellis/classify/pipeline.py and
    # docs/agent-guide/operations.md for integration details.


if __name__ == "__main__":
    main()
