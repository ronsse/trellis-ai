"""Analyze commands -- context effectiveness and insights."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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
    evaluate_pack,
)
from trellis.retrieve.pack_sections import analyze_pack_sections
from trellis.retrieve.token_usage import analyze_token_usage
from trellis.stores.advisory_store import AdvisoryStore
from trellis_cli.stores import get_document_store, get_event_log

analyze_app = typer.Typer(no_args_is_help=True)
console = Console()

# Display thresholds for rate coloring
_RATE_GREEN = 0.7
_RATE_YELLOW = 0.4


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
        console.print(json.dumps(report.model_dump()))
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
        console.print(json.dumps(report.model_dump()))
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
        console.print(json.dumps(report.model_dump()))
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
        console.print(
            json.dumps(report.model_dump(), indent=2, default=str),
            highlight=False,
        )
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
        console.print(
            json.dumps(report.model_dump(), indent=2, default=str),
            highlight=False,
        )
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
        console.print(
            json.dumps(
                {
                    "total_sectioned_packs": report.total_sectioned_packs,
                    "section_stats": rows,
                    "empty_section_flags": report.empty_section_flags,
                },
                indent=2,
            ),
            highlight=False,
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
            console.print(
                json.dumps(
                    {"scenarios": [s.model_dump() for s in scenarios]},
                    default=str,
                )
            )
        else:
            console.print(
                f"[green]Parsed {len(scenarios)} scenario(s).[/green]"
            )
            for s in scenarios:
                console.print(f"  - {s.name}: {s.intent[:60]}")
        return

    reports: list[QualityReport] = []
    for scenario in scenarios:
        pack = _assemble_pack_for_scenario(scenario)
        report = evaluate_pack(pack, scenario, profile=profile)  # type: ignore[arg-type]
        reports.append(report)

    if output_format == "json":
        console.print(
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
            preview = ", ".join(
                report.missing_coverage[:_MISSING_COVERAGE_PREVIEW]
            )
            more = (
                ""
                if len(report.missing_coverage) <= _MISSING_COVERAGE_PREVIEW
                else (
                    f" (+{len(report.missing_coverage) - _MISSING_COVERAGE_PREVIEW} more)"
                )
            )
            console.print(f"  [yellow]missing coverage:[/yellow] {preview}{more}")
        for finding in report.findings:
            console.print(f"  [dim]- {finding}[/dim]")
