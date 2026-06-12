"""Tuners — consume OutcomeEvents and propose ParameterSets.

Each tuner is a read-many-write-one transformer: pull outcomes since a
cursor, aggregate per learning-axis cell, apply a rule set, and emit
:class:`ParameterProposal` records.  Promotion to an active
:class:`ParameterSet` is a separate governance step (see
:mod:`trellis.learning.tuners.promotion`, when it lands).
"""

from trellis.learning.tuners.auto_promote import (
    DEFAULT_AUTO_MIN_EFFECT_SIZE,
    DEFAULT_AUTO_MIN_SAMPLE_SIZE,
    AutoPromoteOutcome,
    AutoPromotePolicy,
    AutoPromoteReport,
    report_to_dict,
    run_auto_promotion,
)
from trellis.learning.tuners.promotion import (
    PromotionPolicy,
    PromotionPreview,
    PromotionResult,
    preview_promotion,
    promote_proposal,
    reject_proposal,
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
    "DEFAULT_AUTO_MIN_EFFECT_SIZE",
    "DEFAULT_AUTO_MIN_SAMPLE_SIZE",
    "DEFAULT_RULES",
    "AggregatedOutcomes",
    "AutoPromoteOutcome",
    "AutoPromotePolicy",
    "AutoPromoteReport",
    "PostPromotionPolicy",
    "PostPromotionReport",
    "PromotionPolicy",
    "PromotionPreview",
    "PromotionResult",
    "RuleTuner",
    "TuningRule",
    "aggregate_outcomes",
    "apply_rules",
    "monitor_post_promotion",
    "preview_promotion",
    "promote_proposal",
    "reject_proposal",
    "report_to_dict",
    "run_auto_promotion",
    "run_post_promotion_sweep",
]
