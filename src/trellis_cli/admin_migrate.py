"""``trellis admin migrate`` — report, and explicitly apply, schema versions.

A report is the default and writes nothing: per store, the version it is at,
the version this build targets and the steps pending. ``--apply`` records the
baseline for a store that has no record and runs its pending steps. Nothing
else in Trellis creates the version record, and no store open applies a step
(:mod:`trellis.ops.schema_migrate`).

How this relates to the two older commands: ``migrate-graph`` copies graph rows
between backends and ``migrate-provenance`` backfills edge provenance in place.
Both move *data*. This versions the *schema*.

Like ``migrate-graph`` it records a meta-Activity when it applies something
(``--no-meta-trace`` skips it); no ``admin`` schema command emits an EventLog
event of its own. A report records nothing.

Exit codes follow :mod:`trellis_cli.exit_codes`, computed the same way
regardless of ``--format`` (``tests/unit/test_format_exit_parity_rule.py``):

* :data:`EXIT_OK` (0) — every store is readable and, with ``--apply``, at its
  target. A pending step in a report is not a failure.
* :data:`EXIT_STORE` (5) — a store could not be read, its shape matches no known
  version, or a step failed (the store stays at the version it had).
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from trellis.core.error_sanitize import sanitize_error_message
from trellis.errors import TrellisError
from trellis.ops.schema_migrate import STATUS_ERROR, MigrateReport, run_migrate
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, exit_code_for
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

console = build_console()
err_console = build_console(stderr=True)

_STATUS_MARKUP = {
    "current": "[green]current[/green]",
    "stamped": "[green]stamped[/green]",
    "applied": "[green]applied[/green]",
    "pending": "[yellow]pending[/yellow]",
    "absent": "[dim]absent[/dim]",
    "unsupported": "[dim]unsupported[/dim]",
    "unrecognized": "[red]UNRECOGNIZED[/red]",
    "failed": "[red]FAILED[/red]",
}


def _version_cell(item: dict[str, Any]) -> str:
    current = item.get("current_version")
    if current is None:
        return "-"
    return f"{current}{'' if item.get('recorded') else ' (from shape)'}"


def _render_text(payload: dict[str, Any]) -> None:
    mode = "apply" if payload["mode"] == "apply" else "report (nothing written)"
    table = Table(title=f"Trellis schema versions: {mode}")
    table.add_column("Store", style="cyan")
    table.add_column("Backend")
    table.add_column("Version")
    table.add_column("Target")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for item in payload["stores"]:
        detail = []
        if item.get("pending"):
            detail.append(f"pending steps: {item['pending']}")
        if item.get("applied"):
            detail.append(f"applied steps: {item['applied']}")
        if item.get("reason"):
            detail.append(str(item["reason"]))
        table.add_row(
            escape(item["store"]),
            escape(item.get("backend") or "?"),
            escape(_version_cell(item)),
            escape(str(item.get("target_version", "-"))),
            _STATUS_MARKUP.get(item["status"], escape(item["status"])),
            escape("; ".join(detail)),
        )
    console.print(table)
    if payload["status"] == STATUS_ERROR:
        console.print("[red]Migration check FAILED for at least one store[/red]")
    elif payload["mode"] == "report":
        console.print(
            "Re-run with --apply to record the baseline and run pending steps."
        )


def _run(apply: bool, no_meta_trace: bool) -> MigrateReport:
    registry = _get_registry()
    with wrap_cli_meta_analysis(
        agent_suffix="admin",
        analyzer_name="cli.admin.migrate",
        disabled=no_meta_trace,
        dry_run=not apply,
    ) as meta:
        report = run_migrate(registry, apply=apply)
        if apply and meta.enabled:
            meta.produced_finding(
                f"schema-migrate-{len(report.stores)}-stores",
                finding_type="SchemaMigrationReport",
            )
    return report


def migrate_command(
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Record the baseline version for stores with no record and run"
            " pending steps. Without it the command only reports."
        ),
    ),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="With --apply, skip recording this run as a meta-Activity.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Report each SQL store's schema version; ``--apply`` to bring it to target.

    The default is a report and writes nothing: the version each store is at
    (read from its record, or from its shape when it has none), the version
    this build targets, and the steps pending. This build ships no migration,
    so ``--apply`` only records the baseline version. Opening a store never
    creates that record or applies a step.

    Covers SQLite and Postgres (pgvector) stores; other backends are
    ``unsupported``. For copying graph rows between backends see
    ``migrate-graph``; this command versions the schema, not the data.
    """
    payload: dict[str, Any]
    try:
        report = _run(apply, no_meta_trace)
        payload = report.as_dict()
        exit_code = EXIT_OK if report.status != STATUS_ERROR else EXIT_STORE
    except TrellisError as exc:
        payload = {
            "status": STATUS_ERROR,
            "mode": "apply" if apply else "report",
            "message": sanitize_error_message(str(exc)),
        }
        exit_code = exit_code_for(exc)

    if output_format == "json":
        emit_json(payload, indent=2)
    elif "message" in payload:
        err_console.print(f"[red]{escape(payload['message'])}[/red]", soft_wrap=True)
    else:
        _render_text(payload)
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


def register(admin_app: typer.Typer) -> None:
    """Mount ``migrate`` onto the ``admin`` Typer app."""
    admin_app.command("migrate")(migrate_command)
