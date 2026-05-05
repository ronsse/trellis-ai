"""Analyze commands -- context effectiveness and insights."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from trellis.extract.telemetry import analyze_extractor_fallbacks
from trellis.learning import (
    analyze_learning_observations,
    build_learning_observations_from_event_log,
    write_learning_review_artifacts,
)
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
from trellis.retrieve.pack_sections import analyze_pack_sections
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.retrieve.token_usage import analyze_token_usage
from trellis.stores.advisory_store import AdvisoryStore
from trellis_cli.stores import get_document_store, get_event_log

analyze_app = typer.Typer(no_args_is_help=True)
console = Console()

# Display thresholds for rate coloring
_RATE_GREEN = 0.7
_RATE_YELLOW = 0.4

# Extractor-fallback display thresholds (inverted — high rate = bad)
_FALLBACK_RATE_RED = 0.5
_FALLBACK_RATE_YELLOW = 0.2


@analyze_app.command("context-effectiveness")
def context_effectiveness(
    days: int = typer.Option(30, help="Days of history to analyze"),
    min_appearances: int = typer.Option(2, help="Minimum item appearances to include"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Analyze which context items correlate with task success."""
    event_log = get_event_log()
    report = analyze_effectiveness(
        event_log,
        days=days,
        min_appearances=min_appearances,
    )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
    else:
        console.print(f"[bold]Context Effectiveness Report[/bold] (last {days} days)")
        console.print(f"  Packs assembled: {report.total_packs}")
        console.print(f"  Feedback received: {report.total_feedback}")
        console.print(f"  Overall success rate: {report.success_rate:.1%}")

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
                    item["item_id"][:20],
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
                console.print(f"  - {item_id}")

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
) -> None:
    """Analyze effectiveness AND apply noise tags to low-value items.

    Runs the full feedback loop: analyze_effectiveness → apply_noise_tags.
    Items that consistently correlate with task failure get tagged with
    signal_quality="noise" so PackBuilder excludes them by default.
    """
    event_log = get_event_log()
    document_store = get_document_store()

    report = run_effectiveness_feedback(
        event_log,
        document_store,
        days=days,
        min_appearances=min_appearances,
    )

    if output_format == "json":
        print(json.dumps(report.model_dump()))
    else:
        console.print(f"[bold]Effectiveness Feedback Applied[/bold] (last {days} days)")
        console.print(f"  Packs analyzed: {report.total_packs}")
        console.print(f"  Feedback events: {report.total_feedback}")
        console.print(f"  Overall success rate: {report.success_rate:.1%}")
        if report.noise_candidates:
            console.print(
                f"  [yellow]Noise tags applied to {len(report.noise_candidates)}"
                f" item(s)[/yellow]:"
            )
            for item_id in report.noise_candidates:
                console.print(f"    - {item_id}")
        else:
            console.print("  [green]No noise candidates found.[/green]")


@analyze_app.command("token-usage")
def token_usage(
    days: int = typer.Option(7, help="Days of history to analyze"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Analyze token usage across CLI, MCP, and SDK layers."""
    event_log = get_event_log()
    report = analyze_token_usage(event_log, days=days)

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Token Usage Report[/bold] (last {days} days)")
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
) -> None:
    """Generate advisories from outcome data.

    Analyzes PACK_ASSEMBLED and FEEDBACK_RECORDED events to find patterns
    that correlate with success or failure, then stores deterministic
    advisories that can be delivered alongside future context packs.
    """
    from trellis_cli.config import get_data_dir  # noqa: PLC0415

    event_log = get_event_log()
    data_dir = get_data_dir()
    store = AdvisoryStore(data_dir / "advisories.json")

    generator = AdvisoryGenerator(
        event_log,
        store,
        min_sample_size=min_sample,
        min_effect_size=min_effect,
    )
    report = generator.generate(days=days)

    if output_format == "json":
        print(json.dumps(report.model_dump(), indent=2, default=str))
    else:
        console.print(f"[bold]Advisory Generation Report[/bold] (last {days} days)")
        console.print(f"  Packs analyzed: {report.total_packs}")
        console.print(f"  Feedback events: {report.total_feedback}")
        console.print(f"  Advisories generated: {report.advisories_generated}")
        console.print(f"  Advisories stored: {report.advisories_stored}")

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

        if report.total_feedback == 0:
            console.print()
            console.print(
                "[dim]No feedback recorded yet. Record outcomes via"
                " 'trellis curate feedback' or the MCP record_feedback"
                " tool to enable advisory generation.[/dim]"
            )


@analyze_app.command("advisory-effectiveness")
def advisory_effectiveness(
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
    data_dir = get_data_dir()
    store = AdvisoryStore(data_dir / "advisories.json")

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
                    score.advisory_id[:15],
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
                console.print(f"  + {adv_id}")

        if report.advisories_suppressed:
            console.print()
            console.print(
                f"[red]Suppressed ({len(report.advisories_suppressed)}):[/red]"
            )
            for adv_id in report.advisories_suppressed:
                console.print(f"  - {adv_id}")

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
) -> None:
    """Audit sectioned pack composition across recent assemblies.

    Reads ``PACK_ASSEMBLED`` events emitted by sectioned pack builds and
    reports per-section item counts, empty rates, and unique item counts.
    Useful for spotting sections that consistently miss their target
    content or deliver far fewer items than their budget allows.
    """
    event_log = get_event_log()
    report = analyze_pack_sections(
        event_log,
        days=days,
        empty_rate_threshold=empty_rate_threshold,
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
                },
                indent=2,
            )
        )
        return

    console.print(f"[bold]Pack Sections Report[/bold] (last {days} days)")
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
    from trellis.ops import ParameterRegistry  # noqa: PLC0415
    from trellis.retrieve.pack_builder import PackBuilder  # noqa: PLC0415
    from trellis.retrieve.rerankers import build_reranker  # noqa: PLC0415
    from trellis.retrieve.strategies import build_strategies  # noqa: PLC0415
    from trellis_cli.stores import _get_registry  # noqa: PLC0415

    registry = _get_registry()
    param_registry = ParameterRegistry(registry.operational.parameter_store)
    builder = PackBuilder(
        strategies=build_strategies(registry, parameter_registry=param_registry),
        event_log=registry.operational.event_log,
        reranker=build_reranker("rrf", parameter_registry=param_registry),
    )
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
) -> None:
    """Score packs against declared scenarios across 5 quality dimensions.

    Scenario mode only: loads ``EvaluationScenario`` fixtures, assembles
    packs via ``PackBuilder``, and scores each on completeness, relevance,
    noise, breadth, and efficiency. Event-log mode (joining to
    ``PACK_ASSEMBLED`` events) is tracked as follow-up work.
    """
    scenarios = _load_scenarios(scenarios_path)
    profile = _resolve_profile(profile_name)

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
    report = analyze_dimension_predictiveness(
        event_log,
        days=days,
        success_threshold=success_threshold,
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
) -> None:
    """Aggregate rejection / budget / strategy signals from PACK_ASSEMBLED.

    Operator surface for the telemetry that ``PackBuilder`` already emits.
    Highlights budget saturation rates, rejection-reason distribution, and
    per-strategy yield so tuning decisions (budget raise, filter audit,
    strategy retire) can be made from data rather than intuition.
    """
    event_log = get_event_log()
    report = analyze_pack_telemetry(event_log, days=days)

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Pack Telemetry Report[/bold] (last {days} days)")
    console.print(f"  Packs assembled: {report.total_packs}")
    if report.total_packs == 0:
        console.print()
        for note in report.notes:
            console.print(f"  [dim]- {note}[/dim]")
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
) -> None:
    """Summarize extractor fallback telemetry per source_hint.

    Reads ``EXTRACTOR_FALLBACK`` + ``EXTRACTION_DISPATCHED`` events emitted
    by :class:`~trellis.extract.dispatcher.ExtractionDispatcher` and reports
    overall fallback rate, reason distribution, and per-source aggregates.
    Read-only — surfaces candidates for graduation (``empty_result``
    dominates) or audit (``prefer_tier_override`` dominates).
    """
    event_log = get_event_log()
    report = analyze_extractor_fallbacks(event_log, days=days)

    if output_format == "json":
        print(json.dumps(report.model_dump()))
        return

    console.print(f"[bold]Extractor Fallback Report[/bold] (last {days} days)")
    console.print(f"  Total dispatches: {report.total_dispatches}")
    console.print(f"  Total fallbacks: {report.total_fallbacks}")
    console.print(f"  Overall fallback rate: {report.overall_fallback_rate:.1%}")

    if report.total_dispatches == 0:
        console.print()
        for note in report.notes:
            console.print(f"  [dim]- {note}[/dim]")
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
    observations = build_learning_observations_from_event_log(event_log, days=days)
    report = analyze_learning_observations(
        observations=observations,
        min_support=min_support,
        artifacts_root=output_dir,
    )
    paths = write_learning_review_artifacts(report=report, output_dir=output_dir)

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
    console.print(f"  Candidates JSON: [cyan]{paths['candidates_path']}[/cyan]")
    console.print(
        f"  Decisions template: [cyan]{paths['decisions_template_path']}[/cyan]"
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
            candidate["candidate_id"],
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
