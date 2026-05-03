"""Learning module for Trellis — intent-family scoring and promotion."""

from trellis.learning.observations import build_learning_observations_from_event_log
from trellis.learning.scoring import (
    PROMOTE_RECOMMENDATIONS,
    analyze_learning_observations,
    build_learning_promotion_payloads,
    normalize_intent_family,
    prepare_learning_promotions,
    write_learning_review_artifacts,
)

__all__ = [
    "PROMOTE_RECOMMENDATIONS",
    "analyze_learning_observations",
    "build_learning_observations_from_event_log",
    "build_learning_promotion_payloads",
    "normalize_intent_family",
    "prepare_learning_promotions",
    "write_learning_review_artifacts",
]
