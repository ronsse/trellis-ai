"""Automatic demotion for current precedents — commit mode's safety rollback.

Owner's stated intent is the sibling of
:mod:`trellis.learning.candidate_auto_promote`: unattended promotion needs a
reversible undo before it can safely commit anything, and this module is
that undo. It reuses the evidence gate already built in
:mod:`trellis.learning.precedent_evidence_gate`
(:func:`~trellis.learning.precedent_evidence_gate.judge_current_precedents`)
rather than re-deriving the join/skip/judge logic, and adds exactly one new
capability that gate deliberately does not have: submitting a governed
``precedent.demote`` command for each ``would_demote`` verdict, capped per
run.

**Scope: evaluation plus, optionally, demotion.** Every switch here is
tunable (see the ``learning.precedent_auto_demote.*`` entries in
:data:`trellis.core.settings_registry.SETTINGS_REGISTRY`) and ships
``enabled=False`` with ``mode="report_only"`` as the default, so this module
produces evidence for picking thresholds rather than demoting anything
unattended. Unlike
:class:`~trellis.learning.candidate_auto_promote.LearningCandidateAutoPromotePolicy`,
:class:`PrecedentAutoDemotePolicy` carries no sibling-policy gate on its own
``mode="commit"``: demotion *is* the reversible rollback a safe commit mode
depends on, so there is nothing further for this policy's own commit mode to
wait on. Both policies still ship ``enabled=False`` by default, so nothing
here demotes unattended out of the box.

Every ``precedent.demote`` this module submits is stamped with
:data:`~trellis.mutate.handlers.AUTO_DEMOTE_REQUESTED_BY` — imported from
``trellis.mutate.handlers`` rather than re-minted here (lazily, inside
:func:`run_precedent_auto_demotion` itself, to avoid a module-level
import cycle through ``trellis.core.settings_registry`` — see that
function's own docstring), so
:class:`~trellis.mutate.handlers.PrecedentDemoteHandler`'s own provenance
check (only a precedent *this policy itself promoted* may be demoted this
way) and this module's submissions agree on the same string by construction.
A human-promoted precedent is filtered out before judging, by the same
evidence gate:
:func:`~trellis.learning.precedent_evidence_gate.judge_current_precedents`
re-derives provenance with
:func:`~trellis.mutate.handlers._was_auto_promoted` and reports it
``"ineligible:not_auto_promoted"`` rather than ever producing a
``would_demote`` outcome for it. The handler's own provenance check (only a
precedent *this policy itself promoted* may be demoted this way) still runs
on every commit — defence in depth against a narrow race between the gate's
read and the commit, not the primary mechanism — so an outcome it refuses
for that reason is now unexpected rather than routine: the automatic
demotion path may only undo what the automatic promotion path (or an
earlier run of this same policy) did.

**Post-change monitoring remains an open gap.** Tier-1 autonomy invariant
(b) — watching what happened *after* an automatic write, not just gating
before it — has no implementation here or in the promotion module. This
module's own evidence gate *is* a form of after-the-fact monitoring for the
promotion side (a demoted precedent is direct evidence a promotion was
wrong), but nothing watches what happens after a demotion itself. Flagged
rather than silently assumed solved.

**Idempotency.** A ``would_demote`` precedent that has already been demoted
by an earlier automatic run (or a human) is never produced as a
``would_demote`` outcome in the first place —
:func:`~trellis.learning.precedent_evidence_gate.judge_current_precedents`
reports it as ``skipped`` with reason
:data:`~trellis.learning.precedent_evidence_gate.SKIPPED_ALREADY_DEMOTED`,
and this module passes that disposition straight through as
``"already_demoted"`` without attempting a redundant commit.

That covers a precedent already demoted *before this run started*; it says
nothing about two runs racing each other within the same evidence window.
For that, every ``precedent.demote`` command this module submits carries an
``idempotency_key`` derived from the precedent id and its evidence-window
start (see :func:`_demote_idempotency_key`), so
:class:`~trellis.mutate.executor.MutationExecutor`'s own idempotency stage
collapses a second, overlapping submission into a
:attr:`~trellis.mutate.commands.CommandStatus.DUPLICATE` result — reported
here as ``"deduplicated"``, never as a second ``"demoted"`` and never
double-charged against ``max_demotions_per_run``. The window start itself is
the later of the precedent's last successful ``precedent.promote`` and its
last successful ``precedent.reinstate`` — see
:func:`~trellis.learning.precedent_evidence_gate.discover_current_precedents_with_coverage`
— so a human reinstatement changes the key: citations from before the
reinstatement stop counting toward demotion, and a later, genuinely new
round of unhelpful citations demotes again under a fresh key rather than
being deduplicated against the demotion the reinstatement already undid.

**Truncation never demotes past what was actually scanned.** When the
join scan backing the evidence gate is truncated, a precedent whose
evidence window starts before the scan's own ``covered_since`` is reported
``"skipped_incomplete_evidence"`` instead of ``"would_demote"`` or
``"demoted"`` — the citations outside the scanned window could as easily be
helpful as unhelpful, so a demotion verdict formed without seeing them is
not evidence of unhelpfulness, just absence of evidence. It is counted, not
dropped, in both modes.
"""

from __future__ import annotations

import functools
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from trellis.core.write_config import TRUTHY, parse_finite_int
from trellis.errors import ConfigError
from trellis.learning.precedent_evidence_gate import (
    ATTRIBUTION_CAVEAT,
    DISPOSITION_REFUSED,
    DISPOSITION_SKIPPED,
    DISPOSITION_WOULD_DEMOTE,
    MIN_UNHELPFUL_CITATIONS,
    SKIPPED_ALREADY_DEMOTED,
    SKIPPED_INCOMPLETE_EVIDENCE,
    PrecedentDemotionOutcome,
    judge_current_precedents,
)
from trellis.mutate import Command, CommandStatus, MutationExecutor, Operation
from trellis.stores.base.event_log import EventLog, EventType, ScanCoverage
from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

PrecedentAutoDemoteMode = Literal["report_only", "commit"]

#: Run-level status vocabulary shared by the emitted event's ``outcome``
#: and ``report_to_dict``'s ``status`` (see :func:`_run_status`). Named
#: the same three values as
#: :data:`trellis.learning.candidate_auto_promote.RunStatus` — kept as
#: a separate alias rather than imported, matching this module's
#: existing by-value-copy relationship to its sibling.
RunStatus = Literal["ok", "degraded", "failed"]


def sanitize_configured_mode(value: object) -> str | None:
    """Let only a validated enum member reach a payload or response.

    By-value copy of
    :func:`trellis.learning.candidate_auto_promote.sanitize_configured_mode`
    — same contract, same reason: ``None`` passes through, either enum
    member passes through, and every other value becomes the fixed marker
    ``"invalid"`` rather than being echoed back into an event payload, an
    API response or CLI output.
    """
    if value is None:
        return None
    if value in ("report_only", "commit"):
        return str(value)
    return "invalid"


# --------------------------------------------------------------------------
# env / setting constants — mirror candidate_auto_promote.py's naming
# scheme for its own three (of six) non-shared knobs.
# --------------------------------------------------------------------------

PRECEDENT_AUTO_DEMOTE_ENABLED_ENV = "TRELLIS_ENABLE_PRECEDENT_AUTO_DEMOTE"
PRECEDENT_AUTO_DEMOTE_MODE_ENV = "TRELLIS_PRECEDENT_AUTO_DEMOTE_MODE"
PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV = (
    "TRELLIS_PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN"
)

#: ``SETTINGS_REGISTRY`` keys for the same three knobs. Resolution order is
#: env > settings > default — no ``config.yaml`` layer, same as
#: ``learning.candidate_auto_promote.*``.
PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING = "learning.precedent_auto_demote.enabled"
PRECEDENT_AUTO_DEMOTE_MODE_SETTING = "learning.precedent_auto_demote.mode"
PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_SETTING = (
    "learning.precedent_auto_demote.max_demotions_per_run"
)

#: Floor for ``max_demotions_per_run``. A *separate* knob from
#: ``learning.candidate_auto_promote.max_promotions_per_run`` — promotion
#: and demotion are different blast radii (one mints precedents, the other
#: retires them) and nothing requires they move together.
MAX_DEMOTIONS_PER_RUN_FLOOR = 1

#: Conservative default — see ``SettingSpec.description`` in
#: ``settings_registry.py`` for the operator-facing justification.
DEFAULT_MAX_DEMOTIONS_PER_RUN = 3

#: Scan limits for the reused evidence gate — mirror
#: ``precedent_evidence_gate.py``'s own module-level defaults, passed
#: through rather than re-declared with different values.
_PROMOTION_SCAN_LIMIT = 500
_JOIN_SCAN_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class PrecedentAutoDemotePolicy:
    """Per-scope opt-in gate for unattended precedent demotion.

    Attributes:
        enabled: Master switch. ``False`` by default — global default OFF.
            When ``False``, :attr:`effective_mode` is always
            ``"report_only"`` regardless of ``mode``, so a run never
            writes.
        mode: ``"report_only"`` (default) records what *would* be demoted
            without writing anything. ``"commit"`` demotes each
            ``would_demote`` precedent through the same governed path a
            human ``trellis curate demote-precedent`` call uses, capped by
            ``max_demotions_per_run``. Unlike the promotion policy's
            ``mode="commit"``, this policy's own commit mode carries no
            further gate at construction: demotion *is* the rollback a
            safe commit mode needs, so there is nothing else for it to
            wait on.
        max_demotions_per_run: Caps how many precedents a single
            ``commit``-mode run may demote, bounding the blast radius of
            one scheduled job run regardless of how many ``would_demote``
            verdicts the evidence gate returns. Its own floor and default
            (:data:`MAX_DEMOTIONS_PER_RUN_FLOOR`,
            :data:`DEFAULT_MAX_DEMOTIONS_PER_RUN`) — not shared with
            ``learning.candidate_auto_promote.max_promotions_per_run``.

    Raises:
        ValueError: If ``mode`` is not ``"report_only"``/``"commit"``, or
            if ``max_demotions_per_run`` is looser than its floor.
    """

    enabled: bool = False
    mode: PrecedentAutoDemoteMode = "report_only"
    max_demotions_per_run: int = DEFAULT_MAX_DEMOTIONS_PER_RUN

    def __post_init__(self) -> None:
        if self.mode not in ("report_only", "commit"):
            msg = 'PrecedentAutoDemotePolicy.mode must be "report_only" or "commit"'
            raise ValueError(msg)
        if self.max_demotions_per_run < MAX_DEMOTIONS_PER_RUN_FLOOR:
            msg = (
                f"PrecedentAutoDemotePolicy.max_demotions_per_run="
                f"{self.max_demotions_per_run} is looser than the floor "
                f"({MAX_DEMOTIONS_PER_RUN_FLOOR})."
            )
            raise ValueError(msg)

    @property
    def effective_mode(self) -> PrecedentAutoDemoteMode:
        """``mode`` only takes effect when ``enabled``.

        Mirrors
        :attr:`~trellis.learning.candidate_auto_promote.LearningCandidateAutoPromotePolicy.effective_mode`:
        a disabled policy always behaves as ``report_only``, whatever
        ``mode`` is configured to.
        """
        if not self.enabled:
            return "report_only"
        return self.mode


@functools.cache
def _warn_mode_refused_in_settings_once(setting: str, stored_mode: str) -> None:
    """Emit ``precedent_auto_demote_mode_refused_in_settings`` once per cause.

    By-value copy of :func:`trellis.learning.candidate_auto_promote.
    _warn_mode_refused_in_settings_once` -- same reason: ``GET /settings``
    (``trellis_api/routes/settings.py``) resolves this family once per
    request, not once per job run, so a stored refused ``mode`` would
    otherwise log a fresh warning on every poll instead of once per
    distinct cause per process. ``cache_clear()`` is test-facing only.
    """
    logger.warning(
        "precedent_auto_demote_mode_refused_in_settings",
        setting=setting,
        stored_mode=stored_mode,
        env_override=PRECEDENT_AUTO_DEMOTE_MODE_ENV,
    )


def resolve_precedent_auto_demote_overrides(
    *,
    settings: Mapping[str, bool | int | float | str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, bool | int | str]:
    """Resolve the three precedent-auto-demote knobs: env > settings > default.

    By-value copy of
    :func:`trellis.learning.candidate_auto_promote.resolve_candidate_auto_promote_overrides`'s
    resolution shape and the same ``mode``-is-validated-here exception:
    an unrecognized stored or env value raises a value-free
    :class:`ValueError` rather than surviving to be recorded as a
    ``configured_mode`` or echoed as a settings ``effective_value``.

    A stored ``mode`` that matches one of
    :data:`~trellis.core.settings_registry.SettingSpec.refused_values`
    for ``learning.precedent_auto_demote.mode`` (today, only
    ``"commit"``) raises :class:`~trellis.errors.ConfigError` instead of
    being resolved: the governed write path
    (:class:`~trellis.mutate.handlers.SettingsUpdateHandler`) already
    refuses to *write* that value through
    ``trellis.core.settings_registry.validate_value``, and this is the
    same refusal applied at *read* time, so a settings file edited by
    hand (outside that governed path) cannot smuggle ``commit`` in
    anyway. This check only ever fires against a value read from the
    settings store — the environment variable path is unchanged and
    carries no such check, matching every other knob in this module.

    Raises:
        ValueError: If the resolved ``mode`` is neither ``"report_only"``
            nor ``"commit"``. The message never includes the offending
            value.
        ConfigError: If a *stored* ``mode`` value is one of the setting's
            ``refused_values``. Names the setting key
            (:data:`PRECEDENT_AUTO_DEMOTE_MODE_SETTING`) and points at
            :data:`PRECEDENT_AUTO_DEMOTE_MODE_ENV` as the only path that
            can still reach ``"commit"``.
    """
    # Lazy: trellis.core.settings_registry imports this module's own
    # env/setting-name constants at module level for its SettingSpec
    # entries, so a module-level import of settings_registry here would
    # complete an import cycle. Mirrors the lazy AUTO_DEMOTE_REQUESTED_BY
    # import in run_precedent_auto_demotion below.
    from trellis.core.settings_registry import get_setting  # noqa: PLC0415

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

    mode_from_env = src_env.get(PRECEDENT_AUTO_DEMOTE_MODE_ENV, "").strip()
    if not mode_from_env and PRECEDENT_AUTO_DEMOTE_MODE_SETTING in settings_map:
        stored_mode = str(settings_map[PRECEDENT_AUTO_DEMOTE_MODE_SETTING])
        spec = get_setting(PRECEDENT_AUTO_DEMOTE_MODE_SETTING)
        if spec is not None and stored_mode in spec.refused_values:
            msg = spec.refused_reason or (
                f"{PRECEDENT_AUTO_DEMOTE_MODE_SETTING}={stored_mode!r} is not accepted."
            )
            _warn_mode_refused_in_settings_once(
                PRECEDENT_AUTO_DEMOTE_MODE_SETTING, stored_mode
            )
            raise ConfigError(msg, setting=PRECEDENT_AUTO_DEMOTE_MODE_SETTING)

    mode = resolve_str(
        PRECEDENT_AUTO_DEMOTE_MODE_ENV,
        PRECEDENT_AUTO_DEMOTE_MODE_SETTING,
        "report_only",
    )
    if mode not in ("report_only", "commit"):
        msg = f'{PRECEDENT_AUTO_DEMOTE_MODE_SETTING} must be "report_only" or "commit"'
        raise ValueError(msg)

    return {
        "enabled": resolve_bool(
            PRECEDENT_AUTO_DEMOTE_ENABLED_ENV,
            PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING,
            False,
        ),
        "mode": mode,
        "max_demotions_per_run": resolve_int(
            PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV,
            PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_SETTING,
            DEFAULT_MAX_DEMOTIONS_PER_RUN,
        ),
    }


@dataclass(frozen=True, slots=True)
class PrecedentAutoDemoteOutcome:
    """What happened to one current precedent during a run.

    Mirrors
    :class:`trellis.learning.candidate_auto_promote.CandidateAutoPromoteOutcome`'s
    shape.
    """

    entity_id: str
    #: ``"would_demote"`` — cleared the gate, run was report-only (or
    #: disabled); ``"demoted"`` — cleared the gate, committed this run;
    #: ``"deduplicated"`` — cleared the gate, commit mode, but an
    #: overlapping run (same precedent, same evidence-window start) had
    #: already committed or was already in flight for the same
    #: ``idempotency_key``, so this run's own command was refused as a
    #: replay rather than counted as a second demotion;
    #: ``"skipped_rate_limited"`` — cleared the gate, commit mode, but the
    #: per-run cap was already reached; ``"refused:<rejection_reason>"`` —
    #: commit attempted and the handler refused it (e.g. a human-promoted
    #: precedent, under ``AUTO_DEMOTE_REQUESTED_BY``'s provenance check);
    #: ``"already_demoted"`` — the evidence gate's own idempotency case;
    #: ``"skipped_incomplete_evidence"`` — the evidence gate withheld a
    #: would-be ``would_demote`` verdict because the join scan that
    #: produced it was truncated before this precedent's own
    #: evidence-window start; ``"ineligible:<reason>"`` — every other
    #: skip or refusal the evidence gate reported (not admitted, not the
    #: current lifecycle state for a reason other than already being
    #: demoted, or not auto-promoted in the first place).
    disposition: str
    reason: str = ""
    promoted_at: str | None = None
    appearances: int = 0
    helpful_count: int = 0
    unhelpful_count: int = 0


@dataclass(frozen=True, slots=True)
class PrecedentAutoDemoteReport:
    """Aggregate result of one :func:`run_precedent_auto_demotion` pass."""

    enabled: bool
    #: What actually happened this run — always ``"report_only"`` when
    #: ``enabled`` is ``False``. See
    #: :attr:`PrecedentAutoDemotePolicy.effective_mode`.
    mode: PrecedentAutoDemoteMode
    #: What the policy was configured to, before any forcing — lets a
    #: reader tell "configured for commit but ran report-only because
    #: disabled" apart from "configured and ran report-only".
    configured_mode: PrecedentAutoDemoteMode
    outcomes: list[PrecedentAutoDemoteOutcome] = field(default_factory=list)
    event_id: str | None = None
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS
    attribution_caveat: str = ATTRIBUTION_CAVEAT
    scan: ScanCoverage = field(default_factory=ScanCoverage)
    notes: list[str] = field(default_factory=list)

    @property
    def would_demote_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "would_demote")

    @property
    def demoted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "demoted")

    @property
    def skipped_rate_limited_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "skipped_rate_limited")

    @property
    def refused_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition.startswith("refused:"))

    @property
    def already_demoted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "already_demoted")

    @property
    def skipped_incomplete_evidence_count(self) -> int:
        return sum(
            1 for o in self.outcomes if o.disposition == "skipped_incomplete_evidence"
        )

    @property
    def deduplicated_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == "deduplicated")

    @property
    def ineligible_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition.startswith("ineligible:"))


def _gate_outcome_to_disposition(outcome: PrecedentDemotionOutcome) -> tuple[str, str]:
    """Translate one evidence-gate outcome into this module's own vocabulary.

    Returns ``(disposition, reason)``. The evidence gate's own three-way
    vocabulary (``would_demote`` / ``refused`` / ``skipped``) is collapsed
    here into the richer set :class:`PrecedentAutoDemoteOutcome` documents:
    ``would_demote`` passes straight through (report-only / disabled, or
    not yet decided in commit mode — the caller overwrites it when it
    actually commits or rate-limits); the gate's own idempotency skip
    (:data:`SKIPPED_ALREADY_DEMOTED`) becomes ``"already_demoted"``; a
    truncated-scan withholding (:data:`SKIPPED_INCOMPLETE_EVIDENCE`)
    becomes ``"skipped_incomplete_evidence"`` — kept out of the generic
    ``ineligible:`` bucket below because, unlike every other skip or
    refusal there, this one is not a verdict about the precedent at all:
    the same precedent may read ``would_demote`` on a later run once the
    scan is no longer truncated, so collapsing it into "ineligible"
    would blur a scan limitation with a judgment; every other skip or
    refusal becomes ``"ineligible:<reason>"`` so a reader scanning for
    that prefix sees every precedent this run declined to demote, for
    whatever reason, in one place.
    """
    if outcome.disposition == DISPOSITION_WOULD_DEMOTE:
        return DISPOSITION_WOULD_DEMOTE, outcome.reason
    if outcome.disposition == DISPOSITION_SKIPPED and outcome.reason == (
        SKIPPED_ALREADY_DEMOTED
    ):
        return "already_demoted", outcome.reason
    if outcome.disposition == DISPOSITION_SKIPPED and outcome.reason == (
        SKIPPED_INCOMPLETE_EVIDENCE
    ):
        return "skipped_incomplete_evidence", outcome.reason
    if outcome.disposition in (DISPOSITION_REFUSED, DISPOSITION_SKIPPED):
        return f"ineligible:{outcome.reason}", outcome.reason
    # Defensive: the evidence gate's vocabulary is closed today, but a
    # reader of this module alone should never see a disposition it does
    # not recognise reach a pack silently relabelled as something else.
    return f"ineligible:{outcome.disposition}", outcome.reason


def _run_status(*, succeeded: int, attempt_failed: int) -> RunStatus:
    """Classify one run's outcome for the emitted event and ``--format json``.

    Mirrors :func:`trellis.learning.candidate_auto_promote._run_status`
    (no ``skipped`` parameter here: this gate never drops a precedent
    silently, see :func:`report_to_dict`'s docstring). ``succeeded`` and
    ``attempt_failed`` both count precedents for which a governed
    ``PRECEDENT_DEMOTE`` write was actually attempted --
    :attr:`PrecedentAutoDemoteReport.demoted_count` and
    :attr:`PrecedentAutoDemoteReport.refused_count` respectively. A
    precedent that was never attempted (``would_demote`` in report-only
    mode, ``skipped_rate_limited``, ``deduplicated``, ``already_demoted``,
    ``ineligible:*``, ``skipped_incomplete_evidence``) affects neither.
    ``"failed"`` requires at least one attempt.
    """
    attempted = succeeded + attempt_failed
    if attempted and not succeeded:
        return "failed"
    if attempt_failed:
        return "degraded"
    return "ok"


def _emit_evaluated_event(
    event_log: EventLog,
    *,
    outcomes: Sequence[PrecedentAutoDemoteOutcome],
    policy: PrecedentAutoDemotePolicy,
    effective_mode: PrecedentAutoDemoteMode,
    configured_mode: PrecedentAutoDemoteMode,
    scan: ScanCoverage,
    source: str,
) -> str | None:
    """Emit the single ``LEARNING_AUTO_DEMOTE_EVALUATED`` event for a run.

    Mirrors
    :func:`trellis.learning.candidate_auto_promote._emit_evaluated_event`'s
    payload shape (``outcome``/``mode``/``configured_mode``/
    ``dispositions``/``counts``/``policy_snapshot``), plus the scan and
    attribution-caveat fields
    :func:`trellis.learning.precedent_evidence_gate._emit_evaluated_event`
    already carries, so a reader of this event does not have to guess
    which shape it is: it carries both.
    """
    would_demote = sum(1 for o in outcomes if o.disposition == "would_demote")
    demoted = sum(1 for o in outcomes if o.disposition == "demoted")
    deduplicated = sum(1 for o in outcomes if o.disposition == "deduplicated")
    skipped_rate_limited = sum(
        1 for o in outcomes if o.disposition == "skipped_rate_limited"
    )
    refused = sum(1 for o in outcomes if o.disposition.startswith("refused:"))
    already_demoted = sum(1 for o in outcomes if o.disposition == "already_demoted")
    skipped_incomplete_evidence = sum(
        1 for o in outcomes if o.disposition == "skipped_incomplete_evidence"
    )
    ineligible = sum(1 for o in outcomes if o.disposition.startswith("ineligible:"))
    payload: dict[str, Any] = {
        # See ``_run_status`` for the ok | degraded | failed vocabulary.
        "outcome": _run_status(succeeded=demoted, attempt_failed=refused),
        "mode": effective_mode,
        "configured_mode": sanitize_configured_mode(configured_mode),
        "min_unhelpful_citations": MIN_UNHELPFUL_CITATIONS,
        "attribution_caveat": ATTRIBUTION_CAVEAT,
        "scan": scan.model_dump(),
        "dispositions": [
            {
                "entity_id": o.entity_id,
                "disposition": o.disposition,
                "reason": o.reason,
                "promoted_at": o.promoted_at,
            }
            for o in outcomes
        ],
        "counts": {
            "total": len(outcomes),
            "would_demote": would_demote,
            "demoted": demoted,
            "deduplicated": deduplicated,
            "skipped_rate_limited": skipped_rate_limited,
            "refused": refused,
            "already_demoted": already_demoted,
            "skipped_incomplete_evidence": skipped_incomplete_evidence,
            "ineligible": ineligible,
        },
        "policy_snapshot": {
            "enabled": policy.enabled,
            "mode": policy.mode,
            "max_demotions_per_run": policy.max_demotions_per_run,
        },
    }
    event = event_log.emit(
        EventType.LEARNING_AUTO_DEMOTE_EVALUATED,
        source,
        payload=payload,
    )
    return event.event_id


def emit_precedent_auto_demote_failure_event(
    event_log: EventLog,
    *,
    error: BaseException,
    source: str = "precedent_auto_demote",
    configured_mode: PrecedentAutoDemoteMode | str | None = None,
) -> str | None:
    """Record a run that never produced outcomes at all.

    Mirrors
    :func:`trellis.learning.candidate_auto_promote.emit_candidate_auto_promote_failure_event`:
    ``outcome`` is ``"refused"`` for a :class:`~trellis.errors.ConfigError`
    and ``"failed"`` for anything else, never includes ``str(error)``
    (only ``error.type``), and swallows its own emission failure rather
    than masking the original exception.
    """
    outcome = "refused" if isinstance(error, ConfigError) else "failed"
    payload: dict[str, Any] = {
        "outcome": outcome,
        "configured_mode": sanitize_configured_mode(configured_mode),
        "error": {
            "type": type(error).__name__,
            "message": "withheld — see error.type",
        },
    }
    try:
        event = event_log.emit(
            EventType.LEARNING_AUTO_DEMOTE_EVALUATED,
            source,
            payload=payload,
        )
    except Exception:
        logger.warning(
            "precedent_auto_demote_failure_event_emit_failed",
            exc_info=True,
            original_error_type=type(error).__name__,
        )
        return None
    return event.event_id


def _demote_idempotency_key(entity_id: str, evidence_window_start: str) -> str:
    """Key one ``precedent.demote`` command to its precedent and evidence window.

    Two overlapping runs judging the same precedent against the same
    evidence-window start (the later of its last successful
    ``precedent.promote`` and ``precedent.reinstate`` — see
    :func:`~trellis.learning.precedent_evidence_gate.discover_current_precedents_with_coverage`)
    must settle on exactly one demotion and one cap charge, not two
    racing commands both landing as ``"demoted"``. A human reinstatement
    moves the window start forward, which changes this key, so a later,
    legitimately new demotion (fresh unhelpful citations after the
    reinstatement) is never deduplicated against the one the
    reinstatement already undid.
    """
    digest = hashlib.sha256(
        f"precedent.demote\0{entity_id}\0{evidence_window_start}".encode()
    ).hexdigest()
    return f"precedent-demote:{digest}"


def run_precedent_auto_demotion(
    *,
    policy: PrecedentAutoDemotePolicy,
    event_log: EventLog,
    graph_store: GraphStore,
    executor: MutationExecutor | None = None,
    requested_by: str | None = None,
    source: str = "precedent_auto_demote",
    force_report_only: bool = False,
    configured_mode: PrecedentAutoDemoteMode | None = None,
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS,
    promotion_scan_limit: int = _PROMOTION_SCAN_LIMIT,
    join_limit: int = _JOIN_SCAN_LIMIT,
) -> PrecedentAutoDemoteReport:
    """Judge every current precedent and, in commit mode, demote what qualifies.

    Calls
    :func:`trellis.learning.precedent_evidence_gate.judge_current_precedents`
    for the discovery/join/judge logic — this function adds only the
    translation into this module's own disposition vocabulary
    (:func:`_gate_outcome_to_disposition`) and, in commit mode, the
    governed write loop.

    Every ``would_demote`` verdict is reported, and in commit mode every
    one is either demoted, reported ``"skipped_rate_limited"`` (the
    ``max_demotions_per_run`` cap was already reached), or reported
    ``"refused:<rejection_reason>"`` (the handler declined the write — most
    commonly because the precedent was promoted by a human, not by this
    policy's own promotion sibling) — nothing is silently dropped.

    ``force_report_only=True`` is an extra guard, checked ahead of
    :attr:`PrecedentAutoDemotePolicy.effective_mode` and never mixed into
    the policy object itself, mirroring
    :func:`trellis.learning.candidate_auto_promote.run_candidate_auto_promotion`'s
    own ``--dry-run`` shape.

    ``configured_mode`` lets a caller report the pre-force value the same
    way that sibling function's parameter does. Omitted (or ``None``),
    this falls back to ``policy.mode``.

    ``requested_by`` defaults to
    :data:`~trellis.mutate.handlers.AUTO_DEMOTE_REQUESTED_BY`, imported
    here lazily (inside this function, not at module level) rather than
    as a top-level default-parameter value: ``trellis.mutate.handlers``
    itself imports ``trellis.core.settings_registry`` (for
    ``SettingsUpdateHandler``), and ``settings_registry`` imports this
    module's own env-var constants for the ``learning.precedent_auto_demote.*``
    :class:`~trellis.core.settings_registry.SettingSpec` entries — a
    module-level import of ``handlers`` here would complete that cycle.
    Mirrors how :func:`trellis.mutate.build_curate_executor` already
    defers its own import of ``trellis.mutate.handlers`` for the same
    reason.

    Exactly one :attr:`EventType.LEARNING_AUTO_DEMOTE_EVALUATED` event is
    emitted per call, covering every current precedent the evidence gate
    discovered — report-only and commit runs both emit it.

    Raises:
        ValueError: If the effective mode is ``"commit"`` but no
            ``executor`` was given.
    """
    if requested_by is None:
        from trellis.mutate.handlers import (  # noqa: PLC0415
            AUTO_DEMOTE_REQUESTED_BY,
        )

        requested_by = AUTO_DEMOTE_REQUESTED_BY
    if configured_mode is None:
        configured_mode = policy.mode
    effective_mode: PrecedentAutoDemoteMode = (
        "report_only" if force_report_only else policy.effective_mode
    )
    if effective_mode == "commit" and executor is None:
        msg = (
            "run_precedent_auto_demotion: effective mode is 'commit' but "
            "no executor was given."
        )
        raise ValueError(msg)

    gate_outcomes, scan = judge_current_precedents(
        event_log,
        graph_store,
        min_unhelpful_citations=min_unhelpful_citations,
        promotion_scan_limit=promotion_scan_limit,
        join_limit=join_limit,
    )

    outcomes: list[PrecedentAutoDemoteOutcome] = []
    demotions_committed = 0

    for gate_outcome in gate_outcomes:
        disposition, reason = _gate_outcome_to_disposition(gate_outcome)

        if disposition != DISPOSITION_WOULD_DEMOTE or effective_mode != "commit":
            outcomes.append(
                PrecedentAutoDemoteOutcome(
                    entity_id=gate_outcome.entity_id,
                    disposition=disposition,
                    reason=reason,
                    promoted_at=gate_outcome.promoted_at,
                    appearances=gate_outcome.appearances,
                    helpful_count=gate_outcome.helpful_count,
                    unhelpful_count=gate_outcome.unhelpful_count,
                )
            )
            continue

        if demotions_committed >= policy.max_demotions_per_run:
            outcomes.append(
                PrecedentAutoDemoteOutcome(
                    entity_id=gate_outcome.entity_id,
                    disposition="skipped_rate_limited",
                    reason=reason,
                    promoted_at=gate_outcome.promoted_at,
                    appearances=gate_outcome.appearances,
                    helpful_count=gate_outcome.helpful_count,
                    unhelpful_count=gate_outcome.unhelpful_count,
                )
            )
            continue

        assert executor is not None  # narrowed by the ValueError guard above
        rationale = (
            "Auto-demoted by trellis.learning.precedent_auto_demote: "
            f"helpful_count={gate_outcome.helpful_count}, "
            f"unhelpful_count={gate_outcome.unhelpful_count}, "
            f"appearances={gate_outcome.appearances}."
        )
        cmd = Command(
            operation=Operation.PRECEDENT_DEMOTE,
            args={"target_id": gate_outcome.entity_id, "reason": rationale},
            target_id=gate_outcome.entity_id,
            target_type="precedent",
            requested_by=requested_by,
            idempotency_key=_demote_idempotency_key(
                gate_outcome.entity_id, gate_outcome.promoted_at or ""
            ),
        )
        result = executor.execute(cmd)
        if result.status == CommandStatus.SUCCESS:
            demotions_committed += 1
            outcomes.append(
                PrecedentAutoDemoteOutcome(
                    entity_id=gate_outcome.entity_id,
                    disposition="demoted",
                    reason=reason,
                    promoted_at=gate_outcome.promoted_at,
                    appearances=gate_outcome.appearances,
                    helpful_count=gate_outcome.helpful_count,
                    unhelpful_count=gate_outcome.unhelpful_count,
                )
            )
        elif result.status == CommandStatus.DUPLICATE:
            # An overlapping run already committed (or is mid-flight on)
            # this exact precedent + evidence-window-start combination --
            # never counted toward demotions_committed, and never
            # reported as "demoted", so a replayed command cannot double
            # a demotion or silently burn the per-run cap on a no-op.
            outcomes.append(
                PrecedentAutoDemoteOutcome(
                    entity_id=gate_outcome.entity_id,
                    disposition="deduplicated",
                    reason=reason,
                    promoted_at=gate_outcome.promoted_at,
                    appearances=gate_outcome.appearances,
                    helpful_count=gate_outcome.helpful_count,
                    unhelpful_count=gate_outcome.unhelpful_count,
                )
            )
        else:
            rejection_reason = result.metadata.get(
                "rejection_reason", result.status.value
            )
            outcomes.append(
                PrecedentAutoDemoteOutcome(
                    entity_id=gate_outcome.entity_id,
                    disposition=f"refused:{rejection_reason}",
                    reason=reason,
                    promoted_at=gate_outcome.promoted_at,
                    appearances=gate_outcome.appearances,
                    helpful_count=gate_outcome.helpful_count,
                    unhelpful_count=gate_outcome.unhelpful_count,
                )
            )

    notes: list[str] = []
    if scan.truncated and scan.note:
        notes.append(scan.note)

    event_id = _emit_evaluated_event(
        event_log,
        outcomes=outcomes,
        policy=policy,
        effective_mode=effective_mode,
        configured_mode=configured_mode,
        scan=scan,
        source=source,
    )

    return PrecedentAutoDemoteReport(
        enabled=policy.enabled,
        mode=effective_mode,
        configured_mode=configured_mode,
        outcomes=outcomes,
        event_id=event_id,
        min_unhelpful_citations=min_unhelpful_citations,
        scan=scan,
        notes=notes,
    )


def _outcome_to_dict(outcome: PrecedentAutoDemoteOutcome) -> dict[str, Any]:
    """Serialise a :class:`PrecedentAutoDemoteOutcome` for ``--format json``."""
    return {
        "entity_id": outcome.entity_id,
        "disposition": outcome.disposition,
        "reason": outcome.reason,
        "promoted_at": outcome.promoted_at,
        "appearances": outcome.appearances,
        "helpful_count": outcome.helpful_count,
        "unhelpful_count": outcome.unhelpful_count,
    }


def report_to_dict(report: PrecedentAutoDemoteReport) -> dict[str, Any]:
    """Serialise a :class:`PrecedentAutoDemoteReport` for ``--format json``.

    ``status`` is one of ``"ok"`` | ``"degraded"`` | ``"failed"``, via
    :func:`_run_status` (same vocabulary and same function the emitted
    event's own ``outcome`` uses, see :func:`_emit_evaluated_event`):
    ``"failed"`` when at least one precedent's demotion was actually
    attempted (:attr:`PrecedentAutoDemoteReport.demoted_count` +
    :attr:`PrecedentAutoDemoteReport.refused_count`) and none of those
    attempts succeeded; ``"degraded"`` when some attempts succeeded and
    some were refused; ``"ok"`` otherwise -- including a report-only run,
    where nothing is ever attempted. A run that failed before reaching
    :func:`judge_current_precedents` never builds a report at all, and is
    reported instead through
    :func:`emit_precedent_auto_demote_failure_event`'s own
    ``"refused"``/``"failed"`` event payload. Named here, rather than
    left implicit, so this JSON shape and the CLI's exit code are both
    read off the same flag (``tests/unit/test_format_exit_parity_rule.py``).

    Unlike :func:`trellis.learning.candidate_auto_promote.report_to_dict`,
    there is no skipped-count-driven ``"degraded"`` case here: that
    sibling's ``skipped_count`` exists because an id-less candidate never
    becomes an outcome at all, while this gate never drops a precedent
    silently — a truncated scan's casualties are still named,
    individually, as ``"skipped_incomplete_evidence"`` outcomes (counted
    in ``skipped_incomplete_evidence_count``), not folded into a
    run-level flag. ``"degraded"``/``"failed"`` here come only from a
    commit-mode write actually being refused. Both commands exit 0 for a
    completed run regardless of status.
    """
    return {
        "status": _run_status(
            succeeded=report.demoted_count, attempt_failed=report.refused_count
        ),
        "enabled": report.enabled,
        "mode": report.mode,
        "configured_mode": sanitize_configured_mode(report.configured_mode),
        "would_demote_count": report.would_demote_count,
        "demoted_count": report.demoted_count,
        "deduplicated_count": report.deduplicated_count,
        "skipped_rate_limited_count": report.skipped_rate_limited_count,
        "refused_count": report.refused_count,
        "already_demoted_count": report.already_demoted_count,
        "skipped_incomplete_evidence_count": report.skipped_incomplete_evidence_count,
        "ineligible_count": report.ineligible_count,
        "min_unhelpful_citations": report.min_unhelpful_citations,
        "attribution_caveat": report.attribution_caveat,
        "event_id": report.event_id,
        "scan": report.scan.model_dump(),
        "notes": report.notes,
        "outcomes": [_outcome_to_dict(o) for o in report.outcomes],
    }


__all__ = [
    "DEFAULT_MAX_DEMOTIONS_PER_RUN",
    "MAX_DEMOTIONS_PER_RUN_FLOOR",
    "PRECEDENT_AUTO_DEMOTE_ENABLED_ENV",
    "PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING",
    "PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV",
    "PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_SETTING",
    "PRECEDENT_AUTO_DEMOTE_MODE_ENV",
    "PRECEDENT_AUTO_DEMOTE_MODE_SETTING",
    "PrecedentAutoDemoteMode",
    "PrecedentAutoDemoteOutcome",
    "PrecedentAutoDemotePolicy",
    "PrecedentAutoDemoteReport",
    "emit_precedent_auto_demote_failure_event",
    "report_to_dict",
    "resolve_precedent_auto_demote_overrides",
    "run_precedent_auto_demotion",
    "sanitize_configured_mode",
]
