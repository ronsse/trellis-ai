"""Bounded backfill for normalized entity-name aliases."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING

import typer

from trellis.mutate import build_curate_executor
from trellis.mutate.name_aliases import backfill_name_aliases
from trellis_cli.exit_codes import EXIT_POLICY, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry, get_graph_store

if TYPE_CHECKING:
    from rich.console import Console
    from typer import Typer


def register(app: Typer) -> None:
    """Register the name-alias backfill command."""

    @app.command("backfill-name-aliases")
    def backfill_name_aliases_command(
        max_nodes: int | None = typer.Option(
            None,
            "--max-nodes",
            min=1,
            help=(
                "Safety bound for the current-node snapshot. Defaults to the"
                " current node count plus one."
            ),
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        """Bind unambiguous normalized names into the graph alias index.

        The snapshot is read with one extra row. If it exceeds the bound,
        no alias is written; rerun with a larger ``--max-nodes``.
        """
        if output_format not in ("text", "json"):
            typer.echo(
                f"Unsupported --format {output_format!r}; expected one of: text, json"
            )
            raise typer.Exit(code=EXIT_VALIDATION)

        graph = get_graph_store()
        effective_max = max_nodes if max_nodes is not None else graph.count_nodes() + 1
        report = backfill_name_aliases(
            graph,
            build_curate_executor(_get_registry()),
            max_nodes=effective_max,
        )
        truncated = report.truncated
        has_progress = any(
            (
                report.bound,
                report.rebound,
                report.already_bound,
                report.contested,
                report.skipped,
            )
        )
        payload = {
            "status": (
                "error"
                if truncated or (report.failed and not has_progress)
                else "partial"
                if report.failed
                else "ok"
            ),
            "max_nodes": effective_max,
            "bound": report.bound,
            "rebound": report.rebound,
            "already_bound": report.already_bound,
            "contested": report.contested,
            "skipped": report.skipped,
            "failed": report.failed,
            "failures": [asdict(failure) for failure in report.failures],
            "commands_submitted": report.commands_submitted,
            "truncated": truncated,
        }

        if output_format == "json":
            typer.echo(json.dumps(payload))
        else:
            _render_report(payload, console=build_console())

        exit_code = 0
        if truncated:
            exit_code = EXIT_VALIDATION
        elif report.failed:
            exit_code = (
                EXIT_POLICY
                if all(failure.reason == "policy" for failure in report.failures)
                else EXIT_STORE
            )
        if exit_code:
            raise typer.Exit(code=exit_code)


def _render_report(payload: Mapping[str, object], *, console: Console) -> None:
    """Render the count-only operator report."""
    if payload["truncated"]:
        console.print(
            "[red]Name-alias backfill refused: the graph exceeded"
            f" --max-nodes={payload['max_nodes']}; no aliases were bound."
            " Rerun with a complete bound.[/red]"
        )
        return
    if payload["failed"]:
        console.print(
            "[red]Name-alias backfill incomplete; inspect the failure"
            " counts and retry after restoring policy/store access.[/red]"
        )
    else:
        console.print("[green]Name-alias backfill complete.[/green]")
    console.print(f"  Bound: {payload['bound']}")
    console.print(f"  Rebound stale owners: {payload['rebound']}")
    console.print(f"  Already bound: {payload['already_bound']}")
    console.print(f"  Contested names: {payload['contested']}")
    console.print(f"  Skipped: {payload['skipped']}")
    console.print(f"  Failed: {payload['failed']}")
