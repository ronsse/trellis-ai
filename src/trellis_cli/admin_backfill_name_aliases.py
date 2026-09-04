"""Bounded backfill for normalized entity-name aliases."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

import typer

from trellis.extract.entity_resolution import backfill_name_aliases
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.output import build_console
from trellis_cli.stores import get_graph_store

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
        graph = get_graph_store()
        effective_max = max_nodes if max_nodes is not None else graph.count_nodes() + 1
        report = backfill_name_aliases(graph, max_nodes=effective_max)
        truncated = report.truncated
        payload = {
            "status": "error" if truncated else "ok",
            "max_nodes": effective_max,
            "bound": report.bound,
            "already_bound": report.already_bound,
            "contested": len(report.contested_keys),
            "skipped": report.skipped,
            "truncated": truncated,
        }

        if output_format == "json":
            typer.echo(json.dumps(payload))
        else:
            _render_report(payload, console=build_console())

        if truncated:
            raise typer.Exit(code=EXIT_INTERNAL)


def _render_report(payload: Mapping[str, object], *, console: Console) -> None:
    """Render the count-only operator report."""
    if payload["truncated"]:
        console.print(
            "[red]Name-alias backfill refused: the graph exceeded"
            f" --max-nodes={payload['max_nodes']}; no aliases were bound.[/red]"
        )
        return
    console.print("[green]Name-alias backfill complete.[/green]")
    console.print(f"  Bound: {payload['bound']}")
    console.print(f"  Already bound: {payload['already_bound']}")
    console.print(f"  Contested names: {payload['contested']}")
    console.print(f"  Skipped: {payload['skipped']}")
