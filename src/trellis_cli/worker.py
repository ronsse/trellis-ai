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

The ``worker_app`` lived in ``trellis_cli.main`` as an empty group; it has
moved here. ``main`` imports it from this module.
"""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import typer
import yaml
from rich.console import Console

from trellis.errors import BackendNotInstalledError
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
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.analyze import _build_learning_registry_or_exit
from trellis_cli.config import get_config_dir, get_data_dir
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.output import emit_json
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

logger = structlog.get_logger(__name__)

worker_app = typer.Typer(help="Run curation workers", no_args_is_help=True)
console = Console()

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
            f"{outcome.proposal_id[:18]}…  {outcome.reason}{suffix}"
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
    advisories_suppressed: int = 0
    advisories_boosted: int = 0
    learning_observations: int = 0
    learning_candidates: int = 0
    candidates_path: str | None = None
    decisions_path: str | None = None
    skipped_stages: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Flat JSON-friendly view for ``--format json`` and structured logs."""
        return {
            "noise_tagged": self.noise_tagged,
            "advisories_generated": self.advisories_generated,
            "advisories_suppressed": self.advisories_suppressed,
            "advisories_boosted": self.advisories_boosted,
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
        advisories_suppressed=advisory["advisories_suppressed"],
        advisories_boosted=advisory["advisories_boosted"],
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
    days: int,
    dry_run: bool,
    skip: bool,
    no_meta_trace: bool,
    skipped: list[str],
) -> dict[str, int]:
    """Stage 1 — effectiveness feedback (demote / noise-tag).

    In dry-run the read-only :func:`analyze_effectiveness` is used so the
    candidate count is still reported without writing noise tags.
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
            report = run_effectiveness_feedback(event_log, document_store, days=days)
        noise_tagged = len(report.noise_candidates)
        if record.enabled and noise_tagged:
            record.produced_finding(
                f"curate-noise-tags-d{days}",
                finding_type="NoiseTagsApplied",
            )
    return {"noise_tagged": noise_tagged}


def _curate_stage_advisories(
    event_log: EventLog,
    advisory_store: AdvisoryStore,
    *,
    days: int,
    dry_run: bool,
    skip: bool,
    no_meta_trace: bool,
    skipped: list[str],
) -> dict[str, int]:
    """Stages 2 & 3 — advisory generation + fitness loop.

    Both stages mutate the advisory store, so a dry-run skips them
    wholesale rather than half-running an analysis with no read-only twin.
    """
    if skip or dry_run:
        skipped.append("advisories")
        return {
            "advisories_generated": 0,
            "advisories_suppressed": 0,
            "advisories_boosted": 0,
        }

    with wrap_cli_meta_analysis(
        agent_suffix="worker",
        analyzer_name="cli.worker.curate.advisories",
        disabled=no_meta_trace,
    ) as record:
        gen = AdvisoryGenerator(event_log, advisory_store).generate(days=days)
        fitness = run_advisory_fitness_loop(event_log, advisory_store, days=days)
        generated = gen.advisories_generated
        suppressed = len(fitness.advisories_suppressed)
        if record.enabled and (generated or suppressed):
            record.produced_finding(
                f"curate-advisories-d{days}",
                finding_type="AdvisoryCycleReport",
            )
    return {
        "advisories_generated": generated,
        "advisories_suppressed": suppressed,
        "advisories_boosted": len(fitness.advisories_boosted),
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
    """Build the AdvisoryStore over ``<data_dir>/advisories.json``.

    Mirrors ``trellis_cli.analyze.generate_advisories`` so the worker
    cycle writes to the same advisory file the analyze commands use.
    """
    return AdvisoryStore(get_data_dir() / "advisories.json")


def _render_cycle_text(result: CurateCycleResult) -> None:
    """Human-readable rendering of one :class:`CurateCycleResult`."""
    mode = "DRY-RUN" if result.dry_run else "LIVE"
    console.print(f"[bold]worker curate[/bold] mode={mode}")
    console.print(
        f"  noise-tagged: {result.noise_tagged}  "
        f"advisories generated: {result.advisories_generated}  "
        f"suppressed: {result.advisories_suppressed}  "
        f"boosted: {result.advisories_boosted}"
    )
    console.print(
        f"  learning observations: {result.learning_observations}  "
        f"candidates: {result.learning_candidates}"
    )
    if result.skipped_stages:
        console.print(f"  [dim]skipped: {', '.join(result.skipped_stages)}[/dim]")
    if result.candidates_path:
        console.print(f"  candidates: [cyan]{result.candidates_path}[/cyan]")
        console.print(f"  decisions:  [cyan]{result.decisions_path}[/cyan]")
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
) -> None:
    """Run :func:`run_curation_cycle` on a fixed interval until signalled.

    Plain ``while`` + interruptible sleep — no scheduler dependency
    (APScheduler / Celery explicitly rejected; Trellis stays
    scheduler-agnostic). Emits one structured ``worker_curate.cycle`` log
    line per cycle with the headline counts.

    ``max_cycles`` and the injectable ``shutdown`` flag exist for tests so
    the loop can run a bounded number of cycles without real signals or
    long sleeps; production callers leave both at their defaults.
    """
    flag = shutdown if shutdown is not None else _ShutdownFlag()
    if shutdown is None:
        # Only install handlers when we own the flag — tests inject their
        # own and drive ``stop`` directly without touching process signals.
        signal.signal(signal.SIGINT, flag.request)
        signal.signal(signal.SIGTERM, flag.request)

    cycle = 0
    while not flag.stop:
        cycle += 1
        result = run_curation_cycle(
            event_log=get_event_log(),
            document_store=get_document_store(),
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
            emit_json({"status": "ok", "cycle": cycle, **result.to_dict()})
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
        return

    result = run_curation_cycle(
        event_log=get_event_log(),
        document_store=get_document_store(),
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
        emit_json({"status": "ok", **result.to_dict()})
        return
    _render_cycle_text(result)


def _reconcile_before_cycle() -> None:
    """Backfill the JSONL feedback log into the EventLog before a cycle.

    Thin wrapper around
    :func:`trellis.feedback.recording.reconcile_feedback_log_to_event_log`
    so ``--reconcile-first`` replays any file-only feedback rows the
    cycle would otherwise miss. Logs the resulting counts.
    """
    from trellis.feedback.recording import (  # noqa: PLC0415
        reconcile_feedback_log_to_event_log,
    )

    result = reconcile_feedback_log_to_event_log(get_data_dir(), get_event_log())
    logger.info(
        "worker_curate.reconciled",
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
    confidence_threshold: float = typer.Option(
        0.5,
        "--confidence-threshold",
        help="Re-enrich documents whose tag_confidence is below this value.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Select + report candidates without calling the LLM."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Batch-enrich under-tagged documents via :class:`EnrichmentService`.

    **Selection predicate.** A document is "unenriched" (a candidate) when
    its ``metadata.content_tags`` is missing/empty, OR it carries no
    ``content_tags.tag_confidence`` stamp, OR that stamp is strictly below
    ``--confidence-threshold``. Documents already tagged at or above the
    threshold are skipped. The newest ``--limit`` documents matching the
    predicate are enriched.

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
        document_store, limit=limit, confidence_threshold=confidence_threshold
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
                console.print(f"  - {cand['doc_id']}")
        return

    enriched = _run_batch_enrichment(
        llm,
        document_store,
        candidates,
        concurrency=concurrency,
        event_log=get_event_log(),
    )

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "dry_run": False,
                "selected": len(candidates),
                "enriched": enriched,
            }
        )
        return
    console.print(
        f"[bold]worker enrich[/bold] — {enriched}/{len(candidates)} "
        f"document(s) enriched"
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
    confidence_threshold: float,
) -> list[dict[str, Any]]:
    """Return documents matching the unenriched / low-confidence predicate.

    See :func:`enrich_cmd` for the predicate definition. Scans the newest
    documents (a generous multiple of ``limit`` so the filter has headroom)
    and returns at most ``limit`` matches.
    """
    scanned = document_store.list_documents(limit=max(limit * 5, limit))
    candidates: list[dict[str, Any]] = []
    for doc in scanned:
        metadata = doc.get("metadata") or {}
        content_tags = metadata.get("content_tags") or {}
        if not content_tags:
            candidates.append(doc)
            continue
        confidence = content_tags.get("tag_confidence")
        if confidence is None or float(confidence) < confidence_threshold:
            candidates.append(doc)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _run_batch_enrichment(
    llm: Any,
    document_store: DocumentStore,
    candidates: list[dict[str, Any]],
    *,
    concurrency: int,
    event_log: EventLog,
) -> int:
    """Enrich candidates and write successful results back via the tag path.

    Returns the number of documents whose tags were updated.
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

    stamp = datetime.now(UTC).isoformat()
    enriched = 0
    for doc, result in zip(candidates, results, strict=True):
        if not result.success:
            logger.warning(
                "worker_enrich.item_failed",
                doc_id=doc.get("doc_id"),
                error=result.error,
                failure_kind=getattr(result.failure_kind, "value", None),
            )
            continue
        metadata = dict(doc.get("metadata") or {})
        content_tags = dict(metadata.get("content_tags") or {})
        content_tags["tags"] = result.auto_tags
        if result.auto_class is not None:
            content_tags["auto_class"] = result.auto_class
        content_tags["auto_importance"] = result.auto_importance
        content_tags["tag_confidence"] = result.tag_confidence
        content_tags["classified_at"] = stamp
        if result.importance_scored_at is not None:
            content_tags["importance_scored_at"] = (
                result.importance_scored_at.isoformat()
            )
        metadata["content_tags"] = content_tags
        document_store.put(doc["doc_id"], doc["content"], metadata)
        enriched += 1
        logger.info("worker_enrich.item_enriched", doc_id=doc.get("doc_id"))
    return enriched


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
