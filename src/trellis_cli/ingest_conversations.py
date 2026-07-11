"""``trellis ingest conversations`` — sync a Claude chat export into memory.

Thin CLI shell over :func:`trellis.ingest_corpus.sync_conversations`; all
sync behaviour (idempotent re-put, chunking, prune) is shared with
``trellis ingest corpus`` via the record-oriented core. See
``docs/design/adr-corpus-ingestion.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from trellis.core.error_sanitize import sanitized_error_payload
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.ingest_corpus import _parse_tags
from trellis_cli.stores import _get_registry

console = Console()

_ACTION_STYLES = {"new": "green", "update": "yellow", "move": "cyan", "skip": "dim"}


def ingest_conversations(
    path: str = typer.Argument(
        ..., help="conversations.json, the .zip export, or a directory holding it"
    ),
    source_system: str = typer.Option(
        "claude-ai",
        "--source-system",
        help="Corpus namespace — part of every doc_id",
    ),
    domain: str | None = typer.Option(
        None, "--domain", help="Domain tag applied to every written document"
    ),
    tag: list[str] = typer.Option(  # noqa: B008 - typer option factory
        [], "--tag", help="Extra metadata as k=v (repeatable)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report the full plan without writing"
    ),
    prune: bool = typer.Option(
        False, "--prune", help="Delete conversations no longer in the export"
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Sync a Claude conversation export into the document store."""
    root = Path(path)
    if not root.exists():
        if output_format == "json":
            typer.echo(
                json.dumps({"status": "error", "message": f"path not found: {path}"})
            )
        else:
            console.print(f"[red]Path not found: {path}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL)

    extra_metadata = _parse_tags(tag, domain, output_format)

    from trellis.ingest_corpus import sync_conversations  # noqa: PLC0415

    registry = _get_registry()
    try:
        report = sync_conversations(
            registry,
            root,
            source_system=source_system,
            extra_metadata=extra_metadata,
            dry_run=dry_run,
            prune=prune,
            requested_by="cli:ingest-conversations",
        )
    except Exception as exc:
        if output_format == "json":
            typer.echo(json.dumps(sanitized_error_payload(exc)))
        else:
            console.print(f"[red]Conversation ingest failed: {exc}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from None

    if output_format == "json":
        typer.echo(
            json.dumps(
                {"status": "planned" if dry_run else "synced", **report.to_payload()}
            )
        )
        return

    counts = report.counts()
    verb = "Plan for" if dry_run else "Synced"
    console.print(f"[green]{verb}[/green] {report.root} ({source_system})")
    for outcome in report.files:
        if outcome.action == "skip":
            continue
        style = _ACTION_STYLES[outcome.action]
        chunk_note = f" ({outcome.chunk_count} chunks)" if outcome.chunk_count else ""
        console.print(
            f"  [{style}]{outcome.action:6}[/{style}] {outcome.relpath}{chunk_note}"
        )
    for entry in report.pruned:
        console.print(
            f"  [red]prune [/red] {entry.get('source_path') or entry['doc_id']}"
        )
    console.print(
        f"  new={counts['ingested']} updated={counts['updated']} "
        f"unchanged={counts['skipped_unchanged']} pruned={counts['pruned']} "
        f"chunks={counts['chunks_written']}"
    )
    for warning in report.warnings:
        detail = " ".join(
            f"{k}={v}" for k, v in warning.items() if k != "kind" and v is not None
        )
        console.print(f"  [yellow]warning[/yellow] {warning['kind']}: {detail}")
