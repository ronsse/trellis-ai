"""Tuners — consume OutcomeEvents and propose ParameterSets.

Each tuner is a read-many-write-one transformer: pull outcomes since a
cursor, aggregate per learning-axis cell, apply a rule set, and emit
:class:`ParameterProposal` records.  Promotion to an active
:class:`ParameterSet` is a separate governance step (see
:mod:`trellis.learning.tuners.promotion`, when it lands).
"""

from trellis.learning.tuners.auto_promote import (
    AUTO_PROMOTE_ENABLED_SETTING,
    AUTO_PROMOTE_MIN_EFFECT_SIZE_SETTING,
    AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING,
    DEFAULT_AUTO_MIN_EFFECT_SIZE,
    DEFAULT_AUTO_MIN_SAMPLE_SIZE,
    AutoPromoteOutcome,
    AutoPromotePolicy,
    AutoPromoteReport,
    report_to_dict,
    resolve_auto_promote_overrides,
    run_auto_promotion,
)
from trellis.learning.tuners.promotion import (
    EffectSize,
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
    "AUTO_PROMOTE_ENABLED_SETTING",
    "AUTO_PROMOTE_MIN_EFFECT_SIZE_SETTING",
    "AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING",
    "DEFAULT_AUTO_MIN_EFFECT_SIZE",
    "DEFAULT_AUTO_MIN_SAMPLE_SIZE",
    "DEFAULT_RULES",
    "AggregatedOutcomes",
    "AutoPromoteOutcome",
    "AutoPromotePolicy",
    "AutoPromoteReport",
    "EffectSize",
    "PostPromotionPolicy",
    "PostPromotionReport",
    "PromotionPolicy",
    "PromotionPreview",
    "PromotionResult",
    "RuleTuner",
    "TuningRule",
    "aggregate_outcomes",
    "apply_rules",
    "resolve_auto_promote_overrides",
    "monitor_post_promotion",
    "preview_promotion",
    "promote_proposal",
    "reject_proposal",
    "report_to_dict",
    "run_auto_promotion",
    "run_post_promotion_sweep",
]
