"""Analyze commands -- context effectiveness and insights."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import typer
import yaml
from rich.markup import escape
from rich.table import Table

from trellis.analyze.domains import analyze_domains
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import StoreWriteRefusedError
from trellis.extract.telemetry import analyze_extractor_fallbacks
from trellis.learning import (
    LEARNING_NOISE_RETRY_KEY,
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_PROMOTE_RETRY_KEY,
    LEARNING_PROMOTE_SUCCESS_KEY,
    LEARNING_SCORING_COMPONENT,
    REQUIRED_LEARNING_PARAMETER_KEYS,
    SCHEMA_EVOLUTION_PARAM_COMPONENT_ID,
    analyze_learning_observations,
    analyze_well_known_candidates,
    build_learning_observations_from_event_log,
    write_learning_review_artifacts,
)
from trellis.learning import (
    RECOMMENDED_SEED_VALUES as SCHEMA_EVOLUTION_SEED_DEFAULTS,
)
from trellis.ops import ParameterRegistry
from trellis.ops.capture_coverage import CaptureCoverageReport
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    analyze_effectiveness,
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    EvaluationProfile,
    EvaluationScenario,
    QualityReport,
    analyze_dimension_predictiveness,
    evaluate_pack,
)
from trellis.retrieve.pack_sections import (
    PACK_SECTIONS_SCAN_LIMIT,
    analyze_pack_sections,
)
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.retrieve.token_usage import analyze_token_usage
from trellis.retrieve.trellis_cost import summarize_trellis_cost
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.advisory_source import resolve_advisory_path
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT
from trellis.stores.base.parameter import ParameterStore
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.config import get_config_dir
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import (
    _get_registry,
    get_document_store,
    get_event_log,
    get_graph_store,
    get_parameter_store,
    get_trace_store,
)

if TYPE_CHECKING:
    from rich.console import Console

logger = structlog.get_logger(__name__)

analyze_app = typer.Typer(no_args_is_help=True)
console = build_console()

# Display thresholds for rate coloring
_RATE_GREEN = 0.7
_RATE_YELLOW = 0.4

# Extractor-fallback display thresholds (inverted — high rate = bad)
_FALLBACK_RATE_RED = 0.5
_FALLBACK_RATE_YELLOW = 0.2

# Seed defaults for the learning ParameterRegistry. These live in the CLI
# module (NOT in trellis.learning.scoring) per the POC directive in
# plan-self-improvement-program.md §2 ("loud on misuse" — the library raises
# when called without a registry; defaults are deliberately operator-facing).
# Operators dismiss the WARN by running 'trellis admin init-learning-params'
# which seeds these values to ``~/.config/trellis/learning_params.yaml``.
LEARNING_PARAMETER_SEED_DEFAULTS: dict[str, float] = {
    LEARNING_PROMOTE_SUCCESS_KEY: 0.75,
    LEARNING_PROMOTE_RETRY_KEY: 0.25,
    LEARNING_NOISE_SUCCESS_KEY: 0.4,
    LEARNING_NOISE_RETRY_KEY: 0.5,
}

LEARNING_PARAMS_CONFIG_FILENAME = "learning_params.yaml"


class _InMemoryParameterStore(ParameterStore):
    """Minimal in-memory ParameterStore for CLI invocations without a config.

    Holds a single snapshot keyed by exact scope. The scoring layer only
    needs ``resolve()`` for its ``ParameterScope(component_id=...)`` query;
    other methods are minimally implemented to satisfy the ABC. Intentionally
    not exposed outside this module — operators who want persistence run
    ``trellis admin init-learning-params``.
    """

    def __init__(self) -> None:
        self._snapshots: dict[
            tuple[str, str | None, str | None, str | None], ParameterSet
        ] = {}

    def put(self, params: ParameterSet) -> ParameterSet:
        self._snapshots[params.scope.key()] = params
        return params

    def get(self, params_version: str) -> ParameterSet | None:
        for snapshot in self._snapshots.values():
            if snapshot.params_version == params_version:
                return snapshot
        return None

    def get_active(self, scope: ParameterScope) -> ParameterSet | None:
        return self._snapshots.get(scope.key())

    def resolve(self, scope: ParameterScope) -> ParameterSet | None:
        # Narrowest first, then walk back to the component-level scope.
        candidates = [
            scope,
            ParameterScope(component_id=scope.component_id),
        ]
        seen: set[tuple[str, str | None, str | None, str | None]] = set()
        for cand in candidates:
            key = cand.key()
            if key in seen:
                continue
            seen.add(key)
            active = self.get_active(cand)
            if active is not None:
                return active
        return None

    def list_versions(
        self,
        scope: ParameterScope | None = None,
        *,
        limit: int = 100,
    ) -> list[ParameterSet]:
        if scope is None:
            return list(self._snapshots.values())[:limit]
        snapshot = self.get_active(scope)
        return [snapshot] if snapshot is not None else []

    def close(self) -> None:
        self._snapshots.clear()


def _load_learning_params_config() -> dict[str, float] | None:
    """Load learning-parameter overrides from the config dir, if present.

    Returns ``None`` when the file does not exist. Raises
    :class:`typer.BadParameter` if the file exists but is malformed —
    operators get a loud error rather than a silent fallback to defaults.
    """
    config_path = get_config_dir() / LEARNING_PARAMS_CONFIG_FILENAME
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
    values: dict[str, float] = {}
    for key in REQUIRED_LEARNING_PARAMETER_KEYS:
        if key not in raw:
            msg = (
                f"{config_path}: missing required key {key!r}. "
                f"Required keys: {list(REQUIRED_LEARNING_PARAMETER_KEYS)}."
            )
            raise typer.BadParameter(msg)
        try:
            values[key] = float(raw[key])
        except (TypeError, ValueError) as exc:
            msg = f"{config_path}: key {key!r} is not a number: {raw[key]!r}"
            raise typer.BadParameter(msg) from exc
    return values


def _build_learning_registry() -> ParameterRegistry:
    """Construct a ParameterRegistry for the learning.scoring component.

    Loads ``~/.config/trellis/learning_params.yaml`` if present; otherwise
    seeds an in-memory store with :data:`LEARNING_PARAMETER_SEED_DEFAULTS`
    and emits a single WARN log line pointing the operator at
    ``trellis admin init-learning-params``.
    """
    overrides = _load_learning_params_config()
    values: dict[str, float | int | str | bool]
    if overrides is None:
        logger.warning(
            "learning.parameter_registry.seeded_defaults",
            component=LEARNING_SCORING_COMPONENT,
            defaults=dict(LEARNING_PARAMETER_SEED_DEFAULTS),
            remediation=(
                "run 'trellis admin init-learning-params' to seed "
                f"{get_config_dir() / LEARNING_PARAMS_CONFIG_FILENAME}"
            ),
        )
        values = dict(LEARNING_PARAMETER_SEED_DEFAULTS)
    else:
        values = dict(overrides)
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id=LEARNING_SCORING_COMPONENT),
            values=values,
            source="cli:analyze",
            notes="seeded by trellis_cli.analyze._build_learning_registry",
        )
    )
    return ParameterRegistry(store=store)


def _build_learning_registry_or_exit() -> ParameterRegistry:
    """Wrap :func:`_build_learning_registry` to translate ``BadParameter``.

    The CLI command surface wants a clean :class:`typer.Exit` on
    misconfiguration rather than the noisy ``BadParameter`` traceback
    Typer surfaces for option-validation failures.
    """
    try:
        return _build_learning_registry()
    except typer.BadParameter as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc


def _print_demotion_outcome(report: Any) -> None:
    """Print what was demoted, beside what was proposed and withheld.

    ``noise_candidates`` is the proposal; the evidence gate decides what
    is written (#336). Printing the proposal count as "applied" is the
    kind of measurement-wired-to-the-wrong-number this repo keeps
    producing, so both numbers are always shown.
    """
    screen = getattr(report, "demotion_screen", None)
    proposed = len(report.noise_candidates)
    if screen is None:
        if proposed:
            console.print(
                f"  [yellow]Noise tags applied to {proposed} item(s)[/yellow]"
            )
        else:
            console.print("  [green]No noise candidates found.[/green]")
        return

    console.print(
        f"  Attributed packs: {screen.attributed_packs}"
        f" (minimum {screen.min_attributed_packs})"
    )
    if screen.admitted:
        console.print(
            f"  [yellow]Noise tags applied to {len(screen.admitted)}"
            f" of {proposed} proposed item(s)[/yellow]:"
        )
        for item_id in screen.admitted:
            console.print(f"    - {escape(item_id)}")
    elif proposed:
        console.print(
            f"  [green]Withheld all {proposed} proposed demotion(s)[/green]"
            " — insufficient citation evidence."
        )
    else:
        console.print("  [green]No noise candidates found.[/green]")

    if screen.refused_by_reason:
        console.print("  Withheld by reason:")
        for reason, count in sorted(screen.refused_by_reason.items()):
            console.print(f"    {reason}: {count}")


@analyze_app.command("context-effectiveness")
def context_effectiveness(
    days: int = typer.Option(30, help="Days of history to analyze"),
    min_appearances: int = typer.Option(2, help="Minimum item appearances to include"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Analyze which context items correlate with task success."""
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.context-effectiveness",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_effectiveness(
            event_log,
            days=days,
            min_appearances=min_appearances,
        )
        if _meta_record.enabled and report.total_packs > 0:
            _meta_record.produced_finding(
                f"effectiveness-report-d{days}-m{min_appearances}",
                finding_type="EffectivenessReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
    else:
        console.print(f"[bold]Context Effectiveness Report[/bold] (last {days} days)")
        console.print(f"  Packs assembled: {report.total_packs}")
        console.print(f"  Feedback received: {report.total_feedback}")
        console.print(f"  Overall success rate: {report.success_rate:.1%}")
        if report.demotion_screen is not None:
            screen = report.demotion_screen
            console.print(
                f"  Noise proposed: {len(report.noise_candidates)}"
                f" — would demote {len(screen.admitted)}"
                f" (attributed packs: {screen.attributed_packs})"
            )

        if report.item_scores:
            console.print()
            table = Table(title="Item Effectiveness")
            table.add_column("Item ID", style="cyan", max_width=20)
            table.add_column("Appearances", justify="right")
            table.add_column("Successes", justify="right")
            table.add_column("Failures", justify="right")
            table.add_column("Rate", justify="right")

            for item in report.item_scores[:20]:
                rate_style = (
                    "green"
                    if item["success_rate"] >= _RATE_GREEN
                    else "yellow"
                    if item["success_rate"] >= _RATE_YELLOW
                    else "red"
                )
                table.add_row(
                    escape(item["item_id"][:20]),
                    str(item["appearances"]),
                    str(item["successes"]),
                    str(item["failures"]),
                    f"[{rate_style}]{item['success_rate']:.1%}[/{rate_style}]",
                )
            console.print(table)

        if report.noise_candidates:
            console.print()
            console.print(
                "[yellow]Noise Candidates[/yellow]"
                " (low success rate, consider removing):"
            )
            for item_id in report.noise_candidates:
                console.print(f"  - {escape(item_id)}")

        if report.total_feedback == 0:
            console.print()
            console.print(
                "[dim]No feedback recorded yet. Use 'trellis curate feedback' or"
                " POST /api/v1/packs/{pack_id}/feedback to record outcomes.[/dim]"
            )


@analyze_app.command("apply-noise-tags")
def apply_noise_tags(
    days: int = typer.Option(30, help="Days of history to analyze"),
    min_appearances: int = typer.Option(2, help="Minimum item appearances to score"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Analyze effectiveness AND apply noise tags to low-value items.

    Runs the full feedback loop: analyze_effectiveness → apply_noise_tags.
    Items that consistently correlate with task failure get tagged with
    signal_quality="noise" so PackBuilder excludes them by default.
    """
    event_log = get_event_log()
    document_store = get_document_store()
    # #338: a demotion that reaches only the document store leaves the
    # semantic axis serving the item's pre-demotion snapshot.
    vector_store = resolve_vector_store(_get_registry())

    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.apply-noise-tags",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = run_effectiveness_feedback(
            event_log,
            document_store,
            days=days,
            min_appearances=min_appearances,
            vector_store=vector_store,
        )
        if _meta_record.enabled and report.noise_candidates:
            _meta_record.produced_finding(
                f"noise-tags-applied-d{days}",
                finding_type="NoiseTagsApplied",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
    else:
        console.print(f"[bold]Effectiveness Feedback Applied[/bold] (last {days} days)")
        console.print(f"  Packs analyzed: {report.total_packs}")
        console.print(f"  Feedback events: {report.total_feedback}")
        console.print(f"  Overall success rate: {report.success_rate:.1%}")
        _print_demotion_outcome(report)


@analyze_app.command("token-usage")
def token_usage(
    days: int = typer.Option(7, help="Days of history to analyze"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        help=(
            "Max events to scan. Raise it when the report says TRUNCATED; "
            "the newest events are kept, so the note names what was cut."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Analyze token usage across CLI, MCP, and SDK layers."""
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.token-usage",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_token_usage(event_log, days=days, limit=limit)
        if _meta_record.enabled and report.total_responses > 0:
            _meta_record.produced_finding(
                f"token-usage-report-d{days}",
                finding_type="TokenUsageReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Token Usage Report[/bold] (last {days} days)")
    if report.scan.truncated:
        # #374, same placement as `analyze backend-health`: before any
        # number, because every count below was computed over a shorter
        # window than the header just claimed.
        console.print(f"  [yellow]window[/yellow] {report.scan.note}")
    console.print(f"  Total responses: {report.total_responses}")
    console.print(f"  Total tokens: {report.total_tokens:,}")
    console.print(f"  Avg tokens/response: {report.avg_tokens_per_response:.1f}")

    if report.by_layer:
        console.print()
        layer_table = Table(title="By Layer")
        layer_table.add_column("Layer", style="cyan")
        layer_table.add_column("Responses", justify="right")
        layer_table.add_column("Total Tokens", justify="right")
        layer_table.add_column("Avg Tokens", justify="right")

        for layer, stats in sorted(report.by_layer.items()):
            layer_table.add_row(
                layer.upper(),
                str(stats["count"]),
                f"{stats['total_tokens']:,}",
                f"{stats['avg_tokens']:.1f}",
            )
        console.print(layer_table)

    if report.by_operation:
        console.print()
        op_table = Table(title="Top Operations by Token Usage")
        op_table.add_column("Operation", style="cyan")
        op_table.add_column("Layer", style="dim")
        op_table.add_column("Calls", justify="right")
        op_table.add_column("Total Tokens", justify="right")
        op_table.add_column("Avg Tokens", justify="right")

        for op in report.by_operation:
            op_table.add_row(
                op["operation"],
                op["layer"],
                str(op["count"]),
                f"{op['total_tokens']:,}",
                f"{op['avg_tokens']:.1f}",
            )
        console.print(op_table)

    if report.over_budget:
        console.print()
        console.print(
            f"[yellow]Over-Budget Responses ({len(report.over_budget)})[/yellow]"
        )
        budget_table = Table()
        budget_table.add_column("Operation", style="cyan")
        budget_table.add_column("Layer")
        budget_table.add_column("Response Tokens", justify="right")
        budget_table.add_column("Budget", justify="right")
        budget_table.add_column("When")

        for item in report.over_budget[:20]:
            budget_table.add_row(
                item["operation"],
                item["layer"],
                str(item["response_tokens"]),
                str(item["budget_tokens"]),
                item["occurred_at"][:16],
            )
        console.print(budget_table)

    if report.total_responses == 0:
        console.print()
        console.print(
            "[dim]No token usage recorded yet. Token tracking is enabled"
            " on MCP macro tools automatically.[/dim]"
        )


def _print_capture_coverage(capture: CaptureCoverageReport) -> None:
    """Render the capture section of ``analyze health``.

    A rate is printed only when one exists. Otherwise the *state* is printed
    — ``unobserved`` / ``stale`` / ``degraded`` are three different problems
    and a single "0%" would send the reader to debug the wrong one.
    """
    if capture.capture_rate is not None:
        console.print(
            f"  Capture: {capture.sessions_with_memory}/"
            f"{capture.eligible_sessions} eligible sessions produced a memory "
            f"({capture.capture_rate:.0%}) over {capture.funnel.sweeps} sweep(s)"
        )
    else:
        console.print(
            f"  Capture: [yellow]{capture.state}[/yellow] \u2014 no rate "
            f"({capture.suppressed_reason or 'unavailable'})"
        )
        if capture.degraded_reason:
            console.print(f"    {capture.degraded_reason}")
    funnel = capture.funnel
    if funnel.sweeps:
        console.print(
            f"    funnel: {funnel.sessions_seen} seen -> "
            f"{funnel.sessions_parsed} parsed -> "
            f"{funnel.sessions_triggered} eligible "
            f"({funnel.sessions_skipped_watermark} watermark, "
            f"{funnel.sessions_skipped_ephemeral} ephemeral, "
            f"{funnel.sessions_skipped_empty} empty, "
            f"{funnel.sessions_sampled_out} sampled out, "
            f"{funnel.sessions_judge_unavailable} judge down)"
        )
    for note in capture.notes:
        console.print(f"    [dim]{note}[/dim]")


@analyze_app.command("health")
def health(
    days: int = typer.Option(7, help="Days of history to analyze"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Deterministic backend health: write rejections + serve attribution.

    The write section aggregates WRITE_REJECTED (tool-boundary schema
    rejections), MUTATION_REJECTED (executor stages), and
    MUTATION_EXECUTED into per-tool accept/reject rates with a closed
    rejection taxonomy. The serve section reports the two coverage rates
    the learning join depends on (packs carrying injected_items[],
    feedback carrying item attribution), and states the retrieval-
    availability assumption untargeted feedback rests on (#365). The
    capture section reports what fraction of eligible sessions produced a
    memory, distinguishing "never deployed", "stopped" and "running but
    capturing nothing" instead of collapsing them into a low rate. Status
    is `warn` with named reasons when any deterministic threshold trips —
    the surface the grooming loop watches.
    """
    from trellis.ops.write_health import summarize_backend_health  # noqa: PLC0415

    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.health",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = summarize_backend_health(event_log, days=days)
        if _meta_record.enabled and report.status != "ok":
            _meta_record.produced_finding(
                f"backend-health-warn-d{days}",
                finding_type="BackendHealthReport",
            )

    if output_format == "json":
        # ``mode="json"`` because the capture section carries
        # ``last_sweep_at`` as a datetime, which json.dumps cannot encode.
        print(json.dumps(report.model_dump(mode="json")))
        return

    status_style = "green" if report.status == "ok" else "yellow"
    console.print(
        f"[bold]Backend Health[/bold] (last {days} days) "
        f"[{status_style}]{report.status.upper()}[/{status_style}]"
    )
    if report.scan.truncated:
        # #374. Printed before any number, because every count below was
        # computed over a shorter window than the header just claimed.
        console.print(f"  [yellow]window[/yellow] {report.scan.note}")
    write = report.write
    console.print(
        f"  Writes: {write.accepted} accepted, "
        f"{write.boundary_rejected} rejected at boundary, "
        f"{write.executor_rejected} rejected in executor "
        f"({write.rejection_rate:.0%} of {write.attempts} attempts)"
    )
    if write.by_tool:
        tool_table = Table(show_header=True, header_style="bold")
        tool_table.add_column("surface")
        tool_table.add_column("accepted", justify="right")
        tool_table.add_column("boundary rej", justify="right")
        tool_table.add_column("executor rej", justify="right")
        for surface, stats in sorted(write.by_tool.items()):
            tool_table.add_row(
                surface,
                str(stats.accepted),
                str(stats.boundary_rejected),
                str(stats.executor_rejected),
            )
        console.print(tool_table)
    if write.boundary_kinds:
        console.print("  Boundary rejection taxonomy:")
        for label, count in sorted(
            write.boundary_kinds.items(), key=lambda item: -item[1]
        ):
            console.print(f"    {count:>3}  {label}")
    serve = report.serve
    console.print(
        f"  Serve: {serve.packs_with_injected_items}/{serve.packs} packs carry "
        f"injected_items ({serve.injected_coverage:.0%}); "
        f"{serve.feedback_attributed}/{serve.feedback_events} feedback events "
        f"attributed ({serve.attribution_rate:.0%})"
    )
    if serve.feedback_events:
        # The headline rate mixes callers who could cite with callers who
        # had no pack to cite. Show the split so the reader can tell an
        # ergonomics problem from a retrieval-adoption one.
        console.print(
            f"    of which pack-targeted: "
            f"{serve.pack_targeted_attributed}/{serve.pack_targeted_feedback} "
            f"cited ({serve.pack_attribution_rate:.0%}); "
            f"{serve.untargeted_feedback} named no pack "
            "(unjoinable by construction)"
        )
    if serve.retrieval_availability_note:
        # #365. Printed next to the number it qualifies, not in a footnote:
        # untargeted feedback is routinely read as "agents are not
        # retrieving", and this report cannot distinguish that from a
        # retrieval that failed in transport.
        console.print(
            f"    [yellow]assumption[/yellow] {serve.retrieval_availability_note}"
        )

    _print_capture_coverage(report.capture)
    for reason in report.reasons:
        console.print(f"  [yellow]warn[/yellow] {reason}")
    if report.status == "ok" and write.attempts == 0 and serve.packs == 0:
        console.print("[dim]  No write or serve activity in window.[/dim]")


def _print_value_bounds(report: Any, *, lower: float) -> None:
    """Print the interval and the conditional beside the headline (#364).

    The headline alone is a point estimate over the 58% of injected tokens
    that got a verdict. The interval says how much of it is guesswork; the
    conditional says what the graded tokens looked like, labelled so it
    cannot be mistaken for a corrected headline.
    """
    upper = report.useful_token_fraction_upper_bound
    if upper is not None:
        console.print(
            f"    [bold]true share lies in [{lower:.1%}, {upper:.1%}][/bold] "
            "[dim](every ungraded token counted as useless .. as useful; "
            "the width IS the unjudged share)[/dim]"
        )
    if report.useful_token_fraction_judged is not None:
        console.print(
            f"    conditional on being graded: "
            f"{report.useful_token_fraction_judged:.1%} of "
            f"{report.judged_tokens:,} judged tokens "
            f"[dim]({report.judged_token_coverage or 0.0:.0%} of injected; "
            "NOT a corrected headline — the ungraded tokens are not "
            "missing at random)[/dim]"
        )
    elif report.judged_suppressed_reason:
        console.print(
            f"    [dim]conditional refused: {report.judged_suppressed_reason}[/dim]"
        )


def _print_value_axis(title: str, cells: list[Any], *, minimum: int) -> None:
    """Render one value-density axis, showing every cell's ``n``.

    A suppressed cell prints why it was refused rather than a fraction —
    the reader must never have to infer that a number is missing.
    """
    if not cells:
        return
    table = Table(title=f"Useful-token fraction by {title.lower()}")
    table.add_column(title, style="cyan")
    table.add_column("packs (n)", justify="right")
    table.add_column("injected tok", justify="right")
    table.add_column("helpful tok", justify="right")
    table.add_column("fraction", justify="right")
    # #364: judgement coverage is not uniform across axes, so a cell's
    # fraction cannot be read without knowing how much of it was graded.
    table.add_column("no verdict", justify="right")
    table.add_column("of graded", justify="right")
    for cell in cells:
        rendered = (
            f"[dim]refused (n<{minimum})[/dim]"
            if cell.suppressed
            else f"{cell.useful_token_fraction or 0.0:.1%}"
        )
        unjudged = (
            "[dim]—[/dim]"
            if cell.unjudged_token_fraction is None
            else f"{cell.unjudged_token_fraction:.0%}"
        )
        conditional = (
            "[dim]—[/dim]"
            if cell.useful_token_fraction_judged is None
            else f"{cell.useful_token_fraction_judged:.1%}"
        )
        table.add_row(
            cell.key,
            str(cell.attributed_packs),
            f"{cell.injected_tokens:,}",
            f"{cell.helpful_tokens:,}",
            rendered,
            unjudged,
            conditional,
        )
    console.print(table)


@analyze_app.command("replay")
def replay(
    days: int = typer.Option(30, help="Days of history to replay"),
    excerpt_max_chars: int | None = typer.Option(
        None,
        "--excerpt-max-chars",
        help="Counterfactual uniform excerpt cap (the width lever).",
    ),
    body_items: int | None = typer.Option(
        None,
        "--body-items",
        help="Graduated-disclosure cut: items past this rank priced as pointers.",
    ),
    max_items: int | None = typer.Option(
        None,
        "--max-items",
        help="Hard item ceiling. Items past it are DROPPED, not demoted.",
    ),
    no_refill: bool = typer.Option(
        False,
        "--no-refill",
        help=(
            "Suppress the greedy re-fill so the pricing effect is isolated "
            "from the admission effect. The shipped walk DOES re-fill, so "
            "this arm is diagnostic, not a prediction."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Replay a window's packs under a different serving policy.

    ``analyze value`` says what the served packs delivered. This says what
    a *different* policy would have delivered on the same packs, same
    citations — the only honest before/after available, because a trimming
    change affects future packs while the window is already assembled.

    The walk is re-run, not modelled: PACK_ASSEMBLED.budget_trace[] records
    every candidate the budget saw and what each was charged, including the
    ones it rejected.

    Read the saving next to what it cost. Two lines always print: how many
    cited-helpful items lost their body to a pointer (still fetchable by
    id), and how many an item ceiling removed outright (not fetchable).
    A policy can always raise the fraction by serving less.

    Examples::

        trellis analyze replay --body-items 12
        trellis analyze replay --excerpt-max-chars 300
        trellis analyze replay --excerpt-max-chars 300 --no-refill
        trellis analyze replay --max-items 12
    """
    from trellis.retrieve.pack_replay import (  # noqa: PLC0415
        ReplayPolicy,
        replay_pack_value,
    )

    try:
        policy = ReplayPolicy(
            excerpt_max_chars=excerpt_max_chars,
            body_items=body_items,
            max_items=max_items,
            refill=not no_refill,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.replay",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = replay_pack_value(event_log, policy=policy, days=days)
        if _meta_record.enabled and report.attributed_packs > 0:
            _meta_record.produced_finding(
                f"pack-replay-report-d{days}",
                finding_type="ReplayReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    _render_replay(report)


def _render_replay(report: Any) -> None:
    """Render a ReplayReport, saving and cost side by side."""
    console.print(
        f"[bold]Pack Policy Replay[/bold] (last {report.window_days} days, "
        f"n={report.attributed_packs} attributed packs)"
    )
    console.print(f"  policy: [cyan]{report.policy}[/cyan]")
    console.print()

    for arm in (report.baseline, report.counterfactual):
        frac = (
            f"{arm.useful_token_fraction:.1%}"
            if arm.useful_token_fraction is not None
            else "suppressed"
        )
        shape = f"{arm.body_items_served} bodies"
        if arm.pointer_items_served:
            shape += f" + {arm.pointer_items_served} pointers"
        console.print(
            f"  [bold]{arm.label:<28}[/bold] {arm.injected_tokens:>7,} tok  "
            f"useful-token fraction {frac:>10}   ({shape})"
        )

    console.print()
    if report.token_delta is not None:
        style = "green" if report.token_delta < 0 else "yellow"
        console.print(f"  tokens: [{style}]{report.token_delta:+.1%}[/{style}]")
    if report.fraction_delta is not None:
        style = "green" if report.fraction_delta > 0 else "red"
        console.print(
            f"  useful-token fraction: [{style}]{report.fraction_delta:+.1%}[/{style}]"
        )

    console.print()
    console.print("  [bold]What the policy cost[/bold]")
    withheld_style = "yellow" if report.helpful_bodies_withheld else "green"
    console.print(
        f"    cited-helpful servings, body withheld (id fetchable): "
        f"[{withheld_style}]{report.helpful_bodies_withheld}[/{withheld_style}]"
        f"/{report.helpful_items_total}"
    )
    dropped_style = "red" if report.helpful_items_dropped else "green"
    console.print(
        f"    cited-helpful servings dropped (unreachable):       "
        f"[{dropped_style}]{report.helpful_items_dropped}[/{dropped_style}]"
        f"/{report.helpful_items_total}"
    )
    console.print(
        f"    items admitted that nobody graded:                  "
        f"{report.admitted_ungraded_items}"
    )

    if report.notes:
        console.print()
        for note in report.notes:
            console.print(f"  [dim]{note}[/dim]")


@analyze_app.command("value")
def value(
    days: int = typer.Option(30, help="Days of history to analyze"),
    model: str | None = typer.Option(
        None, "--model", help="Consuming model for pricing (e.g. claude-opus)"
    ),
    price_per_mtok: float | None = typer.Option(
        None, "--price-per-mtok", help="Override input price, USD per 1M tokens"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Value density of served context: what share of injected tokens got cited.

    Joins PACK_ASSEMBLED.injected_items[].estimated_tokens (the cost of
    each item placed in an agent's prompt) against
    FEEDBACK_RECORDED.helpful_item_ids (the caller's verdict), and reports
    the resulting useful-token fraction per strategy, item type, and
    intent family, plus what one cited item cost in dollars.

    This measures the PRECISION OF WHAT WAS SERVED — a value-density
    proxy. It is not benefit: it cannot say whether memory improved an
    outcome, only what share of the tokens it injected the caller went on
    to cite. Answering the benefit question needs a withhold arm this
    system does not have.

    Every ratio is reported with the number of attributed packs behind it,
    and a ratio computed from fewer than the stated minimum is refused
    rather than rounded — per axis cell as well as overall.
    """
    from trellis.retrieve.pack_value import summarize_pack_value  # noqa: PLC0415

    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.value",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = summarize_pack_value(
            event_log, days=days, model=model, price_per_mtok=price_per_mtok
        )
        if _meta_record.enabled and report.attributed_packs > 0:
            _meta_record.produced_finding(
                f"pack-value-report-d{days}",
                finding_type="PackValueReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Served-Context Value Density[/bold] (last {days} days)")
    if report.scan.truncated:
        console.print(f"  [yellow]window[/yellow] {report.scan.note}")
    console.print(
        f"  Coverage: {report.attributed_packs} attributed pack(s) "
        f"[dim](n for every ratio below; minimum "
        f"{report.min_attributed_packs})[/dim]"
    )
    console.print(
        f"    {report.pack_targeted_attributed}/{report.pack_targeted_feedback} "
        f"pack-targeted feedback events cited items; "
        f"{report.flat_packs}/{report.packs} packs were attributable"
    )
    if report.sectioned_packs_excluded:
        console.print(
            f"    [dim]{report.sectioned_packs_excluded} sectioned pack(s) "
            "excluded — build_sectioned emits no injected_items[].[/dim]"
        )

    console.print()
    if report.suppressed:
        console.print(
            f"  [yellow]Ratio refused[/yellow]: {report.attributed_packs} "
            f"attributed pack(s) is below the "
            f"{report.min_attributed_packs}-pack minimum."
        )
        console.print(
            f"  [dim]Raw counts: {report.helpful_tokens:,} cited-helpful of "
            f"{report.injected_tokens:,} injected tokens.[/dim]"
        )
    else:
        frac = report.useful_token_fraction or 0.0
        style = "green" if frac >= _RATE_YELLOW else "yellow"
        console.print(
            f"  [bold]useful-token fraction: "
            f"[{style}]{frac:.1%}[/{style}][/bold] "
            f"({report.helpful_tokens:,} of {report.injected_tokens:,} "
            f"injected tokens were cited helpful, n={report.attributed_packs})"
        )
        console.print(
            f"    cited unhelpful: {report.unhelpful_token_fraction or 0.0:.1%}"
            f"   no verdict: {report.unjudged_token_fraction or 0.0:.1%}"
        )
        _print_value_bounds(report, lower=frac)
        if report.dollars_per_cited_item is not None:
            console.print(
                f"  [bold]${report.dollars_per_cited_item:,.5f} per cited "
                f"item[/bold] "
                f"[dim](${report.injected_dollars:,.4f} across "
                f"{report.distinct_helpful_items} cited items, at "
                f"${report.price_per_mtok:g}/Mtok {report.model}, "
                f"{report.price_source})[/dim]"
            )

    for title, cells in (
        ("Strategy", report.by_strategy),
        ("Item type", report.by_item_type),
        ("Item namespace", report.by_item_namespace),
        ("Intent family", report.by_intent_family),
    ):
        _print_value_axis(title, cells, minimum=report.min_attributed_packs)

    console.print()
    console.print(
        f"  Response-token attribution: "
        f"{escape(str(report.response_events_with_pack_id))}/{report.response_events} "
        f"TOKEN_TRACKED events carry a pack_id "
        f"({report.response_pack_id_coverage:.0%}); "
        f"{report.attributed_packs_with_response_tokens} attributed pack(s) "
        "priced by rendered response"
    )
    if report.response_dollars_per_cited_item is not None:
        console.print(
            f"    ${report.response_dollars_per_cited_item:,.5f} per cited item "
            "measured on rendered response tokens"
        )

    for note in report.notes:
        console.print(f"  [dim]note: {note}[/dim]")


@analyze_app.command("cost")
def cost(
    days: int = typer.Option(7, help="Days of history to analyze"),
    model: str | None = typer.Option(
        None, "--model", help="Consuming model for pricing (e.g. claude-opus)"
    ),
    price_per_mtok: float | None = typer.Option(
        None, "--price-per-mtok", help="Override input price, USD per 1M tokens"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        help=(
            "Max events to scan. Raise it when the report says TRUNCATED; "
            "the newest events are kept, so the note names what was cut."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Estimate Trellis's cost overhead — dollars of context it injected."""
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.cost",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = summarize_trellis_cost(
            event_log,
            days=days,
            model=model,
            price_per_mtok=price_per_mtok,
            limit=limit,
        )
        if _meta_record.enabled and report.overhead_events > 0:
            _meta_record.produced_finding(
                f"trellis-cost-report-d{days}",
                finding_type="TrellisCostReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Trellis Cost Overhead[/bold] (last {days} days)")
    if report.scan.truncated:
        # #374, and it matters most here: this is the only surface that turns
        # a capped read into a dollar figure. A silently shortened window
        # understates spend, which is the direction nobody investigates.
        console.print(f"  [yellow]window[/yellow] {report.scan.note}")
    console.print(
        f"  Injected {report.overhead_tokens:,} tokens "
        f"across {report.overhead_events} retrievals"
    )
    console.print(
        f"  [green]≈ ${report.overhead_dollars:,.4f}[/green] "
        f"at ${report.price_per_mtok:g}/Mtok input "
        f"({report.model}, {report.price_source})"
    )
    console.print(
        "  [dim]This is the marginal input-token overhead memory adds to "
        "agent turns.\n"
        "  Compare against your provider's input bill for the overhead "
        "fraction.[/dim]"
    )

    if report.by_operation:
        console.print()
        op_table = Table(title="Cost by Operation")
        op_table.add_column("Operation", style="cyan")
        op_table.add_column("Layer", style="dim")
        op_table.add_column("Calls", justify="right")
        op_table.add_column("Tokens", justify="right")
        op_table.add_column("Dollars", justify="right")
        for op in report.by_operation:
            op_table.add_row(
                op["operation"],
                op["layer"],
                str(op["calls"]),
                f"{op['tokens']:,}",
                f"${op['dollars']:,.4f}",
            )
        console.print(op_table)

    if report.overhead_events == 0:
        console.print()
        console.print(
            "[dim]No token usage recorded yet. Trellis's context tools"
            " track it automatically once agents start retrieving.[/dim]"
        )


def _render_advisory_degradation(
    degraded: dict[str, Any] | None,
    target: Console | None = None,
    *,
    aftermath: str = (
        "Writes are refused so the file is intact. Advisories that parsed "
        "are still served in packs."
    ),
) -> None:
    """Print the advisory store's degraded state, or nothing at all.

    One renderer for every text surface — both ``analyze`` commands and
    ``worker curate`` — so a warning cannot exist in ``--format json``
    alone, and so the three cannot drift apart.

    **Every interpolated value is escaped.** ``detail`` is arbitrary
    exception text and ``path`` is an arbitrary filesystem path, and Rich
    reads ``[...]`` as markup: an unescaped ``detail`` of
    ``'expected [advisories] key'`` renders as ``expected  key``, and a path
    under ``/tmp/my [staging] dir/`` turns the recovery line into a command
    that does not run. The recovery command is the entire justification for
    this design — an operator at 03:00 needs the fix, not a diagnosis — so
    it is the one string that must survive rendering byte-for-byte.
    """
    if not degraded:
        return
    out = target if target is not None else console
    out.print(
        f"  [bold red]ADVISORY STORE DEGRADED[/bold red] — "
        f"{escape(str(degraded['reason']))}: {escape(str(degraded['detail']))}"
    )
    out.print(
        f"    file: [cyan]{escape(str(degraded['path']))}[/cyan] "
        f"({degraded['rows_loaded']} row(s) readable, "
        f"{escape(str(degraded['rows_skipped_display']))} not)"
    )
    out.print(f"    {aftermath}")
    # ``soft_wrap`` for the same reason everything above is escaped, one
    # step further: Rich also *folds* a line at the console width, and the
    # default advisory path makes this command 100 characters — so on an
    # 80-column terminal it arrives split across two lines, and a copied
    # ``mv <source>`` with the destination on the next line is not a
    # command that runs. The width comes from the terminal, so the same
    # incident is paste-able for one operator and not for the next.
    # ``soft_wrap`` disables wrapping and cropping while keeping the
    # markup; ``no_wrap`` alone would *truncate* the path instead.
    out.print(
        f"    To reset: [bold]{escape(str(degraded['recovery']))}[/bold]",
        soft_wrap=True,
    )


def _exit_on_refused_advisory_write(
    exc: StoreWriteRefusedError, output_format: str
) -> None:
    """Render a refused advisory write and exit, instead of a traceback.

    Covers the refusal no pre-check can: :meth:`AdvisoryStore.refuse_if_stale`
    fires when another process wrote the file between this command's load
    and its save, and the store was perfectly healthy when the pre-check
    ran (#438). There is no ``LoadDegradation`` to render in that
    case, so this reads the exception rather than the store.

    :data:`~trellis_cli.exit_codes.EXIT_STORE`, which is at once what
    :func:`_exit_if_advisory_store_degraded` uses — the two advisory
    refusals must agree with each other, since a cron wrapper branches on
    the code without knowing which of them it hit — and what
    :func:`~trellis_cli.exit_codes.exit_code_for` already maps a
    ``StoreWriteRefusedError`` to. It used to write ``2`` out, which
    *overrode* that canonical map, so one condition had two codes: ``2``
    here and ``5`` from ``trellis policy list`` meeting the sibling file
    damaged the same way (#489). ``2`` is the value the two rules actually
    conflict at — it means "fix your input, retry", and a wrapper that
    retries with corrected arguments against a file no argument can fix
    loops forever.
    """
    if output_format == "json":
        emit_json(
            {
                "status": "refused",
                "code": exc.code,
                "message": exc.message,
                "path": exc.path,
                "recovery": exc.recovery,
            }
        )
    else:
        console.print(
            f"  [bold yellow]ADVISORY WRITE REFUSED[/bold yellow] — "
            f"{escape(str(exc.message))}",
            soft_wrap=True,
        )
        console.print(
            "    Another process wrote the file after this command read it; "
            "its rows are intact. Re-run to pick them up.",
            soft_wrap=True,
        )
    raise typer.Exit(code=EXIT_STORE)


def _exit_if_advisory_store_degraded(store: AdvisoryStore, output_format: str) -> None:
    """Stop a command that cannot act on a store which loaded degraded.

    Non-zero rather than 0-with-a-warning: the caller asked for work that
    did not happen, and a cron wrapper that only checks the status code has
    to be able to see that.

    The code is :data:`~trellis_cli.exit_codes.EXIT_STORE` — the
    deployment's state is wrong and a human has to change it — and it is
    what ``trellis policy list`` already exits when the *sibling*
    ``DegradableJsonStore`` file loads degraded the same way. One root
    cause, one code (#489). Both ``analyze`` advisory commands use this,
    ``generate-advisories`` reaches the same value inline off
    ``report.store_degradation`` (its generator returns early rather than
    raising, so there is no exception to catch), and
    ``trellis worker curate`` exits it off ``CurateCycleResult.status``.
    """
    degradation = store.degradation
    if degradation is None:
        return
    degraded = degradation.to_dict()
    if output_format == "json":
        emit_json({"status": "degraded", "store_degradation": degraded})
    else:
        _render_advisory_degradation(degraded)
    raise typer.Exit(code=EXIT_STORE)


@analyze_app.command("generate-advisories")
def generate_advisories(
    days: int = typer.Option(30, help="Days of history to analyze"),
    min_sample: int = typer.Option(
        5, "--min-sample", help="Min sample size for advisory generation"
    ),
    min_effect: float = typer.Option(
        0.15, "--min-effect", help="Min effect size to emit an advisory"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Generate advisories from outcome data.

    Analyzes PACK_ASSEMBLED and FEEDBACK_RECORDED events to find patterns
    that correlate with success or failure, then stores deterministic
    advisories that can be delivered alongside future context packs.
    """
    from trellis_cli.config import get_data_dir  # noqa: PLC0415

    event_log = get_event_log()
    # #373: resolve through the one seam, so this writer lands on the file
    # every pack-assembling reader opens.
    store = AdvisoryStore(resolve_advisory_path(get_data_dir() / "stores"))

    try:
        with wrap_cli_meta_analysis(
            agent_suffix="analyze",
            analyzer_name="cli.analyze.generate-advisories",
            disabled=no_meta_trace,
        ) as _meta_record:
            generator = AdvisoryGenerator(
                event_log,
                store,
                min_sample_size=min_sample,
                min_effect_size=min_effect,
            )
            report = generator.generate(days=days)
            if _meta_record.enabled and report.advisories_generated > 0:
                _meta_record.produced_finding(
                    f"advisories-generated-d{days}",
                    finding_type="AdvisoryGenerationReport",
                )
    except StoreWriteRefusedError as exc:
        # The store's own guards, surfaced as a refusal rather than as a
        # traceback. ``generate`` returns early on a *degraded* store, so in
        # practice this is the stale one (#438): another process regenerated
        # advisories while this command was analysing, and the analysis it
        # was about to write is the same analysis, one round later.
        _exit_on_refused_advisory_write(exc, output_format)

    if output_format == "json":
        print(json.dumps(report.model_dump(), indent=2, default=str))
        # Same rule *and the same code* as ``advisory-effectiveness``: the
        # caller asked for advisories and got none, so a wrapper reading
        # only the status code has to see it. The report is emitted first,
        # so the payload stays parseable either way (#393). These two arms
        # do not route through ``_exit_if_advisory_store_degraded`` —
        # ``generate`` returns a report rather than raising, and the
        # degradation is rendered inside each arm's own layout — so they
        # were the two sites a helper-only fix would have left on ``2``
        # (#489).
        if report.store_degradation:
            raise typer.Exit(code=EXIT_STORE)
    else:
        console.print(f"[bold]Advisory Generation Report[/bold] (last {days} days)")
        # Before the counts, not after: every number below is zero because
        # the run refused, and a reader who meets the zeros first has
        # already drawn the wrong conclusion. Rendering it here also keeps
        # the two formats honest with each other — the JSON branch carries
        # ``store_degradation`` whether or not this branch prints it (#393).
        _render_advisory_degradation(report.store_degradation)
        console.print(f"  Packs analyzed: {report.total_packs}")
        console.print(f"  Feedback events: {report.total_feedback}")
        console.print(f"  Advisories generated: {report.advisories_generated}")
        console.print(f"  Advisories stored: {report.advisories_stored}")
        if report.findings_refused_no_comparison_arm:
            console.print(
                "  Findings refused (no comparison arm):"
                f" {report.findings_refused_no_comparison_arm}"
            )
        if report.coverage.note:
            console.print(f"  [yellow]{report.coverage.note}[/yellow]")

        if report.advisories_generated > 0:
            console.print()
            advisories = store.list()
            table = Table(title="Generated Advisories")
            table.add_column("Category", style="cyan")
            table.add_column("Confidence", justify="right")
            table.add_column("Message", max_width=60)
            table.add_column("Scope", style="dim")

            for adv in advisories:
                conf_style = (
                    "green"
                    if adv.confidence >= _RATE_GREEN
                    else "yellow"
                    if adv.confidence >= _RATE_YELLOW
                    else "dim"
                )
                table.add_row(
                    adv.category.value,
                    f"[{conf_style}]{adv.confidence:.2f}[/{conf_style}]",
                    adv.message[:60],
                    adv.scope,
                )
            console.print(table)

        # Gated on the degradation: a refused run reports ``total_feedback=0``
        # because it never read the event log, so this hint would fire and
        # tell the operator to go record feedback — the exact misdiagnosis
        # the banner above it exists to prevent.
        if report.total_feedback == 0 and not report.store_degradation:
            console.print()
            console.print(
                "[dim]No feedback recorded yet. Record outcomes via"
                " 'trellis curate feedback' or the MCP record_feedback"
                " tool to enable advisory generation.[/dim]"
            )

        # The text arm's half of the pair above — same condition, same
        # code, below the rendering rather than inside it.
        if report.store_degradation:
            raise typer.Exit(code=EXIT_STORE)


@analyze_app.command("advisory-effectiveness")
def advisory_effectiveness(  # noqa: PLR0912 - CLI rendering branches per report section
    days: int = typer.Option(30, help="Days of history to analyze"),
    min_presentations: int = typer.Option(
        3, "--min-presentations", help="Min advisory presentations to score"
    ),
    suppress_below: float = typer.Option(
        0.1, "--suppress-below", help="Suppress advisories below this confidence"
    ),
    blend_weight: float = typer.Option(
        0.3, "--blend-weight", help="Weight of observed fitness in confidence update"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Analyze without adjusting confidence"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Analyze advisory effectiveness and adjust confidence.

    Measures how each advisory correlates with pack outcomes, then
    adjusts confidence scores accordingly.  Advisories that consistently
    correlate with success gain confidence; those that correlate with
    failure lose confidence and may be suppressed.
    """
    from trellis.retrieve.effectiveness import (  # noqa: PLC0415
        analyze_advisory_effectiveness,
    )
    from trellis_cli.config import get_data_dir  # noqa: PLC0415

    event_log = get_event_log()
    # #373: same seam as ``generate-advisories`` — the fitness loop must
    # score the advisories that were actually served.
    store = AdvisoryStore(resolve_advisory_path(get_data_dir() / "stores"))

    # The fitness loop writes (put / suppress / restore), and every one of
    # those raises on a store that could not read its file. Exiting here
    # turns a traceback into the recovery command (#393). ``--dry-run`` is
    # read-only and would not raise, but it would score a set that is
    # missing whatever failed to parse, so it stops too.
    _exit_if_advisory_store_degraded(store, output_format)

    try:
        with wrap_cli_meta_analysis(
            agent_suffix="analyze",
            analyzer_name="cli.analyze.advisory-effectiveness",
            disabled=no_meta_trace,
        ) as _meta_record:
            if dry_run:
                report = analyze_advisory_effectiveness(
                    event_log,
                    store,
                    days=days,
                    min_presentations=min_presentations,
                )
            else:
                report = run_advisory_fitness_loop(
                    event_log,
                    store,
                    days=days,
                    min_presentations=min_presentations,
                    suppress_below=suppress_below,
                    blend_weight=blend_weight,
                )
            if _meta_record.enabled and report.advisory_scores:
                _meta_record.produced_finding(
                    f"advisory-fitness-d{days}{'-dryrun' if dry_run else ''}",
                    finding_type="AdvisoryFitnessReport",
                )
    except StoreWriteRefusedError as exc:
        # The pre-check above cannot see this one: the store loaded clean
        # and another process wrote the file while the loop was scoring
        # (#438). The loop writes per advisory, so some adjustments may
        # already have landed — the refusal stops the rest rather than
        # letting them replace the other process's rows.
        _exit_on_refused_advisory_write(exc, output_format)

    if output_format == "json":
        print(json.dumps(report.model_dump(), indent=2, default=str))
    else:
        console.print(f"[bold]Advisory Effectiveness Report[/bold] (last {days} days)")
        console.print(f"  Packs with advisories: {report.total_packs_with_advisories}")
        console.print(f"  Feedback events: {report.total_feedback}")

        if report.advisory_scores:
            console.print()
            table = Table(title="Advisory Fitness")
            table.add_column("Advisory ID", style="cyan", max_width=15)
            table.add_column("Presentations", justify="right")
            table.add_column("Success Rate", justify="right")
            table.add_column("Baseline", justify="right")
            table.add_column("Lift", justify="right")

            for score in report.advisory_scores:
                lift_style = "green" if score.lift > 0 else "red"
                table.add_row(
                    escape(score.advisory_id[:15]),
                    str(score.presentations),
                    f"{score.success_rate:.1%}",
                    f"{score.baseline_rate:.1%}",
                    f"[{lift_style}]{score.lift:+.1%}[/{lift_style}]",
                )
            console.print(table)

        if report.advisories_boosted:
            console.print()
            console.print(f"[green]Boosted ({len(report.advisories_boosted)}):[/green]")
            for adv_id in report.advisories_boosted:
                console.print(f"  + {escape(adv_id)}")

        if report.advisories_suppressed:
            console.print()
            console.print(
                f"[red]Suppressed ({len(report.advisories_suppressed)}):[/red]"
            )
            for adv_id in report.advisories_suppressed:
                console.print(f"  - {escape(adv_id)}")

        if not dry_run and not report.advisory_scores:
            console.print()
            console.print(
                "[dim]No advisories had enough presentations to score."
                " Run 'trellis analyze generate-advisories' first, then"
                " record pack outcomes to build fitness data.[/dim]"
            )


@analyze_app.command("pack-sections")
def pack_sections(
    days: int = typer.Option(30, help="Days of history to analyze"),
    empty_rate_threshold: float = typer.Option(
        0.5,
        "--empty-rate-threshold",
        help="Flag sections whose empty rate meets or exceeds this value",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        PACK_SECTIONS_SCAN_LIMIT,
        "--limit",
        help=(
            "Max events to scan. Raise it when the report says TRUNCATED; "
            "the newest events are kept, so the note names what was cut."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Audit sectioned pack composition across recent assemblies.

    Reads ``PACK_ASSEMBLED`` events emitted by sectioned pack builds and
    reports per-section item counts, empty rates, and unique item counts.
    Useful for spotting sections that consistently miss their target
    content or deliver far fewer items than their budget allows.
    """
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.pack-sections",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_pack_sections(
            event_log,
            days=days,
            empty_rate_threshold=empty_rate_threshold,
            limit=limit,
        )
        if _meta_record.enabled and report.section_stats:
            _meta_record.produced_finding(
                f"pack-sections-report-d{days}",
                finding_type="PackSectionsReport",
            )

    if output_format == "json":
        rows = [
            {
                "name": s.name,
                "packs_count": s.packs_count,
                "total_items": s.total_items,
                "empty_count": s.empty_count,
                "unique_items": s.unique_items,
                "empty_rate": s.empty_rate,
                "avg_items": s.avg_items,
            }
            for s in report.section_stats
        ]
        print(
            json.dumps(
                {
                    "total_sectioned_packs": report.total_sectioned_packs,
                    "section_stats": rows,
                    "empty_section_flags": report.empty_section_flags,
                    # Hand-assembled rather than `report.model_dump()`, so
                    # a new report field reaches this surface only when
                    # named here — which is why the coverage is explicit.
                    "scan": report.scan.model_dump(),
                },
                indent=2,
            )
        )
        return

    console.print(f"[bold]Pack Sections Report[/bold] (last {days} days)")
    if report.scan.truncated:
        # #374. Worth reading twice here: the cap applies to ALL pack
        # events and sectioned packs are filtered out of the result
        # afterwards, so on a deployment where they are a minority this
        # report shrinks by far more than the cap suggests.
        console.print(f"  [yellow]window[/yellow] {report.scan.note}")
    console.print(f"  Sectioned packs analyzed: {report.total_sectioned_packs}")

    if not report.section_stats:
        console.print()
        console.print(
            "[dim]No sectioned packs recorded in this window."
            " Use get_sectioned_context (MCP) or PackBuilder.build_sectioned()"
            " to emit telemetry.[/dim]"
        )
        return

    console.print()
    table = Table(title="Per-Section Composition")
    table.add_column("Section", style="cyan")
    table.add_column("Packs", justify="right")
    table.add_column("Avg items", justify="right")
    table.add_column("Total items", justify="right")
    table.add_column("Unique items", justify="right")
    table.add_column("Empty rate", justify="right")

    for section in report.section_stats:
        rate_style = (
            "red"
            if section.empty_rate >= empty_rate_threshold
            else "yellow"
            if section.empty_rate >= _RATE_YELLOW
            else "green"
        )
        table.add_row(
            section.name,
            str(section.packs_count),
            f"{section.avg_items:.1f}",
            str(section.total_items),
            str(section.unique_items),
            f"[{rate_style}]{section.empty_rate:.1%}[/{rate_style}]",
        )
    console.print(table)

    if report.empty_section_flags:
        console.print()
        console.print(
            f"[red]Frequently empty (empty rate >= {empty_rate_threshold:.0%}):[/red]"
        )
        for name in report.empty_section_flags:
            console.print(f"  ! {name}")


# ---------------------------------------------------------------------------
# Pack Quality Evaluation (scenario mode)
# ---------------------------------------------------------------------------


_MISSING_COVERAGE_PREVIEW = 8


def _load_scenarios(path: Path) -> list[EvaluationScenario]:
    """Parse a YAML fixture file into a list of EvaluationScenario.

    Accepts either a top-level list of scenario dicts or a dict with a
    top-level ``scenarios:`` key holding the list.
    """
    import yaml  # noqa: PLC0415

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "scenarios" in raw:
        raw = raw["scenarios"]
    if not isinstance(raw, list):
        msg = f"{path}: expected a list of scenarios or a top-level 'scenarios' key"
        raise typer.BadParameter(msg)
    scenarios: list[EvaluationScenario] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = f"{path}: scenarios[{idx}] is not a mapping"
            raise typer.BadParameter(msg)
        scenarios.append(EvaluationScenario(**entry))
    return scenarios


def _resolve_profile(profile_name: str | None) -> EvaluationProfile | None:
    if profile_name is None:
        return None
    try:
        return BUILTIN_PROFILES[profile_name]
    except KeyError as exc:
        names = ", ".join(sorted(BUILTIN_PROFILES))
        msg = f"unknown profile {profile_name!r}; choose from: {names}"
        raise typer.BadParameter(msg) from exc


def _assemble_pack_for_scenario(scenario: EvaluationScenario) -> object:
    """Build a Pack for a scenario by running PackBuilder against live stores.

    Imported inline to keep the CLI module light and avoid pulling
    PackBuilder's strategy graph into non-quality commands.
    """
    from trellis.retrieve.builder_factory import build_pack_builder  # noqa: PLC0415
    from trellis_cli.stores import _get_registry  # noqa: PLC0415

    # "Mirror the MCP server / API wire-up" used to be a comment over a
    # hand-copied argument list, and the copy had drifted: this one passed
    # no ``advisory_store``, so a scenario was scored against a builder
    # production does not use. It is the same call now (#410).
    builder = build_pack_builder(_get_registry(), surface="cli.analyze.pack-quality")
    filters: dict[str, object] | None = (
        {"domain": scenario.domain} if scenario.domain else None
    )
    return builder.build(
        intent=scenario.intent,
        domain=scenario.domain,
        filters=filters,
    )


@analyze_app.command("pack-quality")
def pack_quality(
    scenarios_path: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--scenarios",
        "-s",
        help="YAML file defining EvaluationScenario fixtures.",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    profile_name: str | None = typer.Option(
        None,
        "--profile",
        help=(
            "Named weight profile to aggregate dimensions. "
            "One of: code_generation, domain_context. "
            "Omit for a simple mean across dimensions."
        ),
    ),
    assemble: bool = typer.Option(
        True,
        "--assemble/--no-assemble",
        help=(
            "Assemble a live pack per scenario via PackBuilder and score it. "
            "Set --no-assemble to validate scenario parsing only."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Score packs against declared scenarios across 6 quality dimensions.

    Scenario mode only: loads ``EvaluationScenario`` fixtures, assembles
    packs via ``PackBuilder``, and scores each on completeness, relevance,
    noise, breadth, efficiency, and (opt-in via ``expected_shapes``)
    shape_composition. Event-log mode (joining to ``PACK_ASSEMBLED``
    events) is tracked as follow-up work.
    """
    scenarios = _load_scenarios(scenarios_path)
    profile = _resolve_profile(profile_name)

    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.pack-quality",
        disabled=no_meta_trace,
    ) as _meta_record:
        if not assemble:
            if output_format == "json":
                print(
                    json.dumps(
                        {"scenarios": [s.model_dump() for s in scenarios]},
                        default=str,
                    )
                )
            else:
                console.print(f"[green]Parsed {len(scenarios)} scenario(s).[/green]")
                for s in scenarios:
                    console.print(f"  - {s.name}: {s.intent[:60]}")
            return

        reports: list[QualityReport] = []
        for scenario in scenarios:
            pack = _assemble_pack_for_scenario(scenario)
            report = evaluate_pack(pack, scenario, profile=profile)  # type: ignore[arg-type]
            reports.append(report)
        if _meta_record.enabled and reports:
            _meta_record.produced_finding(
                f"pack-quality-report-{len(reports)}-scenarios",
                finding_type="PackQualityReport",
            )

    if output_format == "json":
        print(
            json.dumps(
                {"reports": [r.model_dump() for r in reports]},
                default=str,
            )
        )
        return

    console.print(
        f"[bold]Pack Quality Report[/bold] "
        f"(profile: {profile.name if profile else 'mean'})"
    )
    table = Table(title="Quality Scores by Scenario")
    table.add_column("Scenario", style="cyan")
    table.add_column("Complete", justify="right")
    table.add_column("Relevance", justify="right")
    table.add_column("Noise", justify="right")
    table.add_column("Breadth", justify="right")
    table.add_column("Efficiency", justify="right")
    table.add_column("Weighted", justify="right", style="bold")

    for report in reports:
        dims = report.dimensions
        weighted_style = (
            "green"
            if report.weighted_score >= _RATE_GREEN
            else "yellow"
            if report.weighted_score >= _RATE_YELLOW
            else "red"
        )
        table.add_row(
            report.scenario_name,
            f"{dims.get('completeness', 0.0):.2f}",
            f"{dims.get('relevance', 0.0):.2f}",
            f"{dims.get('noise', 0.0):.2f}",
            f"{dims.get('breadth', 0.0):.2f}",
            f"{dims.get('efficiency', 0.0):.2f}",
            f"[{weighted_style}]{report.weighted_score:.2f}[/{weighted_style}]",
        )
    console.print(table)

    for report in reports:
        if not (report.missing_coverage or report.findings):
            continue
        console.print()
        console.print(f"[bold]{report.scenario_name}[/bold]")
        if report.missing_coverage:
            cap = _MISSING_COVERAGE_PREVIEW
            preview = ", ".join(report.missing_coverage[:cap])
            extra = len(report.missing_coverage) - cap
            more = "" if extra <= 0 else f" (+{extra} more)"
            console.print(f"  [yellow]missing coverage:[/yellow] {preview}{more}")
        for finding in report.findings:
            console.print(f"  [dim]- {finding}[/dim]")


# ---------------------------------------------------------------------------
# Dimension Predictiveness (Pack Quality P3 — validation before calibration)
# ---------------------------------------------------------------------------


_SIGNAL_STYLES: dict[str, str] = {
    "strong": "green",
    "moderate": "green",
    "weak": "yellow",
    "noise": "red",
    "insufficient_data": "dim",
}


def _format_optional_float(value: float | None, fmt: str = "{:+.2f}") -> str:
    return "-" if value is None else fmt.format(value)


@analyze_app.command("dimension-predictiveness")
def dimension_predictiveness(
    days: int = typer.Option(30, help="Days of history to analyze"),
    success_threshold: float = typer.Option(
        0.5, help="Rating threshold to consider a pack successful"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        help=(
            "Max events to scan. Raise it when the report says TRUNCATED; "
            "the newest events are kept, so the note names what was cut."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Validate which quality dimensions actually predict task success.

    Joins ``PACK_QUALITY_SCORED`` events (emitted when a ``PackBuilder``
    evaluator is wired) with ``FEEDBACK_RECORDED`` events by ``pack_id``
    and reports per-dimension point-biserial correlation.

    Read-only analytics. No mutation of profiles, scorers, or classifier
    state — auto-calibration of profile weights is separate P3 work that
    depends on this report as its substrate.
    """
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.dimension-predictiveness",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_dimension_predictiveness(
            event_log,
            days=days,
            success_threshold=success_threshold,
            limit=limit,
        )
        if _meta_record.enabled and report.dimensions:
            _meta_record.produced_finding(
                f"dimension-predictiveness-d{days}",
                finding_type="DimensionPredictivenessReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump(), default=str))
        return

    console.print(f"[bold]Dimension Predictiveness Report[/bold] (last {days} days)")
    console.print(f"  Packs scored: {report.total_packs_scored}")
    console.print(f"  Matched feedback: {report.total_matched_feedback}")
    console.print(f"  Overall success rate: {report.overall_success_rate:.1%}")

    if not report.dimensions and report.weighted_score_predictiveness is None:
        console.print()
        console.print(
            "[dim]No dimensions observed. Wire a PackBuilder evaluator "
            "(see docs/agent-guide/pack-quality-evaluation.md) and record "
            "feedback before this report becomes useful.[/dim]"
        )
        for note in report.notes:
            console.print(f"  [dim]- {note}[/dim]")
        return

    console.print()
    table = Table(title="Per-Dimension Predictiveness")
    table.add_column("Dimension", style="cyan")
    table.add_column("Samples", justify="right")
    table.add_column("Correlation", justify="right")
    table.add_column("Mean|success", justify="right")
    table.add_column("Mean|failure", justify="right")
    table.add_column("Signal")

    rows = list(report.dimensions)
    if report.weighted_score_predictiveness is not None:
        rows.append(report.weighted_score_predictiveness)

    for entry in rows:
        style = _SIGNAL_STYLES.get(entry.signal_classification, "dim")
        table.add_row(
            entry.dimension,
            str(entry.sample_count),
            _format_optional_float(entry.correlation),
            _format_optional_float(entry.mean_score_on_success, "{:.2f}"),
            _format_optional_float(entry.mean_score_on_failure, "{:.2f}"),
            f"[{style}]{entry.signal_classification}[/{style}]",
        )
    console.print(table)

    if report.notes:
        console.print()
        for note in report.notes:
            console.print(f"  [dim]- {note}[/dim]")


# ---------------------------------------------------------------------------
# Pack Telemetry (Gap 3.4 — close-the-loop consumption of PACK_ASSEMBLED)
# ---------------------------------------------------------------------------


@analyze_app.command("pack-telemetry")
def pack_telemetry(
    days: int = typer.Option(7, help="Days of history to analyze"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        help=(
            "Max events to scan. Raise it when the report says TRUNCATED; "
            "the newest events are kept, so the note names what was cut."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Aggregate rejection / budget / strategy signals from PACK_ASSEMBLED.

    Operator surface for the telemetry that ``PackBuilder`` already emits.
    Highlights budget saturation rates, rejection-reason distribution, and
    per-strategy yield so tuning decisions (budget raise, filter audit,
    strategy retire) can be made from data rather than intuition.
    """
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.pack-telemetry",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_pack_telemetry(event_log, days=days, limit=limit)
        if _meta_record.enabled and report.total_packs > 0:
            _meta_record.produced_finding(
                f"pack-telemetry-report-d{days}",
                finding_type="PackTelemetryReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Pack Telemetry Report[/bold] (last {days} days)")
    # Above the numbers, and unconditional. These notes used to render only
    # inside the `total_packs == 0` branch below — but a truncated scan has
    # `total_packs == limit`, so the branch that printed the truncation
    # caveat was exactly the branch truncation guarantees is not taken
    # (#374). Every count below is computed over `report.scan.scanned`
    # packs, not over the window the header just claimed.
    for note in report.notes:
        console.print(f"  [dim]- {note}[/dim]")
    console.print(f"  Packs assembled: {report.total_packs}")
    if report.total_packs == 0:
        return

    console.print(
        f"  Mean items/pack: {report.mean_items_per_pack:.1f} | "
        f"Mean rejected/pack: {report.mean_rejected_per_pack:.1f}"
    )

    def _rate_style(rate: float) -> str:
        if rate >= _RATE_GREEN:
            return "red"
        if rate >= _RATE_YELLOW:
            return "yellow"
        return "green"

    console.print()
    budget_table = Table(title="Budget Saturation")
    budget_table.add_column("Signal", style="cyan")
    budget_table.add_column("Hit rate", justify="right")
    for label, rate in [
        ("max_items", report.max_items_hit_rate),
        ("token_budget", report.max_tokens_hit_rate),
        ("any budget", report.any_budget_hit_rate),
    ]:
        style = _rate_style(rate)
        budget_table.add_row(label, f"[{style}]{rate:.1%}[/{style}]")
    console.print(budget_table)

    if report.rejection_reason_counts:
        console.print()
        rej_table = Table(title="Rejection Reasons")
        rej_table.add_column("Reason", style="cyan")
        rej_table.add_column("Count", justify="right")
        rej_table.add_column("Share", justify="right")
        sorted_reasons = sorted(
            report.rejection_reason_counts.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
        for reason, count in sorted_reasons:
            share = report.rejection_reason_rates.get(reason, 0.0)
            rej_table.add_row(reason, str(count), f"{share:.1%}")
        console.print(rej_table)

    if report.strategy_contributions:
        console.print()
        strat_table = Table(title="Strategy Contribution")
        strat_table.add_column("Strategy", style="cyan")
        strat_table.add_column("Injected", justify="right")
        strat_table.add_column("Rejected", justify="right")
        strat_table.add_column("Yield", justify="right")
        strat_table.add_column("Top rejections")
        for entry in report.strategy_contributions:
            yield_style = (
                "green"
                if entry.yield_rate >= _RATE_GREEN
                else "yellow"
                if entry.yield_rate >= _RATE_YELLOW
                else "red"
            )
            top = ", ".join(f"{r}:{c}" for r, c in entry.top_rejection_reasons)
            strat_table.add_row(
                entry.strategy,
                str(entry.injected),
                str(entry.rejected),
                f"[{yield_style}]{entry.yield_rate:.1%}[/{yield_style}]",
                top,
            )
        console.print(strat_table)

    if report.findings:
        console.print()
        console.print("[bold]Findings[/bold]")
        for finding in report.findings:
            console.print(f"  [yellow]- {finding}[/yellow]")


# ---------------------------------------------------------------------------
# Extractor Fallbacks (Gap 4.3 — graduation tracking substrate)
# ---------------------------------------------------------------------------


@analyze_app.command("extractor-fallbacks")
def extractor_fallbacks(
    days: int = typer.Option(30, help="Days of history to analyze"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        help=(
            "Max events to scan. Raise it when the report says TRUNCATED; "
            "the newest events are kept, so the note names what was cut."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Summarize extractor fallback telemetry per source_hint.

    Reads ``EXTRACTOR_FALLBACK`` + ``EXTRACTION_DISPATCHED`` events emitted
    by :class:`~trellis.extract.dispatcher.ExtractionDispatcher` and reports
    overall fallback rate, reason distribution, and per-source aggregates.
    Read-only — surfaces candidates for graduation (``empty_result``
    dominates) or audit (``prefer_tier_override`` dominates).
    """
    event_log = get_event_log()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.extractor-fallbacks",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_extractor_fallbacks(event_log, days=days, limit=limit)
        if _meta_record.enabled and report.total_dispatches > 0:
            _meta_record.produced_finding(
                f"extractor-fallbacks-report-d{days}",
                finding_type="ExtractorFallbackReport",
            )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Extractor Fallback Report[/bold] (last {days} days)")
    # Same hoist as `pack-telemetry`, same reason: the truncation caveat used
    # to render only when `total_dispatches == 0`, and a truncated scan has
    # `total_dispatches == limit` (#374).
    for note in report.notes:
        console.print(f"  [dim]- {note}[/dim]")
    console.print(f"  Total dispatches: {report.total_dispatches}")
    console.print(f"  Total fallbacks: {report.total_fallbacks}")
    console.print(f"  Overall fallback rate: {report.overall_fallback_rate:.1%}")

    if report.total_dispatches == 0:
        return

    if report.reason_counts:
        console.print()
        reason_table = Table(title="Fallback Reasons")
        reason_table.add_column("Reason", style="cyan")
        reason_table.add_column("Count", justify="right")
        for reason, count in sorted(
            report.reason_counts.items(), key=lambda kv: kv[1], reverse=True
        ):
            reason_table.add_row(reason, str(count))
        console.print(reason_table)

    if report.per_source:
        console.print()
        source_table = Table(title="Per-Source Fallback Rates")
        source_table.add_column("source_hint", style="cyan")
        source_table.add_column("Dispatches", justify="right")
        source_table.add_column("Fallbacks", justify="right")
        source_table.add_column("Rate", justify="right")
        source_table.add_column("Top reasons")
        for stats in sorted(
            report.per_source,
            key=lambda s: s.fallback_rate,
            reverse=True,
        ):
            rate_style = (
                "red"
                if stats.fallback_rate >= _FALLBACK_RATE_RED
                else "yellow"
                if stats.fallback_rate >= _FALLBACK_RATE_YELLOW
                else "green"
            )
            top_reasons = ", ".join(
                f"{r}:{c}"
                for r, c in sorted(
                    stats.reasons.items(), key=lambda kv: kv[1], reverse=True
                )[:3]
            )
            source_table.add_row(
                stats.source_hint,
                str(stats.total_dispatches),
                str(stats.fallback_events),
                f"[{rate_style}]{stats.fallback_rate:.1%}[/{rate_style}]",
                top_reasons,
            )
        console.print(source_table)

    if report.findings:
        console.print()
        console.print("[bold]Findings[/bold]")
        for finding in report.findings:
            console.print(f"  [yellow]- {finding}[/yellow]")


# ---------------------------------------------------------------------------
# Domain Usage (WP7 — observability for the primary retrieval slice)
# ---------------------------------------------------------------------------


@analyze_app.command("domains")
def domains(
    days: int = typer.Option(30, help="Days of pack/feedback history to analyze"),
    limit: int = typer.Option(
        1000, help="Max traces, documents, and events per source to scan"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Report observed ``domain`` usage across traces, documents, and packs.

    ``domain`` is the primary retrieval slice. This read-only report joins the
    three places a domain surfaces — ``TraceContext.domain`` (TraceStore),
    ``ContentTags.domain`` in document metadata (DocumentStore), and pack +
    feedback events (EventLog, grouped by the pack payload's domain) — and
    tallies, per domain: document count, trace count, packs served, graded
    packs, and success rate from ``FEEDBACK_RECORDED``. Items, traces, and
    packs with no domain are surfaced under ``(none)`` so coverage gaps stay
    visible.

    **Out of scope:** automatic domain discovery / clustering and a domain
    *promotion* analyzer. Those follow the column-leaf pattern (contract
    first, gated on production telemetry) — see
    ``docs/design/adr-column-leaf-modeling-guardrails.md`` and
    ``docs/design/adr-autonomy-ladder.md`` tier 2. This report is the empirical
    substrate a future ADR amendment would build on.
    """
    trace_store = get_trace_store()
    document_store = get_document_store()
    event_log = get_event_log()

    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.domains",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = analyze_domains(
            trace_store,
            document_store,
            event_log,
            days=days,
            scan_limit=limit,
        )
        if _meta_record.enabled and report.domains:
            _meta_record.produced_finding(
                f"domain-usage-report-d{days}",
                finding_type="DomainUsageReport",
            )

    if output_format == "json":
        emit_json(report.to_payload())
        return

    console.print(f"[bold]Domain Usage Report[/bold] (last {days} days)")
    console.print(f"  Domains observed: {len(report.domains)}")

    if not report.domains:
        console.print()
        console.print(
            "[dim]No domains observed. Ingest traces or documents that carry a"
            " domain, or run 'trellis demo load' to populate sample data.[/dim]"
        )
        return

    console.print()
    table = Table(title="Per-Domain Usage")
    table.add_column("Domain", style="cyan")
    table.add_column("Documents", justify="right")
    table.add_column("Traces", justify="right")
    table.add_column("Packs served", justify="right")
    table.add_column("Graded packs", justify="right")
    table.add_column("Success rate", justify="right")

    for entry in report.domains:
        if entry.success_rate is None:
            rate_cell = "[dim]-[/dim]"
        else:
            rate_style = (
                "green"
                if entry.success_rate >= _RATE_GREEN
                else "yellow"
                if entry.success_rate >= _RATE_YELLOW
                else "red"
            )
            rate_cell = f"[{rate_style}]{entry.success_rate:.1%}[/{rate_style}]"
        domain_cell = (
            f"[dim]{entry.domain}[/dim]" if entry.domain == "(none)" else entry.domain
        )
        table.add_row(
            domain_cell,
            str(entry.document_count),
            str(entry.trace_count),
            str(entry.packs_served),
            str(entry.graded_packs),
            rate_cell,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Learning Candidates (H2.3 — operator surface for the promote half)
# ---------------------------------------------------------------------------


@analyze_app.command("learning-candidates")
def learning_candidates(
    output_dir: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--output-dir",
        "-o",
        help=(
            "Directory for the candidates JSON + decisions template. "
            "Created if it doesn't exist."
        ),
    ),
    days: int = typer.Option(30, help="Days of EventLog history to scan"),
    min_support: int = typer.Option(
        2,
        "--min-support",
        help=(
            "Minimum times an item must appear in graded packs to score as a candidate"
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Score the EventLog into learning candidates for human review.

    Joins ``PACK_ASSEMBLED`` + ``FEEDBACK_RECORDED`` events into
    learning observations, scores them against the promote /
    investigate-noise thresholds, and writes two artifacts to
    ``--output-dir``:

      * ``intent_learning_candidates.json`` — the scored report.
      * ``promotion_decisions.template.json`` — a blank approval form.
        Edit this file and set ``approved: true`` on candidates you
        want to promote, then pass it to ``trellis curate
        promote-learning``.

    Read-only. Does not mutate the graph; the promote step does that
    after a human review pass.
    """
    event_log = get_event_log()
    registry = _build_learning_registry_or_exit()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.learning-candidates",
        disabled=no_meta_trace,
    ) as _meta_record:
        observations = build_learning_observations_from_event_log(event_log, days=days)
        report = analyze_learning_observations(
            observations=observations,
            registry=registry,
            min_support=min_support,
            artifacts_root=output_dir,
        )
        paths = write_learning_review_artifacts(report=report, output_dir=output_dir)
        if _meta_record.enabled and report.get("candidate_count", 0) > 0:
            _meta_record.produced_finding(
                f"learning-candidates-d{days}-m{min_support}",
                finding_type="LearningCandidatesReport",
            )

    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "observation_count": report["observation_count"],
                    "candidate_count": report["candidate_count"],
                    "candidates_path": paths["candidates_path"],
                    "decisions_template_path": paths["decisions_template_path"],
                    "candidates": report["candidates"],
                }
            )
        )
        return

    console.print(
        f"[bold]Learning Candidates Report[/bold] (last {days} days, "
        f"min_support={min_support})"
    )
    console.print(f"  Observations scanned: {report['observation_count']}")
    console.print(f"  Candidates generated: {report['candidate_count']}")
    console.print(f"  Candidates JSON: [cyan]{escape(paths['candidates_path'])}[/cyan]")
    console.print(
        f"  Decisions template: [cyan]{escape(paths['decisions_template_path'])}[/cyan]"
    )

    if not report["candidates"]:
        console.print()
        console.print(
            "[dim]No candidates met the threshold. Either no graded packs "
            "in this window, or no item appeared often enough to score. "
            "Lower --min-support or wait for more feedback.[/dim]"
        )
        return

    console.print()
    table = Table(title="Candidates by Recommendation")
    table.add_column("Candidate ID", style="cyan", max_width=24)
    table.add_column("Recommendation", style="bold")
    table.add_column("Item type", style="dim")
    table.add_column("Served", justify="right")
    table.add_column("Success rate", justify="right")
    table.add_column("Retry rate", justify="right")
    for candidate in report["candidates"]:
        metrics = candidate["metrics"]
        rec_style = (
            "green"
            if candidate["recommendation_type"].startswith("promote_")
            else "yellow"
        )
        table.add_row(
            escape(candidate["candidate_id"]),
            f"[{rec_style}]{candidate['recommendation_type']}[/{rec_style}]",
            candidate.get("item_type") or "-",
            str(metrics["times_served"]),
            f"{metrics['success_rate']:.1%}",
            f"{metrics['retry_rate']:.1%}",
        )
    console.print(table)
    console.print()
    console.print(
        "[dim]Edit the decisions template to approve promotions, then run "
        "[bold]trellis curate promote-learning[/bold] with both files.[/dim]"
    )


# ---------------------------------------------------------------------------
# Schema Evolution — well-known promotion candidates (self-improvement item 5)
# ---------------------------------------------------------------------------
#
# Note: ``_InMemoryParameterStore`` is defined once at the top of this module
# (originally added for the learning-candidates path in Item 3). The
# schema-evolution registry reuses it — keeping a single in-process
# ParameterStore implementation avoids the merge-time duplication we saw
# when Items 3 and 5 landed in parallel.


def _build_schema_evolution_registry() -> ParameterRegistry:
    """Construct a ParameterRegistry for ``learning.schema_evolution``.

    Resolution order:

    1. Persistent ParameterStore from the configured registry, if an
       active snapshot exists for the schema-evolution component and
       carries every key in :data:`SCHEMA_EVOLUTION_SEED_DEFAULTS`.
    2. In-memory snapshot seeded with the recommended defaults
       (count=500, distinct extractors=2, distinct domains=2,
       signal_quality=standard, window=7d, cooldown=7d). One WARN log
       line so operators notice they're running unseeded.
    """
    persistent_store = get_parameter_store()
    persistent_registry = ParameterRegistry(persistent_store)
    persistent_snapshot = persistent_registry.get_values(
        ParameterScope(component_id=SCHEMA_EVOLUTION_PARAM_COMPONENT_ID)
    )
    if all(k in persistent_snapshot for k in SCHEMA_EVOLUTION_SEED_DEFAULTS):
        return persistent_registry

    logger.warning(
        "schema_evolution.parameter_registry.seeded_defaults",
        component=SCHEMA_EVOLUTION_PARAM_COMPONENT_ID,
        defaults=dict(SCHEMA_EVOLUTION_SEED_DEFAULTS),
        remediation=(
            "seed via ParameterStore.put() with a ParameterSet "
            "containing the keys in "
            "trellis.learning.RECOMMENDED_SEED_VALUES"
        ),
    )
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id=SCHEMA_EVOLUTION_PARAM_COMPONENT_ID),
            values=dict(SCHEMA_EVOLUTION_SEED_DEFAULTS),
            source="cli:analyze",
            notes="seeded by trellis_cli.analyze._build_schema_evolution_registry",
        )
    )
    return ParameterRegistry(store=store)


@analyze_app.command("schema-evolution")
def schema_evolution(
    kinds: str = typer.Option(
        "entity_type,edge_kind",
        "--kinds",
        help=(
            "Comma-separated subset of candidate kinds to analyze. "
            "Choices: 'entity_type', 'edge_kind'."
        ),
    ),
    no_emit: bool = typer.Option(
        False,
        "--no-emit",
        help=(
            "Dry-run: surface candidates without emitting "
            "WELL_KNOWN_CANDIDATE events to the EventLog."
        ),
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Exit non-zero (code 1) when any new candidate is surfaced. "
            "Useful for CI gates that want to flag potential schema growth."
        ),
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Surface open-string types eligible for canonical promotion.

    Reads the GraphStore for current ``node_type`` / ``edge_type``
    values, joins them against the EventLog's ``MUTATION_EXECUTED``
    history, and reports values that crossed the operator-tunable
    promotion thresholds (count, distinct extractors, distinct domains,
    signal quality, evidence window). Surfaced candidates are emitted
    as ``WELL_KNOWN_CANDIDATE`` events unless ``--no-emit`` is set.

    The loop is **surface-only** — it never auto-mutates
    :mod:`trellis.schemas.well_known`. The promotion path is a
    human-authored ADR amendment; use ``trellis admin
    draft-promotion-adr <candidate_id>`` to scaffold one.

    See ``docs/design/adr-well-known-promotion-loop.md``.
    """
    parsed_kinds = tuple(k.strip() for k in kinds.split(",") if k.strip())
    valid_kinds = {"entity_type", "edge_kind"}
    invalid = [k for k in parsed_kinds if k not in valid_kinds]
    if invalid:
        msg = (
            f"--kinds: invalid value(s) {invalid!r}; choose from {sorted(valid_kinds)}"
        )
        raise typer.BadParameter(msg)

    graph_store = get_graph_store()
    event_log = get_event_log()
    registry = _build_schema_evolution_registry()

    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.schema-evolution",
        disabled=no_meta_trace,
    ) as _meta_record:
        candidates = analyze_well_known_candidates(
            graph_store=graph_store,
            event_log=event_log,
            registry=registry,
            candidate_kinds=parsed_kinds,  # type: ignore[arg-type]
            emit_events=not no_emit,
        )
        if _meta_record.enabled:
            for cand in candidates:
                _meta_record.produced_finding(
                    cand.candidate_id,
                    finding_type="WellKnownCandidate",
                )

    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "candidate_count": len(candidates),
                    "emitted": (not no_emit) and len(candidates) > 0,
                    "candidates": [c.to_event_payload() for c in candidates],
                }
            )
        )
    else:
        mode = "DRY-RUN" if no_emit else "EMIT"
        console.print(
            f"[bold]Schema-evolution candidates[/bold]  ({mode})  "
            f"{len(candidates)} surfaced"
        )
        if not candidates:
            console.print(
                "[dim]No open-string types crossed the promotion thresholds. "
                "Adjust thresholds via the ParameterRegistry "
                f"('{SCHEMA_EVOLUTION_PARAM_COMPONENT_ID}' component) if this is "
                "unexpected.[/dim]"
            )
        else:
            table = Table(title="Promotion Candidates")
            table.add_column("Kind", style="cyan")
            table.add_column("Open string", style="bold")
            table.add_column("Count", justify="right")
            table.add_column("Extractors", justify="right")
            table.add_column("Domains", justify="right")
            table.add_column("Suggested", style="green")
            table.add_column("candidate_id", style="dim")
            for c in candidates:
                suggested = c.suggested_canonical_name
                if c.naming_collision:
                    suggested = f"[yellow]{suggested}[/yellow]"
                table.add_row(
                    c.candidate_kind,
                    c.open_string_value,
                    str(c.count),
                    str(len(c.distinct_extractors)),
                    str(len(c.distinct_domains)),
                    suggested,
                    escape(c.candidate_id),
                )
            console.print(table)
            for c in candidates:
                if c.notes:
                    console.print(
                        f"[dim]{escape(c.candidate_id)}: {'; '.join(c.notes)}[/dim]"
                    )

    if strict and candidates:
        raise typer.Exit(code=EXIT_INTERNAL)
