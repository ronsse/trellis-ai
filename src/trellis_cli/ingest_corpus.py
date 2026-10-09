"""``trellis ingest corpus`` — sync a directory of files into memory.

Thin CLI shell over :func:`trellis.ingest_corpus.sync_corpus`; all sync
behaviour (idempotent re-put, chunking, move detection, prune) lives in
the shared routine so future entry points behave identically. See
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
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.ingest_corpus import CorpusSyncReport

console = build_console()

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
                console.print(
                    f"[red]Invalid --tag {escape(repr(raw))}: expected k=v[/red]",
                    soft_wrap=True,
                )
            raise typer.Exit(code=EXIT_VALIDATION)
        metadata[key.strip()] = value.strip()
    if domain:
        metadata["domain"] = domain
    return metadata


def _sync_outcome(report: CorpusSyncReport) -> tuple[str, int]:
    """``(status, exit_code)`` for a finished sync, dry run or not.

    A prune that kept documents it could not check did not finish, so the
    run is ``partial`` and exits 5 — a dry run included, since its plan is
    just as incomplete (#633).
    """
    if report.prune_withheld:
        return "partial", EXIT_STORE
    return ("planned" if report.dry_run else "synced"), 0


def _render_report(report: CorpusSyncReport) -> None:
    """The text arm of ``ingest corpus``."""
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
        pruned_name = entry.get("source_path") or entry["doc_id"]
        console.print(f"  [red]prune [/red] {escape(pruned_name)}", soft_wrap=True)
    for entry in report.prune_withheld:
        withheld_name = entry.get("source_path") or entry["doc_id"]
        console.print(
            f"  [yellow]withheld[/yellow] {escape(withheld_name)}: "
            f"{escape(str(entry['detail']))}",
            soft_wrap=True,
        )
    console.print(
        f"  new={counts['ingested']} updated={counts['updated']} "
        f"moved={counts['moved']} unchanged={counts['skipped_unchanged']} "
        f"pruned={counts['pruned']} withheld={counts['prune_withheld']} "
        f"chunks={counts['chunks_written']} "
        f"unsupported={counts['skipped_unsupported']}"
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
            f"{escape(detail)}",
            soft_wrap=True,
        )


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
        False,
        "--prune",
        help="Delete documents whose source file is gone "
        "(ones it cannot check are kept, and the run exits 5)",
    ),
    extract: bool = typer.Option(
        False,
        "--extract",
        help="Mine entities/edges from prose into the graph "
        "(also needs TRELLIS_ENABLE_MEMORY_EXTRACTION; LLM cost)",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Sync a corpus directory into the document store, idempotently.

    Input contract: normalized text. Files ending .md or .markdown are
    ingested; other files are reported as unsupported and skipped. Some
    paths are skipped with no mention in that report: paths --include
    does not match, and dot-files, dot-directories and symlinked
    directories below the root (symlinks to directories are never
    followed). A directory that cannot be read, the root included, is
    skipped and reported as an unreadable_directory warning. Convert
    other formats (PDF, audio, per-tool exports) first. A Claude
    conversation export (JSON) goes through `trellis ingest conversations`.

    --prune deletes a document only when its source file is verifiably
    gone. One it cannot check is kept and listed as withheld, and the run
    exits 5 with status "partial".
    """
    root = Path(path)
    # ``file_identity``, not ``path_is_present``: no legible failure sits
    # downstream of a root whose ``stat`` fails.
    identity = file_identity(root)
    if identity is None or isinstance(identity, UnknownFileIdentity):
        reason = "not found" if identity is None else f"unreadable ({identity.detail})"
        if output_format == "json":
            typer.echo(
                json.dumps({"status": "error", "message": f"path {reason}: {path}"})
            )
        else:
            console.print(
                f"[red]Path {escape(reason)}: {escape(path)}[/red]", soft_wrap=True
            )
        raise typer.Exit(code=EXIT_VALIDATION)

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
            extract=extract,
            requested_by="cli:ingest-corpus",
        )
    except Exception as exc:
        if output_format == "json":
            typer.echo(json.dumps(sanitized_error_payload(exc)))
        else:
            console.print(
                f"[red]Corpus ingest failed: {escape(str(exc))}[/red]", soft_wrap=True
            )
        raise typer.Exit(code=EXIT_INTERNAL) from None

    status, exit_code = _sync_outcome(report)
    if output_format == "json":
        typer.echo(json.dumps({"status": status, **report.to_payload()}))
    else:
        _render_report(report)
    if exit_code:
        raise typer.Exit(code=exit_code)
