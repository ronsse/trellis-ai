"""Learning module for Trellis — intent-family scoring and promotion."""

from trellis.learning.pack_observations import (
    build_learning_observations_from_event_log,
)
from trellis.learning.schema_evolution import (
    PARAM_COMPONENT_ID as SCHEMA_EVOLUTION_PARAM_COMPONENT_ID,
)
from trellis.learning.schema_evolution import (
    RECOMMENDED_SEED_VALUES,
    WellKnownCandidate,
    analyze_well_known_candidates,
)
from trellis.learning.schema_evolution import (
    REQUIRED_PARAM_KEYS as REQUIRED_SCHEMA_EVOLUTION_PARAM_KEYS,
)
from trellis.learning.scoring import (
    LEARNING_NOISE_RETRY_KEY,
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_PROMOTE_RETRY_KEY,
    LEARNING_PROMOTE_SUCCESS_KEY,
    LEARNING_SCORING_COMPONENT,
    PROMOTE_RECOMMENDATIONS,
    REQUIRED_LEARNING_PARAMETER_KEYS,
    analyze_learning_observations,
    build_learning_promotion_payloads,
    normalize_intent_family,
    prepare_learning_promotions,
    submit_learning_promotion,
    write_learning_review_artifacts,
)

__all__ = [
    "LEARNING_NOISE_RETRY_KEY",
    "LEARNING_NOISE_SUCCESS_KEY",
    "LEARNING_PROMOTE_RETRY_KEY",
    "LEARNING_PROMOTE_SUCCESS_KEY",
    "LEARNING_SCORING_COMPONENT",
    "PROMOTE_RECOMMENDATIONS",
    "RECOMMENDED_SEED_VALUES",
    "REQUIRED_LEARNING_PARAMETER_KEYS",
    "REQUIRED_SCHEMA_EVOLUTION_PARAM_KEYS",
    "SCHEMA_EVOLUTION_PARAM_COMPONENT_ID",
    "WellKnownCandidate",
    "analyze_learning_observations",
    "analyze_well_known_candidates",
    "build_learning_observations_from_event_log",
    "build_learning_promotion_payloads",
    "normalize_intent_family",
    "prepare_learning_promotions",
    "submit_learning_promotion",
    "write_learning_review_artifacts",
]
