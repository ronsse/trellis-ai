"""Tuners — consume OutcomeEvents and propose ParameterSets.

Each tuner is a read-many-write-one transformer: pull outcomes since a
cursor, aggregate per learning-axis cell, apply a rule set, and emit
:class:`ParameterProposal` records.  Promotion to an active
:class:`ParameterSet` is a separate governance step (see
:mod:`trellis.learning.tuners.promotion`, when it lands).
"""

from trellis.learning.tuners.promotion import (
    PromotionPolicy,
    PromotionResult,
    promote_proposal,
)
from trellis.learning.tuners.rollback import (
    PostPromotionPolicy,
    PostPromotionReport,
    monitor_post_promotion,
    run_post_promotion_sweep,
)
from trellis.learning.tuners.rule_tuner import (
    DEFAULT_RULES,
    AggregatedOutcomes,
    RuleTuner,
    TuningRule,
    aggregate_outcomes,
    apply_rules,
)

__all__ = [
    "DEFAULT_RULES",
    "AggregatedOutcomes",
    "PostPromotionPolicy",
    "PostPromotionReport",
    "PromotionPolicy",
    "PromotionResult",
    "RuleTuner",
    "TuningRule",
    "aggregate_outcomes",
    "apply_rules",
    "monitor_post_promotion",
    "promote_proposal",
    "run_post_promotion_sweep",
]
