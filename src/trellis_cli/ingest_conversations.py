"""``trellis ingest conversations`` — sync a Claude chat export into memory.

Thin CLI shell over :func:`trellis.ingest_corpus.sync_conversations`; all
sync behaviour (idempotent re-put, chunking, prune) is shared with
``trellis ingest corpus`` via the record-oriented core. See
``docs/design/adr-corpus-ingestion.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.markup import escape

from trellis.core.error_sanitize import sanitized_error_payload
from trellis.core.path_presence import UnknownFileIdentity, file_identity
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_VALIDATION
from trellis_cli.ingest_corpus import _parse_tags, _sync_outcome
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.ingest_corpus import CorpusSyncReport

console = build_console()

_ACTION_STYLES = {"new": "green", "update": "yellow", "move": "cyan", "skip": "dim"}


def _render_report(report: CorpusSyncReport) -> None:
    """The text arm of ``ingest conversations``."""
    counts = report.counts()
    verb = "Plan for" if report.dry_run else "Synced"
    # The parentheses sit inside escape(): escape() doubles a lone trailing
    # backslash, and with no tag after it the extra backslash prints.
    console.print(
        f"[green]{verb}[/green] {escape(str(report.root))} "
        f"{escape('(' + report.source_system + ')')}"
    )
    for outcome in report.files:
        if outcome.action == "skip":
            continue
        style = _ACTION_STYLES[outcome.action]
        chunk_note = f" ({outcome.chunk_count} chunks)" if outcome.chunk_count else ""
        console.print(
            f"  [{style}]{outcome.action:6}[/{style}] "
            f"{escape(outcome.relpath)}{chunk_note}"
        )
    for entry in report.pruned:
        console.print(
            f"  [red]prune [/red] {escape(entry.get('source_path') or entry['doc_id'])}"
        )
    for entry in report.prune_withheld:
        withheld_name = entry.get("source_path") or entry["doc_id"]
        console.print(
            f"  [yellow]withheld[/yellow] {escape(withheld_name)}: "
            f"{escape(str(entry['detail']))}"
        )
    console.print(
        f"  new={counts['ingested']} updated={counts['updated']} "
        f"unchanged={counts['skipped_unchanged']} pruned={counts['pruned']} "
        f"withheld={counts['prune_withheld']} chunks={counts['chunks_written']}"
    )
    if counts["entities_extracted"] or counts["edges_extracted"]:
        console.print(
            f"  [magenta]extracted[/magenta] "
            f"entities={counts['entities_extracted']} "
            f"edges={counts['edges_extracted']}"
        )
    for warning in report.warnings:
        detail = " ".join(
            f"{k}={v}" for k, v in warning.items() if k != "kind" and v is not None
        )
        console.print(
            f"  [yellow]warning[/yellow] {escape(str(warning['kind']))}: "
            f"{escape(detail)}"
        )


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
        False,
        "--prune",
        help="Delete conversations no longer in the export (if the export "
        "was not read whole, ones it cannot check are kept, and the run exits 5)",
    ),
    extract: bool = typer.Option(
        False,
        "--extract",
        help="Mine entities/edges from conversation prose into the graph "
        "(also needs TRELLIS_ENABLE_MEMORY_EXTRACTION; LLM cost)",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Sync a Claude conversation export into the document store."""
    root = Path(path)
    # ``file_identity``, not ``Path.exists()``, which raises EACCES and
    # ENAMETOOLONG as a traceback. Not ``path_is_present`` either: what the
    # sync does with a root it cannot stat is not a legible failure.
    identity = file_identity(root)
    if identity is None or isinstance(identity, UnknownFileIdentity):
        reason = "not found" if identity is None else f"unreadable ({identity.detail})"
        if output_format == "json":
            typer.echo(
                json.dumps({"status": "error", "message": f"path {reason}: {path}"})
            )
        else:
            console.print(f"[red]Path {escape(reason)}: {escape(path)}[/red]")
        raise typer.Exit(code=EXIT_VALIDATION)

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
            extract=extract,
            requested_by="cli:ingest-conversations",
        )
    except Exception as exc:
        if output_format == "json":
            typer.echo(json.dumps(sanitized_error_payload(exc)))
        else:
            console.print(f"[red]Conversation ingest failed: {escape(str(exc))}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from None

    status, exit_code = _sync_outcome(report)
    if output_format == "json":
        typer.echo(json.dumps({"status": status, **report.to_payload()}))
    else:
        _render_report(report)
    if exit_code:
        raise typer.Exit(code=exit_code)
