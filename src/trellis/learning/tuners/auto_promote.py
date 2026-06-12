"""Tier-1 autonomy — config-gated auto-promotion of tuner proposals.

This module is the policy contract for the **Tier 1** rung of the
autonomy ladder (see ``docs/design/adr-autonomy-ladder.md``). It does
*not* introduce a second mutation path: a qualifying proposal is
promoted through the exact same :func:`promote_proposal` governance
pipeline the manual ``trellis metrics promote --commit`` command uses,
and degradation is unwound through the exact same
:func:`monitor_post_promotion` rollback the manual sweep uses. What this
module adds on top is the four tier-1 invariants the ADR requires before
any of that is allowed to run without a human in the loop:

(a) **Reversible through existing versioned state.** Every auto-promotion
    targets a versioned :class:`ParameterSet`; the prior snapshot is the
    rollback target. No invariant code here — it is a property of the
    store and is asserted by refusing to auto-promote a proposal whose
    scope has no resolvable baseline when ``require_baseline`` is set.

(b) **Post-change monitoring exists and is enabled.** Auto-promotion is
    only offered bundled with :func:`monitor_post_promotion` running at
    ``auto_demote=True``. The two are inseparable in
    :func:`run_auto_promotion` — you cannot auto-promote without arming
    the rollback.

(c) **Dedicated audit events.** Each auto-promotion emits
    :class:`EventType.PARAMS_AUTO_PROMOTED` *in addition to* the
    ``PARAMS_UPDATED`` the governance pipeline already writes; each
    auto-rollback emits :class:`EventType.PARAMS_AUTO_ROLLED_BACK`. The
    autonomous path is self-identifying in the log.

(d) **Per-scope opt-in, global default OFF.** :class:`AutoPromotePolicy`
    carries ``enabled`` (default ``False``). When disabled,
    :func:`run_auto_promotion` performs zero mutations and emits zero
    events — it only reports what *would* have happened.

The thresholds on :class:`AutoPromotePolicy` are deliberately **stricter**
than the manual :class:`PromotionPolicy` defaults. The justification is in
the ADR §"Chosen tier-1 thresholds"; the short version is that an
unattended promotion must clear a higher evidentiary bar than one a human
is about to eyeball.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.base import utc_now
from trellis.learning.tuners.promotion import (
    DEFAULT_MIN_EFFECT_SIZE,
    DEFAULT_MIN_SAMPLE_SIZE,
    PromotionPolicy,
    PromotionResult,
    promote_proposal,
)
from trellis.learning.tuners.rollback import (
    PostPromotionPolicy,
    PostPromotionReport,
    monitor_post_promotion,
)
from trellis.stores.base.event_log import EventLog, EventType

if TYPE_CHECKING:
    from trellis.learning.tuners.rule_tuner import RuleTuner
    from trellis.ops.registry import ParameterRegistry
    from trellis.schemas.parameters import ParameterProposal
    from trellis.stores.base.outcome import OutcomeStore
    from trellis.stores.base.parameter import ParameterStore
    from trellis.stores.base.tuner_state import TunerStateStore

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tier-1 thresholds — stricter than the manual-promote defaults.
#
# Manual promote (PromotionPolicy): min_sample_size=5, min_effect_size=0.15.
# A human reviews each manual promotion, so the gate only has to surface
# obvious non-starters. An *unattended* promotion has no such reviewer, so
# the auto gate demands materially more evidence:
#
#   * sample size 30 (6x the manual floor) — matches DEFAULT_RULES'
#     min_sample_size, i.e. the auto-promote floor never trusts a cell the
#     tuner itself would not have fired on.
#   * effect size 0.25 (vs 0.15) — only changes whose measured relative
#     delta against the live baseline is large enough that noise is an
#     unlikely explanation auto-apply.
#
# Both are asserted to dominate the manual defaults at construction time so
# a future edit can't silently weaken the autonomous path below the manual
# one.
# ---------------------------------------------------------------------------
DEFAULT_AUTO_MIN_SAMPLE_SIZE = 30
DEFAULT_AUTO_MIN_EFFECT_SIZE = 0.25


@dataclass(frozen=True, slots=True)
class AutoPromotePolicy:
    """Per-scope opt-in gate for unattended parameter promotion (Tier 1).

    Attributes:
        enabled: Master switch. ``False`` by default — global default OFF
            per tier-1 invariant (d). When ``False``,
            :func:`run_auto_promotion` mutates nothing and emits nothing.
        min_sample_size: Lower bound on ``ParameterProposal.sample_size``
            for *auto*-promotion. Must be ``>= PromotionPolicy``'s manual
            default; enforced at construction.
        min_effect_size: Lower bound on the relative effect against the
            live baseline for *auto*-promotion. Must be ``>=
            PromotionPolicy``'s manual default; enforced at construction.
        require_baseline: When ``True`` (default), a proposal whose scope
            has no resolvable baseline snapshot is *not* auto-promoted —
            the bootstrap (first-ever snapshot for a scope) is left for a
            human, because there is nothing to roll back to (tier-1
            invariant (a)). Manual promotion still allows the bootstrap.
        post_promotion: The monitoring posture armed for every
            auto-promotion. ``auto_demote`` is forced ``True`` here (tier-1
            invariant (b)); a value of ``False`` is rejected at
            construction so the autonomous path can never promote without
            an armed rollback.

    Raises:
        ValueError: If ``min_sample_size`` / ``min_effect_size`` are looser
            than the manual :class:`PromotionPolicy` defaults, or if
            ``post_promotion.auto_demote`` is ``False``.
    """

    enabled: bool = False
    min_sample_size: int = DEFAULT_AUTO_MIN_SAMPLE_SIZE
    min_effect_size: float = DEFAULT_AUTO_MIN_EFFECT_SIZE
    require_baseline: bool = True
    post_promotion: PostPromotionPolicy = field(
        default_factory=lambda: PostPromotionPolicy(auto_demote=True)
    )

    def __post_init__(self) -> None:
        if self.min_sample_size < DEFAULT_MIN_SAMPLE_SIZE:
            msg = (
                f"AutoPromotePolicy.min_sample_size={self.min_sample_size} is "
                f"looser than the manual PromotionPolicy default "
                f"({DEFAULT_MIN_SAMPLE_SIZE}); the autonomous gate must be at "
                f"least as strict as the human-reviewed one."
            )
            raise ValueError(msg)
        if self.min_effect_size < DEFAULT_MIN_EFFECT_SIZE:
            msg = (
                f"AutoPromotePolicy.min_effect_size={self.min_effect_size} is "
                f"looser than the manual PromotionPolicy default "
                f"({DEFAULT_MIN_EFFECT_SIZE}); the autonomous gate must be at "
                f"least as strict as the human-reviewed one."
            )
            raise ValueError(msg)
        if not self.post_promotion.auto_demote:
            msg = (
                "AutoPromotePolicy.post_promotion.auto_demote must be True — "
                "tier-1 autonomy may not auto-promote without an armed "
                "rollback (invariant (b))."
            )
            raise ValueError(msg)

    def to_promotion_policy(self) -> PromotionPolicy:
        """Project onto the :class:`PromotionPolicy` the gate runs under.

        ``allow_no_baseline`` is derived from :attr:`require_baseline` so
        the *same* governance pipeline enforces the stricter bootstrap rule
        without a separate code path.
        """
        return PromotionPolicy(
            min_sample_size=self.min_sample_size,
            min_effect_size=self.min_effect_size,
            allow_no_baseline=not self.require_baseline,
        )


@dataclass(frozen=True, slots=True)
class AutoPromoteOutcome:
    """What happened to one proposal during an auto-promotion pass."""

    proposal_id: str
    #: ``"auto_promoted"`` — qualified and was promoted (commit run);
    #: ``"would_auto_promote"`` — qualified but the pass was a dry-run;
    #: ``"pending_manual"`` — did not clear the auto gate, left for a human;
    #: ``"disabled"`` — the policy was disabled, no action taken;
    #: ``"skipped"`` — the proposal was not promotable (already terminal /
    #: not found).
    disposition: str
    reason: str
    params_version: str | None = None
    effect_size: float | None = None
    #: Set only when an auto-promotion was monitored post-commit.
    post_promotion: PostPromotionReport | None = None
    #: Set when monitoring rolled the snapshot back.
    rolled_back_to: str | None = None


@dataclass(frozen=True, slots=True)
class AutoPromoteReport:
    """Aggregate result of one :func:`run_auto_promotion` pass."""

    enabled: bool
    dry_run: bool
    proposals_considered: int
    auto_promoted: int
    rolled_back: int
    pending_manual: int
    outcomes: list[AutoPromoteOutcome]


def run_auto_promotion(
    *,
    tuner: RuleTuner,
    parameter_store: ParameterStore,
    tuner_state: TunerStateStore,
    outcome_store: OutcomeStore,
    event_log: EventLog,
    policy: AutoPromotePolicy,
    parameter_registry: ParameterRegistry | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
    monitor_prior: bool = True,
    source: str = "tuner.auto_promote",
    now: datetime | None = None,
) -> AutoPromoteReport:
    """Run one tuning pass, then auto-promote every qualifying proposal.

    Pipeline:

    1. ``tuner.run(...)`` produces / refreshes proposals (same logic the
       manual ``metrics tune`` command drives).
    2. Each persisted proposal is gated against
       :meth:`AutoPromotePolicy.to_promotion_policy`. Non-qualifying
       proposals are reported as ``pending_manual`` and left untouched for
       manual review — they are *not* rejected.
    3. Qualifying proposals are promoted via :func:`promote_proposal`
       (the governance pipeline; emits ``PARAMS_UPDATED``), then a
       dedicated :class:`EventType.PARAMS_AUTO_PROMOTED` event is emitted.
    4. Each freshly promoted ``params_version`` is immediately run through
       :func:`monitor_post_promotion` with the armed
       :attr:`AutoPromotePolicy.post_promotion` policy. If monitoring
       demotes it, a :class:`EventType.PARAMS_AUTO_ROLLED_BACK` event is
       emitted alongside the rollback's own ``PARAMS_UPDATED``.

    When :attr:`AutoPromotePolicy.enabled` is ``False`` *or* ``dry_run`` is
    ``True``, no proposal is promoted and no event is emitted; the report
    classifies what *would* have happened. With ``enabled=False`` the
    behaviour is byte-identical to running the tuner alone — this is the
    "disabled config => zero behaviour change" contract.

    Args:
        tuner: Configured :class:`RuleTuner`; ``run`` is invoked once.
        parameter_store: Promotion target + rollback source.
        tuner_state: Proposal storage the tuner writes to.
        outcome_store: Signal source for post-promotion monitoring.
        event_log: Destination for governance + tier-1 audit events.
        policy: The tier-1 gate. ``enabled=False`` => no-op mutation-wise.
        parameter_registry: Optional cache invalidated on promotion.
        since / until: Forwarded to ``tuner.run``.
        dry_run: Report only; never mutate or emit. Independent of
            ``policy.enabled`` — both must be truthy to actually promote.
        source: Event source label on every emitted event.
        now: Reference timestamp for monitoring (tests).
    """
    proposals = tuner.run(since=since, until=until)

    promotion_policy = policy.to_promotion_policy()
    effective_dry_run = dry_run or not policy.enabled

    outcomes: list[AutoPromoteOutcome] = []
    auto_promoted = 0
    rolled_back = 0
    pending_manual = 0

    for proposal in proposals:
        qualifies, reason, effect = _evaluate(
            proposal,
            parameter_store=parameter_store,
            policy=policy,
        )
        if not qualifies:
            pending_manual += 1
            outcomes.append(
                AutoPromoteOutcome(
                    proposal_id=proposal.proposal_id,
                    disposition="pending_manual",
                    reason=reason,
                    effect_size=effect,
                )
            )
            logger.info(
                "auto_promote.pending_manual",
                proposal_id=proposal.proposal_id,
                reason=reason,
            )
            continue

        if effective_dry_run:
            outcomes.append(
                AutoPromoteOutcome(
                    proposal_id=proposal.proposal_id,
                    disposition="disabled"
                    if not policy.enabled
                    else "would_auto_promote",
                    reason=reason,
                    effect_size=effect,
                )
            )
            continue

        outcome = _promote_and_monitor(
            proposal=proposal,
            parameter_store=parameter_store,
            tuner_state=tuner_state,
            outcome_store=outcome_store,
            event_log=event_log,
            policy=policy,
            promotion_policy=promotion_policy,
            parameter_registry=parameter_registry,
            source=source,
            now=now,
        )
        outcomes.append(outcome)
        if outcome.disposition == "auto_promoted":
            auto_promoted += 1
            if outcome.rolled_back_to is not None:
                rolled_back += 1
        elif outcome.disposition == "skipped":
            # A proposal the governance pipeline declined (e.g. it raced to
            # a terminal status between tuner.run and here) is not pending
            # manual either — surface it as-is.
            pass

    # Re-monitor prior auto-promotions: a promotion from an earlier pass may
    # only now have enough post-promotion samples to judge. This is how
    # degradation that accrues *after* the promoting pass gets caught and
    # rolled back — on a later pass — matching the periodic production cadence.
    if not effective_dry_run and monitor_prior:
        swept = _sweep_prior_promotions(
            parameter_store=parameter_store,
            outcome_store=outcome_store,
            event_log=event_log,
            policy=policy,
            source=source,
            now=now,
            since=since,
            limit=200,
        )
        # Avoid double-counting versions already monitored inline this pass.
        inline_versions = {
            o.params_version for o in outcomes if o.params_version is not None
        }
        for swept_outcome in swept:
            if swept_outcome.params_version in inline_versions:
                continue
            outcomes.append(swept_outcome)
            rolled_back += 1

    return AutoPromoteReport(
        enabled=policy.enabled,
        dry_run=effective_dry_run,
        proposals_considered=len(proposals),
        auto_promoted=auto_promoted,
        rolled_back=rolled_back,
        pending_manual=pending_manual,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evaluate(
    proposal: ParameterProposal,
    *,
    parameter_store: ParameterStore,
    policy: AutoPromotePolicy,
) -> tuple[bool, str, float | None]:
    """Return ``(qualifies, reason, effect_size)`` for the auto gate.

    Runs the *same* policy primitives the governance pipeline uses
    (:func:`_compute_effect_size` + :func:`_apply_policy`) so the gate the
    auto path applies is identical in shape to the manual one, only with
    stricter thresholds.
    """
    # Local import keeps the private gate primitives in one module.
    from trellis.learning.tuners.promotion import (  # noqa: PLC0415
        _apply_policy,
        _compute_effect_size,
    )

    baseline = parameter_store.resolve(proposal.scope)
    baseline_values = baseline.values if baseline else None
    effect, has_non_numeric = _compute_effect_size(
        proposal.proposed_values, baseline_values
    )
    rejection = _apply_policy(
        proposal=proposal,
        policy=policy.to_promotion_policy(),
        baseline_values=baseline_values,
        effect=effect,
        has_non_numeric=has_non_numeric,
    )
    if rejection is not None:
        return False, rejection, effect
    return True, "passes_auto_gate", effect


def _promote_and_monitor(
    *,
    proposal: ParameterProposal,
    parameter_store: ParameterStore,
    tuner_state: TunerStateStore,
    outcome_store: OutcomeStore,
    event_log: EventLog,
    policy: AutoPromotePolicy,
    promotion_policy: PromotionPolicy,
    parameter_registry: ParameterRegistry | None,
    source: str,
    now: datetime | None,
) -> AutoPromoteOutcome:
    """Promote one qualifying proposal, emit tier-1 events, then monitor."""
    result: PromotionResult = promote_proposal(
        proposal.proposal_id,
        tuner_state=tuner_state,
        parameter_store=parameter_store,
        event_log=event_log,
        parameter_registry=parameter_registry,
        policy=promotion_policy,
        source=source,
    )

    if result.status != "promoted" or result.params_version is None:
        logger.info(
            "auto_promote.not_promoted",
            proposal_id=proposal.proposal_id,
            status=result.status,
            reason=result.reason,
        )
        return AutoPromoteOutcome(
            proposal_id=proposal.proposal_id,
            disposition="skipped",
            reason=f"governance_{result.status}:{result.reason}",
            effect_size=result.effect_size,
        )

    # Tier-1 invariant (c): dedicated audit event for the autonomous action.
    event_log.emit(
        EventType.PARAMS_AUTO_PROMOTED,
        source=source,
        entity_id=result.params_version,
        entity_type="parameter_set",
        payload={
            "proposal_id": proposal.proposal_id,
            "params_version": result.params_version,
            "scope": list(proposal.scope.key()),
            "proposed_values": dict(proposal.proposed_values),
            "effect_size": result.effect_size,
            "sample_size": proposal.sample_size,
            "tuner": proposal.tuner,
            "min_sample_size": policy.min_sample_size,
            "min_effect_size": policy.min_effect_size,
        },
    )
    logger.info(
        "auto_promote.promoted",
        proposal_id=proposal.proposal_id,
        params_version=result.params_version,
        effect_size=result.effect_size,
    )

    # Tier-1 invariant (b): armed post-promotion monitoring runs immediately.
    report, rolled_back_to = _monitor_and_emit_rollback(
        result.params_version,
        parameter_store=parameter_store,
        outcome_store=outcome_store,
        event_log=event_log,
        policy=policy,
        source=source,
        now=now,
        proposal_id=proposal.proposal_id,
    )

    return AutoPromoteOutcome(
        proposal_id=proposal.proposal_id,
        disposition="auto_promoted",
        reason="ok",
        params_version=result.params_version,
        effect_size=result.effect_size,
        post_promotion=report,
        rolled_back_to=rolled_back_to,
    )


def _monitor_and_emit_rollback(
    params_version: str,
    *,
    parameter_store: ParameterStore,
    outcome_store: OutcomeStore,
    event_log: EventLog,
    policy: AutoPromotePolicy,
    source: str,
    now: datetime | None,
    proposal_id: str | None,
) -> tuple[PostPromotionReport, str | None]:
    """Monitor one promoted version and emit the tier-1 rollback event.

    Returns ``(report, rolled_back_to)``. The underlying
    :func:`monitor_post_promotion` already writes ``PARAMETERS_DEGRADED``
    and (on demotion) the rollback ``PARAMS_UPDATED``; this wrapper adds the
    dedicated :class:`EventType.PARAMS_AUTO_ROLLED_BACK` so the autonomous
    rollback is self-identifying (tier-1 invariant (c)).
    """
    report = monitor_post_promotion(
        params_version,
        parameter_store=parameter_store,
        outcome_store=outcome_store,
        event_log=event_log,
        policy=policy.post_promotion,
        source=source,
        now=now,
    )

    if report.action != "demoted" or report.demoted_version is None:
        return report, None

    event_log.emit(
        EventType.PARAMS_AUTO_ROLLED_BACK,
        source=source,
        entity_id=params_version,
        entity_type="parameter_set",
        payload={
            "proposal_id": proposal_id,
            "degraded_version": params_version,
            "rollback_version": report.demoted_version,
            "baseline_version": report.baseline_version,
            "scope": list(report.scope.key()) if report.scope else None,
            "degradation": report.degradation,
            "post_samples": report.post_samples,
            "baseline_samples": report.baseline_samples,
        },
    )
    logger.info(
        "auto_promote.rolled_back",
        proposal_id=proposal_id,
        degraded_version=params_version,
        rollback_version=report.demoted_version,
    )
    return report, report.demoted_version


def _sweep_prior_promotions(
    *,
    parameter_store: ParameterStore,
    outcome_store: OutcomeStore,
    event_log: EventLog,
    policy: AutoPromotePolicy,
    source: str,
    now: datetime | None,
    since: datetime | None,
    limit: int,
) -> list[AutoPromoteOutcome]:
    """Re-monitor recent auto-promotions and roll back any that degraded.

    A promotion made in an earlier ``run_auto_promotion`` pass may not have
    accumulated enough post-promotion samples to judge yet. This sweep walks
    recent :class:`EventType.PARAMS_AUTO_PROMOTED` events and re-runs
    monitoring on each version, so degradation is caught on a *later* pass —
    matching how the loop actually runs in production (periodic ``worker
    tune`` invocations). De-duplicates by version and skips versions already
    rolled back.
    """
    effective_since = since or (now or utc_now()) - timedelta(days=30)
    promoted_events = event_log.get_events(
        event_type=EventType.PARAMS_AUTO_PROMOTED,
        since=effective_since,
        limit=limit,
    )
    rolled_back_events = event_log.get_events(
        event_type=EventType.PARAMS_AUTO_ROLLED_BACK,
        since=effective_since,
        limit=limit,
    )
    already_rolled: set[str] = set()
    for rolled_event in rolled_back_events:
        degraded = (rolled_event.payload or {}).get("degraded_version")
        if isinstance(degraded, str):
            already_rolled.add(degraded)

    outcomes: list[AutoPromoteOutcome] = []
    seen: set[str] = set()
    for event in promoted_events:
        payload = event.payload or {}
        version = payload.get("params_version") or event.entity_id
        if not version or version in seen or version in already_rolled:
            continue
        seen.add(version)
        report, rolled_back_to = _monitor_and_emit_rollback(
            version,
            parameter_store=parameter_store,
            outcome_store=outcome_store,
            event_log=event_log,
            policy=policy,
            source=source,
            now=now,
            proposal_id=payload.get("proposal_id"),
        )
        if rolled_back_to is not None:
            outcomes.append(
                AutoPromoteOutcome(
                    proposal_id=payload.get("proposal_id") or version,
                    disposition="auto_promoted",
                    reason="rolled_back_on_later_sweep",
                    params_version=version,
                    post_promotion=report,
                    rolled_back_to=rolled_back_to,
                )
            )
    return outcomes


def _outcome_to_dict(outcome: AutoPromoteOutcome) -> dict[str, Any]:
    """Serialise an :class:`AutoPromoteOutcome` for ``--format json``."""
    post = outcome.post_promotion
    return {
        "proposal_id": outcome.proposal_id,
        "disposition": outcome.disposition,
        "reason": outcome.reason,
        "params_version": outcome.params_version,
        "effect_size": outcome.effect_size,
        "rolled_back_to": outcome.rolled_back_to,
        "post_promotion": (
            {
                "verdict": post.verdict,
                "action": post.action,
                "post_samples": post.post_samples,
                "baseline_samples": post.baseline_samples,
                "degradation": post.degradation,
            }
            if post is not None
            else None
        ),
    }


def report_to_dict(report: AutoPromoteReport) -> dict[str, Any]:
    """Serialise an :class:`AutoPromoteReport` for ``--format json``."""
    return {
        "enabled": report.enabled,
        "dry_run": report.dry_run,
        "proposals_considered": report.proposals_considered,
        "auto_promoted": report.auto_promoted,
        "rolled_back": report.rolled_back,
        "pending_manual": report.pending_manual,
        "outcomes": [_outcome_to_dict(o) for o in report.outcomes],
    }
