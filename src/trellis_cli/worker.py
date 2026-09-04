"""``trellis worker`` — unattended curation/learning workers.

This module owns the ``worker`` command group. It ships the autonomy
surfaces from ``docs/design/adr-autonomy-ladder.md``:

* :func:`tune_cmd` (``trellis worker tune``) — Tier-1: runs one
  :class:`RuleTuner` pass and, when ``learning.auto_promote.enabled`` is
  set, auto-promotes every qualifying proposal through the *same*
  governance pipeline ``trellis metrics promote --commit`` uses — no new
  mutation path.
* :func:`curate_cmd` (``trellis worker curate``) — Tier-2: one full
  curation cycle (effectiveness feedback → advisory generation → advisory
  fitness → learning-candidate artifacts). The promote-half stays
  human-gated: candidates are written to ``--output-dir`` for review via
  ``trellis curate promote-learning``; this command never promotes.
  ``--interval`` turns it into a plain ``while + sleep`` loop with a
  graceful SIGINT/SIGTERM shutdown — no scheduler dependency.
* :func:`enrich_cmd` (``trellis worker enrich``) — batch LLM enrichment of
  unenriched / low-confidence-tagged documents.
* :func:`mine_precedents_cmd` (``trellis worker mine-precedents``) — wraps
  :meth:`PrecedentMiner.generate_precedent_candidates`.
* :func:`capture_sessions_cmd` (``trellis worker capture-sessions``) — one
  Claude Code session-capture sweep, delegating to the same
  :func:`~trellis_workers.session_capture.sweep.run_sweep` the
  ``trellis-session-capture`` console script runs.
* :func:`embed_traces_cmd` (``trellis worker embed-traces``) — one
  trace-summary embed pass, so traces are reachable by semantic search at
  all; see :mod:`trellis_workers.trace_embed`.

The ``worker_app`` lived in ``trellis_cli.main`` as an empty group; it has
moved here. ``main`` imports it from this module.
"""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import typer
import yaml
from rich.markup import escape

from trellis.core.derived_metadata import apply_derived_metadata
from trellis.core.hashing import content_hash
from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.core.vector_metadata import (
    resolve_vector_store,
    sync_vector_metadata,
)
from trellis.errors import BackendNotInstalledError, StaleStoreWriteError
from trellis.learning import (
    analyze_learning_observations,
    build_learning_observations_from_event_log,
    write_learning_review_artifacts,
)
from trellis.learning.tuners import (
    AutoPromotePolicy,
    PostPromotionPolicy,
    RuleTuner,
    report_to_dict,
    run_auto_promotion,
)
from trellis.ops.write_health import record_write_rejection
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.schemas.memory_op import (
    REF_TYPE_DOCUMENT,
    InputDigest,
    JudgedOpType,
    SubjectRef,
)
from trellis.stores.advisory_source import (
    ADVISORY_FILENAME,
    ADVISORY_WRITER_SURFACE,
    resolve_advisory_path,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.analyze import (
    _build_learning_registry_or_exit,
    _render_advisory_degradation,
)
from trellis_cli.config import get_config_dir, get_data_dir
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import (
    _get_registry,
    get_document_store,
    get_event_log,
    get_outcome_store,
    get_parameter_store,
    get_trace_store,
    get_tuner_state_store,
)

if TYPE_CHECKING:
    from trellis.ops import ParameterRegistry
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.trace import TraceStore
    from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger(__name__)

worker_app = typer.Typer(help="Run curation workers", no_args_is_help=True)
console = build_console()

#: Auto-promote config lives in the main Trellis config under
#: ``learning.auto_promote``. We read it from ``config.yaml`` in the config
#: dir, mirroring the ``learning_params.yaml`` plumbing in
#: ``trellis_cli.analyze`` but keyed inside the shared config file.
CONFIG_FILENAME = "config.yaml"
AUTO_PROMOTE_CONFIG_SECTION = "auto_promote"
LEARNING_CONFIG_SECTION = "learning"


@dataclass(frozen=True, slots=True)
class _RawAutoPromoteConfig:
    """The raw ``learning.auto_promote`` block, post type-validation."""

    enabled: bool
    min_sample_size: int
    min_effect_size: float
    require_baseline: bool
    post_min_samples: int
    post_regression_threshold: float
    post_lookback_days: int


def _load_auto_promote_config() -> _RawAutoPromoteConfig | None:
    """Load ``learning.auto_promote`` from ``config.yaml``, if present.

    Returns ``None`` when the file or section is absent — the caller then
    falls back to a disabled policy (global default OFF). Raises
    :class:`typer.BadParameter` if the section exists but is malformed, so
    operators get a loud error rather than a silent default. Unknown keys
    in the section are rejected to honour ``extra="forbid"`` discipline.
    """
    config_path = get_config_dir() / CONFIG_FILENAME
    if not config_path.exists():
        return None
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"Invalid YAML in {config_path}: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(raw, dict):
        msg = f"{config_path}: expected a mapping, got {type(raw).__name__}"
        raise typer.BadParameter(msg)

    learning = raw.get(LEARNING_CONFIG_SECTION)
    if not isinstance(learning, dict):
        return None
    section = learning.get(AUTO_PROMOTE_CONFIG_SECTION)
    if section is None:
        return None
    if not isinstance(section, dict):
        msg = (
            f"{config_path}: learning.{AUTO_PROMOTE_CONFIG_SECTION} must be a "
            f"mapping, got {type(section).__name__}"
        )
        raise typer.BadParameter(msg)

    allowed = {
        "enabled",
        "min_sample_size",
        "min_effect_size",
        "require_baseline",
        "post_min_samples",
        "post_regression_threshold",
        "post_lookback_days",
    }
    unknown = set(section) - allowed
    if unknown:
        msg = (
            f"{config_path}: unknown key(s) in learning."
            f"{AUTO_PROMOTE_CONFIG_SECTION}: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}."
        )
        raise typer.BadParameter(msg)

    prefix = f"{config_path}: learning.{AUTO_PROMOTE_CONFIG_SECTION}"

    def _int(key: str, default: int) -> int:
        value = section.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            msg = f"{prefix}.{key} is not an int: {value!r}"
            raise typer.BadParameter(msg) from exc

    def _float(key: str, default: float) -> float:
        value = section.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            msg = f"{prefix}.{key} is not a number: {value!r}"
            raise typer.BadParameter(msg) from exc

    def _bool(key: str, default: bool) -> bool:
        value = section.get(key, default)
        if not isinstance(value, bool):
            msg = f"{prefix}.{key} must be true/false, got {value!r}"
            raise typer.BadParameter(msg)
        return value

    # Defaults intentionally mirror the AutoPromotePolicy / PostPromotionPolicy
    # constructor defaults so an operator only sets what they want to change.
    return _RawAutoPromoteConfig(
        enabled=_bool("enabled", False),
        min_sample_size=_int("min_sample_size", 30),
        min_effect_size=_float("min_effect_size", 0.25),
        require_baseline=_bool("require_baseline", True),
        post_min_samples=_int("post_min_samples", 20),
        post_regression_threshold=_float("post_regression_threshold", 0.10),
        post_lookback_days=_int("post_lookback_days", 7),
    )


def _build_auto_promote_policy() -> AutoPromotePolicy:
    """Build the :class:`AutoPromotePolicy` from config, or a disabled default.

    Absent config => ``AutoPromotePolicy(enabled=False)`` — global default
    OFF, zero behaviour change versus running the tuner alone.
    """
    cfg = _load_auto_promote_config()
    if cfg is None:
        return AutoPromotePolicy(enabled=False)
    return AutoPromotePolicy(
        enabled=cfg.enabled,
        min_sample_size=cfg.min_sample_size,
        min_effect_size=cfg.min_effect_size,
        require_baseline=cfg.require_baseline,
        post_promotion=PostPromotionPolicy(
            min_samples_post_promote=cfg.post_min_samples,
            regression_threshold=cfg.post_regression_threshold,
            auto_demote=True,
            lookback_window=timedelta(days=cfg.post_lookback_days),
        ),
    )


def _build_auto_promote_policy_or_exit() -> AutoPromotePolicy:
    """Build the policy, translating config errors into a clean CLI exit."""
    try:
        return _build_auto_promote_policy()
    except typer.BadParameter as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc
    except ValueError as exc:
        # AutoPromotePolicy.__post_init__ rejects thresholds looser than the
        # manual gate or a disarmed rollback.
        console.print(f"[red]invalid learning.auto_promote config: {exc}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc


@worker_app.command("tune")
def tune_cmd(
    tuner_name: str = typer.Option("rule_tuner", "--tuner-name"),
    since_days: int | None = typer.Option(
        None,
        "--since-days",
        help="Force rescan of the last N days (ignores the tuner cursor).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would auto-promote without mutating or emitting.",
    ),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Run a RuleTuner pass and auto-promote qualifying proposals (Tier 1).

    Reuses the same RuleTuner logic behind ``trellis metrics tune``. When
    ``learning.auto_promote.enabled`` is set in ``config.yaml``, each
    proposal that clears the *stricter* auto thresholds is promoted through
    the same governance pipeline ``trellis metrics promote --commit`` uses,
    emits ``PARAMS_AUTO_PROMOTED``, and is armed with post-promotion
    monitoring (``auto_demote=True``) so degradation triggers an
    auto-rollback and ``PARAMS_AUTO_ROLLED_BACK``.

    Proposals that do not clear the auto gate stay ``pending`` for manual
    review via ``trellis metrics promote`` — they are reported, never
    rejected. With auto-promote disabled (the default) this command is a
    pure tuner pass: zero promotions, zero events beyond the tuner's own.
    """
    policy = _build_auto_promote_policy_or_exit()

    tuner = RuleTuner(
        get_outcome_store(),
        get_tuner_state_store(),
        tuner_name=tuner_name,
    )
    since = (
        datetime.now(UTC) - timedelta(days=since_days)
        if since_days is not None
        else None
    )

    report = run_auto_promotion(
        tuner=tuner,
        parameter_store=get_parameter_store(),
        tuner_state=get_tuner_state_store(),
        outcome_store=get_outcome_store(),
        event_log=get_event_log(),
        policy=policy,
        since=since,
        dry_run=dry_run,
        source="trellis.worker.tune",
    )

    if output_format == "json":
        payload = report_to_dict(report)
        payload["status"] = "ok"
        payload["tuner_name"] = tuner_name
        emit_json(payload)
        return

    _render_text(report, tuner_name=tuner_name)


def _render_text(report: Any, *, tuner_name: str) -> None:
    """Human-readable rendering of an :class:`AutoPromoteReport`."""
    mode = (
        "DISABLED" if not report.enabled else ("DRY-RUN" if report.dry_run else "LIVE")
    )
    console.print(
        f"[bold]worker tune[/bold] tuner={tuner_name} mode={mode} "
        f"→ {report.proposals_considered} proposal(s) considered"
    )
    console.print(
        f"  auto-promoted: {report.auto_promoted}  "
        f"rolled-back: {report.rolled_back}  "
        f"pending-manual: {report.pending_manual}"
    )
    for outcome in report.outcomes:
        color = {
            "auto_promoted": "green",
            "would_auto_promote": "cyan",
            "pending_manual": "yellow",
            "disabled": "dim",
            "skipped": "red",
        }.get(outcome.disposition, "white")
        suffix = ""
        if outcome.params_version:
            suffix += f"  → {outcome.params_version}"
        if outcome.rolled_back_to:
            suffix += f"  ⟲ rolled back to {outcome.rolled_back_to}"
        console.print(
            f"  [{color}]{outcome.disposition}[/{color}] "
            f"{escape(outcome.proposal_id[:18])}…  {outcome.reason}{suffix}"
        )
    if report.pending_manual:
        console.print(
            "[dim]Pending proposals stay eligible for manual review: "
            "'trellis metrics promote <proposal_id> --commit'.[/dim]"
        )


# ---------------------------------------------------------------------------
# worker curate — Tier-2 full curation cycle (ADR autonomy ladder)
# ---------------------------------------------------------------------------
#
# The cycle calls the LIBRARY functions directly (no shelling out to other
# CLI commands) in a fixed order:
#
#   1. run_effectiveness_feedback   — demote: noise-tag low-value items
#   2. AdvisoryGenerator.generate   — mine advisories from outcome data
#   3. run_advisory_fitness_loop    — adjust advisory confidence / suppress
#   4. build_learning_observations_from_event_log
#      + analyze_learning_observations
#      + write_learning_review_artifacts  — promote-HALF artifacts only
#
# Step 4 is surface-only: it writes review artifacts to ``--output-dir``.
# Promotion itself stays human-gated via ``trellis curate promote-learning``
# (Tier 2, docs/design/adr-autonomy-ladder.md). This command never promotes.


@dataclass(frozen=True, slots=True)
class CurateCycleResult:
    """Per-cycle counts from :func:`run_curation_cycle`.

    Every field defaults to ``0`` / ``None`` so a skipped stage reads as a
    clean no-op rather than a missing key. ``dry_run`` mirrors the flag the
    cycle ran under; ``candidates_path`` / ``decisions_path`` are ``None``
    when the learning stage was skipped or ran dry.
    """

    noise_tagged: int = 0
    advisories_generated: int = 0
    #: Findings that cleared the sample floor on the arm carrying them but
    #: had no comparison arm to be measured against, so nothing was emitted
    #: for them. Carried to the nightly surface because this is where an
    #: operator sees the generated count drop and has nothing to explain it
    #: (#383).
    advisories_refused: int = 0
    advisories_suppressed: int = 0
    advisories_boosted: int = 0
    learning_observations: int = 0
    learning_candidates: int = 0
    candidates_path: str | None = None
    decisions_path: str | None = None
    skipped_stages: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = False
    #: Set when the advisory file could not be read in full, in which case
    #: both advisory stages were skipped rather than run against a store
    #: whose every ``get`` answers ``None`` (#393). This is the nightly
    #: surface, and it is where a corrupt file otherwise reports a clean
    #: cycle: zeros for the advisory counts are indistinguishable from a
    #: quiet night unless something says which it was.
    advisory_store_degraded: dict[str, Any] | None = None
    #: Set when another process wrote the advisory file while this cycle
    #: held a view of it, so the remaining advisory writes were refused
    #: rather than replacing that process's rows (#438). Distinct from
    #: :attr:`advisory_store_degraded` because the *fix* differs: a
    #: degraded store needs an operator to look at the file, a stale one
    #: needs nothing at all — the next cycle re-reads and re-derives. It is
    #: carried anyway rather than swallowed, because a cycle that wrote
    #: fewer advisories than it computed is not a quiet night, and a
    #: refusal that recurs every night is a second writer nobody knows
    #: about.
    advisory_store_stale: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        """``"degraded"`` when a stage was skipped for a broken store.

        The headline, not just the body. ``"ok"`` over a cycle that skipped
        advisory generation is the same lie as an unexplained zero: a
        wrapper reading only ``status`` would record a clean nightly run
        against a file the store could not read (#393).

        The **exit code follows this field**: anything but ``"ok"`` exits
        :data:`~trellis_cli.exit_codes.EXIT_STORE` (#448, #489), on both format
        surfaces. That reverses an earlier decision here — that a cycle which
        still ran its noise-tag and learning stages "did real work", so failing
        the cron job would misreport those. The stages that ran are in the
        payload either way; what the zero exit reported was that nothing needed
        looking at, and on the one *unattended* advisory writer that is the
        only signal a shell ever sees. The refusal is also carried in
        ``advisory_store_degraded`` / ``advisory_store_stale``, in a red banner
        on the text surface, in an ``error``-level log line, and — since #448 —
        in a ``WRITE_REJECTED`` event that ``trellis analyze health`` counts,
        which is the half that makes recurrence legible.

        ``"stale"`` is the third value and reports the other refusal
        (#438): the file was healthy, another process wrote it mid-cycle,
        and this cycle declined to overwrite that. Still distinct from
        ``"degraded"`` even though the two now share an exit code — the
        fixes differ, so a wrapper reading the payload can tell a broken
        file from a transient race — and not folded into ``"ok"``, because
        the advisory counts are then lower than the cycle computed and
        nothing else says why. ``degraded`` wins if both somehow apply: it
        is the one that needs a human.
        """
        if self.advisory_store_degraded:
            return "degraded"
        return "stale" if self.advisory_store_stale else "ok"

    def to_dict(self) -> dict[str, Any]:
        """Flat JSON-friendly view for ``--format json`` and structured logs."""
        return {
            "noise_tagged": self.noise_tagged,
            "advisories_generated": self.advisories_generated,
            "advisories_refused": self.advisories_refused,
            "advisories_suppressed": self.advisories_suppressed,
            "advisories_boosted": self.advisories_boosted,
            "advisory_store_degraded": self.advisory_store_degraded,
            "advisory_store_stale": self.advisory_store_stale,
            "learning_observations": self.learning_observations,
            "learning_candidates": self.learning_candidates,
            "candidates_path": self.candidates_path,
            "decisions_path": self.decisions_path,
            "skipped_stages": list(self.skipped_stages),
            "dry_run": self.dry_run,
        }


def run_curation_cycle(
    *,
    event_log: EventLog,
    document_store: DocumentStore,
    vector_store: VectorStore | None,
    advisory_store: AdvisoryStore,
    learning_registry: ParameterRegistry,
    output_dir: Path,
    days: int = 30,
    dry_run: bool = False,
    skip_noise_tags: bool = False,
    skip_advisories: bool = False,
    skip_learning: bool = False,
    no_meta_trace: bool = False,
) -> CurateCycleResult:
    """Run one full curation cycle against injected stores.

    Factored out of :func:`curate_cmd` so both the one-shot CLI path and
    the ``--interval`` loop body call the same code, and so unit / loop
    tests can drive a cycle without spawning a process or sleeping.

    Stages run in the fixed order documented at module level. Each stage
    is wrapped in its own ``wrap_cli_meta_analysis`` context so the
    meta-trace graph attributes findings per stage rather than lumping the
    whole cycle into one Activity.

    ``vector_store`` is **required and keyword-only, but may be ``None``**.
    It is required precisely because it was once absent: this function is
    what production's nightly ``curate-nightly`` cron runs, and it demoted
    document-store-only for the whole life of #338's fix, re-opening the
    divergence that fix closed on the one path nobody watches (#381). A
    default of ``None`` would have made that omission look deliberate. A
    deployment with no vector store still passes ``None`` explicitly, and
    :func:`~trellis.core.vector_metadata.resolve_vector_store` is the
    helper that produces one (or ``None``, loudly) from a registry.

    ``dry_run`` semantics:

    * noise-tag stage — analysis only via the read-only
      :func:`analyze_effectiveness`; no ``apply_noise_tags`` write.
    * advisory stages — skipped entirely (both ``generate`` and the
      fitness loop mutate the advisory store).
    * learning stage — observations are still built and scored, but no
      artifacts are written to disk.

    Returns a :class:`CurateCycleResult` with per-stage counts.
    """
    skipped: list[str] = []

    noise = _curate_stage_noise_tags(
        event_log,
        document_store,
        vector_store=vector_store,
        days=days,
        dry_run=dry_run,
        skip=skip_noise_tags,
        no_meta_trace=no_meta_trace,
        skipped=skipped,
    )
    advisory = _curate_stage_advisories(
        event_log,
        advisory_store,
        days=days,
        dry_run=dry_run,
        skip=skip_advisories,
        no_meta_trace=no_meta_trace,
        skipped=skipped,
    )
    learning = _curate_stage_learning(
        event_log,
        learning_registry,
        output_dir=output_dir,
        days=days,
        dry_run=dry_run,
        skip=skip_learning,
        no_meta_trace=no_meta_trace,
        skipped=skipped,
    )

    return CurateCycleResult(
        noise_tagged=noise["noise_tagged"],
        advisories_generated=advisory["advisories_generated"],
        advisories_refused=advisory["advisories_refused"],
        advisories_suppressed=advisory["advisories_suppressed"],
        advisories_boosted=advisory["advisories_boosted"],
        advisory_store_degraded=advisory["store_degradation"],
        advisory_store_stale=advisory["store_stale"],
        learning_observations=learning["learning_observations"],
        learning_candidates=learning["learning_candidates"],
        candidates_path=learning["candidates_path"],
        decisions_path=learning["decisions_path"],
        skipped_stages=tuple(skipped),
        dry_run=dry_run,
    )


def _curate_stage_noise_tags(
    event_log: EventLog,
    document_store: DocumentStore,
    *,
    vector_store: VectorStore | None,
    days: int,
    dry_run: bool,
    skip: bool,
    no_meta_trace: bool,
    skipped: list[str],
) -> dict[str, int]:
    """Stage 1 — effectiveness feedback (demote / noise-tag).

    In dry-run the read-only :func:`analyze_effectiveness` is used so the
    candidate count is still reported without writing noise tags — and no
    vector row is touched either, since nothing was written to mirror.

    ``vector_store`` is forwarded so the demotion reaches the *semantic*
    axis. Without it the write lands in the document store alone and
    :class:`~trellis.retrieve.strategies.SemanticSearch` keeps serving the
    item's pre-demotion embed-time snapshot (#338, re-opened on this path
    by #381).
    """
    if skip:
        skipped.append("noise_tags")
        return {"noise_tagged": 0}

    from trellis.retrieve.effectiveness import (  # noqa: PLC0415
        analyze_effectiveness,
    )

    with wrap_cli_meta_analysis(
        agent_suffix="worker",
        analyzer_name="cli.worker.curate.noise-tags",
        disabled=no_meta_trace,
    ) as record:
        if dry_run:
            report = analyze_effectiveness(event_log, days=days)
        else:
            report = run_effectiveness_feedback(
                event_log,
                document_store,
                days=days,
                vector_store=vector_store,
            )
        # What the evidence gate admitted, not what the usage-rate rule
        # proposed (#336). The two differ by ~60% on the reference
        # deployment, and reporting the proposal here would put a number
        # in the nightly log that no write ever matched.
        screen = report.demotion_screen
        noise_tagged = (
            len(screen.admitted) if screen is not None else len(report.noise_candidates)
        )
        if record.enabled and noise_tagged:
            record.produced_finding(
                f"curate-noise-tags-d{days}",
                finding_type="NoiseTagsApplied",
            )
    return {"noise_tagged": noise_tagged}


#: Bloat guard on the ``msg`` carried into the rejection row. Same value as
#: ``policy_source._MAX_REJECTION_MSG`` and for the same reason: the store's
#: refusal messages already name the file and the recovery command, so the
#: whole point is to carry them verbatim to an operator reading the event
#: without re-running the job that failed.
_MAX_REJECTION_MSG = 500


def _record_refused_advisory_write(
    event_log: EventLog, *, kind: str, message: str
) -> None:
    """Make a refused advisory write visible to ``trellis analyze health``.

    The nightly cron is the only *unattended* advisory writer, and it is
    the one that swallowed the refusal: ``trellis analyze``'s two advisory
    commands exit ``EXIT_STORE`` (``2`` until #489) and ``POST
    /advisories/generate`` answers 409, but this
    stage reported the refusal into a ``status`` field and a structlog line
    and returned. A refusal that recurs every night therefore escalated
    nowhere at all — verified rather than assumed: the reference
    deployment's ``curate-nightly.sh`` pipes this command's JSON to a log
    and reads nothing out of it, and the only consumer downstream
    (``roadmap-nightly.sh``) greps that log's tail for
    ``advisories_generated``. Neither reads ``status`` (#448).

    The signal is a ``WRITE_REJECTED`` event, which is the repo's existing
    channel for "a write died before it became a Command" and is already
    read by ``trellis analyze health``
    (:func:`~trellis.ops.write_health.summarize_write_health`). #425 took
    the same resolution for :func:`build_policy_gate`, and for the same
    reason: ``analyze health`` is the canonical reader, so building a
    second one for a signal it already has would be the wrong shape, and
    ``trellis admin doctor`` does not exist.

    Emitted **here and not on the loud surfaces**. ``analyze`` and the REST
    route already tell their caller, and their caller retries by hand; if
    they emitted too, the count in ``analyze health`` would mix a human
    hitting Ctrl-R with the standing two-writer conflict this exists to
    surface. What the window should read is *nights the unattended writer
    was refused*.

    Fail-soft: :func:`record_write_rejection` swallows an event-log
    failure, and the cycle carries on either way. Telemetry must never
    escalate a contained refusal into a crashed cron job.
    """
    record_write_rejection(
        event_log,
        tool=ADVISORY_WRITER_SURFACE,
        # An explicit row rather than ``classify_rejection``'s fallback:
        # nothing here is a payload the caller could fix, and ``other@``
        # would pool it with every unclassified boundary failure in
        # ``boundary_kinds``. Named, it also reaches ``repeated_collisions``
        # once it recurs, which is the reading that matters — the same file
        # refusing the same writer, night after night.
        rejections=[
            {
                "kind": kind,
                "loc": ADVISORY_FILENAME,
                "msg": message[:_MAX_REJECTION_MSG],
            }
        ],
        source=ADVISORY_WRITER_SURFACE,
    )


def _curate_stage_advisories(
    event_log: EventLog,
    advisory_store: AdvisoryStore,
    *,
    days: int,
    dry_run: bool,
    skip: bool,
    no_meta_trace: bool,
    skipped: list[str],
) -> dict[str, Any]:
    """Stages 2 & 3 — advisory generation + fitness loop.

    Both stages mutate the advisory store, so a dry-run skips them
    wholesale rather than half-running an analysis with no read-only twin.

    A **degraded** store skips them for the same reason and one more: the
    fitness loop's ``put`` / ``suppress`` / ``restore`` all raise on a
    store that could not read its file (#393), and letting that escape
    would take the learning stage down with it. Checking once here is what
    keeps the refusal a *reported skip* rather than a traceback that ends
    the cycle — the store-level raise stays as the backstop for callers
    that never check.
    """
    degradation = advisory_store.degradation
    if skip or dry_run or degradation is not None:
        skipped.append("advisories")
    # Reported whether or not the stage was going to run: a ``--dry-run`` is
    # the natural "is my nightly healthy?" probe, and staying silent there
    # hides the state from the one command an operator runs to look for it.
    if degradation is not None:
        logger.error(
            "worker_curate.advisories_skipped_degraded_store",
            **degradation.to_dict(),
            impact=(
                "Advisory generation and the fitness loop were both skipped. "
                "Suppression decisions in the file are intact and untouched; "
                "no new advisories were produced."
            ),
        )
        # Emitted on a ``--dry-run`` and a ``--skip-advisories`` too, for
        # the same reason the log line above is: the file is broken whether
        # or not this invocation meant to write it, and the run that finds
        # it is most often the health probe.
        _record_refused_advisory_write(
            event_log,
            kind="config_unreadable",
            message=(
                f"{degradation.reason}: {degradation.detail} "
                f"(recovery: {degradation.recovery})"
            ),
        )
    if skip or dry_run or degradation is not None:
        return {
            "advisories_generated": 0,
            "advisories_refused": 0,
            "advisories_suppressed": 0,
            "advisories_boosted": 0,
            "store_degradation": degradation.to_dict() if degradation else None,
            "store_stale": None,
        }

    generated = refused = suppressed = boosted = 0
    stale: dict[str, Any] | None = None
    try:
        with wrap_cli_meta_analysis(
            agent_suffix="worker",
            analyzer_name="cli.worker.curate.advisories",
            disabled=no_meta_trace,
        ) as record:
            gen = AdvisoryGenerator(event_log, advisory_store).generate(days=days)
            generated = gen.advisories_generated
            refused = gen.findings_refused_no_comparison_arm
            fitness = run_advisory_fitness_loop(event_log, advisory_store, days=days)
            suppressed = len(fitness.advisories_suppressed)
            boosted = len(fitness.advisories_boosted)
            if record.enabled and (generated or suppressed):
                record.produced_finding(
                    f"curate-advisories-d{days}",
                    finding_type="AdvisoryCycleReport",
                )
    except StaleStoreWriteError as exc:
        # Another process wrote the advisory file while this cycle held a
        # view of it (#438). Caught here and nowhere deeper for the same
        # reason the degraded pre-check is here: an escaping raise would
        # take the *learning* stage down with it, and that stage neither
        # reads nor writes this file.
        #
        # Deliberately **not** added to ``skipped_stages``. Unlike the
        # degraded case the stage really ran, and the fitness loop writes
        # per advisory, so some adjustments may already have landed — the
        # counts below are what did. Calling that a skip would be a second
        # wrong report on top of the first. ``status`` goes to ``"stale"``
        # and :attr:`CurateCycleResult.advisory_store_stale` carries the
        # refusal.
        stale = {
            "path": exc.path,
            "code": exc.code,
            "message": exc.message,
            "recovery": exc.recovery,
        }
        _record_refused_advisory_write(
            event_log, kind="stale_write", message=exc.message
        )
        # ``error``, not ``exception``: this is an expected, transient race
        # between two writers, and a traceback in the nightly log would
        # read as a crash. ``message`` already carries the recovery.
        logger.error(  # noqa: TRY400 — no traceback on purpose; see above
            "worker_curate.advisories_refused_stale_store",
            **stale,
            advisories_generated=generated,
            advisories_suppressed=suppressed,
            impact=(
                "Another process wrote the advisory file mid-cycle; this "
                "cycle's remaining advisory writes were refused rather than "
                "replacing it. The counts above are what landed before the "
                "refusal. No operator action is needed: the next cycle "
                "re-reads the file and re-derives."
            ),
        )
    return {
        "advisories_generated": generated,
        "advisories_refused": refused,
        "advisories_suppressed": suppressed,
        "advisories_boosted": boosted,
        "store_degradation": None,
        "store_stale": stale,
    }


def _curate_stage_learning(
    event_log: EventLog,
    learning_registry: ParameterRegistry,
    *,
    output_dir: Path,
    days: int,
    dry_run: bool,
    skip: bool,
    no_meta_trace: bool,
    skipped: list[str],
) -> dict[str, Any]:
    """Stage 4 — learning candidates (promote-half artifacts, surface only).

    Observations are always scored; artifacts are written to disk only
    outside dry-run. Promotion itself stays human-gated.
    """
    if skip:
        skipped.append("learning")
        return {
            "learning_observations": 0,
            "learning_candidates": 0,
            "candidates_path": None,
            "decisions_path": None,
        }

    candidates_path: str | None = None
    decisions_path: str | None = None
    with wrap_cli_meta_analysis(
        agent_suffix="worker",
        analyzer_name="cli.worker.curate.learning",
        disabled=no_meta_trace,
    ) as record:
        observations = build_learning_observations_from_event_log(event_log, days=days)
        report = analyze_learning_observations(
            observations=observations,
            registry=learning_registry,
            artifacts_root=output_dir,
        )
        if not dry_run:
            paths = write_learning_review_artifacts(
                report=report, output_dir=output_dir
            )
            candidates_path = paths["candidates_path"]
            decisions_path = paths["decisions_template_path"]
        if record.enabled and report["candidate_count"]:
            record.produced_finding(
                f"curate-learning-d{days}",
                finding_type="LearningCandidatesReport",
            )
    return {
        "learning_observations": report["observation_count"],
        "learning_candidates": report["candidate_count"],
        "candidates_path": candidates_path,
        "decisions_path": decisions_path,
    }


def _advisory_store_from_data_dir() -> AdvisoryStore:
    """Build the AdvisoryStore this deployment reads and writes.

    Resolved through :func:`trellis.stores.advisory_source.resolve_advisory_path`
    (#373) rather than by joining a filename here. Mirroring
    ``trellis_cli.analyze`` is no longer enough on its own: this writer and
    the analyze commands agreed with each other and disagreed with every
    *reader*, which is how 37 nightly-refreshed advisories stayed invisible
    to every pack. Resolution is symmetric, so an existing legacy-path file
    keeps being written in place rather than being orphaned by a second one.
    """
    return AdvisoryStore(resolve_advisory_path(get_data_dir() / "stores"))


def _render_cycle_text(result: CurateCycleResult) -> None:
    """Human-readable rendering of one :class:`CurateCycleResult`."""
    mode = "DRY-RUN" if result.dry_run else "LIVE"
    console.print(f"[bold]worker curate[/bold] mode={mode}")
    console.print(
        f"  noise-tagged: {result.noise_tagged}  "
        f"advisories generated: {result.advisories_generated}  "
        f"refused (no comparison arm): {result.advisories_refused}  "
        f"suppressed: {result.advisories_suppressed}  "
        f"boosted: {result.advisories_boosted}"
    )
    console.print(
        f"  learning observations: {result.learning_observations}  "
        f"candidates: {result.learning_candidates}"
    )
    if result.advisory_store_degraded:
        # Not dim, and above the skipped line: the zeros in the advisory
        # counts printed above are not a quiet night, and the operator has
        # to act before the next cycle does anything (#393).
        _render_advisory_degradation(
            result.advisory_store_degraded,
            console,
            aftermath=(
                "Advisory generation and the fitness loop were skipped; "
                "writes are refused so the file is intact."
            ),
        )
    if result.advisory_store_stale:
        # Yellow, not red: nothing is broken and nothing needs an operator.
        # Printed all the same, because the advisory counts above are lower
        # than this cycle computed and this is the only line that says so
        # (#438).
        stale = result.advisory_store_stale
        console.print(
            "  [bold yellow]ADVISORY WRITE REFUSED (stale)[/bold yellow] — "
            f"{escape(str(stale['message']))}",
            soft_wrap=True,
        )
        console.print(
            "    Another process wrote the file mid-cycle; its rows are "
            "intact. The next cycle re-reads and re-derives.",
            soft_wrap=True,
        )
    if result.skipped_stages:
        console.print(f"  [dim]skipped: {', '.join(result.skipped_stages)}[/dim]")
    if result.candidates_path:
        console.print(f"  candidates: [cyan]{escape(result.candidates_path)}[/cyan]")
        console.print(
            f"  decisions:  [cyan]{escape(str(result.decisions_path))}[/cyan]"
        )
        console.print(
            "[dim]Promotion stays human-gated — review the decisions "
            "template, then run [bold]trellis curate promote-learning[/bold].[/dim]"
        )
    elif not result.dry_run and "learning" not in result.skipped_stages:
        console.print("[dim]No learning candidates met the threshold this cycle.[/dim]")


class _ShutdownFlag:
    """Cooperative shutdown latch toggled by SIGINT / SIGTERM.

    The interval loop polls :attr:`stop` between cycles instead of
    sleeping through the whole interval, so Ctrl-C (SIGINT) or a
    ``kill`` (SIGTERM) drains the current cycle and exits cleanly rather
    than leaving a half-written artifact or a tortured traceback.
    """

    def __init__(self) -> None:
        self.stop = False

    def request(self, signum: int, _frame: Any) -> None:
        logger.info("worker_curate.shutdown_requested", signal=signum)
        self.stop = True


def _run_curate_loop(
    *,
    interval: int,
    output_dir: Path,
    days: int,
    dry_run: bool,
    skip_noise_tags: bool,
    skip_advisories: bool,
    skip_learning: bool,
    no_meta_trace: bool,
    output_format: str,
    max_cycles: int | None = None,
    shutdown: _ShutdownFlag | None = None,
) -> CurateCycleResult | None:
    """Run :func:`run_curation_cycle` on a fixed interval until signalled.

    Plain ``while`` + interruptible sleep — no scheduler dependency
    (APScheduler / Celery explicitly rejected; Trellis stays
    scheduler-agnostic). Emits one structured ``worker_curate.cycle`` log
    line per cycle with the headline counts.

    ``max_cycles`` and the injectable ``shutdown`` flag exist for tests so
    the loop can run a bounded number of cycles without real signals or
    long sleeps; production callers leave both at their defaults.

    Returns the **last** cycle's result, or ``None`` when the flag was
    already set and no cycle ran, so the caller can derive an exit code
    from it. Last rather than worst: a loop that raced with a second writer
    at 03:00 and has run clean for the twelve cycles since is not a state
    anyone should page on, and every cycle's refusal reaches the EventLog
    regardless (:func:`_record_refused_advisory_write`) — which is where
    recurrence is legible and an exit code is not.
    """
    flag = shutdown if shutdown is not None else _ShutdownFlag()
    if shutdown is None:
        # Only install handlers when we own the flag — tests inject their
        # own and drive ``stop`` directly without touching process signals.
        signal.signal(signal.SIGINT, flag.request)
        signal.signal(signal.SIGTERM, flag.request)

    cycle = 0
    result: CurateCycleResult | None = None
    while not flag.stop:
        cycle += 1
        result = run_curation_cycle(
            event_log=get_event_log(),
            document_store=get_document_store(),
            vector_store=resolve_vector_store(_get_registry()),
            advisory_store=_advisory_store_from_data_dir(),
            learning_registry=_build_learning_registry_or_exit(),
            output_dir=output_dir,
            days=days,
            dry_run=dry_run,
            skip_noise_tags=skip_noise_tags,
            skip_advisories=skip_advisories,
            skip_learning=skip_learning,
            no_meta_trace=no_meta_trace,
        )
        logger.info("worker_curate.cycle", cycle=cycle, **result.to_dict())
        if output_format == "json":
            emit_json({"status": result.status, "cycle": cycle, **result.to_dict()})
        else:
            _render_cycle_text(result)

        if max_cycles is not None and cycle >= max_cycles:
            break
        # Interruptible sleep: poll the flag once per second so a signal
        # received mid-interval is honoured promptly.
        slept = 0
        while slept < interval and not flag.stop:
            time.sleep(1)
            slept += 1

    logger.info("worker_curate.loop_stopped", cycles_run=cycle)
    return result


def _exit_if_advisory_write_refused(result: CurateCycleResult | None) -> None:
    """Exit non-zero when a cycle's ``status`` is anything but ``ok`` (#448).

    The invariant is deliberately narrow and total: **the exit code is a
    function of ``status`` and nothing else.** That is what keeps the two
    surfaces from drifting apart again — a cycle that renders
    ``"degraded"`` into JSON and exits 0 is the #437 divergence wearing a
    different hat, and the machine surface is the one a cron reads.

    Called *below* the ``--format`` branch on both paths, never inside an
    arm of it (``tests/unit/test_format_exit_parity_rule.py``).

    The code is :data:`~trellis_cli.exit_codes.EXIT_STORE`, taken from the
    canonical map rather than written out. It was a module-private ``2``
    (#448), on the rule that the advisory refusals must agree *with each
    other* because a cron wrapper branches on the code without knowing
    which of them it hit. That rule is kept and the value moved: ``2`` is
    the one value at which it collides with the map
    :func:`~trellis_cli.exit_codes.exit_code_for` makes canonical, under
    which the same damaged-file condition exits ``5`` from ``trellis policy
    list``. Both rules hold at ``EXIT_STORE`` — the advisory surfaces still
    agree with each other, and one root cause now has one code (#489). The
    module's other non-zero exits stay ``EXIT_INTERNAL``; they report a
    different thing. (Not "the other two": there are eight such sites
    across four functions — ``_build_auto_promote_policy_or_exit``,
    ``_require_llm_client_or_exit``, ``capture_sessions_cmd`` and
    ``embed_traces_cmd`` — and the count #448 wrote out had already gone
    stale, so this states the rule rather than a number that rots.)

    This overturns the previous decision, recorded on
    :attr:`CurateCycleResult.status`, that the exit stays 0 because the
    cycle "still ran its noise-tag and learning stages and did real work".
    That is true and is not the point: ``worker embed-traces`` in this same
    module already exits non-zero on a pass that did most of its work and
    left a gap, on exactly the reasoning that a green exit hides the silent
    gap the worker exists to close. The stages that ran are reported in the
    payload either way; what a zero exit reported was that nothing needed
    looking at.
    """
    if result is None or result.status == "ok":
        return
    raise typer.Exit(code=EXIT_STORE)


@worker_app.command("curate")
def curate_cmd(
    output_dir: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--output-dir",
        "-o",
        help="Directory for learning-candidate review artifacts.",
    ),
    days: int = typer.Option(30, "--days", help="Days of EventLog history to scan."),
    interval: int | None = typer.Option(
        None,
        "--interval",
        help=(
            "Loop mode: re-run the cycle every N seconds until SIGINT/SIGTERM. "
            "Omit for a single cycle. No scheduler dependency — plain sleep."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Analyze only — no noise tags, no advisory mutations, no artifacts.",
    ),
    reconcile_first: bool = typer.Option(
        False,
        "--reconcile-first",
        help=(
            "Backfill pack_feedback.jsonl into the EventLog before the cycle "
            "(runs reconcile_feedback_log_to_event_log against the data dir)."
        ),
    ),
    skip_noise_tags: bool = typer.Option(
        False, "--skip-noise-tags", help="Skip the effectiveness/noise-tag stage."
    ),
    skip_advisories: bool = typer.Option(
        False, "--skip-advisories", help="Skip advisory generation + fitness stages."
    ),
    skip_learning: bool = typer.Option(
        False, "--skip-learning", help="Skip the learning-candidate stage."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording each stage as a meta-Activity.",
    ),
) -> None:
    """Run one full curation cycle (Tier-2 autonomy).

    Calls the curation library functions directly, in order:
    effectiveness feedback (demote / noise-tag) → advisory generation →
    advisory fitness loop → learning-candidate scoring + review artifacts.

    The promote half stays **human-gated**: learning candidates are
    written to ``--output-dir`` for review via
    ``trellis curate promote-learning``. This command NEVER promotes
    (docs/design/adr-autonomy-ladder.md, Tier 2).

    With ``--interval N`` the cycle repeats every ``N`` seconds until
    SIGINT/SIGTERM, logging one structured line per cycle. No scheduler
    dependency is introduced — the interval is a plain-sleep convenience.
    """
    output_dir = output_dir.expanduser()

    if reconcile_first:
        _reconcile_before_cycle()

    if interval is not None:
        if interval <= 0:
            msg = "--interval must be a positive number of seconds"
            raise typer.BadParameter(msg)
        _exit_if_advisory_write_refused(
            _run_curate_loop(
                interval=interval,
                output_dir=output_dir,
                days=days,
                dry_run=dry_run,
                skip_noise_tags=skip_noise_tags,
                skip_advisories=skip_advisories,
                skip_learning=skip_learning,
                no_meta_trace=no_meta_trace,
                output_format=output_format,
            )
        )
        return

    result = run_curation_cycle(
        event_log=get_event_log(),
        document_store=get_document_store(),
        vector_store=resolve_vector_store(_get_registry()),
        advisory_store=_advisory_store_from_data_dir(),
        learning_registry=_build_learning_registry_or_exit(),
        output_dir=output_dir,
        days=days,
        dry_run=dry_run,
        skip_noise_tags=skip_noise_tags,
        skip_advisories=skip_advisories,
        skip_learning=skip_learning,
        no_meta_trace=no_meta_trace,
    )

    if output_format == "json":
        emit_json({"status": result.status, **result.to_dict()})
    else:
        _render_cycle_text(result)
    # Below the format branch, never inside an arm of it: #437 shipped the
    # opposite and the machine surface was the one reporting success.
    _exit_if_advisory_write_refused(result)


def _reconcile_before_cycle() -> None:
    """Backfill the JSONL feedback log into the EventLog before a cycle.

    Thin wrapper around
    :func:`trellis.feedback.recording.reconcile_feedback_log_to_event_log`
    so ``--reconcile-first`` replays any file-only feedback rows the
    cycle would otherwise miss. Logs the resulting counts.

    Scans ``<stores_dir>/feedback`` — where the MCP tool and the REST
    pack-feedback route append — plus ``<data_dir>`` itself, which
    earlier ad-hoc runs used. Reconciliation is keyed on ``feedback_id``
    and idempotent, so covering both costs one extra empty scan and
    never double-emits.

    ``stores_dir`` comes off the registry rather than being re-derived
    from the environment: ``StoreRegistry.from_config_dir`` lets
    ``data_dir:`` in ``config.yaml`` override ``$TRELLIS_DATA_DIR``, so
    an env-only derivation would scan an empty directory and report a
    healthy no-op on exactly the deployments ``trellis admin init
    --data-dir`` produces.
    """
    from trellis.feedback.recording import (  # noqa: PLC0415
        feedback_log_dir,
        reconcile_feedback_log_to_event_log,
    )

    event_log = get_event_log()
    stores_dir = _get_registry().stores_dir
    log_dirs = [get_data_dir()]
    if stores_dir is not None:
        log_dirs.insert(0, feedback_log_dir(stores_dir))
    found = [d for d in log_dirs if (d / "pack_feedback.jsonl").exists()]
    if not found:
        # Loud about the no-op: "reconcile ran and found nothing" and
        # "reconcile looked in the wrong place" are the same silence.
        logger.info(
            "worker_curate.reconcile_no_log",
            log_dirs=[str(d) for d in log_dirs],
        )
        return
    for log_dir in found:
        result = reconcile_feedback_log_to_event_log(log_dir, event_log)
        logger.info(
            "worker_curate.reconciled",
            log_dir=str(log_dir),
            scanned=result.scanned,
            already_present=result.already_present,
            emitted=result.emitted,
            failed=result.failed,
        )


# ---------------------------------------------------------------------------
# worker enrich — batch LLM enrichment of under-tagged documents
# ---------------------------------------------------------------------------


@worker_app.command("enrich")
def enrich_cmd(
    concurrency: int = typer.Option(
        3, "--concurrency", help="Parallel enrichment requests."
    ),
    limit: int = typer.Option(50, "--limit", help="Max documents to enrich this run."),
    reenrich: bool = typer.Option(
        False,
        "--reenrich",
        help=(
            "Also re-enrich documents already enriched once. Default is to skip them."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Select + report candidates without calling the LLM."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Batch-enrich under-tagged documents via :class:`EnrichmentService`.

    **Selection predicate.** A document is a candidate when its
    ``metadata.content_tags`` is missing or empty, or when it has never been
    through this path — ``content_tags.classified_mode != "enrichment"``.
    ``--reenrich`` takes the already-enriched too. The newest ``--limit``
    documents matching the predicate are enriched.

    This used to select on ``content_tags.tag_confidence``, which does not
    work and cannot: that key is not part of ``ContentTags`` and is never
    written, so the read returned ``None`` for every document and the
    threshold could never skip anything. The confidence value behind it was
    itself a constant copied out of the prompt's own example. ``classified_mode``
    is a fact the path actually records.

    Results are written back through the tagging path: each enriched
    document's ``metadata.content_tags`` is updated with the LLM-suggested
    tags / classification / importance plus a fresh ``classified_at`` and
    ``tag_confidence`` stamp, then persisted via ``DocumentStore.put``.

    **Requires an LLM extra.** Enrichment needs a configured ``llm:`` block
    and the matching ``[llm-openai]`` / ``[llm-anthropic]`` extra. When no
    client can be built this command exits non-zero with an actionable
    message — it never silently no-ops.
    """
    document_store = get_document_store()
    llm = _require_llm_client_or_exit()

    candidates = _select_enrichment_candidates(
        document_store, limit=limit, reenrich=reenrich
    )

    if dry_run:
        if output_format == "json":
            emit_json(
                {
                    "status": "ok",
                    "dry_run": True,
                    "selected": len(candidates),
                    "doc_ids": [c["doc_id"] for c in candidates],
                }
            )
        else:
            console.print(
                f"[bold]worker enrich[/bold] DRY-RUN — "
                f"{len(candidates)} candidate(s) selected"
            )
            for cand in candidates:
                console.print(f"  - {escape(cand['doc_id'])}")
        return

    summary = _run_batch_enrichment(
        llm,
        document_store,
        candidates,
        concurrency=concurrency,
        event_log=get_event_log(),
        vector_store=resolve_vector_store(_get_registry()),
    )

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "dry_run": False,
                "selected": len(candidates),
                "enriched": summary.enriched,
                "failed": summary.failed,
                # #421: concurrent writes seen inside the LLM window. Not a
                # failure count — those rows were enriched onto their current
                # content — but a standing rate here means candidate pages are
                # racing another writer and the batch wants a smaller --limit.
                "stale_snapshot": summary.stale_snapshot,
                "vanished": summary.vanished,
                "vector_rows_synced": summary.vector_rows_synced,
            }
        )
        return
    console.print(
        f"[bold]worker enrich[/bold] — {summary.enriched}/{len(candidates)} "
        f"document(s) enriched"
    )
    if summary.stale_snapshot or summary.vanished:
        console.print(
            f"  [yellow]concurrent writes during the LLM window:[/yellow] "
            f"{summary.stale_snapshot} document(s) changed (enriched onto "
            f"current content), {summary.vanished} deleted (skipped)"
        )


def _require_llm_client_or_exit() -> Any:
    """Return a built LLM client or exit loudly when none is available.

    Enrichment is opt-in but must be loud on misuse: an operator who runs
    ``worker enrich`` without an LLM configured gets a clear, actionable
    error naming the missing config / extra rather than a silent skip.
    """
    registry = _get_registry()
    try:
        llm = registry.build_llm_client()
    except BackendNotInstalledError as exc:
        console.print(
            f"[red]worker enrich requires an LLM SDK that is not installed: "
            f"{exc}[/red]\n"
            "[dim]Install it, e.g. 'uv pip install trellis-ai[llm-openai]', "
            "and configure an 'llm:' block in config.yaml.[/dim]"
        )
        raise typer.Exit(code=EXIT_INTERNAL) from exc
    if llm is None:
        console.print(
            "[red]worker enrich requires an LLM client but none is "
            "configured.[/red]\n"
            "[dim]Add an 'llm:' block to ~/.trellis/config.yaml (provider, "
            "api_key_env, model) and install the matching extra "
            "([llm-openai] / [llm-anthropic]).[/dim]"
        )
        raise typer.Exit(code=EXIT_INTERNAL)
    return llm


def _select_enrichment_candidates(
    document_store: DocumentStore,
    *,
    limit: int,
    reenrich: bool = False,
) -> list[dict[str, Any]]:
    """Return documents that have not been through the enrichment path.

    See :func:`enrich_cmd` for the predicate definition. Scans the newest
    documents (a generous multiple of ``limit`` so the filter has headroom)
    and returns at most ``limit`` matches.
    """
    # ``include_chunks`` is named rather than defaulted (#396): this walker
    # scans for un-enriched documents, and a chunk row is as enrichable as
    # its parent.
    scanned = document_store.list_documents(
        limit=max(limit * 5, limit), include_chunks=True
    )
    candidates: list[dict[str, Any]] = []
    for doc in scanned:
        metadata = doc.get("metadata") or {}
        content_tags = metadata.get("content_tags") or {}
        if not content_tags:
            candidates.append(doc)
            continue
        if reenrich or content_tags.get("classified_mode") != "enrichment":
            candidates.append(doc)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _enriched_content_tags(
    prior: Any,
    result: Any,
    *,
    stamp: datetime,
) -> dict[str, Any]:
    """Merge an ``EnrichmentResult`` into a **valid** ``ContentTags`` mapping.

    This path used to write four keys ``ContentTags`` forbids — ``tags``,
    ``auto_class``, ``auto_importance``, ``tag_confidence`` — so every
    enrichment run produced a ``content_tags`` no reader could parse. The
    refresh path caught the ``ValidationError``, logged
    ``existing_tags_malformed``, and re-classified from scratch, which is how
    a whole subsystem's output was discarded in silence.

    Two deliberate omissions:

    * **``auto_tags`` do not become the ``domain`` facet.** ``domain``
      hard-excludes a document from a domain-scoped query on mismatch, so an
      LLM's unreviewed topic guesses cannot go there — the same rule that makes
      ``include_domain=False`` the default everywhere else. They are preserved
      under ``custom["llm_tags"]``, visible and non-excluding, and the
      promotion ladder (#321) is the reviewed path from a proposal to real
      vocabulary.
    * **``tag_confidence`` is not a tag.** It rides the ``EnrichmentResult``
      and the event; it has no home in a tag set.
    """
    from trellis.schemas.classification import ContentTags  # noqa: PLC0415

    tags = (
        ContentTags.model_validate(prior) if isinstance(prior, dict) else ContentTags()
    )
    custom = dict(tags.custom)
    if result.auto_tags:
        custom["llm_tags"] = [str(t) for t in result.auto_tags]
    classified_by = list(dict.fromkeys([*tags.classified_by, "llm_facet"]))

    update: dict[str, Any] = {
        "custom": custom,
        "classified_by": classified_by,
        "classified_at": stamp,
        "classified_mode": "enrichment",
    }
    if result.importance_scored_at is not None:
        update["importance_scored_at"] = result.importance_scored_at
    # Re-validate rather than trusting `model_copy`, which does not coerce —
    # a string stamp would round-trip as a string and silently violate the
    # `datetime` field type.
    merged = ContentTags.model_validate({**tags.model_dump(mode="python"), **update})
    return merged.model_dump(mode="json")


def _enrichment_updates(
    current_metadata: dict[str, Any],
    result: Any,
    *,
    stamp: datetime,
) -> dict[str, Any]:
    """The metadata keys ``worker enrich`` owns, derived against *current*.

    The three keys below are the entire footprint of an enrichment write.
    Expressing it as an overlay rather than as a whole-bag rewrite is what
    lets :func:`~trellis.core.derived_metadata.apply_derived_metadata` carry
    every other key through untouched — including keys a concurrent writer
    added while the LLM was running (#421).

    ``auto_importance`` and ``document_form`` are omitted rather than written
    as falsy when the model supplied neither, so a prior value survives; that
    was the pre-#421 behaviour too, and it is now the row's *current* prior
    rather than the snapshot's.
    """
    updates: dict[str, Any] = {
        "content_tags": _enriched_content_tags(
            current_metadata.get("content_tags"), result, stamp=stamp
        )
    }
    # `auto_importance` is read from *flat* metadata by
    # `retrieve.strategies._apply_importance`; writing it inside
    # `content_tags` (as this path used to) put it where no reader looks.
    if result.auto_importance:
        updates["auto_importance"] = result.auto_importance
    # The enrichment vocabulary is a document *form*, not a content-type
    # facet — same reconciliation `schemas/document_metadata.py` made for
    # the flat key. See `classify.classifiers.llm` for the full argument.
    if result.auto_class:
        updates["document_form"] = result.auto_class
    return updates


@dataclass
class BatchEnrichmentResult:
    """Counts from one ``worker enrich`` write pass.

    ``stale_snapshot`` is the #421 race made countable. It fires when the
    stored content changed between candidate selection and the write-back —
    i.e. a concurrent write landed inside the LLM window. The enrichment is
    still applied (onto the current content), so this is a concurrency signal
    for the operator, not a failure count; ``enriched`` includes these rows.
    ``vanished`` counts rows deleted inside the same window, which are *not*
    written — a ``put`` would resurrect them.
    """

    enriched: int = 0
    failed: int = 0
    stale_snapshot: int = 0
    vanished: int = 0
    vector_rows_synced: int = 0


def _run_batch_enrichment(
    llm: Any,
    document_store: DocumentStore,
    candidates: list[dict[str, Any]],
    *,
    concurrency: int,
    event_log: EventLog,
    vector_store: VectorStore | None,
) -> BatchEnrichmentResult:
    """Enrich candidates and write successful results back via the tag path.

    ``enriched`` counts the documents whose tags were updated **in the
    document store** — the authoritative count, unchanged by whether a
    vector row existed to mirror onto.

    ``vector_store`` is required keyword-only (``None`` is allowed) for the
    same reason it is on :func:`run_curation_cycle`: this is a *post-embed*
    writer of exactly the two keys
    :data:`~trellis.core.vector_metadata.SYNCED_METADATA_KEYS` covers, and
    it selects documents that are already stored and already embedded. A
    write here that skips the mirror leaves the semantic axis scoring the
    document on its pre-enrichment ``auto_importance`` and serving its
    pre-enrichment ``content_tags`` — #338 again, on a second path (found
    while fixing #381).

    **The write-back re-reads (#421).** ``candidates`` are snapshots taken
    before *N* LLM calls — minutes on a corpus-scale batch — so writing
    ``doc["content"]`` and ``doc["metadata"]`` back would silently revert any
    write that landed inside that window. Every write therefore goes through
    :func:`~trellis.core.derived_metadata.apply_derived_metadata`, which
    re-reads the row and merges only the three keys this path owns onto
    whatever it says now. One ``get`` per written row against *N* completed
    model calls is not a cost worth reasoning about, and it is what makes
    ``preserve_updated_at=True`` true by construction rather than in intent.
    """
    from trellis_workers.enrichment.service import EnrichmentService  # noqa: PLC0415

    service = EnrichmentService(llm, event_log=event_log)
    items = [
        {
            "content": doc.get("content", ""),
            "title": (doc.get("metadata") or {}).get("title", ""),
            "tags": list(
                ((doc.get("metadata") or {}).get("content_tags") or {}).get("tags", [])
            ),
        }
        for doc in candidates
    ]
    results = asyncio.run(service.batch_enrich(items, concurrency=concurrency))

    stamp = datetime.now(UTC)
    summary = BatchEnrichmentResult()
    for doc, result in zip(candidates, results, strict=True):
        if not result.success:
            summary.failed += 1
            logger.warning(
                "worker_enrich.item_failed",
                doc_id=doc.get("doc_id"),
                error=result.error,
                failure_kind=getattr(result.failure_kind, "value", None),
            )
            continue
        # Metadata-only: ``content`` is the row's own and only derived tags
        # change. Same operation as ``classify.refresh``, which passes the
        # flag for the same reason, and at the same scale — a full pass
        # re-stamps every document it touches to one instant, after which
        # ``KeywordSearch``'s recency decay is measuring the enrichment run
        # rather than the documents. Widest exposure of the five to the
        # other consumers too: ``_select_enrichment_candidates`` filters on
        # neither lifecycle nor ``source_path``, so a ``superseded`` row is an
        # ordinary candidate here (resetting the age ``mutate.retention``
        # prunes on), and one full pass re-dates the ``newest_item_at`` that
        # ``retrieve.file_context`` reports for every corpus path at once
        # (#406).
        #
        # ``apply_derived_metadata`` is what makes that flag honest: the row
        # is re-read here, so the content written is the current one and the
        # prior tag bag these updates merge onto is the current one too
        # (#421). ``doc`` is a pre-LLM snapshot and is deliberately *not* the
        # source of either.
        write = apply_derived_metadata(
            document_store,
            doc["doc_id"],
            partial(_enrichment_updates, result=result, stamp=stamp),
            snapshot_content=doc.get("content"),
        )
        if write.content_changed:
            summary.stale_snapshot += 1
        if not write.written:
            # The row was deleted inside the LLM window. Not resurrected: a
            # ``put`` on a missing id inserts, which would undo the delete and
            # re-file pre-LLM content under a fresh ``created_at``.
            summary.vanished += 1
            continue
        assert write.metadata is not None  # `written` implies the merged bag
        summary.enriched += 1
        if result.judging_model_id:
            judged_content = doc.get("content", "")
            emit_memory_op_judged(
                event_log,
                op_type=JudgedOpType.CLASSIFICATION,
                source="worker:enrich",
                model_id=result.judging_model_id,
                input_digest=InputDigest(
                    hash=content_hash(judged_content),
                    length=len(judged_content),
                    source_refs=[doc["doc_id"]],
                ),
                decision=result.auto_class or "unclassified",
                confidence=(
                    result.class_confidence
                    if result.class_confidence is not None
                    else 0.0
                ),
                subject_ref=SubjectRef(
                    ref_type=REF_TYPE_DOCUMENT,
                    ref_id=doc["doc_id"],
                ),
                entity_type="document",
            )
        else:
            logger.error(
                "memory_op_judged_identity_missing",
                operation="classification",
                doc_id=doc["doc_id"],
            )
        # After the authoritative write, never before — the document row is
        # what a re-run repairs from, so it has to land first. Fail-soft: a
        # mirror failure must not lose the tag that was already written.
        # Mirrors ``write.metadata`` (what actually landed), never the
        # snapshot bag — mirroring the snapshot would push the very keys the
        # re-read exists to preserve onto the vector row.
        if sync_vector_metadata(vector_store, doc["doc_id"], write.metadata):
            summary.vector_rows_synced += 1
        logger.info("worker_enrich.item_enriched", doc_id=doc.get("doc_id"))
    logger.info(
        "worker_enrich.batch_written",
        enriched=summary.enriched,
        failed=summary.failed,
        stale_snapshot=summary.stale_snapshot,
        vanished=summary.vanished,
        vector_rows_synced=summary.vector_rows_synced,
        vector_store_supplied=vector_store is not None,
    )
    return summary


# ---------------------------------------------------------------------------
# worker mine-precedents — wrap PrecedentMiner.generate_precedent_candidates
# ---------------------------------------------------------------------------


@worker_app.command("mine-precedents")
def mine_precedents_cmd(
    domain: str | None = typer.Option(
        None, "--domain", help="Restrict mining to this trace domain."
    ),
    min_traces: int = typer.Option(
        3, "--min-traces", help="Minimum failure/partial traces required to mine."
    ),
    limit: int = typer.Option(100, "--limit", help="Max traces to analyze."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report how many failure traces are in scope without calling the LLM.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Mine precedent candidates from failure / partial traces.

    Wraps :meth:`PrecedentMiner.generate_precedent_candidates`. Candidates
    are surfaced (and persisted as the miner intends — it emits
    ``PRECEDENT_PROMOTED`` events for each). They are **not** auto-promoted
    into the graph; review them before acting.

    Requires an LLM extra. Without a configured client this command exits
    loudly (the miner would otherwise return an empty list silently).
    """
    trace_store = get_trace_store()
    event_log = get_event_log()
    llm = _require_llm_client_or_exit()

    if dry_run:
        in_scope = _count_failure_traces(trace_store, domain=domain, limit=limit)
        if output_format == "json":
            emit_json(
                {
                    "status": "ok",
                    "dry_run": True,
                    "domain": domain,
                    "failure_traces_in_scope": in_scope,
                    "min_traces": min_traces,
                    "would_mine": in_scope >= min_traces,
                }
            )
        else:
            console.print(
                f"[bold]worker mine-precedents[/bold] DRY-RUN — "
                f"{in_scope} failure/partial trace(s) in scope "
                f"(min_traces={min_traces})"
            )
        return

    from trellis_workers.learning.miner import PrecedentMiner  # noqa: PLC0415

    miner = PrecedentMiner(trace_store, event_log=event_log, llm=llm)
    precedents = asyncio.run(
        miner.generate_precedent_candidates(
            domain=domain, min_traces=min_traces, limit=limit
        )
    )

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "dry_run": False,
                "domain": domain,
                "candidate_count": len(precedents),
                "candidates": [
                    {
                        "precedent_id": p.precedent_id,
                        "title": p.title,
                        "confidence": p.confidence,
                    }
                    for p in precedents
                ],
            }
        )
        return
    console.print(
        f"[bold]worker mine-precedents[/bold] — "
        f"{len(precedents)} candidate(s) generated"
    )
    for p in precedents:
        console.print(f"  - [{p.confidence:.2f}] {p.title}")
    if precedents:
        console.print(
            "[dim]Candidates are surfaced, not promoted. Review before "
            "acting on them.[/dim]"
        )


def _count_failure_traces(
    trace_store: TraceStore,
    *,
    domain: str | None,
    limit: int,
) -> int:
    """Count failure/partial traces in scope — the miner's eligibility input."""
    from trellis.schemas.enums import OutcomeStatus  # noqa: PLC0415

    traces = trace_store.query(domain=domain, limit=limit)
    return sum(
        1
        for t in traces
        if t.outcome
        and t.outcome.status in (OutcomeStatus.FAILURE, OutcomeStatus.PARTIAL)
    )


# ---------------------------------------------------------------------------
# worker capture-sessions — one Claude Code session-capture sweep
# ---------------------------------------------------------------------------


@worker_app.command("capture-sessions")
def capture_sessions_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Plan the sweep without writing memories or advancing the watermark.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Run one Claude Code session-capture sweep.

    The ``trellis worker`` front door for the sweep that also ships as the
    ``trellis-session-capture`` console script; both call the same
    :func:`run_sweep`, so the transcript root, watermark, sampling and model
    env vars documented in ``docs/agent-guide/session-auto-capture.md`` behave
    identically here.

    **Requires a distillation judge.** Distillation fail-closes on a missing
    client, so a sweep without one captures nothing while looking like a clean
    run. This command exits non-zero when no judge can be built, and again
    when the judge went away mid-sweep — ``sessions_judge_unavailable`` counts
    the sessions left un-watermarked for a later retry, and
    ``TRELLIS_CAPTURE_STRICT=0`` downgrades that second case to a reported
    count with a zero exit.
    """
    # Imported here, not at module scope: trellis_workers is an optional
    # install alongside the CLI, and every other `trellis worker` command
    # pays the same lazy-import cost.
    from trellis_workers.session_capture.sweep import (  # noqa: PLC0415
        CaptureJudgeUnavailableError,
        judge_unavailable_sessions,
        run_sweep,
        strict_mode,
    )

    try:
        report = run_sweep(registry=_get_registry(), dry_run=dry_run)
    except CaptureJudgeUnavailableError as exc:
        if output_format == "json":
            emit_json({"status": "error", "message": str(exc)})
        else:
            # escape(): the remediation names the `[llm-openai]` /
            # `[llm-anthropic]` extras, which Rich would eat as markup tags.
            console.print(f"[red]worker capture-sessions: {escape(str(exc))}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    payload = report.to_payload()
    unjudged = judge_unavailable_sessions(report)
    payload["sessions_judge_unavailable"] = unjudged

    if output_format == "json":
        # "partial", not "ok": the command itself treats an unjudged session
        # as a failed run, so a consumer keying off `status` must not read it
        # as success.
        emit_json({"status": "partial" if unjudged else "ok", **payload})
    else:
        _render_capture_text(payload)

    if unjudged and strict_mode():
        raise typer.Exit(code=EXIT_INTERNAL)


def _render_capture_text(payload: dict[str, Any]) -> None:
    """Human-readable rendering of a :class:`CaptureReport` payload."""
    mode = "DRY-RUN" if payload["dry_run"] else "LIVE"
    console.print(f"[bold]worker capture-sessions[/bold] mode={mode}")
    console.print(
        f"  sessions: {payload['sessions_seen']} seen  "
        f"{payload['sessions_parsed']} parsed  "
        f"{payload['sessions_triggered']} triggered  "
        f"{payload['sessions_skipped_watermark']} watermark-skipped"
    )
    console.print(
        f"  candidates: {payload['candidates_distilled']} distilled  "
        f"{payload['candidates_blocked_scan']} secret-blocked  "
        f"{payload['candidates_rejected_injection']} injection-blocked  "
        f"{payload['candidates_rejected_worthiness']} unworthy"
    )
    console.print(
        f"  memories written: {payload['memories_written']}  "
        f"unchanged: {payload['memories_skipped_unchanged']}"
    )
    if payload["reconcile_enabled"]:
        console.print(
            f"  reconcile: {payload['candidates_reconciled_noop']} noop  "
            f"{payload['candidates_reconciled_supersede']} supersede"
        )
    if payload["supersessions_failed"]:
        # Never folded into the reconcile line above: a supersession the
        # judge decided and the store could not apply is a defect, not a
        # tally (#407).
        console.print(
            f"[red]  {payload['supersessions_failed']} supersession(s) could "
            f"not be applied — see warnings.[/red]"
        )
    if payload["sessions_judge_unavailable"]:
        console.print(
            f"[red]  {payload['sessions_judge_unavailable']} session(s) left "
            f"unjudged — the judge was unreachable; they stay un-watermarked "
            f"for a later retry.[/red]"
        )


# ---------------------------------------------------------------------------
# trellis worker embed-traces
# ---------------------------------------------------------------------------


@worker_app.command("embed-traces")
def embed_traces_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Count what would be embedded; write nothing, move no cursor.",
    ),
    limit: int = typer.Option(
        0, "--limit", help="Stop after this many traces (0 = no limit)."
    ),
    max_scan: int = typer.Option(
        0,
        "--max-scan",
        help=(
            "Traces processed per pass (0 = the worker default). Bounds the "
            "work, not the reads: the pass refuses rather than reading less, "
            "because slicing the oldest traces soundly needs the whole range."
        ),
    ),
    page_size: int = typer.Option(
        0, "--page-size", help="Traces per trace-store query (0 = default)."
    ),
    reset_watermark: bool = typer.Option(
        False,
        "--reset-watermark",
        help=(
            "Forget the cursor and re-scan from the beginning. Costs time, "
            "never rows — traces that already have a vector row are skipped."
        ),
    ),
    no_step_errors: bool = typer.Option(
        False,
        "--no-step-errors",
        help="Render only intent and outcome, omitting recorded step errors.",
    ),
    watermark: Path | None = typer.Option(  # noqa: B008 - typer option default
        None, "--watermark", help="Cursor file (default: <config dir>/…)."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Embed trace summaries so traces are reachable by semantic search.

    ``save_experience`` writes a trace and nothing else, and no retrieval
    strategy reads the trace store — keyword reads documents, semantic reads
    vectors, graph reads the graph. A trace's only surface has been the
    name-only ``trace:<id>`` Activity node trace extraction mints. This pass
    renders each trace's intent, outcome summary and recorded step errors into
    a document, embeds it, and records the write through the governed
    :class:`~trellis.mutate.MutationExecutor`. It never modifies a trace.

    **Requires an embedder and a vector store.** Like ``trellis admin
    reindex-vectors``, running the command is the opt-in — no feature flag —
    but a missing embedder exits non-zero rather than reporting a clean pass
    over zero rows.

    Safe to interrupt. The cursor advances only through a contiguous run of
    traces confirmed to have a vector row, and correctness does not depend on
    it: the check that decides whether a trace is done asks the vector store.
    """
    from trellis_workers.trace_embed import (  # noqa: PLC0415
        TraceEmbedScanLimitError,
        TraceEmbedUnavailableError,
        run_trace_embed_pass,
    )

    # ``0`` means "leave the worker's own default alone" — the constants live
    # in trellis_workers, which is a lazy import here (it is an optional
    # install alongside the CLI), so restating them as typer defaults would
    # duplicate a number that could drift.
    overrides: dict[str, Any] = {}
    if max_scan > 0:
        overrides["max_scan"] = max_scan
    if page_size > 0:
        overrides["page_size"] = page_size

    try:
        report = run_trace_embed_pass(
            _get_registry(),
            watermark_path=watermark,
            limit=limit,
            dry_run=dry_run,
            reset_watermark=reset_watermark,
            include_step_errors=not no_step_errors,
            **overrides,
        )
    except (TraceEmbedUnavailableError, TraceEmbedScanLimitError) as exc:
        if output_format == "json":
            emit_json({"status": "error", "message": str(exc)})
        else:
            console.print(f"[red]worker embed-traces: {escape(str(exc))}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    payload = report.to_dict()
    if output_format == "json":
        emit_json(payload)
    else:
        _render_embed_traces_text(payload)

    if report.failed or report.skipped_empty:
        # A pass that left traces unreachable did not do the job it was
        # scheduled to do, and a green exit would hide exactly the silent gap
        # this worker exists to close.
        raise typer.Exit(code=EXIT_INTERNAL)


def _render_embed_traces_text(payload: dict[str, Any]) -> None:
    mode = "DRY-RUN" if payload["dry_run"] else "LIVE"
    console.print(f"[bold]worker embed-traces[/bold] mode={mode}")
    console.print(
        f"  traces: {payload['scanned']} scanned  "
        f"{payload['embedded']} embedded  "
        f"{payload['skipped_existing']} already embedded  "
        f"{payload['skipped_empty']} nothing to render"
    )
    console.print(
        f"  cursor: {payload['watermark_before'] or '(none)'} -> "
        f"{payload['watermark_after'] or '(none)'}"
        + ("  [yellow](stopped early)[/yellow]" if payload["stopped_early"] else "")
    )
    if payload["more_remaining"]:
        console.print("[yellow]  more traces remain — run again to continue.[/yellow]")
    for failure in payload["failures"]:
        console.print(
            f"[red]  {escape(failure['trace_id'])}: "
            f"{escape(str(failure['error']))}[/red]"
        )
