"""Automatic promotion for learning candidates — report-only (Tier 1).

Owner's stated intent is "eventually we want automatic promotion"; every
switch here is tunable (see the ``learning.candidate_auto_promote.*``
entries in :data:`trellis.core.settings_registry.SETTINGS_REGISTRY`) and
ships ``enabled=False`` with ``mode="report_only"`` as the default, so this
module produces evidence for picking thresholds rather than promoting
anything unattended.

**Scope: evaluation only, by default.** This module never demotes
anything itself. ``mode="commit"`` is refused at construction
(:meth:`LearningCandidateAutoPromotePolicy.__post_init__` raises
:class:`~trellis.errors.ConfigError`) unless the caller also passes
``demotion_enabled=True`` — the sibling precedent auto-demotion policy
(:class:`trellis.learning.precedent_auto_demote.PrecedentAutoDemotePolicy`)
being switched on, which is the rollback a safe commit mode depends on.
Both policies still ship ``enabled=False`` by default, so ``report_only``
is the only mode that runs unattended out of the box; an operator must
deliberately enable auto-demotion *and* flip this policy to ``commit``
before an unattended promotion can happen at all.

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

Invariant (a) reversible rollback is what a safe commit mode needs;
:mod:`trellis.learning.precedent_auto_demote` now supplies it, which is
why commit mode's refusal is conditional on that policy's ``enabled``
rather than unconditional — but invariant (b) post-change monitoring is
still an open gap (see that module's own docstring), and both policies
ship ``enabled=False``, so nothing here promotes unattended out of the
box.

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

import functools
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
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: Requester identity stamped on every governed command this policy
#: submits — the promotion (``ENTITY_CREATE`` / ``LINK_CREATE`` /
#: ``PRECEDENT_PROMOTE``, via :func:`submit_learning_promotion`). Demotion
#: is not implemented here yet (see the module docstring's "Scope:
#: evaluation only") and, when it ships, must stamp its *own* identity —
#: :data:`~trellis.mutate.handlers.AUTO_DEMOTE_REQUESTED_BY`, not this one.
#: ``PrecedentDemoteHandler``'s provenance check is conditional on *who is
#: demoting* (manual-trigger PR), so reusing this constant for the demote
#: command's ``requested_by`` would make the automatic demote path look,
#: to that check, like the thing it is demoting rather than the caller
#: doing the demoting. The ``auto:`` prefix mirrors no existing convention
#: exactly (CLI/API/MCP/worker surfaces use ``<surface>:<verb>``) on
#: purpose: an audit reader filtering by ``requested_by`` should be able
#: to tell "a human" from "this policy" at a glance, the same way
#: :func:`run_auto_promotion` never reuses ``cli:``/``api:`` identities
#: for its own promotions.
AUTO_PROMOTE_REQUESTED_BY = "auto:learning_candidate"

CandidateAutoPromoteMode = Literal["report_only", "commit"]

#: Run-level status vocabulary shared by the emitted event's ``outcome``
#: and ``report_to_dict``'s ``status`` (see :func:`_run_status`). Named
#: the same three values as
#: :data:`trellis.learning.precedent_auto_demote.RunStatus` — kept as a
#: separate alias rather than imported, matching this module's existing
#: by-value-copy relationship to its sibling.
RunStatus = Literal["ok", "degraded", "failed"]


def sanitize_configured_mode(value: object) -> str | None:
    """Let only a validated enum member reach a payload or response.

    ``configured_mode`` is threaded through call sites that straddle a
    resolver or a policy construction (``emit_candidate_auto_promote_
    failure_event`` accepts ``str | None`` precisely so a caller mid-
    resolution can still report what it was trying). This is the single
    point that refuses anything else before it reaches an event payload,
    an API response or CLI output: ``None`` passes through (nothing
    configured, or not resolved yet), either enum member passes through,
    and every other value — a raw env or stored string included — becomes
    the fixed marker ``"invalid"`` instead of being echoed.
    """
    if value is None:
        return None
    if value in ("report_only", "commit"):
        return str(value)
    return "invalid"


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
        mode: ``"report_only"`` (default) records what *would* be
            promoted without writing anything. ``"commit"`` promotes each
            qualifying candidate through the same governed path a human
            review-queue approval uses; constructing this policy with
            ``mode="commit"`` raises :class:`~trellis.errors.ConfigError`
            unless ``demotion_enabled`` is also ``True`` — commit mode's
            rollback is the precedent auto-demotion policy, and running
            one without the other leaves a bad unattended promotion with
            no automatic undo. This check does not look at ``enabled``:
            a commit-mode policy with ``enabled=False`` still constructs
            (and immediately behaves as ``report_only`` via
            :attr:`effective_mode`), so flipping ``enabled`` on later is a
            safe no-op once demotion is already enabled.
        demotion_enabled: Whether the sibling precedent auto-demotion
            policy
            (:class:`trellis.learning.precedent_auto_demote.PrecedentAutoDemotePolicy`)
            is enabled. ``False`` by default. This is the only thing
            ``mode="commit"`` checks at construction — it is the caller's
            responsibility to pass the demotion policy's real
            ``enabled`` value here (see
            :mod:`trellis.learning.precedent_auto_demote` for where that
            is resolved), not a copy of this policy's own ``enabled``.
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
        ConfigError: If ``mode="commit"`` and ``demotion_enabled`` is
            ``False`` — commit mode is refused until auto-demotion (its
            rollback) is enabled too.
    """

    enabled: bool = False
    mode: CandidateAutoPromoteMode = "report_only"
    min_helpful_count: int = DEFAULT_MIN_HELPFUL_COUNT
    min_distinct_sessions: int = DEFAULT_MIN_DISTINCT_SESSIONS
    require_zero_unhelpful: bool = DEFAULT_REQUIRE_ZERO_UNHELPFUL
    max_promotions_per_run: int = DEFAULT_MAX_PROMOTIONS_PER_RUN
    demotion_enabled: bool = False

    def __post_init__(self) -> None:
        if self.mode not in ("report_only", "commit"):
            msg = (
                "LearningCandidateAutoPromotePolicy.mode must be "
                '"report_only" or "commit"'
            )
            raise ValueError(msg)
        if self.mode == "commit" and not self.demotion_enabled:
            # Conditional on ``demotion_enabled``, not on this
            # policy's own ``enabled`` — this is the only place a policy
            # comes into existence
            # (``trellis_cli.curate._build_candidate_auto_promote_policy``
            # today). Commit mode needs a reversible rollback (Tier-1
            # invariant (a)); that rollback is the precedent auto-demotion
            # policy (:mod:`trellis.learning.precedent_auto_demote`), so
            # commit is refused until the caller says that policy is also
            # enabled. See the module docstring's "Scope: evaluation only,
            # by default".
            msg = (
                f"{CANDIDATE_AUTO_PROMOTE_MODE_SETTING}: commit mode is not "
                "available until auto-demotion is enabled "
                "(learning.precedent_auto_demote.enabled)"
            )
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
        ``mode`` is configured to. A policy with ``mode="commit"`` can
        exist with ``enabled=False`` (construction only checks
        ``demotion_enabled``, not ``enabled``), and this property is what
        makes that combination inert: it still reports ``report_only``
        here, so flipping ``enabled`` on later — with ``demotion_enabled``
        already ``True`` — is the only switch left to produce a real
        commit-mode run.
        """
        if not self.enabled:
            return "report_only"
        return self.mode


@functools.cache
def _warn_mode_refused_in_settings_once(setting: str, stored_mode: str) -> None:
    """Emit ``candidate_auto_promote_mode_refused_in_settings`` once per cause.

    ``resolve_candidate_auto_promote_overrides`` resolves this family on
    every call, including ``GET /settings`` (``trellis_api/routes/
    settings.py``), which calls it once per request rather than once per
    job run. A stored refused ``mode`` is a standing misconfiguration,
    not a per-request event, so logging a warning on every poll turned
    one bad `settings.json` entry into one warning per request forever.
    ``functools.cache`` makes a repeat call with the same ``(setting,
    stored_mode)`` a no-op: the body runs once per distinct cause per
    process, the same dedup :func:`trellis.core.vector_metadata.
    _warn_vector_store_unavailable_once` already applies to a different
    standing cause.

    ``_warn_mode_refused_in_settings_once.cache_clear()`` is test-facing
    only, to isolate cases within one process — a production process
    does not reset it mid-flight.
    """
    logger.warning(
        "candidate_auto_promote_mode_refused_in_settings",
        setting=setting,
        stored_mode=stored_mode,
        env_override=CANDIDATE_AUTO_PROMOTE_MODE_ENV,
    )


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
    function never duplicates that check. ``mode`` is the one exception:
    it is validated here, raising a value-free :class:`ValueError`, so an
    unrecognized stored or env value never survives long enough to be
    recorded as a ``configured_mode`` or echoed as a settings
    ``effective_value`` — both read-side surfaces assume the resolver
    already refused anything that is not an enum member.

    A *stored* ``mode`` in the setting's ``refused_values`` (a
    ``settings.json`` edited by hand, outside the governed write path) is
    refused here rather than honoured; commit mode stays reachable only
    through the environment variable.

    Raises:
        ValueError: If the resolved ``mode`` is neither ``"report_only"``
            nor ``"commit"``. The message never includes the offending
            value.
        ConfigError: If a stored ``mode`` is one of the setting's
            ``refused_values``.
    """
    # Lazy: trellis.core.settings_registry imports this module's own
    # constants at module level, so a module-level import would cycle.
    from trellis.core.settings_registry import get_setting  # noqa: PLC0415

    src_env: Mapping[str, str] = os.environ if env is None else env
    settings_map: Mapping[str, bool | int | float | str] = settings or {}

    mode_from_env = src_env.get(CANDIDATE_AUTO_PROMOTE_MODE_ENV, "").strip()
    if not mode_from_env and CANDIDATE_AUTO_PROMOTE_MODE_SETTING in settings_map:
        stored_mode = str(settings_map[CANDIDATE_AUTO_PROMOTE_MODE_SETTING])
        spec = get_setting(CANDIDATE_AUTO_PROMOTE_MODE_SETTING)
        if spec is not None and stored_mode in spec.refused_values:
            msg = spec.refused_reason or (
                f"{CANDIDATE_AUTO_PROMOTE_MODE_SETTING}={stored_mode!r} "
                "is not accepted."
            )
            _warn_mode_refused_in_settings_once(
                CANDIDATE_AUTO_PROMOTE_MODE_SETTING, stored_mode
            )
            raise ConfigError(msg, setting=CANDIDATE_AUTO_PROMOTE_MODE_SETTING)

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

    mode = resolve_str(
        CANDIDATE_AUTO_PROMOTE_MODE_ENV,
        CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
        "report_only",
    )
    if mode not in ("report_only", "commit"):
        msg = f'{CANDIDATE_AUTO_PROMOTE_MODE_SETTING} must be "report_only" or "commit"'
        raise ValueError(msg)

    return {
        "enabled": resolve_bool(
            CANDIDATE_AUTO_PROMOTE_ENABLED_ENV,
            CANDIDATE_AUTO_PROMOTE_ENABLED_SETTING,
            False,
        ),
        "mode": mode,
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

    @property
    def submission_failed_count(self) -> int:
        """Candidates where a write was actually attempted and did not
        succeed -- the ``ineligible:submission_*`` dispositions
        :func:`run_candidate_auto_promotion` reports when
        ``submission.get("status") != "promoted"``. A strict subset of
        :attr:`ineligible_count`: that count also includes dispositions
        such as ``ineligible:run_cap_reached``, which were never
        attempted at all, so it cannot stand in for this one in
        :func:`_run_status`."""
        return sum(
            1
            for o in self.outcomes
            if o.disposition.startswith("ineligible:submission_")
        )


def _run_status(*, succeeded: int, attempt_failed: int, skipped: int) -> RunStatus:
    """Classify one run's outcome for the emitted event and ``--format json``.

    ``succeeded`` and ``attempt_failed`` both count candidates for which a
    governed write was actually attempted (:attr:`CandidateAutoPromoteReport.
    promoted_count` and :attr:`CandidateAutoPromoteReport.submission_failed_count`
    respectively) -- a candidate that was never attempted (``would_promote``,
    ``already_promoted``, ``ineligible:run_cap_reached``, ...) affects neither.
    ``"failed"`` requires at least one attempt: a run with nothing to attempt
    is never reported as having failed every attempt. ``skipped`` keeps this
    module's pre-existing signal -- an id-less candidate dropped before it
    could become an outcome at all -- folded into the same ``"degraded"``
    bucket as a mixed attempt, never into ``"ok"``.
    """
    attempted = succeeded + attempt_failed
    if attempted and not succeeded:
        return "failed"
    if skipped or attempt_failed:
        return "degraded"
    return "ok"


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
    submission_failed = sum(
        1 for o in outcomes if o.disposition.startswith("ineligible:submission_")
    )
    payload: dict[str, Any] = {
        # Top-level and not just derivable from ``counts.skipped`` so
        # ``loop_health.py``'s ``_build_candidate_auto_promote_row`` can
        # read a run's outcome the same way for a completed evaluation
        # (this event) and for a run that never got this far at all
        # (see ``_emit_failed_event`` below, same event type, same key).
        # See ``_run_status`` for the ok | degraded | failed vocabulary.
        "outcome": _run_status(
            succeeded=promoted, attempt_failed=submission_failed, skipped=skipped
        ),
        "mode": effective_mode,
        "configured_mode": sanitize_configured_mode(configured_mode),
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
        "configured_mode": sanitize_configured_mode(configured_mode),
        "error": {
            "type": type(error).__name__,
            # Never ``str(error)``: a resolver or policy ``ValueError``'s
            # message can embed the raw stored/env value it rejected
            # (``configured_mode`` itself, or an out-of-range knob). The
            # type above already identifies what kind of failure this
            # was; this fixed string is all the message ever carries.
            "message": "withheld — see error.type",
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
    graph_store: GraphStore | None = None,
    document_store: DocumentStore | None = None,
    requested_by: str = AUTO_PROMOTE_REQUESTED_BY,
    source: str = "candidate_auto_promote",
    force_report_only: bool = False,
    configured_mode: CandidateAutoPromoteMode | None = None,
) -> CandidateAutoPromoteReport:
    """Evaluate every candidate and report what would be promoted.

    ``policy.mode`` can be ``"commit"`` once the policy was constructed
    with ``demotion_enabled=True`` (see
    :meth:`LearningCandidateAutoPromotePolicy.__post_init__`) — the
    commit-execution branch below is reachable in that configuration,
    gated only by :attr:`LearningCandidateAutoPromotePolicy.enabled`
    (via ``effective_mode``) and by the per-run cap. Both policies still
    ship ``enabled=False`` by default, so nothing commits unattended out
    of the box.

    ``force_report_only=True`` is an extra guard, checked ahead of
    :attr:`LearningCandidateAutoPromotePolicy.effective_mode` and never
    mixed into the policy object itself, so a dry-run can never be
    mistaken for a persisted configuration change. It is also what makes
    ``trellis curate auto-promote-candidates --dry-run`` work when
    ``learning.candidate_auto_promote.mode`` is configured as
    ``"commit"`` but ``learning.precedent_auto_demote.enabled`` is
    ``False``: a ``mode="commit"`` policy cannot be constructed without
    ``demotion_enabled=True`` (``__post_init__`` raises
    :class:`~trellis.errors.ConfigError` otherwise), so the CLI resolves
    ``--dry-run`` before building the policy and forces the mode passed
    to construction to ``"report_only"`` first — see
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

    ``document_store`` is read-only here: a committed promotion embeds a
    bounded excerpt of its source document (see
    :func:`~trellis.learning.scoring.build_learning_promotion_payloads`),
    and with ``None`` it simply carries none.

    Raises:
        ValueError: If the effective mode is ``"commit"`` but no
            ``executor`` or ``graph_store`` was given. Unreachable
            today — see above.
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
    if effective_mode == "commit" and graph_store is None:
        msg = (
            "run_candidate_auto_promotion: effective mode is 'commit' but "
            "no graph_store was given."
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
        assert graph_store is not None  # narrowed by the ValueError guard above
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
            document_store=document_store,
        )
        submission = submit_learning_promotion(
            executor,
            promotion["entity_payload"],
            promotion["edge_payloads"],
            requested_by=requested_by,
            graph_store=graph_store,
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
    """Serialise a :class:`CandidateAutoPromoteReport` for ``--format json``.

    ``status`` is one of ``"ok"`` | ``"degraded"`` | ``"failed"``, via
    :func:`_run_status` (same vocabulary and same function the emitted
    event's own ``outcome`` uses, see :func:`_emit_evaluated_event`):
    ``"failed"`` when at least one candidate's promotion was actually
    attempted (:attr:`CandidateAutoPromoteReport.promoted_count` +
    :attr:`CandidateAutoPromoteReport.submission_failed_count`) and none
    of those attempts succeeded; ``"degraded"`` when some attempts
    succeeded and some failed, or ``report.skipped_count`` is nonzero
    (an id-less candidate dropped before it could become an outcome at
    all); ``"ok"`` otherwise. Never ``"error"``: a run that failed before
    reaching :func:`run_candidate_auto_promotion` never builds a report
    at all, and is reported instead through
    :func:`emit_candidate_auto_promote_failure_event`'s own
    ``"refused"``/``"failed"`` event payload and the CLI's own
    ``"error"``-status JSON. The CLI still exits 0 for ``"degraded"``
    and ``"failed"`` alike: every candidate given an outcome was
    evaluated correctly, so nothing here is a command failure, only
    something the caller should notice (``docs/agent-guide/operations.md``).
    Named here, rather than left implicit, so this JSON shape and the
    CLI's exit code are both read off the same flag
    (``tests/unit/test_format_exit_parity_rule.py``).
    """
    return {
        "status": _run_status(
            succeeded=report.promoted_count,
            attempt_failed=report.submission_failed_count,
            skipped=report.skipped_count,
        ),
        "enabled": report.enabled,
        "mode": report.mode,
        "configured_mode": sanitize_configured_mode(report.configured_mode),
        "would_promote_count": report.would_promote_count,
        "already_promoted_count": report.already_promoted_count,
        "promoted_count": report.promoted_count,
        "ineligible_count": report.ineligible_count,
        "skipped_count": report.skipped_count,
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
