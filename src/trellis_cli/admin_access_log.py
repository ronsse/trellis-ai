"""``trellis admin access-log``: per-key last-used time and read counts.

Reads the ``access.key_read`` events (:mod:`trellis.core.access_log`) over a
window and prints one row per key: reads, items returned, surfaces and the
latest read. The key store is read only for each key's name and revoked flag.

Exit codes follow :mod:`trellis_cli.exit_codes`: :data:`EXIT_OK` (0) on a
read, :data:`EXIT_VALIDATION` (2) for a window out of range, :data:`EXIT_STORE`
(5) on a backend failure. The exit never depends on ``--format``.
"""

from __future__ import annotations

import json

import structlog
import typer
from rich.markup import escape
from rich.table import Table

from trellis.core.access_log import summarize_key_reads
from trellis.core.error_sanitize import sanitize_error_message
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console
from trellis_cli.stores import get_api_key_store, get_event_log

logger = structlog.get_logger(__name__)
console = build_console()

MAX_WINDOW_HOURS = 24.0 * 90


def _render_text(payload: dict[str, object], hours: float) -> None:
    """The human table, in its own function so no caught exception text is in scope."""
    keys = payload["keys"]
    assert isinstance(keys, list)
    table = Table(title=f"Key reads, last {hours:g}h ({len(keys)} keys)")
    for column in ("key_id", "name", "reads", "items", "surfaces", "last_read_at"):
        table.add_column(column)
    for row in keys:
        table.add_row(
            escape(row["key_id"]),
            escape(row["name"] or "-") + (" (revoked)" if row["revoked"] else ""),
            str(row["reads"]),
            str(row["items_returned"]),
            ",".join(f"{k}:{v}" for k, v in row["by_surface"].items()),
            row["last_read_at"],
        )
    console.print(table)
    if payload["log_failures"]:
        console.print(
            f"[yellow]{payload['log_failures']} reads were not recorded "
            "(access.log_failed).[/yellow]"
        )
    if payload["truncated"]:
        console.print(
            "[yellow]Scan cap reached: reads before "
            f"{escape(str(payload['covered_since']))} are missing.[/yellow]"
        )


def access_log_command(*, hours: float, key_id: str | None, output_format: str) -> None:
    """CLI body for ``access-log``."""
    exit_code = EXIT_OK
    payload: dict[str, object]
    if not 0 < hours <= MAX_WINDOW_HOURS:
        exit_code = EXIT_VALIDATION
        payload = {
            "status": "error",
            "error": f"--hours must be above 0 and at most {MAX_WINDOW_HOURS:g}",
        }
    else:
        try:
            report = summarize_key_reads(
                get_event_log(),
                window_hours=hours,
                key_id=key_id,
                key_store=get_api_key_store(),
            )
            payload = {"status": "ok", **report.model_dump(mode="json")}
        except Exception as exc:
            logger.warning("admin.access_log_failed", error_type=type(exc).__name__)
            exit_code = EXIT_STORE
            payload = {"status": "error", "error": sanitize_error_message(str(exc))}

    if output_format == "json":
        typer.echo(json.dumps(payload))
    elif payload["status"] != "ok":
        console.print(
            f"[red]{escape(str(payload['error']))}[/red]", markup=True, soft_wrap=True
        )
    else:
        _render_text(payload, hours)
    raise typer.Exit(code=exit_code)


def register(admin_app: typer.Typer) -> None:
    """Mount ``access-log`` onto the ``admin`` Typer app."""

    @admin_app.command("access-log")
    def access_log(  # pragma: no cover — Typer wrapper only
        hours: float = typer.Option(168.0, "--hours", help="Window, in hours."),
        key_id: str | None = typer.Option(None, "--key-id", help="Only this key."),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
    ) -> None:
        """Per-key last-used time and read counts, from the access log."""
        access_log_command(hours=hours, key_id=key_id, output_format=output_format)


__all__ = ["access_log_command", "register"]
