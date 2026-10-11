"""Automatic promotion for learning candidates — report-only first (Tier 1).

Plan p1 PR 7. Owner's stated intent is "eventually we want automatic
promotion"; every switch here is tunable (see the
``learning.candidate_auto_promote.*`` entries in
:data:`trellis.core.settings_registry.SETTINGS_REGISTRY`) and ships
``enabled=False`` with ``mode="report_only"`` as the default, so this PR
produces evidence for picking thresholds rather than promoting anything on
its own.

Mirrors the shape of :mod:`trellis.learning.tuners.auto_promote` (the
tuner's Tier-1 autonomy gate for *parameter* promotion) without reusing its
code — the domain here is *learning-candidate* promotion to a precedent,
a different write path
(:func:`trellis.learning.scoring.submit_learning_promotion` vs.
:func:`trellis.learning.tuners.promotion.promote_proposal`) with a
different rollback primitive
(:data:`~trellis.mutate.commands.Operation.PRECEDENT_UPDATE` vs. a
parameter-set rollback). Tier-1 autonomy invariants, mapped onto this
policy exactly as ``auto_promote.py`` maps them onto parameter promotion:

(a) reversible through existing versioned state — demotion rewrites the
    promoted node via SCD-2 (``PrecedentUpdateHandler``), never a delete;
(b) post-change monitoring armed — :func:`sweep_auto_demotions` is that
    monitor, intended to run on the same schedule as promotion;
(c) dedicated audit events — :attr:`EventType.LEARNING_AUTO_PROMOTE_EVALUATED`
    self-identifies every run, autonomous or report-only;
(d) per-scope opt-in / global default OFF — ``enabled=False`` by default,
    and a disabled policy always behaves as ``report_only`` regardless of
    its configured ``mode`` (:attr:`LearningCandidateAutoPromotePolicy.effective_mode`).

Two data gaps are reported honestly here rather than silently assumed away:

**Holdout-arm coverage.** The brief asks for "a holdout arm present for
its intent family *if the data carries one*". It doesn't:
:mod:`trellis.core.pack_holdout` implements one global, per-pack holdout
rate — there is no per-intent-family holdout marker anywhere in the
pipeline. :func:`_holdout_arm_status` always returns ``"unknown"`` rather
than fabricate ``True``/``False``, and nothing in :func:`_evaluate_candidate`
gates on it — it is reported, never enforced.

**The agreement metric's disagree count.** :func:`compute_agreement_metric`
compares this policy's ``would_promote`` dispositions against later human
Review-queue decisions on the same candidate. Read against the real
``REVIEW_DECISION_RECORDED`` payload (``trellis_api.routes.admin``'s
``promote_learning_candidates`` route and ``prepare_learning_promotions``,
which silently drops every ``approved=False`` decision before building any
result row), there is no event anywhere that records a human *rejecting* a
candidate for this surface — only ``action="promote"`` exists, and its
``candidate_ids`` can only ever contain approved ids. ``disagree`` is
therefore always ``0`` today: a fact about what the system currently
records, not a shortcut taken here. ``AgreementMetric.rate`` is ``None``
(never a divide-by-zero) when ``agree + disagree == 0``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog

from trellis.core.write_config import TRUTHY
from trellis.learning.scoring import (
    PROMOTE_RECOMMENDATIONS,
    build_learning_promotion_payloads,
    submit_learning_promotion,
)
from trellis.mutate import Command, CommandStatus, MutationExecutor, Operation
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)

#: Requester identity stamped on every governed command this policy
#: submits — both the promotion (``ENTITY_CREATE`` / ``LINK_CREATE`` /
#: ``PRECEDENT_PROMOTE``, via :func:`submit_learning_promotion`) and the
#: demotion (``PRECEDENT_UPDATE``, via :func:`sweep_auto_demotions`). The
#: ``auto:`` prefix mirrors no existing convention exactly (CLI/API/MCP/
#: worker surfaces use ``<surface>:<verb>``) on purpose: an audit reader
#: filtering by ``requested_by`` should be able to tell "a human" from
#: "this policy" at a glance, the same way :func:`run_auto_promotion`
#: never reuses ``cli:``/``api:`` identities for its own promotions.
AUTO_PROMOTE_REQUESTED_BY = "auto:learning_candidate"

CandidateAutoPromoteMode = Literal["report_only", "commit"]

# --------------------------------------------------------------------------
# env / setting constants — mirror trellis.learning.tuners.auto_promote's
# naming scheme for the three ``learning.auto_promote.*`` knobs.
# --------------------------------------------------------------------------

CANDIDATE_AUTO_PROMOTE_ENABLED_ENV = "TRELLIS_ENABLE_CANDIDATE_AUTO_PROMOTE"
CANDIDATE_AUTO_PROMOTE_MODE_ENV = "TRELLIS_CANDIDATE_AUTO_PROMOTE_MODE"
CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV = (
    "TRELLIS_CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT"
)
CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV = (
    "TRELLIS_CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS"
)
CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_ENV = (
    "TRELLIS_CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL"
)
CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV = (
    "TRELLIS_CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN"
)

#: ``SETTINGS_REGISTRY`` keys for the same six knobs — dotted to mirror
#: ``learning.auto_promote.*``. Resolution order is env > settings >
#: default (no ``config.yaml`` layer: this policy has no legacy
#: file-based config to shadow).
CANDIDATE_AUTO_PROMOTE_ENABLED_SETTING = "learning.candidate_auto_promote.enabled"
CANDIDATE_AUTO_PROMOTE_MODE_SETTING = "learning.candidate_auto_promote.mode"
CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING = (
    "learning.candidate_auto_promote.min_helpful_count"
)
CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_SETTING = (
    "learning.candidate_auto_promote.min_distinct_sessions"
)
CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_SETTING = (
    "learning.candidate_auto_promote.require_zero_unhelpful"
)
CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_SETTING = (
    "learning.candidate_auto_promote.max_promotions_per_run"
)

#: Floors enforced by :meth:`LearningCandidateAutoPromotePolicy.__post_init__`
#: — identical to the ``minimum=`` values registered for the three int
#: knobs in ``SETTINGS_REGISTRY``. ``MIN_DISTINCT_SESSIONS_FLOOR = 2`` is
#: what makes a single-session (n=1) candidate structurally unable to
#: qualify, regardless of configuration.
MIN_HELPFUL_COUNT_FLOOR = 1
MIN_DISTINCT_SESSIONS_FLOOR = 2
MAX_PROMOTIONS_PER_RUN_FLOOR = 1

#: Conservative defaults — see each ``SettingSpec.description`` in
#: ``settings_registry.py`` for the operator-facing justification.
DEFAULT_MIN_HELPFUL_COUNT = 5
DEFAULT_MIN_DISTINCT_SESSIONS = 3
DEFAULT_REQUIRE_ZERO_UNHELPFUL = True
DEFAULT_MAX_PROMOTIONS_PER_RUN = 3

#: Bounds on how far back :func:`sweep_auto_demotions` looks for this
#: policy's own promotions when the caller gives no explicit ``since`` —
#: mirrors :func:`trellis.learning.tuners.auto_promote._sweep_prior_promotions`'s
#: 30-day default window.
_DEFAULT_DEMOTION_SWEEP_WINDOW = timedelta(days=30)
_MUTATION_SCAN_LIMIT = 500
_FEEDBACK_SCAN_LIMIT = 2000
_AGREEMENT_SCAN_LIMIT = 500


@dataclass(frozen=True, slots=True)
class LearningCandidateAutoPromotePolicy:
    """Per-scope opt-in gate for unattended learning-candidate promotion.

    Attributes:
        enabled: Master switch. ``False`` by default — global default OFF
            per tier-1 invariant (d). When ``False``,
            :attr:`effective_mode` is always ``"report_only"`` regardless
            of ``mode``, so a run never writes.
        mode: ``"report_only"`` (default) records what *would* be
            promoted without writing anything; ``"commit"`` actually
            submits qualifying candidates through
            :func:`trellis.learning.scoring.submit_learning_promotion`.
            Only takes effect when ``enabled`` is ``True``.
        min_helpful_count: Minimum helpful-citation count
            (``citation_evidence.helpful_count``) for auto-promotion.
            There is no manual-review equivalent floor to match (a human
            reviewer judges case-by-case); the default of 5 is a
            deliberately conservative starting point for gathering
            report-only evidence, not a measured threshold.
        min_distinct_sessions: Minimum number of distinct sessions whose
            feedback cited the candidate helpful
            (``citation_evidence.distinct_helpful_sessions``). Floored at
            2 — a single-session (n=1) candidate can never qualify for
            unattended promotion, no matter how it is configured.
        require_zero_unhelpful: When ``True`` (default), any unhelpful
            citation (``citation_evidence.unhelpful_count > 0``) disqualifies
            the candidate. This is the one knob that can make auto-promote
            disagree with what a human reviewer might still approve (a
            human can see *context* behind an unhelpful citation; this
            policy cannot) — setting it ``False`` is a deliberate,
            registered-as-unsafe loosening.
        max_promotions_per_run: Caps how many candidates a single
            ``commit``-mode run may promote, bounding the blast radius of
            one scheduled job run regardless of how many candidates
            qualify.

    Raises:
        ValueError: If ``mode`` is not ``"report_only"``/``"commit"``, or
            if any int knob is looser than its floor.
    """

    enabled: bool = False
    mode: CandidateAutoPromoteMode = "report_only"
    min_helpful_count: int = DEFAULT_MIN_HELPFUL_COUNT
    min_distinct_sessions: int = DEFAULT_MIN_DISTINCT_SESSIONS
    require_zero_unhelpful: bool = DEFAULT_REQUIRE_ZERO_UNHELPFUL
    max_promotions_per_run: int = DEFAULT_MAX_PROMOTIONS_PER_RUN

    def __post_init__(self) -> None:
        if self.mode not in ("report_only", "commit"):
            msg = (
                f"LearningCandidateAutoPromotePolicy.mode={self.mode!r} must "
                'be "report_only" or "commit".'
            )
            raise ValueError(msg)
        if self.min_helpful_count < MIN_HELPFUL_COUNT_FLOOR:
            msg = (
                f"LearningCandidateAutoPromotePolicy.min_helpful_count="
                f"{self.min_helpful_count} is looser than the floor "
                f"({MIN_HELPFUL_COUNT_FLOOR}); the autonomous gate must stay "
                "at least this strict."
            )
            raise ValueError(msg)
        if self.min_distinct_sessions < MIN_DISTINCT_SESSIONS_FLOOR:
            msg = (
                f"LearningCandidateAutoPromotePolicy.min_distinct_sessions="
                f"{self.min_distinct_sessions} is looser than the floor "
                f"({MIN_DISTINCT_SESSIONS_FLOOR}); a single-session (n=1) "
                "candidate must never qualify for unattended promotion."
            )
            raise ValueError(msg)
        if self.max_promotions_per_run < MAX_PROMOTIONS_PER_RUN_FLOOR:
            msg = (
                f"LearningCandidateAutoPromotePolicy.max_promotions_per_run="
                f"{self.max_promotions_per_run} is looser than the floor "
                f"({MAX_PROMOTIONS_PER_RUN_FLOOR})."
            )
            raise ValueError(msg)

    @property
    def effective_mode(self) -> CandidateAutoPromoteMode:
        """``mode`` only takes effect when ``enabled``.

        Mirrors how ``AutoPromotePolicy.enabled`` gates
        ``run_auto_promotion``'s write path: a disabled policy always
        behaves as ``report_only``, whatever ``mode`` is configured to,
        so flipping ``mode`` to ``commit`` ahead of flipping ``enabled``
        is a safe no-op rather than a trap an operator can spring by
        reordering two settings writes.
        """
        if not self.enabled:
            return "report_only"
        return self.mode


def resolve_candidate_auto_promote_overrides(
    *,
    settings: Mapping[str, bool | int | float | str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, bool | int | str]:
    """Resolve the six candidate-auto-promote knobs: env > settings > default.

    No ``config.yaml`` layer, unlike
    :func:`trellis.learning.tuners.auto_promote.resolve_auto_promote_overrides`
    — this policy has no legacy file-based config to shadow (see the
    ``learning.candidate_auto_promote.*`` registration note in
    ``settings_registry.py``'s module docstring).

    Resolves precedence only. The caller still constructs
    :class:`LearningCandidateAutoPromotePolicy` with the result, and
    *that* construction is what rejects a value looser than the floors
    (:meth:`LearningCandidateAutoPromotePolicy.__post_init__`) — this
    function never duplicates that check.
    """
    src_env: Mapping[str, str] = os.environ if env is None else env
    settings_map: Mapping[str, bool | int | float | str] = settings or {}

    def resolve_bool(env_name: str, setting_name: str, default: bool) -> bool:
        raw = src_env.get(env_name, "")
        if raw.strip():
            return raw.strip().lower() in TRUTHY
        if setting_name in settings_map:
            return bool(settings_map[setting_name])
        return default

    def resolve_int(env_name: str, setting_name: str, default: int) -> int:
        raw = src_env.get(env_name, "")
        if raw.strip():
            return int(float(raw))
        if setting_name in settings_map:
            return int(settings_map[setting_name])  # type: ignore[arg-type]
        return default

    def resolve_str(env_name: str, setting_name: str, default: str) -> str:
        raw = src_env.get(env_name, "")
        if raw.strip():
            return raw.strip()
        if setting_name in settings_map:
            return str(settings_map[setting_name])
        return default

    return {
        "enabled": resolve_bool(
            CANDIDATE_AUTO_PROMOTE_ENABLED_ENV,
            CANDIDATE_AUTO_PROMOTE_ENABLED_SETTING,
            False,
        ),
        "mode": resolve_str(
            CANDIDATE_AUTO_PROMOTE_MODE_ENV,
            CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
            "report_only",
        ),
        "min_helpful_count": resolve_int(
            CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV,
            CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING,
            DEFAULT_MIN_HELPFUL_COUNT,
        ),
        "min_distinct_sessions": resolve_int(
            CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV,
            CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_SETTING,
            DEFAULT_MIN_DISTINCT_SESSIONS,
        ),
        "require_zero_unhelpful": resolve_bool(
            CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_ENV,
            CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_SETTING,
            DEFAULT_REQUIRE_ZERO_UNHELPFUL,
        ),
        "max_promotions_per_run": resolve_int(
            CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV,
            CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_SETTING,
            DEFAULT_MAX_PROMOTIONS_PER_RUN,
        ),
    }


def _holdout_arm_status(candidate: Mapping[str, Any]) -> str:  # noqa: ARG001
    """Whether this candidate's intent family has a holdout arm.

    **Documented data gap**: :mod:`trellis.core.pack_holdout` implements
    one global, per-pack holdout rate — no per-intent-family "holdout
    arm" concept exists anywhere in the pipeline today. This function
    never fabricates one; it always returns ``"unknown"`` so a report
    reader sees an honest gap rather than a silently-assumed
    ``True``/``False``. Non-gating: :func:`_evaluate_candidate` never
    reads this field. Kept as its own function (rather than inlined) so
    the day a per-intent-family holdout marker exists, exactly one call
    site changes.
    """
    return "unknown"


def _evaluate_candidate(
    candidate: Mapping[str, Any], *, policy: LearningCandidateAutoPromotePolicy
) -> tuple[bool, str]:
    """Qualify one candidate against the policy's floors.

    Returns ``(True, "qualifies")`` or ``(False, "ineligible:<reason>")``.
    Checked in this order, so a report reader scanning
    ``"ineligible:not_promotable"`` never also has to wonder whether the
    citation evidence was fine:

    1. ``not_promotable`` — ``recommendation_type`` not in
       :data:`~trellis.learning.scoring.PROMOTE_RECOMMENDATIONS`.
    2. ``unhelpful_citations`` — ``require_zero_unhelpful`` and
       ``unhelpful_count > 0``.
    3. ``insufficient_helpful_count`` — below ``min_helpful_count``.
    4. ``insufficient_distinct_sessions`` — below ``min_distinct_sessions``
       (this is the check that refuses a single-session n=1 candidate).
    """
    recommendation_type = str(candidate.get("recommendation_type", "")).strip()
    if recommendation_type not in PROMOTE_RECOMMENDATIONS:
        return False, "ineligible:not_promotable"

    evidence = candidate.get("citation_evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    helpful_count = int(evidence.get("helpful_count", 0) or 0)
    unhelpful_count = int(evidence.get("unhelpful_count", 0) or 0)
    distinct_sessions = int(evidence.get("distinct_helpful_sessions", 0) or 0)

    if policy.require_zero_unhelpful and unhelpful_count > 0:
        return False, "ineligible:unhelpful_citations"
    if helpful_count < policy.min_helpful_count:
        return False, "ineligible:insufficient_helpful_count"
    if distinct_sessions < policy.min_distinct_sessions:
        return False, "ineligible:insufficient_distinct_sessions"

    return True, "qualifies"


@dataclass(frozen=True, slots=True)
class CandidateAutoPromoteOutcome:
    """What happened to one candidate (or prior promotion) during a run."""

    candidate_id: str
    #: ``"would_promote"`` — qualified, run was report-only (or disabled);
    #: ``"promoted"`` — qualified and committed this run;
    #: ``"ineligible:<reason>"`` — did not clear the gate (see
    #: :func:`_evaluate_candidate` and the run cap check);
    #: ``"demoted"`` — (from :func:`sweep_auto_demotions`) a prior
    #: auto-promotion was rolled back on its first unhelpful citation.
    disposition: str
    reason: str = ""
    entity_id: str | None = None
    holdout_arm: str = "unknown"


@dataclass(frozen=True, slots=True)
class CandidateAutoPromoteReport:
    """Aggregate result of one :func:`run_candidate_auto_promotion` pass."""

    enabled: bool
    #: What actually happened this run — always ``"report_only"`` when
    #: ``enabled`` is ``False``. See
    #: :attr:`LearningCandidateAutoPromotePolicy.effective_mode`.
    mode: CandidateAutoPromoteMode
    #: What the policy was configured to, before any forcing — lets a
    #: reader tell "configured for commit but ran report-only because
    #: disabled" apart from "configured and ran report-only".
    configured_mode: CandidateAutoPromoteMode
    outcomes: list[CandidateAutoPromoteOutcome] = field(default_factory=list)
    event_id: str | None = None

    @property
    def would_promote_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "would_promote")

    @property
    def promoted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "promoted")

    @property
    def ineligible_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition.startswith("ineligible:"))


def _emit_evaluated_event(
    event_log: EventLog,
    *,
    outcomes: Sequence[CandidateAutoPromoteOutcome],
    policy: LearningCandidateAutoPromotePolicy,
    effective_mode: CandidateAutoPromoteMode,
    configured_mode: CandidateAutoPromoteMode,
    source: str,
) -> str | None:
    """Emit the single ``LEARNING_AUTO_PROMOTE_EVALUATED`` event for a run."""
    would_promote = sum(1 for o in outcomes if o.disposition == "would_promote")
    promoted = sum(1 for o in outcomes if o.disposition == "promoted")
    ineligible = sum(1 for o in outcomes if o.disposition.startswith("ineligible:"))
    payload: dict[str, Any] = {
        "mode": effective_mode,
        "configured_mode": configured_mode,
        "dispositions": [
            {
                "candidate_id": o.candidate_id,
                "disposition": o.disposition,
                "reason": o.reason,
                "entity_id": o.entity_id,
                "holdout_arm": o.holdout_arm,
            }
            for o in outcomes
        ],
        "counts": {
            "total": len(outcomes),
            "would_promote": would_promote,
            "promoted": promoted,
            "ineligible": ineligible,
        },
        "policy_snapshot": {
            "enabled": policy.enabled,
            "mode": policy.mode,
            "min_helpful_count": policy.min_helpful_count,
            "min_distinct_sessions": policy.min_distinct_sessions,
            "require_zero_unhelpful": policy.require_zero_unhelpful,
            "max_promotions_per_run": policy.max_promotions_per_run,
        },
    }
    event = event_log.emit(
        EventType.LEARNING_AUTO_PROMOTE_EVALUATED,
        source,
        payload=payload,
    )
    return event.event_id


def run_candidate_auto_promotion(
    *,
    candidates: Sequence[Mapping[str, Any]],
    policy: LearningCandidateAutoPromotePolicy,
    event_log: EventLog,
    executor: MutationExecutor | None = None,
    requested_by: str = AUTO_PROMOTE_REQUESTED_BY,
    source: str = "candidate_auto_promote",
    force_report_only: bool = False,
) -> CandidateAutoPromoteReport:
    """Evaluate every candidate and, in commit mode, promote the qualifying ones.

    ``force_report_only=True`` is how ``trellis curate
    auto-promote-candidates --dry-run`` overrides the configured
    ``mode`` regardless of what the policy resolved to — the flag is
    checked ahead of :attr:`LearningCandidateAutoPromotePolicy.effective_mode`,
    never mixed into the policy object itself, so a dry-run can never be
    mistaken for a persisted configuration change.

    Each candidate gets exactly one :class:`CandidateAutoPromoteOutcome`.
    Exactly one :attr:`EventType.LEARNING_AUTO_PROMOTE_EVALUATED` event is
    emitted per call, covering every candidate in ``candidates`` — report-
    only and commit runs both emit it, so a report-only run is visible in
    the audit trail even though it writes nothing else.

    Raises:
        ValueError: If the effective mode is ``"commit"`` but no
            ``executor`` was given.
    """
    configured_mode = policy.mode
    effective_mode: CandidateAutoPromoteMode = (
        "report_only" if force_report_only else policy.effective_mode
    )
    if effective_mode == "commit" and executor is None:
        msg = (
            "run_candidate_auto_promotion: effective mode is 'commit' but "
            "no executor was given."
        )
        raise ValueError(msg)

    outcomes: list[CandidateAutoPromoteOutcome] = []
    promotions_committed = 0

    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        holdout_arm = _holdout_arm_status(candidate)
        qualifies, reason = _evaluate_candidate(candidate, policy=policy)

        if not qualifies:
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=candidate_id,
                    disposition=reason,
                    holdout_arm=holdout_arm,
                )
            )
            continue

        if effective_mode != "commit":
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=candidate_id,
                    disposition="would_promote",
                    reason=reason,
                    holdout_arm=holdout_arm,
                )
            )
            continue

        if promotions_committed >= policy.max_promotions_per_run:
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=candidate_id,
                    disposition="ineligible:run_cap_reached",
                    holdout_arm=holdout_arm,
                )
            )
            continue

        assert executor is not None  # narrowed by the ValueError guard above
        promotion_name = str(candidate.get("precedent_name", "")).strip()
        evidence = candidate.get("citation_evidence")
        if not isinstance(evidence, Mapping):
            evidence = {}
        rationale = (
            "Auto-promoted by trellis.learning.candidate_auto_promote: "
            f"helpful_count={evidence.get('helpful_count')}, "
            f"distinct_helpful_sessions={evidence.get('distinct_helpful_sessions')}, "
            f"unhelpful_count={evidence.get('unhelpful_count')}."
        )
        promotion = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name=promotion_name,
            rationale=rationale,
        )
        submission = submit_learning_promotion(
            executor,
            promotion["entity_payload"],
            promotion["edge_payloads"],
            requested_by=requested_by,
        )
        if submission.get("status") == "promoted":
            promotions_committed += 1
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=candidate_id,
                    disposition="promoted",
                    reason=reason,
                    entity_id=promotion.get("entity_id"),
                    holdout_arm=holdout_arm,
                )
            )
        else:
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=candidate_id,
                    disposition=f"ineligible:submission_{submission.get('status')}",
                    entity_id=promotion.get("entity_id"),
                    holdout_arm=holdout_arm,
                )
            )

    event_id = _emit_evaluated_event(
        event_log,
        outcomes=outcomes,
        policy=policy,
        effective_mode=effective_mode,
        configured_mode=configured_mode,
        source=source,
    )

    return CandidateAutoPromoteReport(
        enabled=policy.enabled,
        mode=effective_mode,
        configured_mode=configured_mode,
        outcomes=outcomes,
        event_id=event_id,
    )


def sweep_auto_demotions(
    *,
    event_log: EventLog,
    executor: MutationExecutor,
    requested_by: str = AUTO_PROMOTE_REQUESTED_BY,
    now: datetime | None = None,
    since: datetime | None = None,
    limit: int = _MUTATION_SCAN_LIMIT,
) -> list[CandidateAutoPromoteOutcome]:
    """Demote, through the governed path, every auto-promotion with an
    unhelpful citation.

    Tier-1 invariant (b): post-change monitoring armed. Nothing in
    :func:`run_candidate_auto_promotion`'s write path watches for a bad
    outcome after the fact — this sweep is that watch. Run it on the same
    schedule as promotion (or more often) and a precedent this policy
    promoted gets rolled back, governed, on its first unhelpful signal.

    **Identifying "this policy's own promotions" without a new store.**
    Reads ``MUTATION_EXECUTED`` events for
    ``operation="precedent.promote"``, ``requested_by=<requested_by>``,
    ``status="success"`` — :class:`~trellis.mutate.executor.MutationExecutor`'s
    own audit event already carries exactly this (``command.target_id``,
    stamped onto the event as ``entity_id``, is the precedent's durable
    graph id, since :class:`~trellis.mutate.handlers.PrecedentPromoteHandler`
    sets ``entity_id=command.target_id``) — no new tracking state is
    needed, and this function only *reads* events and *writes* only
    through a governed :data:`~trellis.mutate.commands.Operation.PRECEDENT_UPDATE`
    command — never the EventLog directly.

    **Detecting "an unhelpful citation" — documented gap.** Scans
    ``FEEDBACK_RECORDED`` events globally for
    ``entity_id in payload["unhelpful_item_ids"]``. No dedicated
    "precedent citation" tracking exists; this is the best signal
    available today (confirmed against
    :func:`trellis.feedback.attribution.stray_citations`, which shows
    feedback can legitimately cite an id outside any
    ``PackBuilder``-assembled pack, e.g. one served by ``get_lessons``),
    not a joined, audited citation record.

    Already-demoted precedents (an existing ``PRECEDENT_UPDATED`` event
    with ``payload["demoted"]`` true for that ``entity_id``) are skipped,
    so a second sweep never re-submits a demotion for the same precedent.
    """
    effective_since = (
        since or (now or datetime.now(UTC)) - _DEFAULT_DEMOTION_SWEEP_WINDOW
    )

    own_promotions = event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED,
        since=effective_since,
        payload_filters={
            "operation": Operation.PRECEDENT_PROMOTE.value,
            "requested_by": requested_by,
            "status": "success",
        },
        limit=limit,
    )
    promoted_ids = {event.entity_id for event in own_promotions if event.entity_id}
    if not promoted_ids:
        return []

    demoted_events = event_log.get_events(
        event_type=EventType.PRECEDENT_UPDATED,
        since=effective_since,
        limit=limit,
    )
    already_demoted = {
        event.entity_id
        for event in demoted_events
        if event.entity_id and bool((event.payload or {}).get("demoted"))
    }
    pending = promoted_ids - already_demoted
    if not pending:
        return []

    feedback_events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        since=effective_since,
        limit=_FEEDBACK_SCAN_LIMIT,
    )
    unhelpful_ids: set[str] = set()
    for event in feedback_events:
        unhelpful_ids.update(
            str(item_id)
            for item_id in (event.payload or {}).get("unhelpful_item_ids") or []
        )

    outcomes: list[CandidateAutoPromoteOutcome] = []
    for entity_id in sorted(pending & unhelpful_ids):
        command = Command(
            operation=Operation.PRECEDENT_UPDATE,
            args={
                "precedent_id": entity_id,
                "reason": (
                    "auto-demoted: first unhelpful citation after an auto-promotion"
                ),
            },
            target_id=entity_id,
            target_type="entity",
            requested_by=requested_by,
        )
        result = executor.execute(command)
        if result.status == CommandStatus.SUCCESS:
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=entity_id,
                    disposition="demoted",
                    entity_id=entity_id,
                )
            )
        else:
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=entity_id,
                    disposition=f"ineligible:demotion_{result.status.value}",
                    entity_id=entity_id,
                )
            )
            logger.warning(
                "candidate_auto_promote.demotion_failed",
                entity_id=entity_id,
                status=result.status.value,
            )
    return outcomes


@dataclass(frozen=True, slots=True)
class AgreementMetric:
    """Agreement between this policy's ``would_promote`` calls and later
    human Review-queue decisions on the same candidate.

    ``disagree`` is, as of today's data, structurally always ``0`` — see
    the module docstring's "agreement metric's disagree count" section.
    ``rate`` is ``None`` (never a divide-by-zero) when
    ``agree + disagree == 0``.
    """

    agree: int
    disagree: int
    undecided: int
    rate: float | None


def compute_agreement_metric(
    event_log: EventLog,
    *,
    since: datetime | None = None,
    limit: int = _AGREEMENT_SCAN_LIMIT,
) -> AgreementMetric:
    """Compare past ``would_promote`` dispositions against human decisions.

    Reads every candidate this policy ever marked ``would_promote`` from
    past :attr:`EventType.LEARNING_AUTO_PROMOTE_EVALUATED` events, and
    every candidate id a human has since approved from
    :attr:`EventType.REVIEW_DECISION_RECORDED` events for
    ``surface="learning_promotion"`` / ``action="promote"``. ``agree`` is
    the intersection; ``undecided`` is everything this policy flagged that
    no human decision has touched yet (approved or rejected). ``disagree``
    is always ``0`` today — no event records a human rejecting a candidate
    for this surface (see module docstring).
    """
    evaluated_events = event_log.get_events(
        event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED,
        since=since,
        limit=limit,
    )
    would_promote_ids: set[str] = set()
    for event in evaluated_events:
        for row in (event.payload or {}).get("dispositions") or []:
            if isinstance(row, Mapping) and row.get("disposition") == "would_promote":
                candidate_id = row.get("candidate_id")
                if isinstance(candidate_id, str) and candidate_id:
                    would_promote_ids.add(candidate_id)

    if not would_promote_ids:
        return AgreementMetric(agree=0, disagree=0, undecided=0, rate=None)

    decision_events = event_log.get_events(
        event_type=EventType.REVIEW_DECISION_RECORDED,
        since=since,
        payload_filters={"surface": "learning_promotion", "action": "promote"},
        limit=limit,
    )
    approved_ids: set[str] = set()
    for event in decision_events:
        for candidate_id in (event.payload or {}).get("candidate_ids") or []:
            if isinstance(candidate_id, str) and candidate_id:
                approved_ids.add(candidate_id)

    # No "reject" event exists for this surface today — `disagree` is
    # always 0 as a fact about the data, never a shortcut taken here.
    agree = len(would_promote_ids & approved_ids)
    disagree = 0
    undecided = len(would_promote_ids - approved_ids)
    denom = agree + disagree
    rate = (agree / denom) if denom else None
    return AgreementMetric(
        agree=agree, disagree=disagree, undecided=undecided, rate=rate
    )


def _outcome_to_dict(outcome: CandidateAutoPromoteOutcome) -> dict[str, Any]:
    """Serialise a :class:`CandidateAutoPromoteOutcome` for ``--format json``."""
    return {
        "candidate_id": outcome.candidate_id,
        "disposition": outcome.disposition,
        "reason": outcome.reason,
        "entity_id": outcome.entity_id,
        "holdout_arm": outcome.holdout_arm,
    }


def report_to_dict(report: CandidateAutoPromoteReport) -> dict[str, Any]:
    """Serialise a :class:`CandidateAutoPromoteReport` for ``--format json``."""
    return {
        "enabled": report.enabled,
        "mode": report.mode,
        "configured_mode": report.configured_mode,
        "would_promote_count": report.would_promote_count,
        "promoted_count": report.promoted_count,
        "ineligible_count": report.ineligible_count,
        "event_id": report.event_id,
        "outcomes": [_outcome_to_dict(o) for o in report.outcomes],
    }


def agreement_metric_to_dict(metric: AgreementMetric) -> dict[str, Any]:
    """Serialise an :class:`AgreementMetric` for ``--format json``."""
    return {
        "agree": metric.agree,
        "disagree": metric.disagree,
        "undecided": metric.undecided,
        "rate": metric.rate,
    }
