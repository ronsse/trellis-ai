"""Promote parameter proposals through the governance pipeline.

A :class:`ParameterProposal` is the tuner's recommendation; promotion
is the governed decision to turn it into an active
:class:`ParameterSet`. This module runs the decision pipeline:

1. **Validate** — the proposal exists and is still eligible
   (``status == "pending" or "canary"``, not terminal).
2. **Policy gate** — :class:`PromotionPolicy` check.  Rejects
   proposals that don't meet the minimum sample size or whose
   effect size against the active baseline is too small.
3. **Execute** — write a new :class:`ParameterSet` snapshot via
   :class:`ParameterStore.put`, update the proposal status, and
   invalidate any cached values in an optional
   :class:`ParameterRegistry`.
4. **Emit** — append a :class:`PARAMS_UPDATED` event to the
   :class:`EventLog`.  A policy rejection emits
   :class:`TUNER_PROPOSAL_REJECTED` instead so the audit trail
   captures the decision either way.

The module deliberately **does not** route through
:class:`MutationExecutor` yet. The executor needs a parameter-aware
operation type to be registered; that refactor is a follow-up.
Current implementation still follows the same
``validate → policy → execute → emit`` shape so the later wiring is
cosmetic, not structural.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from trellis.schemas.parameters import ParameterProposal, ParameterSet
from trellis.stores.base.event_log import EventLog, EventType

if TYPE_CHECKING:
    from trellis.ops.registry import ParameterRegistry
    from trellis.stores.base.parameter import ParameterStore
    from trellis.stores.base.tuner_state import TunerStateStore

logger = structlog.get_logger(__name__)


#: Default policy — five samples, 15 % relative effect against the
#: baseline.  The values line up with the :class:`RuleTuner` default
#: rule set; tighten by passing a custom :class:`PromotionPolicy`.
DEFAULT_MIN_SAMPLE_SIZE = 5
DEFAULT_MIN_EFFECT_SIZE = 0.15


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """Gate parameters that must pass before a proposal promotes.

    Args:
        min_sample_size: Lower bound on ``ParameterProposal.sample_size``.
            Proposals from cells with fewer samples are rejected
            regardless of effect magnitude.
        min_effect_size: Lower bound on the absolute relative delta
            ``abs(proposed - baseline) / max(abs(baseline), epsilon)``.
            Only applied to numeric values when a baseline exists.
        allow_no_baseline: When ``True`` (default) a proposal with no
            active snapshot for its scope can still promote — this is
            the bootstrap case. When ``False`` the cell must have at
            least one prior snapshot.
        allow_non_numeric: When ``True`` (default) a proposal setting
            a non-numeric value (``str`` / ``bool``) skips the
            ``min_effect_size`` check. Numeric baselines always
            enforce it.
    """

    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE
    min_effect_size: float = DEFAULT_MIN_EFFECT_SIZE
    allow_no_baseline: bool = True
    allow_non_numeric: bool = True


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """Outcome of a single :func:`promote_proposal` call."""

    proposal_id: str
    status: str  # "promoted" | "rejected" | "skipped"
    reason: str
    params_version: str | None = None
    effect_size: float | None = None
    baseline_values: dict[str, Any] | None = None


def _compute_effect_size(
    proposed: dict[str, Any], baseline: dict[str, Any] | None
) -> tuple[float | None, bool]:
    """Return ``(effect_size, has_non_numeric)`` for the proposed change.

    Effect size is the max relative delta across all numeric keys in
    the proposal:

        max over k of abs(proposed[k] - baseline[k]) / max(abs(baseline[k]), eps)

    Non-numeric values (strings, booleans) count as "differs" but do
    not contribute to the numeric effect; caller decides whether to
    allow them through based on :attr:`PromotionPolicy.allow_non_numeric`.
    """
    eps = 1e-9
    max_delta: float | None = None
    has_non_numeric = False

    for key, proposed_value in proposed.items():
        baseline_value = (baseline or {}).get(key)
        if isinstance(proposed_value, bool) or isinstance(baseline_value, bool):
            if proposed_value != baseline_value:
                has_non_numeric = True
            continue
        if isinstance(proposed_value, str) or isinstance(baseline_value, str):
            if proposed_value != baseline_value:
                has_non_numeric = True
            continue
        if baseline_value is None:
            # No baseline for this key — treat as infinite relative change.
            max_delta = float("inf")
            continue
        try:
            p = float(proposed_value)
            b = float(baseline_value)
        except (TypeError, ValueError):
            continue
        denom = max(abs(b), eps)
        delta = abs(p - b) / denom
        if max_delta is None or delta > max_delta:
            max_delta = delta

    return max_delta, has_non_numeric


def promote_proposal(
    proposal_id: str,
    *,
    tuner_state: TunerStateStore,
    parameter_store: ParameterStore,
    event_log: EventLog,
    parameter_registry: ParameterRegistry | None = None,
    policy: PromotionPolicy | None = None,
    source: str = "tuner.promotion",
    force: bool = False,
) -> PromotionResult:
    """Validate, gate, execute, and emit for one proposal.

    Args:
        proposal_id: The proposal to act on.
        tuner_state: Store holding the proposal record and its status.
        parameter_store: Target for the new :class:`ParameterSet`.
        event_log: Destination for the audit event
            (:class:`EventType.PARAMS_UPDATED` on success,
            :class:`EventType.TUNER_PROPOSAL_REJECTED` on policy rejection).
        parameter_registry: Optional in-process registry whose cache
            is invalidated on successful promotion so the next call
            re-resolves to the new snapshot.
        policy: Gate to apply.  Defaults to :class:`PromotionPolicy`.
        source: Event source label.  Pipe a human-readable tool name
            when promoting from the CLI.
        force: When ``True`` the policy gate is skipped.  Still
            requires the proposal to exist and not be in a terminal
            status; emits a ``force=true`` flag on the event payload.
    """
    effective_policy = policy or PromotionPolicy()

    proposal = tuner_state.get_proposal(proposal_id)
    if proposal is None:
        return PromotionResult(
            proposal_id=proposal_id,
            status="skipped",
            reason="proposal_not_found",
        )

    if proposal.status in {"promoted", "rejected"}:
        return PromotionResult(
            proposal_id=proposal_id,
            status="skipped",
            reason=f"proposal_already_{proposal.status}",
        )

    baseline_snapshot = parameter_store.resolve(proposal.scope)
    baseline_values = baseline_snapshot.values if baseline_snapshot else None

    effect, has_non_numeric = _compute_effect_size(
        proposal.proposed_values, baseline_values
    )

    if not force:
        gate_result = _apply_policy(
            proposal=proposal,
            policy=effective_policy,
            baseline_values=baseline_values,
            effect=effect,
            has_non_numeric=has_non_numeric,
        )
        if gate_result is not None:
            # Policy rejection — update proposal status and emit event.
            tuner_state.update_status(
                proposal_id,
                "rejected",
                notes=gate_result,
            )
            event_log.emit(
                EventType.TUNER_PROPOSAL_REJECTED,
                source=source,
                entity_id=proposal_id,
                entity_type="parameter_proposal",
                payload={
                    "proposal_id": proposal_id,
                    "scope": list(proposal.scope.key()),
                    "proposed_values": dict(proposal.proposed_values),
                    "sample_size": proposal.sample_size,
                    "effect_size": effect,
                    "reason": gate_result,
                },
            )
            logger.info(
                "tuner.promotion.rejected",
                proposal_id=proposal_id,
                reason=gate_result,
            )
            return PromotionResult(
                proposal_id=proposal_id,
                status="rejected",
                reason=gate_result,
                effect_size=effect,
                baseline_values=baseline_values,
            )

    # Execute: merge the proposed values onto the baseline so partial
    # proposals only change what they touch.
    merged_values: dict[str, Any] = dict(baseline_values or {})
    merged_values.update(proposal.proposed_values)
    new_snapshot = ParameterSet(
        scope=proposal.scope,
        values=merged_values,
        source=f"tuner:{proposal.tuner}",
        notes=f"Promoted from proposal {proposal_id}",
        metadata={
            "proposal_id": proposal_id,
            "rule_name": proposal.metadata.get("rule_name"),
        },
    )
    stored = parameter_store.put(new_snapshot)

    tuner_state.update_status(
        proposal_id,
        "promoted",
        notes=f"Promoted as params_version={stored.params_version}",
    )

    if parameter_registry is not None:
        parameter_registry.invalidate(proposal.scope)

    event_log.emit(
        EventType.PARAMS_UPDATED,
        source=source,
        entity_id=stored.params_version,
        entity_type="parameter_set",
        payload={
            "proposal_id": proposal_id,
            "params_version": stored.params_version,
            "baseline_version": baseline_snapshot.params_version
            if baseline_snapshot
            else None,
            "scope": list(proposal.scope.key()),
            "proposed_values": dict(proposal.proposed_values),
            "baseline_values": dict(baseline_values or {}),
            "effect_size": effect,
            "sample_size": proposal.sample_size,
            "tuner": proposal.tuner,
            "force": force,
        },
    )
    logger.info(
        "tuner.promotion.accepted",
        proposal_id=proposal_id,
        params_version=stored.params_version,
        effect_size=effect,
        force=force,
    )

    return PromotionResult(
        proposal_id=proposal_id,
        status="promoted",
        reason="ok",
        params_version=stored.params_version,
        effect_size=effect,
        baseline_values=baseline_values,
    )


def _apply_policy(  # noqa: PLR0911 — gate is a straight-line decision tree; each branch maps to a distinct rejection reason.
    *,
    proposal: ParameterProposal,
    policy: PromotionPolicy,
    baseline_values: dict[str, Any] | None,
    effect: float | None,
    has_non_numeric: bool,
) -> str | None:
    """Run the gate; return a rejection reason, or ``None`` on pass."""
    if proposal.sample_size < policy.min_sample_size:
        return (
            f"sample_size={proposal.sample_size} < "
            f"min_sample_size={policy.min_sample_size}"
        )

    # Non-numeric change (string/bool).  Pass through when allowed.
    if has_non_numeric and effect is None:
        if not policy.allow_non_numeric:
            return "non_numeric_change_disallowed"
        return None

    # No baseline: bootstrap case.
    if baseline_values is None:
        if policy.allow_no_baseline:
            return None
        return "no_baseline_snapshot_for_scope"

    # Effect size defined and finite: compare to min.
    if effect is None:
        # All proposed values matched baseline exactly — nothing to do.
        return "zero_effect_proposed_equals_baseline"
    if effect != float("inf") and effect < policy.min_effect_size:
        return f"effect_size={effect:.4f} < min_effect_size={policy.min_effect_size}"
    return None
