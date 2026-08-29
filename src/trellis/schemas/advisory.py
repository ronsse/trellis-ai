"""Advisory schema for Trellis.

Advisories are deterministic, evidence-backed suggestions generated from
outcome data.  They are computed by the :class:`AdvisoryGenerator` and
delivered alongside context packs so agents know *what to do differently*
based on past successes and failures.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from trellis.core.base import TimestampedModel, VersionedModel
from trellis.core.ids import generate_ulid


class AdvisoryCategory(StrEnum):
    """Category of advice an advisory provides."""

    APPROACH = "approach"  # "Agents that validated schema first succeeded 82%"
    SCOPE = "scope"  # "Narrowing to 3 entities improved success by 40%"
    ENTITY = "entity"  # "Entity X in 80% of successful traces"
    ANTI_PATTERN = "anti_pattern"  # "Skipping validation → 3x failure rate"
    QUERY = "query"  # "Include 'deployment' in your context query"


class AdvisoryStatus(StrEnum):
    """Lifecycle state of an advisory.

    ``SUPPRESSED`` is a reversible soft-delete: the advisory stays in
    the store (so later evidence can restore it) but is filtered out
    of retrieval by default. Prior to the 2.1 fix, advisories were
    hard-deleted on fitness-loop suppression and unrecoverable.
    """

    ACTIVE = "active"
    SUPPRESSED = "suppressed"


class DriftPattern(StrEnum):
    """Pattern classification emitted on ``AdvisoryDriftAlert`` (Gap 2.4)."""

    #: Recent success_rate dropped materially vs. the full window.
    REGIME_SHIFT_DECLINE = "regime_shift_decline"
    #: Recent lift and full lift have opposite signs, with non-trivial magnitude.
    LIFT_SIGN_FLIP = "lift_sign_flip"


class AdvisoryEvidence(VersionedModel):
    """Statistical backing for an advisory."""

    sample_size: int
    success_rate_with: float
    #: Success rate of the packs that did **not** carry the subject. The
    #: generator refuses to emit an advisory at all when that arm is too
    #: small to measure, so this is never a stand-in zero — see
    #: :meth:`~trellis.retrieve.advisory_generator.AdvisoryGenerator._supported_effect`
    #: and #383, where a ``0.0`` fallback made every ``effect_size`` a
    #: restatement of the deployment's own success rate.
    success_rate_without: float
    effect_size: float  # success_rate_with - success_rate_without
    #: What the *evidence* implies, as of the run that last wrote this block
    #: — ``_compute_confidence(sample_size, |effect_size|)``. Deliberately
    #: distinct from :attr:`Advisory.confidence`, which is the delivery gate
    #: and is owned by the fitness loop once that loop has scored the row.
    #: Storing both is the point: a row reading ``confidence=0.09`` beside
    #: ``evidence_confidence=1.00`` says "the correlation is strong and the
    #: outcomes are bad", which is the one thing a reviewer needs to see and
    #: which a single conflated number could never express.
    evidence_confidence: float | None = None
    #: **Pack ids**, not trace ids — the ``entity_id`` of the
    #: ``PACK_ASSEMBLED`` events that exemplify the claim (successes for a
    #: positive effect, failures for a negative one). The field name
    #: predates the generator ever populating it and is kept because
    #: renaming it would fail validation on every advisory already stored.
    representative_trace_ids: list[str] = Field(default_factory=list)


class Advisory(TimestampedModel, VersionedModel):
    """A single actionable suggestion for an agent.

    Advisories are generated deterministically from outcome data — never
    by an LLM at read time.  Each carries its statistical evidence so
    the consuming agent can weight it appropriately.
    """

    advisory_id: str = Field(default_factory=generate_ulid)
    category: AdvisoryCategory
    confidence: float  # 0.0-1.0, derived from sample size + effect size
    message: str  # Human/agent-readable suggestion
    evidence: AdvisoryEvidence
    scope: str  # domain, intent pattern, or entity type
    entity_id: str | None = None  # for ENTITY advisories
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: AdvisoryStatus = AdvisoryStatus.ACTIVE
    suppressed_at: datetime | None = None
    suppression_reason: str | None = None
    #: When :func:`~trellis.retrieve.effectiveness.run_advisory_fitness_loop`
    #: last wrote an outcome-blended :attr:`confidence`. ``None`` means the
    #: loop has never scored this advisory — it has not been served
    #: ``min_presentations`` times — so :attr:`confidence` is still the
    #: generator's statistic and the generator keeps it current.
    #:
    #: **This is an ownership handoff, and it has to be recorded rather than
    #: inferred.** Nightly curation runs generation and then the fitness loop
    #: in one cycle. If the generator rewrote ``confidence`` after the loop
    #: owned it, the blend could never compound: every emitted advisory has
    #: ``confidence >= 0.15`` by construction, so ``0.7 x 0.15 = 0.105`` stays
    #: above ``suppress_below = 0.1`` in a single pass, and demotion would be
    #: arithmetically unreachable no matter how badly the advice performed.
    fitness_scored_at: datetime | None = None
