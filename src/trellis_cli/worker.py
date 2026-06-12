"""``trellis worker`` — unattended curation/learning workers.

This module owns the ``worker`` command group. Today it ships exactly one
subcommand, :func:`tune_cmd` (``trellis worker tune``), the Tier-1
autonomy surface from ``docs/design/adr-autonomy-ladder.md``: it runs one
:class:`RuleTuner` pass and, when ``learning.auto_promote.enabled`` is set,
auto-promotes every qualifying proposal through the *same* governance
pipeline ``trellis metrics promote --commit`` uses — no new mutation path.

WP3 will add ``curate`` / ``enrich`` / ``mine-precedents`` subcommands to
this same ``worker_app``; the group is defined here so those land next to
``tune`` rather than scattered across modules.

The ``worker_app`` lived in ``trellis_cli.main`` as an empty group; it has
moved here. ``main`` imports it from this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
import typer
import yaml
from rich.console import Console

from trellis.learning.tuners import (
    AutoPromotePolicy,
    PostPromotionPolicy,
    RuleTuner,
    report_to_dict,
    run_auto_promotion,
)
from trellis_cli.config import get_config_dir
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.stores import (
    get_event_log,
    get_outcome_store,
    get_parameter_store,
    get_tuner_state_store,
)

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


def _emit_json(payload: Any) -> None:
    """Write JSON via ``typer.echo`` so Rich doesn't line-wrap long values."""
    typer.echo(json.dumps(payload))


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
        payload["tuner_name"] = tuner_name
        _emit_json(payload)
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
