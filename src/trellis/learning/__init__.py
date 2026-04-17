"""Learning module for Trellis — intent-family scoring and promotion."""

from trellis.learning.scoring import (
    analyze_learning_observations,
    build_learning_promotion_payloads,
    normalize_intent_family,
    prepare_learning_promotions,
    write_learning_review_artifacts,
)

__all__ = [
    "analyze_learning_observations",
    "build_learning_promotion_payloads",
    "normalize_intent_family",
    "prepare_learning_promotions",
    "write_learning_review_artifacts",
]
