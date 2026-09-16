"""Replay historical feedback into the outcome store, for the tuner."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import typer

from trellis.feedback.backfill import DEFAULT_EVENT_LIMIT, backfill_outcomes
from trellis.learning.tuners.rule_tuner import DEFAULT_WINDOW_DAYS
from trellis_cli.exit_codes import EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console
from trellis_cli.stores import get_event_log, get_outcome_store

if TYPE_CHECKING:
    from rich.console import Console
    from typer import Typer

#: Cells listed in the text report.  The full set always rides the JSON
#: payload; the text arm is an operator summary, not the data.
_TEXT_CELL_LIMIT = 10


def register(app: Typer) -> None:
    """Register the outcome backfill command."""

    @app.command("backfill-outcomes")
    def backfill_outcomes_command(
        window_days: int = typer.Option(
            DEFAULT_WINDOW_DAYS,
            "--window-days",
            min=1,
            help=(
                "Days of feedback history to replay. Defaults to the rule"
                " tuner's own trailing window."
            ),
        ),
        event_limit: int = typer.Option(
            DEFAULT_EVENT_LIMIT,
            "--event-limit",
            min=1,
            help="Ceiling on feedback events read in one pass.",
        ),
        apply_changes: bool = typer.Option(
            False,
            "--apply",
            help="Write the planned rows. Omit for a dry run.",
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        """Bridge past feedback events into the ops-tier outcome store.

        The outcome bridge only sees feedback recorded after it shipped,
        so on an existing deployment the rule tuner's trailing window
        starts empty and its first useful pass is a month away. This
        replays the history through that same bridge.

        Dry by default, and idempotent: rows a previous pass already
        wrote are counted and skipped, so an interrupted run is finished
        by repeating it.
        """
        if output_format not in ("text", "json"):
            typer.echo(
                f"Unsupported --format {output_format!r}; expected one of: text, json"
            )
            raise typer.Exit(code=EXIT_VALIDATION)

        report = backfill_outcomes(
            event_log=get_event_log(),
            outcome_store=get_outcome_store(),
            window_days=window_days,
            apply=apply_changes,
            event_limit=event_limit,
        )

        payload: dict[str, Any] = {
            "status": (
                "error"
                if report.events_truncated
                else "partial"
                if report.events_failed
                else "ok"
            ),
            "applied": report.applied,
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "window_days": window_days,
            "events_scanned": report.events_scanned,
            "events_truncated": report.events_truncated,
            "events_replayable": report.events_replayable,
            "events_not_replayable": report.events_not_replayable,
            "events_pack_targeted": report.events_pack_targeted,
            "events_failed": report.events_failed,
            "rows_planned": report.rows_planned,
            "rows_already_present": report.rows_already_present,
            "rows_pending": report.rows_pending,
            "rows_written": report.rows_written,
            "rows_by_component": report.rows_by_component,
            "existing_rows_in_window": report.existing_rows_in_window,
            "cells": [asdict(cell) for cell in report.cells],
        }

        if output_format == "json":
            typer.echo(json.dumps(payload))
        else:
            _render_report(payload, console=build_console())

        exit_code = 0
        if report.events_truncated:
            exit_code = EXIT_VALIDATION
        elif report.events_failed:
            exit_code = EXIT_STORE
        if exit_code:
            raise typer.Exit(code=exit_code)


def _render_report(payload: Mapping[str, object], *, console: Console) -> None:
    """Render the operator summary."""
    applied = bool(payload["applied"])
    if payload["events_truncated"]:
        console.print(
            "[red]Outcome backfill incomplete: the event limit"
            f" ({payload['events_scanned']}) was reached before the window"
            " ended. Rerun with a larger --event-limit or a shorter"
            " --window-days.[/red]"
        )
    elif payload["events_failed"]:
        console.print(
            f"[red]Outcome backfill bridged {payload['events_failed']} event(s)"
            " with errors; rows for those events are missing.[/red]"
        )
    elif applied:
        console.print("[green]Outcome backfill applied.[/green]")
    else:
        console.print(
            "[yellow]Outcome backfill dry run — nothing written."
            " Rerun with --apply.[/yellow]"
        )

    console.print(
        f"  Window: {payload['window_start']} → {payload['window_end']}"
        f" ({payload['window_days']}d)"
    )
    console.print(
        f"  Feedback events: {payload['events_scanned']} scanned,"
        f" {payload['events_replayable']} replayable"
        f" ({payload['events_pack_targeted']} pack-targeted)"
    )
    if payload["events_not_replayable"]:
        console.print(
            f"  Not replayable: {payload['events_not_replayable']}"
            " (governed feedback surface — no feedback_id, no item attribution)"
        )
    console.print(
        f"  Outcome rows: {payload['rows_planned']} planned,"
        f" {payload['rows_already_present']} already present,"
        f" {payload['rows_pending']} pending,"
        f" {payload['rows_written']} written"
    )
    by_component = payload["rows_by_component"]
    if isinstance(by_component, Mapping) and by_component:
        console.print("  Pending rows by component:")
        for component_id, count in by_component.items():
            # markup=False, not escape(): a component id is a dotted name
            # today, but this line carries no styling of its own, so the
            # wholesale switch is the one that cannot be half-applied.
            console.print(f"    {component_id}: {count}", markup=False)
    _render_cells(payload, console=console)


def _render_cells(payload: Mapping[str, object], *, console: Console) -> None:
    """Render the learning-axis cells the tuner would then see."""
    cells = payload["cells"]
    if not isinstance(cells, Sequence) or not cells:
        console.print("  Tuner cells after this pass: none")
        return
    console.print(
        f"  Tuner cells after this pass: {len(cells)}"
        f" (over {payload['existing_rows_in_window']} existing"
        f" + {payload['rows_pending']} pending rows)"
    )
    for cell in cells[:_TEXT_CELL_LIMIT]:
        if not isinstance(cell, Mapping):
            continue
        reference_rate = cell["reference_rate"]
        rendered_rate = (
            "n/a" if reference_rate is None else f"{float(reference_rate):.4f}"
        )
        scope = "/".join(
            str(cell[axis] or "*") for axis in ("domain", "intent_family", "tool_name")
        )
        # The brackets around the scope are meant literally, and Rich reads
        # them as a style tag: with markup on, `[trellis-ai/plan/*]` renders
        # as the empty string and the operator is shown a cell with no axes
        # (#492). An all-wildcard scope survives, which is why a fixture
        # whose axes are all None cannot see this.
        console.print(
            f"    {cell['component_id']} [{scope}]:"
            f" n={cell['count']},"
            f" served={cell['items_served']},"
            f" referenced={cell['items_referenced']},"
            f" reference_rate={rendered_rate}",
            markup=False,
        )
    if len(cells) > _TEXT_CELL_LIMIT:
        console.print(
            f"    … {len(cells) - _TEXT_CELL_LIMIT} more"
            " (use --format json for the full set)"
        )
