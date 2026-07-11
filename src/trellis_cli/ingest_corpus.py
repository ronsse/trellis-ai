"""``trellis ingest corpus`` — sync a directory of files into memory.

Thin CLI shell over :func:`trellis.ingest_corpus.sync_corpus`; all sync
behaviour (idempotent re-put, chunking, move detection, prune) lives in
the shared routine so future entry points behave identically. See
``docs/design/adr-corpus-ingestion.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from trellis.core.error_sanitize import sanitized_error_payload
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_VALIDATION
from trellis_cli.stores import _get_registry

console = Console()

_ACTION_STYLES = {
    "new": "green",
    "update": "yellow",
    "move": "cyan",
    "skip": "dim",
}


def _parse_tags(
    tags: list[str], domain: str | None, output_format: str
) -> dict[str, str]:
    """``--tag k=v`` pairs (+ ``--domain``) into an operator metadata dict."""
    metadata: dict[str, str] = {}
    for raw in tags:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            if output_format == "json":
                typer.echo(
                    json.dumps(
                        {
                            "status": "error",
                            "message": f"invalid --tag {raw!r}: expected k=v",
                        }
                    )
                )
            else:
                console.print(f"[red]Invalid --tag {raw!r}: expected k=v[/red]")
            raise typer.Exit(code=EXIT_VALIDATION)
        metadata[key.strip()] = value.strip()
    if domain:
        metadata["domain"] = domain
    return metadata


def ingest_corpus(
    path: str = typer.Argument(..., help="Directory (or single file) to ingest"),
    source_system: str = typer.Option(
        "corpus",
        "--source-system",
        help="Corpus namespace — part of every doc_id (e.g. 'obsidian')",
    ),
    domain: str | None = typer.Option(
        None, "--domain", help="Domain tag applied to every written document"
    ),
    tag: list[str] = typer.Option(  # noqa: B008 - typer option factory
        [], "--tag", help="Extra metadata as k=v (repeatable)"
    ),
    include: list[str] = typer.Option(  # noqa: B008 - typer option factory
        [], "--include", help="Glob filter over relative paths (repeatable)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report the full plan without writing"
    ),
    prune: bool = typer.Option(
        False, "--prune", help="Delete documents whose source file vanished"
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Sync a corpus directory into the document store, idempotently."""
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

    from trellis.ingest_corpus import sync_corpus  # noqa: PLC0415

    registry = _get_registry()
    try:
        report = sync_corpus(
            registry,
            root,
            source_system=source_system,
            extra_metadata=extra_metadata,
            include=tuple(include),
            dry_run=dry_run,
            prune=prune,
            requested_by="cli:ingest-corpus",
        )
    except Exception as exc:
        if output_format == "json":
            typer.echo(json.dumps(sanitized_error_payload(exc)))
        else:
            console.print(f"[red]Corpus ingest failed: {exc}[/red]")
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
        pruned_name = entry.get("source_path") or entry["doc_id"]
        console.print(f"  [red]prune [/red] {pruned_name}")
    console.print(
        f"  new={counts['ingested']} updated={counts['updated']} "
        f"moved={counts['moved']} unchanged={counts['skipped_unchanged']} "
        f"pruned={counts['pruned']} chunks={counts['chunks_written']} "
        f"unsupported={counts['skipped_unsupported']}"
    )
    for warning in report.warnings:
        detail = " ".join(
            f"{k}={v}" for k, v in warning.items() if k != "kind" and v is not None
        )
        console.print(f"  [yellow]warning[/yellow] {warning['kind']}: {detail}")
