"""Promote parameter proposals through the governance pipeline.

A :class:`ParameterProposal` is the tuner's recommendation; promotion
is the governed decision to turn it into an active
:class:`ParameterSet`. This module runs the decision pipeline:

1. **Validate** — the proposal exists and is still eligible
   (``status == "pending" or "canary"``, not terminal), and its scope
   is not one of :data:`~trellis.mutate.immutable_core.GOVERNING_KEYS`
   — the parameters that gate *another* learning loop, which no tuner
   may retune and which ``force=True`` does not unlock.
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

from trellis.mutate.immutable_core import governing_key_refusal
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
            ``abs(proposed - baseline) / max(abs(baseline), epsilon)``,
            taken as the max over the proposed keys **that carry a
            numeric baseline**. Keys the baseline does not carry are
            not comparable at any threshold, so they neither satisfy
            this floor nor exempt the keys beside them from it; they
            are reported as :attr:`EffectSize.unbaselined_keys` and
            governed by ``allow_no_baseline``.
        allow_no_baseline: When ``True`` (default) a proposal the
            baseline cannot be compared against can still promote —
            the bootstrap case. This covers both shapes: no active
            snapshot for the scope at all, and a snapshot that carries
            none of the proposed keys. When ``False`` the cell must
            have a prior snapshot carrying at least one proposed key.
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


@dataclass(frozen=True, slots=True)
class PromotionPreview:
    """Read-only forecast of what :func:`promote_proposal` would decide.

    Produced by :func:`preview_promotion` without mutating any store or
    emitting any event. Both the ``trellis metrics promote`` dry-run and
    the API Review-queue confirm step render this so the operator sees the
    predicted decision before committing. ``status`` is one of
    ``"promoted"`` / ``"rejected"`` / ``"skipped"`` — the same vocabulary
    :class:`PromotionResult.status` uses, so the confirm UI and the commit
    result speak the same language.
    """

    proposal_id: str
    status: str  # "promoted" | "rejected" | "skipped"
    reason: str
    proposed_values: dict[str, Any]
    baseline_values: dict[str, Any]
    effect_size: float | None = None
    sample_size: int = 0


@dataclass(frozen=True, slots=True)
class EffectSize:
    """What the effect-size gate could, and could not, compare.

    Separating the two is the point. The previous encoding folded them
    into one ``float`` by assigning ``inf`` for a key the baseline did
    not carry, which cost three distinct things:

    * ``inf`` is **not representable in the audit log**. Payloads are
      ``json.dumps``'d into a ``JSONB`` column, and Postgres rejects the
      resulting ``Infinity`` literal outright (``invalid input syntax
      for type json``). Since ``parameter_store.put`` and the tuner-state
      update both run *before* the emit, a promotion carrying an unseen
      key applied the parameter, recorded ``status="promoted"``, raised
      at the caller and wrote **no** ``parameters.updated`` audit row.
      Every first promotion for a scope has that shape, so on the blessed
      cloud backend the actuator could not complete a single clean
      promotion.
    * ``inf`` was **assigned**, not maxed, so one unseen key laundered
      every other key in the same proposal past ``min_effect_size``.
    * The guard that tried to contain it — ``effect != float("inf") and
      effect < min_effect_size`` — was dead code: ``inf < x`` is already
      ``False`` for every finite ``x``.

    Args:
        comparable_max: Max relative delta over the numeric keys that
            **have** a numeric baseline, or ``None`` when no key does.
            This is the only value the ``min_effect_size`` floor reads,
            and the only one that reaches ``effect_size`` on the wire.
        unbaselined_keys: Numeric keys the baseline does not carry, in
            proposal order. These are not comparable at any threshold;
            reporting them is what turns the bootstrap allowance into a
            visible decision rather than an artifact of the encoding.
        has_non_numeric: A ``str`` / ``bool`` key differs from baseline.
    """

    comparable_max: float | None
    unbaselined_keys: tuple[str, ...]
    has_non_numeric: bool


def _compute_effect_size(
    proposed: dict[str, Any], baseline: dict[str, Any] | None
) -> EffectSize:
    """Return the comparable effect and the keys that had nothing to compare.

    Effect size is the max relative delta across the numeric keys that
    carry a numeric baseline:

        max over k of abs(proposed[k] - baseline[k]) / max(abs(baseline[k]), eps)

    A key the baseline does not carry contributes to
    :attr:`EffectSize.unbaselined_keys` instead of to the maximum — it has
    no relative delta, and pretending it has an infinite one is what let a
    0.4% move on a mature key ride along beside it. Non-numeric values
    (strings, booleans) count as "differs" but do not contribute to the
    numeric effect; the caller decides whether to allow them through based
    on :attr:`PromotionPolicy.allow_non_numeric`.
    """
    eps = 1e-9
    max_delta: float | None = None
    unbaselined: list[str] = []
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
            # Nothing to compare against — record the key, do not invent
            # a magnitude for it.
            unbaselined.append(key)
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

    return EffectSize(
        comparable_max=max_delta,
        unbaselined_keys=tuple(unbaselined),
        has_non_numeric=has_non_numeric,
    )


def _reject(
    *,
    proposal_id: str,
    proposal: ParameterProposal,
    reason: str,
    effect: EffectSize | None,
    baseline_values: dict[str, Any] | None,
    tuner_state: TunerStateStore,
    event_log: EventLog,
    source: str,
) -> PromotionResult:
    """Mark a proposal rejected, emit the audit event, and shape the result.

    The one refusal constructor for :func:`promote_proposal`'s two gates
    — the immutable core and :class:`PromotionPolicy` — so the audit
    payload and the returned ``PromotionResult`` cannot drift between
    them. The payload is byte-identical to the one the policy branch
    emitted before this helper existed, ``effect_size`` included: a
    governing-key refusal passes ``None`` for it, which is the key the
    policy branch already wrote whenever the effect was incomputable.

    :func:`reject_proposal` deliberately does **not** route through here.
    Its payload carries ``manual: True`` and no ``effect_size`` at all,
    and folding it in would silently add a key to an audit event that
    already ships.
    """
    tuner_state.update_status(proposal_id, "rejected", notes=reason)
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
            "effect_size": effect.comparable_max if effect else None,
            "uncomparable_keys": list(effect.unbaselined_keys) if effect else [],
            "reason": reason,
        },
    )
    logger.info("tuner.promotion.rejected", proposal_id=proposal_id, reason=reason)
    return PromotionResult(
        proposal_id=proposal_id,
        status="rejected",
        reason=reason,
        effect_size=effect.comparable_max if effect else None,
        baseline_values=baseline_values,
    )


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

    # The immutable core, above the ``force`` branch on purpose: ``force``
    # is documented as skipping *the policy gate*, and a flag that also
    # lifts the one constraint separating a self-tuning loop from a loop
    # that tunes its own stop would make the constraint advisory. No
    # baseline is resolved first — no value of the effect size could
    # change this decision, and reading the store to decorate a refusal
    # is how a refusal comes to depend on store state.
    governing = governing_key_refusal(proposal.scope.component_id)
    if governing is not None:
        return _reject(
            proposal_id=proposal_id,
            proposal=proposal,
            reason=governing,
            effect=None,
            baseline_values=None,
            tuner_state=tuner_state,
            event_log=event_log,
            source=source,
        )

    baseline_snapshot = parameter_store.resolve(proposal.scope)
    baseline_values = baseline_snapshot.values if baseline_snapshot else None

    effect = _compute_effect_size(proposal.proposed_values, baseline_values)

    if not force:
        gate_result = _apply_policy(
            proposal=proposal,
            policy=effective_policy,
            baseline_values=baseline_values,
            effect=effect,
        )
        if gate_result is not None:
            return _reject(
                proposal_id=proposal_id,
                proposal=proposal,
                reason=gate_result,
                effect=effect,
                baseline_values=baseline_values,
                tuner_state=tuner_state,
                event_log=event_log,
                source=source,
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
            "effect_size": effect.comparable_max,
            "uncomparable_keys": list(effect.unbaselined_keys),
            "sample_size": proposal.sample_size,
            "tuner": proposal.tuner,
            "force": force,
        },
    )
    logger.info(
        "tuner.promotion.accepted",
        proposal_id=proposal_id,
        params_version=stored.params_version,
        effect_size=effect.comparable_max,
        force=force,
    )

    return PromotionResult(
        proposal_id=proposal_id,
        status="promoted",
        reason="ok",
        params_version=stored.params_version,
        effect_size=effect.comparable_max,
        baseline_values=baseline_values,
    )


def preview_promotion(
    proposal_id: str,
    *,
    tuner_state: TunerStateStore,
    parameter_store: ParameterStore,
    policy: PromotionPolicy | None = None,
    force: bool = False,
) -> PromotionPreview:
    """Forecast :func:`promote_proposal` without mutating or emitting.

    Runs validate + the policy gate exactly as :func:`promote_proposal`
    would, but writes nothing and emits no event. Returns a
    :class:`PromotionPreview` describing the *predicted* decision so a
    caller (the CLI dry-run, the API confirm step) can show the operator
    what would happen before they commit.

    The predicted ``status`` matches what a subsequent
    :func:`promote_proposal` call with the same ``policy`` / ``force``
    would produce, modulo store state changing in between.
    """
    effective_policy = policy or PromotionPolicy()

    proposal = tuner_state.get_proposal(proposal_id)
    if proposal is None:
        return PromotionPreview(
            proposal_id=proposal_id,
            status="skipped",
            reason="proposal_not_found",
            proposed_values={},
            baseline_values={},
        )

    if proposal.status in {"promoted", "rejected"}:
        return PromotionPreview(
            proposal_id=proposal_id,
            status="skipped",
            reason=f"proposal_already_{proposal.status}",
            proposed_values=dict(proposal.proposed_values),
            baseline_values={},
            sample_size=proposal.sample_size,
        )

    # Mirrors :func:`promote_proposal`'s immutable-core refusal, above the
    # ``force`` branch and above the baseline resolve, so the dry-run
    # cannot report a promotion the commit will refuse. A preview that
    # disagrees with its commit is the format/exit-parity failure (#437)
    # wearing different clothes.
    governing = governing_key_refusal(proposal.scope.component_id)
    if governing is not None:
        return PromotionPreview(
            proposal_id=proposal_id,
            status="rejected",
            reason=governing,
            proposed_values=dict(proposal.proposed_values),
            baseline_values={},
            sample_size=proposal.sample_size,
        )

    baseline_snapshot = parameter_store.resolve(proposal.scope)
    baseline_values = baseline_snapshot.values if baseline_snapshot else None

    effect = _compute_effect_size(proposal.proposed_values, baseline_values)
    reason = (
        None
        if force
        else _apply_policy(
            proposal=proposal,
            policy=effective_policy,
            baseline_values=baseline_values,
            effect=effect,
        )
    )
    predicted_status = "rejected" if reason else "promoted"
    return PromotionPreview(
        proposal_id=proposal_id,
        status=predicted_status,
        reason=reason or "ok",
        proposed_values=dict(proposal.proposed_values),
        baseline_values=dict(baseline_values or {}),
        effect_size=effect.comparable_max,
        sample_size=proposal.sample_size,
    )


def reject_proposal(
    proposal_id: str,
    *,
    tuner_state: TunerStateStore,
    event_log: EventLog,
    reason: str = "rejected_by_reviewer",
    source: str = "tuner.promotion",
) -> PromotionResult:
    """Human-gated rejection of a pending proposal.

    The tier-2 review surface (see ``docs/design/adr-autonomy-ladder.md``)
    lets an operator reject a proposal outright rather than promote it.
    This follows the same ``validate → update_status → emit`` shape as the
    policy-rejection branch of :func:`promote_proposal`, emitting a
    :class:`EventType.TUNER_PROPOSAL_REJECTED` event so the audit trail
    captures the manual decision.

    Args:
        proposal_id: The proposal to reject.
        tuner_state: Store holding the proposal record and its status.
        event_log: Destination for the
            :class:`EventType.TUNER_PROPOSAL_REJECTED` audit event.
        reason: Human-supplied rejection rationale; recorded on both the
            proposal's ``notes`` and the event payload.
        source: Event source label.
    """
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

    tuner_state.update_status(proposal_id, "rejected", notes=reason)
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
            "reason": reason,
            "manual": True,
        },
    )
    logger.info(
        "tuner.promotion.manual_rejected",
        proposal_id=proposal_id,
        reason=reason,
    )
    return PromotionResult(
        proposal_id=proposal_id,
        status="rejected",
        reason=reason,
    )


def _apply_policy(  # noqa: PLR0911 — gate is a straight-line decision tree; each branch maps to a distinct rejection reason.
    *,
    proposal: ParameterProposal,
    policy: PromotionPolicy,
    baseline_values: dict[str, Any] | None,
    effect: EffectSize,
) -> str | None:
    """Run the gate; return a rejection reason, or ``None`` on pass."""
    if proposal.sample_size < policy.min_sample_size:
        return (
            f"sample_size={proposal.sample_size} < "
            f"min_sample_size={policy.min_sample_size}"
        )

    comparable = effect.comparable_max
    unbaselined = effect.unbaselined_keys

    # Non-numeric change (string/bool).  Pass through when allowed.
    if effect.has_non_numeric and comparable is None and not unbaselined:
        if not policy.allow_non_numeric:
            return "non_numeric_change_disallowed"
        return None

    # No baseline: bootstrap case.
    if baseline_values is None:
        if policy.allow_no_baseline:
            return None
        return "no_baseline_snapshot_for_scope"

    if comparable is None:
        if unbaselined:
            # The scope has a snapshot, but it carries none of the proposed
            # keys — a per-key bootstrap. Distinct from "proposed equals
            # baseline", which would describe a wholly new proposal as a
            # no-op.
            if policy.allow_no_baseline:
                return None
            return "no_baseline_for_proposed_keys=" + ",".join(unbaselined)
        # All proposed values matched baseline exactly — nothing to do.
        return "zero_effect_proposed_equals_baseline"

    if comparable < policy.min_effect_size:
        reason = (
            f"effect_size={comparable:.4f} < min_effect_size={policy.min_effect_size}"
        )
        if unbaselined:
            # Name them: the floor was applied over a strict subset of the
            # proposal, and an operator reading the rejection is entitled
            # to know which keys it could not weigh.
            reason += " (uncomparable keys: " + ",".join(unbaselined) + ")"
        return reason
    return None
