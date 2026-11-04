"""``trellis analyze intent-families``: relabel served packs under new families.

Lives beside ``analyze.py`` and is mounted by one call to :func:`register` at its
foot, as ``pack-judge`` is. It is a read of the event log and the config file and
writes nothing: no meta-trace, no event. It is a measurement. Serving and learning
still key on ``normalize_intent_family``, and nothing reads this report
(:mod:`trellis.learning.intent_families`).

The exit code does not depend on ``--format``: a truncated scan exits
``EXIT_STORE`` and a refused ``--since`` / ``--until`` exits ``EXIT_VALIDATION``,
each decided below the format branch. A malformed ``retrieval.intent_families``
block raises :class:`~trellis.errors.ConfigError` for the CLI boundary to render
on whichever surface was asked for.
"""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from trellis.learning.intent_families import (
    STATUS_TRUNCATED,
    IntentFamilyError,
    IntentFamilyReport,
    load_rule_set,
    parse_instant,
    render_markdown,
    summarize_intent_families,
)
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT
from trellis_cli.exit_codes import EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console, emit_json, emit_machine_text
from trellis_cli.stores import _get_registry

console = build_console()


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _render_text(report: IntentFamilyReport) -> None:
    console.print(
        f"[bold]Intent families[/bold] ({escape(report.status)}): "
        f"{report.packs_analyzed} of {report.packs_served} served packs carry item "
        f"rows; rule set {escape(report.rule_set.source)} "
        f"({report.rule_set.rules} rules). Measurement only, serving and learning "
        "still use the legacy key.",
        soft_wrap=True,
    )
    if report.empty_reason:
        console.print(f"  empty: {escape(report.empty_reason)}")
    console.print(
        f"  {report.fallback_packs} packs ({_pct(report.fallback_share)}) fall to "
        f"{escape(report.rule_set.fallback)}; {report.packs_without_intent} carry no "
        f"intent; {report.legacy_stamp_differs} stamped differently from the legacy "
        "family recomputed here"
    )
    table = Table(box=None, pad_edge=False, padding=(0, 1), header_style="bold")
    for name in ("family", "packs", "graded", "helpful", "unhelpful"):
        table.add_column(name, justify="left" if name == "family" else "right")
    for name in ("rate", "95% CI", "rating", "top rules"):
        table.add_column(name, justify="left" if name == "top rules" else "right")
    for row in report.families:
        interval = (
            "-"
            if row.helpful_rate_low is None or row.helpful_rate_high is None
            else f"{row.helpful_rate_low:.0%}-{row.helpful_rate_high:.0%}"
        )
        table.add_row(
            escape(row.family),
            str(row.packs),
            str(row.graded_packs),
            str(row.helpful_citations),
            str(row.unhelpful_citations),
            _pct(row.helpful_rate),
            interval,
            "-" if row.mean_rating is None else f"{row.mean_rating:.2f}",
            escape(", ".join(f"{r.rule_id}({r.packs})" for r in row.top_rules) or "-"),
        )
    console.print(table)
    for row in report.families:
        if row.packs and row.withheld_reason:
            console.print(
                f"  rates withheld, {escape(row.family)}: "
                f"{escape(row.withheld_reason)}",
                soft_wrap=True,
            )
    console.print("[bold]Legacy by new[/bold] (packs)")
    for cell in report.crosstab:
        console.print(
            f"  {escape(cell.legacy)} -> {escape(cell.new)}: {cell.packs}",
            soft_wrap=True,
        )
    if report.scan.truncated:
        console.print(
            f"  [red]truncated[/red] {escape(report.scan.note)}", soft_wrap=True
        )


def intent_families_command(
    days: int = typer.Option(
        30, "--days", min=1, help="Window length when --since is not given."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Window start, an ISO-8601 date or time (UTC if naive)."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Window end, an ISO-8601 date or time (UTC if naive)."
    ),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        min=1,
        help="Max events to scan per event type; the newest are kept.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text, json or markdown."
    ),
) -> None:
    """Relabel served packs under a software-work family set and report helpfulness.

    A measurement over history: for each served pack it computes the legacy
    family (``normalize_intent_family``) and the new one (the shipped rule set,
    or ``retrieval.intent_families`` in config.yaml), then reports packs, graded
    packs, helpful and unhelpful citations, the helpful rate with a Wilson 95%
    interval, the mean pack rating, the share in the fallback family, a
    legacy-by-new cross-tab and the top matched rule ids. A family below 20
    graded packs has its rates withheld with the reason stated.

    Nothing here changes serving or learning, both of which still use the legacy
    key. Held-out packs are dropped. No intent text is printed: families, rule
    ids and counts only.

    Read-only. Exits ``EXIT_VALIDATION`` for an unparseable ``--since`` or
    ``--until`` and ``EXIT_STORE`` when the scan hit its cap, on every
    ``--format``. No data is exit 0 with ``empty_reason`` stated. A malformed
    ``retrieval.intent_families`` block is a ``ConfigError``, never the
    defaults.
    """
    try:
        start = parse_instant(since, option="--since")
        end = parse_instant(until, option="--until")
    except IntentFamilyError as exc:
        if output_format == "json":
            emit_json({"status": "error", "message": str(exc)})
        else:
            console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    registry = _get_registry()
    rule_set = load_rule_set(registry.retrieval_config)
    report = summarize_intent_families(
        registry.operational.event_log,
        rule_set=rule_set,
        days=days,
        since=start,
        until=end,
        limit=limit,
    )

    if output_format == "json":
        emit_json(report.model_dump(mode="json"))
    elif output_format == "markdown":
        emit_machine_text(render_markdown(report))
    else:
        _render_text(report)

    # Below the format branch, deliberately (#437): one fact, one exit.
    if report.status == STATUS_TRUNCATED:
        raise typer.Exit(EXIT_STORE)


def register(analyze_app: typer.Typer) -> None:
    """Mount ``intent-families`` onto the ``analyze`` Typer app."""
    analyze_app.command("intent-families")(intent_families_command)


__all__ = ["intent_families_command", "register"]
