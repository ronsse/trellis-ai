"""``trellis analyze pack-scenario``: a served, graded pack as an eval scenario.

Lives beside ``analyze.py`` and is mounted by one call to :func:`register` at its
foot, as ``session-repeats`` is. It reads the event log and writes only the
``--out`` file (stdout when omitted) plus the meta-Activity every analyze
command records, which ``--no-meta-trace`` skips. No serving change
(:mod:`trellis.analyze.pack_scenario`).

The output is a ``scenarios:`` YAML document that ``trellis analyze
pack-quality --scenarios`` loads. Any refusal (unknown, held-out, no-intent or
sectioned pack) writes nothing and exits ``EXIT_VALIDATION`` on both formats,
as does an ``--out`` that already exists without ``--force``.

The scenario intent is the request's own text: text output prints it with
``markup=False``, and every id or path goes through :func:`rich.markup.escape`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml
from rich.markup import escape

from trellis.analyze.pack_scenario import PackScenarioExport, export_pack_scenarios
from trellis.core.path_presence import path_is_present
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import get_event_log

console = build_console()
err_console = build_console(stderr=True)

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_OUT_EXISTS = "out_exists"


def render_scenarios_yaml(export: PackScenarioExport) -> str:
    """The ``scenarios:`` YAML document ``pack-quality --scenarios`` loads."""
    document = {"scenarios": [s.model_dump(mode="json") for s in export.scenarios]}
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _render_summary(export: PackScenarioExport, out: Path | None, wrote: bool) -> None:
    target = err_console if out is None else console
    lim = export.limits
    for refusal in export.refusals:
        target.print(
            f"[red]refused[/red] {escape(refusal.pack_id)}: "
            f"{escape(refusal.reason)} ({escape(refusal.message)})",
            soft_wrap=True,
        )
    for scenario in export.scenarios:
        target.print(
            f"  {escape(scenario.name)}: "
            f"{escape(str(scenario.metadata.get('verdict')))}, "
            f"{len(scenario.expected_categories)} expected categor(ies), "
            f"{len(scenario.expected_shapes or {})} expected shape(s); intent:"
        )
        target.print(f"    {scenario.intent}", markup=False, highlight=False)
    if wrote and out is not None:
        target.print(
            f"[green]Wrote {lim.exported} scenario(s)[/green] to {escape(str(out))}",
            soft_wrap=True,
        )
    elif export.refusals:
        target.print(
            "[yellow]Nothing written: every requested pack must export.[/yellow]"
        )
    target.print(
        f"  [dim]{lim.requested} requested, {lim.exported} exported, "
        f"{len(export.refusals)} refused, "
        f"{escape(str(lim.duplicate_pack_ids))} repeated id(s); "
        f"{lim.ungraded_exported} ungraded; {lim.feedback_rows_matched} feedback "
        f"row(s) matched, {lim.stray_citations} stray citation(s), "
        f"{lim.items_named_both_ways} item(s) named both ways, "
        f"{lim.helpful_items_without_category} helpful item(s) with no "
        "category.[/dim]"
    )
    if export.scan.truncated:
        target.print(f"  [yellow]window[/yellow] {escape(export.scan.note or '')}")
    for statement in lim.statements:
        target.print(f"  [dim]note: {escape(statement)}[/dim]")


def pack_scenario_command(
    pack_ids: list[str] = typer.Option(  # noqa: B008 - typer option default
        ..., "--pack-id", help="Pack to export; repeat for several."
    ),
    out: Path | None = typer.Option(  # noqa: B008 - typer option default
        None, "--out", help="YAML file to write; stdout when omitted."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing --out."),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--limit",
        help="Max feedback events to scan; truncation is reported.",
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Export served, graded packs as pack-quality evaluation scenarios.

    Reads the PACK_ASSEMBLED event and the FEEDBACK_RECORDED events naming each
    pack (both feedback families, latest verdict decides). Expectations come
    only from served items a grader named helpful; required_coverage stays
    empty. Held-out, sectioned and intent-less packs are refused. Read-only.
    """
    out_exists = out is not None and not force and path_is_present(out)
    export = PackScenarioExport()
    wrote = False
    if not out_exists:
        with wrap_cli_meta_analysis(
            agent_suffix="analyze",
            analyzer_name="cli.analyze.pack_scenario",
            disabled=no_meta_trace,
        ) as _meta_record:
            export = export_pack_scenarios(get_event_log(), pack_ids, limit=limit)
            if _meta_record.enabled and export.scenarios:
                _meta_record.produced_finding(
                    f"pack-scenarios-{len(export.scenarios)}",
                    finding_type="PackScenarioExport",
                )
    refused = bool(export.refusals)
    yaml_text = render_scenarios_yaml(export)
    if out is not None and not out_exists and not refused:
        with out.open("w" if force else "x", encoding="utf-8") as handle:
            handle.write(yaml_text)
        wrote = True

    if out_exists:
        status = STATUS_OUT_EXISTS
    elif refused:
        status = STATUS_REFUSED
    else:
        status = STATUS_OK
    exit_code = EXIT_OK if status == STATUS_OK else EXIT_VALIDATION

    if output_format == "json":
        payload: dict[str, Any] = {
            "status": status,
            "out": str(out) if out is not None else None,
            "written": wrote,
            **export.model_dump(mode="json"),
        }
        emit_json(payload)
    elif out_exists:
        err_console.print(
            f"[red]{escape(str(out))} exists[/red]; pass --force to overwrite it.",
            soft_wrap=True,
        )
    else:
        if out is None and not refused:
            typer.echo(yaml_text, nl=False)
        _render_summary(export, out, wrote)
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


def register(analyze_app: typer.Typer) -> None:
    """Mount ``pack-scenario`` onto the ``analyze`` Typer app."""
    analyze_app.command("pack-scenario")(pack_scenario_command)


__all__ = ["pack_scenario_command", "register", "render_scenarios_yaml"]
