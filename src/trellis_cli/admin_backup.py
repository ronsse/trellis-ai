"""``trellis admin backup`` — copy the SQLite stores while they are in use.

A plain ``cp *.db`` of a live store drops acknowledged writes that still sit
in its write-ahead log and passes ``integrity_check`` anyway, so this command
copies each store with SQLite's online backup API instead
(:mod:`trellis.ops.backup`), writes a manifest next to the copies, and says
what it did *not* copy. There is no restore command: the procedure is in
``docs/agent-guide/operations.md``.

Exit codes follow :mod:`trellis_cli.exit_codes`, computed the same way
regardless of ``--format`` (``tests/unit/test_format_exit_parity_rule.py``):

* :data:`EXIT_OK` (0) — every configured store was copied and checked.
* :data:`EXIT_VALIDATION` (2) — ``--output`` is not an empty directory.
* :data:`EXIT_STORE` (5) — a store failed, a store is on a backend this
  command does not copy (``incomplete``), or no store file was found.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from trellis.core.error_sanitize import sanitize_error_message
from trellis.errors import TrellisError
from trellis.ops.backup import (
    MANIFEST_NAME,
    STATUS_ERROR,
    STATUS_INCOMPLETE,
    STATUS_OK,
    backup_stores,
    prepare_target,
)
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, exit_code_for
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

console = build_console()
err_console = build_console(stderr=True)

_STATUS_MARKUP = {
    "copied": "[green]copied[/green]",
    "empty": "[green]empty[/green]",
    "not_applicable": "[dim]not applicable[/dim]",
    "absent": "[yellow]absent[/yellow]",
    "skipped": "[red]SKIPPED[/red]",
    "failed": "[red]FAILED[/red]",
}

_HEADLINE = {
    STATUS_OK: "[green]Backup complete[/green]",
    STATUS_INCOMPLETE: "[red]Backup INCOMPLETE: some data was not copied[/red]",
    STATUS_ERROR: "[red]Backup FAILED[/red]",
}


def _detail(item: dict[str, Any]) -> str:
    parts: list[str] = []
    if item.get("file"):
        parts.append(str(item["file"]))
    if item.get("size_bytes") is not None:
        parts.append(f"{item['size_bytes']} bytes")
    if item.get("integrity_check") not in (None, "ok"):
        parts.append(f"integrity_check: {item['integrity_check']}")
    if item.get("tables"):
        parts.append(", ".join(f"{t}={n}" for t, n in item["tables"].items()))
    elif item.get("rows") is not None and item.get("file"):
        parts.append(f"{item['rows']} lines")
    if item.get("reason"):
        parts.append(str(item["reason"]))
    return escape("; ".join(parts))


def _render_text(payload: dict[str, Any], output: Path) -> None:
    table = Table(title=f"Trellis backup -> {escape(str(output))}")
    table.add_column("Item", style="cyan")
    table.add_column("Backend")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for item in payload["stores"]:
        table.add_row(
            escape(item["name"]),
            escape(item.get("backend") or "?"),
            _STATUS_MARKUP.get(item["status"], escape(item["status"])),
            _detail(item),
        )
    for item in payload["files"]:
        table.add_row(
            escape(item["name"]),
            "file copy",
            _STATUS_MARKUP.get(item["status"], escape(item["status"])),
            _detail(item),
        )
    console.print(table)
    if payload["not_copied"]:
        console.print("[yellow]Under the stores directory, not copied:[/yellow]")
        for name in payload["not_copied"]:
            console.print(f"  - {escape(name)}")
    console.print(_HEADLINE.get(payload["status"], escape(payload["status"])))
    console.print(f"Manifest: {escape(str(output / MANIFEST_NAME))}")


def backup_command(
    output: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--output",
        help="Empty or new directory to write the backup into.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Back up the SQLite stores, safely, while they are in use.

    Each store is copied with SQLite's online backup API, so writes
    acknowledged to a running server are in the copy (a plain file copy
    loses those still in the write-ahead log). Also copied: the policy
    file and the feedback audit log. A manifest records, per store, the
    file, its size, row counts per table and the ``integrity_check`` of
    the copy. The stores are copied one after another, so the set is not
    one instant: stop writers if documents and events must agree.

    A store on a backend other than SQLite is reported ``skipped`` (use
    that backend's own dump tool) and the exit status is non-zero, so a
    backup that left data out never reads as success. Files under the
    stores directory that were not copied are named in the report.

    The output holds every memory and the API-key hashes: keep it private.
    """
    registry = _get_registry()
    payload: dict[str, Any]
    try:
        prepare_target(output)
        report = backup_stores(registry, output)
        payload = report.as_dict()
        exit_code = EXIT_OK if report.status == STATUS_OK else EXIT_STORE
    except TrellisError as exc:
        payload = {"status": STATUS_ERROR, "message": sanitize_error_message(str(exc))}
        exit_code = exit_code_for(exc)

    if output_format == "json":
        emit_json({**payload, "output": str(output)})
    elif "message" in payload:
        err_console.print(f"[red]{escape(payload['message'])}[/red]", soft_wrap=True)
    else:
        _render_text(payload, output)
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


def register(admin_app: typer.Typer) -> None:
    """Mount ``backup`` onto the ``admin`` Typer app."""
    admin_app.command("backup")(backup_command)
