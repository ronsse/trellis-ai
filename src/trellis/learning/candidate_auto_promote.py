"""Automatic promotion for learning candidates — report-only (Tier 1).

Owner's stated intent is "eventually we want automatic promotion"; every
switch here is tunable (see the ``learning.candidate_auto_promote.*``
entries in :data:`trellis.core.settings_registry.SETTINGS_REGISTRY`) and
ships ``enabled=False`` with ``mode="report_only"`` as the default, so this
module produces evidence for picking thresholds rather than promoting
anything unattended.

**Scope: evaluation only.** This module never commits a promotion and
never demotes anything. ``mode="commit"`` is refused at construction
(:meth:`LearningCandidateAutoPromotePolicy.__post_init__` raises
:class:`~trellis.errors.ConfigError`) on every path that builds the policy
— unattended commit, and the auto-demotion rollback a safe commit mode
would depend on, are a follow-up. ``report_only`` is the only mode that
can run today.

Mirrors the shape of :mod:`trellis.learning.tuners.auto_promote` (the
tuner's Tier-1 autonomy gate for *parameter* promotion) without reusing its
code — the domain here is *learning-candidate* promotion to a precedent,
a different write path
(:func:`trellis.learning.scoring.submit_learning_promotion` vs.
:func:`trellis.learning.tuners.promotion.promote_proposal`). Tier-1
autonomy invariants this module satisfies today:

(c) dedicated audit events — :attr:`EventType.LEARNING_AUTO_PROMOTE_EVALUATED`
    self-identifies every run, including one that never reaches
    :func:`run_candidate_auto_promotion` at all
    (:func:`emit_candidate_auto_promote_failure_event`, called from
    ``trellis_cli.curate.auto_promote_candidates`` on every exit path);
(d) per-scope opt-in / global default OFF — ``enabled=False`` by default,
    and a disabled policy always behaves as ``report_only`` regardless of
    its configured ``mode`` (:attr:`LearningCandidateAutoPromotePolicy.effective_mode`).

Invariants (a) reversible rollback and (b) post-change monitoring are what
a safe commit mode needs and do not exist yet — that is exactly why commit
mode is refused rather than shipped half-built.

**Idempotency.** :func:`run_candidate_auto_promotion` never proposes a
candidate that already has a precedent, promoted by a human or by an
earlier automatic run: it scans ``MUTATION_EXECUTED`` for every successful
``precedent.promote`` and matches on the deterministic id from
:func:`trellis.learning.scoring.precedent_entity_id_for_candidate`. A match
is reported as ``already_promoted`` and excluded from
``would_promote_count`` and from the per-run cap, so an existing precedent
(human or automatic) is never double-counted or crowded out by its own
re-evaluation.

Three data gaps are reported honestly here rather than silently assumed away:

**Holdout-arm coverage.** Reporting a holdout arm for a candidate's
intent family would need a per-intent-family holdout marker. None
exists: :mod:`trellis.core.pack_holdout` implements one global,
per-pack holdout rate — there is no per-intent-family holdout marker
anywhere in the pipeline. :func:`_holdout_arm_status` always returns
``"unknown"`` rather than fabricate ``True``/``False``, and nothing in
:func:`_evaluate_candidate` gates on it — it is reported, never enforced.

**Session identity.** ``citation_evidence.distinct_helpful_sessions``
(:mod:`trellis.learning.scoring`) counts real ``PACK_ASSEMBLED.session_id``
values only — never a run or pack id, and never a feedback row with no
session id folded into one shared pseudo-session. A single conversation
that asks repeatedly and grades its own packs helpful cannot read as
several independent sessions, and a helpful citation with no captured
session id is reported separately (``sessionless_citations``) rather than
silently discounted or silently counted.

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
(never a divide-by-zero) when ``agree + disagree == 0``, and is otherwise
always exactly ``1.0`` for the same reason — :func:`agreement_metric_to_dict`
carries the raw ``agree``/``disagree``/``undecided`` counts and an explicit
note alongside ``rate`` so a reader never mistakes a structural ceiling for
a measured agreement rate.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import structlog

from trellis.core.write_config import TRUTHY, parse_finite_int
from trellis.errors import ConfigError
from trellis.learning.scoring import (
    PROMOTE_RECOMMENDATIONS,
    build_learning_promotion_payloads,
    precedent_entity_id_for_candidate,
    submit_learning_promotion,
)
from trellis.mutate import MutationExecutor, Operation
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)

#: Requester identity stamped on every governed command this policy
#: submits — the promotion (``ENTITY_CREATE`` / ``LINK_CREATE`` /
#: ``PRECEDENT_PROMOTE``, via :func:`submit_learning_promotion`). Demotion
#: is not implemented here yet (see the module docstring's "Scope:
#: evaluation only"); when it ships it should stamp the same identity. The
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

#: Scan limit for the already-promoted check in
#: :func:`run_candidate_auto_promotion` — read newest-first (``order="desc"``)
#: so a truncated scan on a busy log drops the oldest promotions, never the
#: most recent ones (see ``EventLog.scan_events``'s newest-first idiom).
_PROMOTION_SCAN_LIMIT = 500
_AGREEMENT_SCAN_LIMIT = 500


@dataclass(frozen=True, slots=True)
class LearningCandidateAutoPromotePolicy:
    """Per-scope opt-in gate for unattended learning-candidate promotion.

    Attributes:
        enabled: Master switch. ``False`` by default — global default OFF
            per tier-1 invariant (d). When ``False``,
            :attr:`effective_mode` is always ``"report_only"`` regardless
            of ``mode``, so a run never writes.
        mode: ``"report_only"`` (default, and the only mode that can
            construct) records what *would* be promoted without writing
            anything. ``"commit"`` is reserved for a follow-up that adds
            auto-demotion as commit mode's rollback; constructing this
            policy with ``mode="commit"`` always raises
            :class:`~trellis.errors.ConfigError`, regardless of
            ``enabled``.
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
        ConfigError: If ``mode="commit"`` — commit mode is not available
            until auto-demotion ships.
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
        if self.mode == "commit":
            # Refused unconditionally — regardless of ``enabled`` — because
            # this is the only place a policy comes into existence
            # (``trellis_cli.curate._build_candidate_auto_promote_policy``
            # today). Commit mode needs a reversible rollback (Tier-1
            # invariant (a)) and post-change monitoring (invariant (b));
            # neither exists yet, so there is no safe way to let this
            # resolve. See the module docstring's "Scope: evaluation only".
            msg = "commit mode is not available until auto-demotion ships"
            raise ConfigError(msg, setting=CANDIDATE_AUTO_PROMOTE_MODE_SETTING)
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
        ``run_auto_promotion``'s write path: once a policy exists, a
        disabled policy always behaves as ``report_only``, whatever
        ``mode`` is configured to. But a policy with ``mode="commit"``
        never gets that far: ``__post_init__`` refuses it unconditionally,
        regardless of ``enabled``. So flipping ``mode`` to ``commit`` is
        never a safe no-op — it fails policy construction, and therefore
        the scheduled job, on the next run whether or not ``enabled`` is
        set.
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
            return parse_finite_int(raw)
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
    candidate: Mapping[str, Any],
    *,
    policy: LearningCandidateAutoPromotePolicy,
    already_promoted: bool = False,
) -> tuple[bool, str]:
    """Qualify one candidate against the policy's floors.

    Returns ``(True, "qualifies")`` or ``(False, "<disposition>")``. Checked
    in this order, so a report reader scanning
    ``"ineligible:not_promotable"`` never also has to wonder whether the
    citation evidence was fine:

    0. ``already_promoted`` — a precedent already exists for this
       candidate's deterministic entity id (human-approved or from an
       earlier automatic run). Its own disposition, not nested under
       ``"ineligible:*"``, so it is never counted against the per-run cap
       — see :func:`run_candidate_auto_promotion`.
    1. ``ineligible:not_promotable`` — ``recommendation_type`` not in
       :data:`~trellis.learning.scoring.PROMOTE_RECOMMENDATIONS`.
    2. ``ineligible:unhelpful_citations`` — ``require_zero_unhelpful`` and
       ``unhelpful_count > 0``.
    3. ``ineligible:insufficient_helpful_count`` — below ``min_helpful_count``.
    4. ``ineligible:insufficient_distinct_sessions`` — below
       ``min_distinct_sessions`` (this is the check that refuses a
       single-session n=1 candidate).
    """
    if already_promoted:
        return False, "already_promoted"

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
    #: ``"promoted"`` — qualified and committed this run (unreachable today
    #: — see the module docstring's "Scope: evaluation only");
    #: ``"already_promoted"`` — a precedent already exists for this
    #: candidate, human-approved or from an earlier automatic run;
    #: ``"ineligible:<reason>"`` — did not clear the gate (see
    #: :func:`_evaluate_candidate` and the run cap check).
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
    #: Candidates with no usable ``candidate_id`` -- these get no
    #: :class:`CandidateAutoPromoteOutcome` at all (there is nothing to
    #: name it with), so unlike every other count on this report,
    #: ``skipped_count`` cannot be derived from ``outcomes`` after the
    #: fact. A run with a nonzero count here evaluated fewer candidates
    #: than it was given, silently by the loop's own logic -- see
    #: :func:`run_candidate_auto_promotion`'s module docstring note and
    #: ``loop_health.py``'s ``last_status`` derivation, which reads this
    #: same count off the emitted event to avoid reporting "ok" for a run
    #: that dropped input.
    skipped_count: int = 0

    @property
    def would_promote_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "would_promote")

    @property
    def promoted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "promoted")

    @property
    def already_promoted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "already_promoted")

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
    skipped: int = 0,
) -> str | None:
    """Emit the single ``LEARNING_AUTO_PROMOTE_EVALUATED`` event for a run.

    ``skipped`` is a candidate-id-less input that never became an
    outcome at all -- it is reported alongside ``counts["total"]``
    (which only ever counts ``outcomes``) so a reader can tell "every
    candidate given to this run produced an outcome" apart from "some
    were silently dropped before evaluation."
    """
    would_promote = sum(1 for o in outcomes if o.disposition == "would_promote")
    promoted = sum(1 for o in outcomes if o.disposition == "promoted")
    already_promoted = sum(1 for o in outcomes if o.disposition == "already_promoted")
    ineligible = sum(1 for o in outcomes if o.disposition.startswith("ineligible:"))
    payload: dict[str, Any] = {
        # Top-level and not just derivable from ``counts.skipped`` so
        # ``loop_health.py``'s ``_build_candidate_auto_promote_row`` can
        # read a run's outcome the same way for a completed evaluation
        # (this event) and for a run that never got this far at all
        # (see ``_emit_failed_event`` below, same event type, same key).
        "outcome": "degraded" if skipped else "ok",
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
            "already_promoted": already_promoted,
            "ineligible": ineligible,
            "skipped": skipped,
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


def emit_candidate_auto_promote_failure_event(
    event_log: EventLog,
    *,
    error: BaseException,
    source: str = "candidate_auto_promote",
    configured_mode: CandidateAutoPromoteMode | str | None = None,
) -> str | None:
    """Record a run that never produced outcomes at all.

    Every exit path before or during a ``run_candidate_auto_promotion``
    call -- a refused ``mode="commit"`` config, a malformed
    ``--candidates`` file, a non-finite env override -- still has to
    leave a trace in the same event stream :func:`_emit_evaluated_event`
    writes to, or a failure looks identical to "this job never ran" and
    ``loop_health.py``'s ``last_status`` keeps reporting whatever a prior
    *successful* run last wrote, forever. Reuses
    :attr:`EventType.LEARNING_AUTO_PROMOTE_EVALUATED` rather than a new
    event type -- same reasoning as the module's "add a health signal as
    an event, not a new probe" rule: one ordered stream lets a reader take
    "the latest event of this type" as the run's current status,
    regardless of which code path produced it.

    ``outcome`` is ``"refused"`` for a :class:`~trellis.errors.ConfigError`
    (a deliberate policy gate -- e.g. a stored or env ``mode="commit"``
    without ``--dry-run``) and ``"failed"`` for anything else (a malformed
    file, a non-finite env value, ...). Swallows its own emission failure
    (logs instead) rather than letting an advisory write mask the
    original exception -- the same shape as every other best-effort step
    on a write path (see ``trellis.core.write_provenance.
    resolve_stamp_staleness``).

    Returns ``None`` (not the caller's exception) on an emission failure,
    so a caller that always re-raises the original error after calling
    this never has to guard against a second one escaping from here.
    """
    outcome = "refused" if isinstance(error, ConfigError) else "failed"
    payload: dict[str, Any] = {
        "outcome": outcome,
        "configured_mode": configured_mode,
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    try:
        event = event_log.emit(
            EventType.LEARNING_AUTO_PROMOTE_EVALUATED,
            source,
            payload=payload,
        )
    except Exception:
        logger.warning(
            "candidate_auto_promote_failure_event_emit_failed",
            exc_info=True,
            original_error_type=type(error).__name__,
        )
        return None
    return event.event_id


def _scan_already_promoted_entity_ids(
    event_log: EventLog, *, limit: int = _PROMOTION_SCAN_LIMIT
) -> set[str]:
    """Entity ids with >=1 successful ``precedent.promote``, human or auto.

    Newest-first (``order="desc"``) so a truncated scan on a busy log
    drops the oldest promotions rather than the most recent ones — the
    hazard documented on ``EventLog.get_events``'s ``order`` parameter and
    the reason :func:`~trellis.stores.base.event_log.EventLog.scan_events`
    exists. This reads with ``payload_filters``, which ``scan_events``
    does not accept, so it calls ``get_events`` directly with an explicit
    ``order`` rather than using that helper.

    No ``requested_by`` filter: a precedent a human approved and one this
    policy promoted earlier are both reasons to never re-propose the same
    candidate (see :func:`run_candidate_auto_promotion` and the module
    docstring's "Idempotency" section).
    """
    events = event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED,
        order="desc",
        payload_filters={
            "operation": Operation.PRECEDENT_PROMOTE.value,
            "status": "success",
        },
        limit=limit,
    )
    return {event.entity_id for event in events if event.entity_id}


def run_candidate_auto_promotion(
    *,
    candidates: Sequence[Mapping[str, Any]],
    policy: LearningCandidateAutoPromotePolicy,
    event_log: EventLog,
    executor: MutationExecutor | None = None,
    requested_by: str = AUTO_PROMOTE_REQUESTED_BY,
    source: str = "candidate_auto_promote",
    force_report_only: bool = False,
    configured_mode: CandidateAutoPromoteMode | None = None,
) -> CandidateAutoPromoteReport:
    """Evaluate every candidate and report what would be promoted.

    ``policy.mode`` can only ever be ``"report_only"`` — constructing a
    ``mode="commit"`` policy already raised
    :class:`~trellis.errors.ConfigError` (see
    :meth:`LearningCandidateAutoPromotePolicy.__post_init__`) — so
    ``effective_mode`` can never resolve to ``"commit"`` here, and the
    commit-execution branch below is unreachable until a follow-up lifts
    that gate. It is left in place deliberately, unexercised, rather than
    removed, so that follow-up is a smaller diff.

    ``force_report_only=True`` is an extra guard, checked ahead of
    :attr:`LearningCandidateAutoPromotePolicy.effective_mode` and never
    mixed into the policy object itself, so a dry-run can never be
    mistaken for a persisted configuration change. It is not what makes
    ``trellis curate auto-promote-candidates --dry-run`` work when
    ``learning.candidate_auto_promote.mode`` is configured as
    ``"commit"``: a ``mode="commit"`` policy cannot even be constructed
    (``__post_init__`` raises :class:`~trellis.errors.ConfigError`
    unconditionally), so the CLI applies ``--dry-run`` earlier, before
    building the policy — see
    ``trellis_cli.curate._build_candidate_auto_promote_policy``.

    Before evaluating any candidate, scans for every entity id that
    already has a successful ``precedent.promote``
    (:func:`_scan_already_promoted_entity_ids`) — human-approved or from
    an earlier automatic run — and reports a match as ``"already_promoted"``
    rather than re-proposing it (see the module docstring's
    "Idempotency" section).

    Each candidate with a usable ``candidate_id`` gets exactly one
    :class:`CandidateAutoPromoteOutcome`; one with no id (blank or
    missing) gets none and is counted in
    :attr:`CandidateAutoPromoteReport.skipped_count` instead, since
    there is nothing to name an outcome with. Exactly one
    :attr:`EventType.LEARNING_AUTO_PROMOTE_EVALUATED` event is emitted
    per call, covering every candidate in ``candidates`` (its
    ``counts.skipped`` field is how a reader sees that drop) — report-
    only and commit runs both emit it, so a report-only run is visible in
    the audit trail even though it writes nothing else.

    ``configured_mode`` lets a caller that forced ``policy.mode`` before
    construction (``trellis_cli.curate._build_candidate_auto_promote_policy``,
    for ``--dry-run``) pass the *true* pre-force value through — once a
    ``mode="commit"`` policy has been forced to ``"report_only"`` so it
    can even construct, ``policy.mode`` can no longer answer "what was
    this configured to" on its own. Omitted (or ``None``), this falls
    back to ``policy.mode``, unchanged from before this parameter
    existed.

    Raises:
        ValueError: If the effective mode is ``"commit"`` but no
            ``executor`` was given. Unreachable today — see above.
    """
    if configured_mode is None:
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

    already_promoted_entity_ids = _scan_already_promoted_entity_ids(event_log)

    outcomes: list[CandidateAutoPromoteOutcome] = []
    promotions_committed = 0
    skipped = 0

    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        if not candidate_id:
            skipped += 1
            continue
        holdout_arm = _holdout_arm_status(candidate)
        candidate_entity_id = precedent_entity_id_for_candidate(candidate_id)
        already_promoted = candidate_entity_id in already_promoted_entity_ids
        qualifies, reason = _evaluate_candidate(
            candidate, policy=policy, already_promoted=already_promoted
        )

        if not qualifies:
            outcomes.append(
                CandidateAutoPromoteOutcome(
                    candidate_id=candidate_id,
                    disposition=reason,
                    entity_id=candidate_entity_id if already_promoted else None,
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
        skipped=skipped,
    )

    return CandidateAutoPromoteReport(
        enabled=policy.enabled,
        mode=effective_mode,
        configured_mode=configured_mode,
        outcomes=outcomes,
        event_id=event_id,
        skipped_count=skipped,
    )


#: Why :attr:`AgreementMetric.rate` structurally cannot be anything but
#: ``None`` or ``1.0`` today: no event records a human *rejecting* a
#: learning-promotion candidate (see :func:`compute_agreement_metric`), so
#: ``disagree`` is always ``0`` and ``rate = agree / (agree + disagree)``
#: collapses to ``agree / agree`` whenever there is any data at all.
#: Carried verbatim in every :class:`AgreementMetric` so a reader of the
#: serialised payload (:func:`agreement_metric_to_dict`) sees the
#: constraint next to the number, not only in this module's prose.
_AGREEMENT_RATE_NOTE = (
    "rate can only be None (no would_promote candidates reviewed by a "
    "human yet) or 1.0 (no event today records a human rejecting a "
    "learning-promotion candidate, so disagree is always 0) — never a "
    "value in between."
)


@dataclass(frozen=True, slots=True)
class AgreementMetric:
    """Agreement between this policy's ``would_promote`` calls and later
    human Review-queue decisions on the same candidate.

    ``agree``, ``disagree`` and ``undecided`` are the raw counts ``rate``
    is derived from — read them directly rather than reconstructing them
    from ``rate``, which loses the denominator. ``disagree`` is, as of
    today's data, structurally always ``0`` — see the module docstring's
    "agreement metric's disagree count" section. ``rate`` is ``None``
    (never a divide-by-zero) when ``agree + disagree == 0``. ``note``
    states that structural constraint in plain language; see
    :data:`_AGREEMENT_RATE_NOTE`.
    """

    agree: int
    disagree: int
    undecided: int
    rate: float | None
    note: str = _AGREEMENT_RATE_NOTE


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
        "already_promoted_count": report.already_promoted_count,
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
        "note": metric.note,
    }
