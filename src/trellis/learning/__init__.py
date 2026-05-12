"""Learning module for Trellis — intent-family scoring and promotion."""

from trellis.learning.observations import build_learning_observations_from_event_log
from trellis.learning.schema_evolution import (
    RECOMMENDED_SEED_VALUES,
    WellKnownCandidate,
    analyze_well_known_candidates,
)
from trellis.learning.schema_evolution import (
    REQUIRED_PARAM_KEYS as REQUIRED_SCHEMA_EVOLUTION_PARAM_KEYS,
)
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
    "RECOMMENDED_SEED_VALUES",
    "REQUIRED_SCHEMA_EVOLUTION_PARAM_KEYS",
    "WellKnownCandidate",
    "analyze_learning_observations",
    "analyze_well_known_candidates",
    "build_learning_observations_from_event_log",
    "build_learning_promotion_payloads",
    "normalize_intent_family",
    "prepare_learning_promotions",
    "write_learning_review_artifacts",
]
