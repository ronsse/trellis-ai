"""Retrieve commands — search and fetch from the experience graph."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.console import Console

from trellis.retrieve.file_context import build_file_context
from trellis.retrieve.precedents import list_precedents as _list_precedents
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_VALIDATION
from trellis_cli.output import (
    emit_json,
    emit_machine_text,
    format_output,
    truncate_values,
)
from trellis_cli.stores import (
    LOCAL_SOURCE_SYSTEM,
    get_document_store,
    get_event_log,
    get_graph_store,
    get_trace_store,
)

retrieve_app = typer.Typer(no_args_is_help=True)
console = Console()

_FMT_HELP = "Output format: text, json, jsonl, tsv"
_FIELDS_HELP = "Comma-separated fields to include"
_TRUNC_HELP = "Max characters for text fields"
_QUIET_HELP = "Suppress Rich formatting"
#: Both commands below hand back whole document rows, so they exclude
#: ``<parent>#chunk-N`` fragments by default — see
#: :data:`trellis.ingest_corpus.models.CHUNK_ID_SEPARATOR` for the rule.
#: The exclusion is pushed into the store rather than applied to the printed
#: list, so the row cap (``--limit`` / ``--max-items``) refills with the
#: documents the fragments were sliced from instead of the list simply
#: getting shorter. A short list therefore still means "that is all there
#: is", which is exactly what a post-hoc filter would have destroyed.
_CHUNKS_HELP = (
    "Include <parent>#chunk-N fragment rows. Excluded by default: they are"
    " slices of documents the same search already ranks."
)


def _doc_preview(doc: dict[str, Any], width: int) -> str:
    """One-line preview from a document search result — whitespace collapsed."""
    text = doc.get("snippet") or doc.get("content") or ""
    return " ".join(text.split())[:width]


@retrieve_app.command()
def pack(
    intent: str = typer.Option(..., help="Intent for pack assembly"),
    domain: str = typer.Option(None, help="Domain scope"),
    agent: str = typer.Option(None, "--agent", help="Agent ID scope"),
    max_items: int = typer.Option(50, help="Maximum items in pack"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    include_chunks: bool = typer.Option(False, "--include-chunks", help=_CHUNKS_HELP),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """Assemble a retrieval pack for a given intent."""
    # ``pack`` is treated as a row surface (#396) on the strength of what it
    # does, not what it is called: despite the name, and despite being
    # documented as "assemble a retrieval pack", it reaches past
    # ``PackBuilder`` straight to ``DocumentStore.search`` and prints doc ids
    # to a human. (Not a #262 regression — that ADR's "one retrieval path"
    # covers the MCP macro tools and never included this command.) Under the
    # rule that makes it a whole-row surface, and the operator previewing a
    # 56%-chunk corpus should not be shown fragments.
    #
    # This is the one classification in #396 that a later change can
    # invalidate rather than extend: routing this through ``PackBuilder``
    # (#410) would make it a *pack* surface, where chunks are the
    # retrievable unit — and ``--include-chunks`` should then be removed,
    # not inverted.
    store = get_document_store()
    filters = {}
    if domain:
        filters["domain"] = domain
    results = store.search(
        query=intent, limit=max_items, filters=filters, include_chunks=include_chunks
    )

    if output_format == "json":
        payload = json.dumps(
            {
                "status": "ok",
                "intent": intent,
                "domain": domain,
                "agent_id": agent,
                "count": len(results),
                # Echoed for the same reason the REST route echoes it: a
                # machine consumer of ``--format json`` must be able to tell
                # which of the two result sets it is holding.
                "include_chunks": include_chunks,
                "items": [r["doc_id"] for r in results],
            }
        )
        emit_machine_text(payload)
    elif quiet:
        for r in results:
            sys.stdout.write(r["doc_id"] + "\n")
    else:
        console.print(f"[green]Pack assembled[/green] ({len(results)} items)")
        console.print(f"  Intent: {intent}")
        if domain:
            console.print(f"  Domain: {domain}")
        if agent:
            console.print(f"  Agent: {agent}")
        for r in results:
            preview = _doc_preview(r, 80)
            console.print(f"  - {r['doc_id']}: {preview}")


@retrieve_app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, help="Maximum results"),
    domain: str = typer.Option(None, help="Domain scope"),
    output_format: str = typer.Option("text", "--format", help=_FMT_HELP),
    fields: str = typer.Option(None, "--fields", help=_FIELDS_HELP),
    truncate: int = typer.Option(None, "--truncate", help=_TRUNC_HELP),
    include_chunks: bool = typer.Option(False, "--include-chunks", help=_CHUNKS_HELP),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """Search the experience graph."""
    store = get_document_store()
    filters = {}
    if domain:
        filters["domain"] = domain
    results = store.search(
        query=query, limit=limit, filters=filters, include_chunks=include_chunks
    )

    if output_format in ("json", "jsonl", "tsv"):
        if output_format == "json" and not fields:
            # Preserve backward-compatible JSON structure
            out_items = truncate_values(results, truncate)
            payload = json.dumps(
                {
                    "status": "ok",
                    "query": query,
                    "count": len(out_items),
                    "include_chunks": include_chunks,
                    "results": out_items,
                }
            )
        else:
            wrapper = (
                {"status": "ok", "query": query} if output_format == "json" else None
            )
            payload = format_output(
                results,
                output_format,
                fields=fields,
                truncate=truncate,
                wrapper=wrapper,
            )
        emit_machine_text(payload)
    else:
        trunc = truncate or 80
        if not quiet:
            console.print(f"[green]Search results[/green] ({len(results)} found)")
        for r in results:
            preview = _doc_preview(r, trunc)
            if quiet:
                sys.stdout.write(f"{r['doc_id']}: {preview}\n")
            else:
                console.print(f"  - {r['doc_id']}: {preview}")


@retrieve_app.command()
def trace(
    trace_id: str = typer.Argument(..., help="Trace ID to retrieve"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Retrieve a specific trace by ID."""
    store = get_trace_store()
    result = store.get(trace_id)

    if result is None:
        if output_format == "json":
            emit_json({"status": "not_found", "trace_id": trace_id})
        else:
            console.print(f"[yellow]Trace not found[/yellow]: {trace_id}")
        raise typer.Exit(code=EXIT_INTERNAL)

    if output_format == "json":
        # ``model_dump_json`` is a serializer like any other, so this goes
        # through the emitter too. It sat six lines below the not_found arm
        # that #403 fixed and was missed twice -- by the issue's grep (which
        # looked for ``json.dumps``) and by the first pass of the AST rule
        # (whose serializer set had no Pydantic sibling). The rule now names
        # it, which is what stops the third miss.
        emit_machine_text(result.model_dump_json())
    else:
        console.print(f"[green]Trace[/green]: {result.trace_id}")
        console.print(f"  Source: {result.source}")
        console.print(f"  Intent: {result.intent}")
        if result.outcome:
            console.print(f"  Outcome: {result.outcome.status}")


@retrieve_app.command()
def entity(
    entity_id: str = typer.Argument(..., help="Entity ID to retrieve"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Retrieve a specific entity by ID."""
    store = get_graph_store()
    result = store.get_node(entity_id)

    # Fallback: resolve via local aliases so memorable names ("user-api") work.
    if result is None:
        alias_match = store.resolve_alias(LOCAL_SOURCE_SYSTEM, entity_id)
        if alias_match:
            result = store.get_node(alias_match["entity_id"])

    if result is None:
        if output_format == "json":
            emit_json({"status": "not_found", "entity_id": entity_id})
        else:
            console.print(f"[yellow]Entity not found[/yellow]: {entity_id}")
        raise typer.Exit(code=EXIT_INTERNAL)

    if output_format == "json":
        emit_json(result)
    else:
        console.print(f"[green]Entity[/green]: {entity_id}")
        console.print(f"  Type: {result.get('node_type', 'unknown')}")
        props = result.get("properties", {})
        for k, v in props.items():
            console.print(f"  {k}: {v}")


@retrieve_app.command()
def traces(
    limit: int = typer.Option(20, help="Maximum traces to return"),
    domain: str = typer.Option(None, help="Domain scope"),
    agent: str = typer.Option(None, "--agent", help="Agent ID filter"),
    output_format: str = typer.Option("text", "--format", help=_FMT_HELP),
    fields: str = typer.Option(None, "--fields", help=_FIELDS_HELP),
    truncate: int = typer.Option(None, "--truncate", help=_TRUNC_HELP),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """List recent traces."""
    store = get_trace_store()
    results = store.query(domain=domain, agent_id=agent, limit=limit)
    total = store.count(domain=domain)

    items = [t.to_summary_dict() for t in results]

    if output_format in ("json", "jsonl", "tsv"):
        if output_format == "json" and not fields:
            # Preserve backward-compatible JSON structure
            out_items = truncate_values(items, truncate)
            payload = json.dumps(
                {
                    "status": "ok",
                    "total": total,
                    "count": len(out_items),
                    "traces": out_items,
                }
            )
        else:
            wrapper = (
                {"status": "ok", "total": total} if output_format == "json" else None
            )
            payload = format_output(
                items,
                output_format,
                fields=fields,
                truncate=truncate,
                wrapper=wrapper,
            )
        emit_machine_text(payload)
    else:
        trunc = truncate or 60
        if not quiet:
            console.print(f"[green]Traces[/green] ({len(results)} of {total})")
        for t in results:
            outcome = t.outcome.status.value if t.outcome else "unknown"
            intent = t.intent[:trunc]
            line = f"  - {t.trace_id[:12]}... [{t.source.value}] {intent} ({outcome})"
            if quiet:
                sys.stdout.write(line.strip() + "\n")
            else:
                console.print(line)


@retrieve_app.command()
def precedents(
    domain: str = typer.Option(None, help="Domain scope"),
    limit: int = typer.Option(20, help="Maximum results"),
    output_format: str = typer.Option("text", "--format", help=_FMT_HELP),
    fields: str = typer.Option(None, "--fields", help=_FIELDS_HELP),
    truncate: int = typer.Option(None, "--truncate", help=_TRUNC_HELP),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """List precedents, optionally scoped by domain."""
    event_log = get_event_log()
    items = _list_precedents(event_log, domain=domain, limit=limit)

    if output_format in ("json", "jsonl", "tsv"):
        wrapper = {"status": "ok"} if output_format == "json" else None
        output = format_output(
            items,
            output_format,
            fields=fields,
            truncate=truncate,
            wrapper=wrapper,
        )
        emit_machine_text(output)
    else:
        if not quiet:
            console.print(f"[green]Precedents[/green] ({len(items)} found)")
        for item in items:
            title = item.get("title") or item.get("entity_id") or "unknown"
            line = f"  - {title} ({item.get('entity_id', '')})"
            if quiet:
                sys.stdout.write(line.strip() + "\n")
            else:
                console.print(line)


@retrieve_app.command("file-context")
def file_context(
    paths: list[str] = typer.Argument(  # noqa: B008 - typer option factory
        ..., help="File paths to look up"
    ),
    include_unconfirmed: bool = typer.Option(
        False,
        "--include-unconfirmed",
        help="Also surface unconfirmed extraction mints (#301)",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text, json, jsonl"
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """Show what memory already holds about specific files.

    The shell-callable half of read-time file context (#307): a
    ``PreToolUse`` hook runs this before a file is opened and decides,
    from each path's ``newest_item_at`` against the file's mtime,
    whether the stored context still describes the file on disk.
    """
    # ``tsv`` is the one group-wide format this command cannot honour:
    # a path entry carries nested document and entity lists, and
    # flattening them into cells would emit Python reprs. Refuse rather
    # than hand a hook something that parses but means nothing.
    if output_format not in ("text", "json", "jsonl"):
        console.print(
            f"[red]Unsupported --format {output_format!r};"
            " expected one of: text, json, jsonl[/red]"
        )
        raise typer.Exit(EXIT_VALIDATION)

    result = build_file_context(
        get_document_store(),
        get_graph_store(),
        paths,
        include_unconfirmed=include_unconfirmed,
    )
    entries: list[dict[str, Any]] = result["paths"]
    truncated = bool(result["graph_scan_truncated"])

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "count": len(entries),
                "paths": entries,
                "graph_scan_truncated": truncated,
            }
        )
        return
    if output_format == "jsonl":
        for entry in entries:
            emit_json({**entry, "graph_scan_truncated": truncated})
        return

    def _line(text: str, markup: str | None = None) -> None:
        # Rich hard-wraps at 80 columns when stdout is a pipe, which
        # splits long absolute paths mid-line for the shell hook this
        # command exists to serve. ``--quiet`` writes raw, like every
        # sibling command here.
        if quiet:
            sys.stdout.write(text + "\n")
        else:
            console.print(markup if markup is not None else text)

    if truncated:
        _line(
            "Warning: graph scan hit its cap; entity lists may be incomplete",
            "[yellow]Warning: graph scan hit its cap;"
            " entity lists may be incomplete[/yellow]",
        )
    for entry in entries:
        _line(entry["path"], f"[green]{entry['path']}[/green]")
        if not entry["documents"] and not entry["entities"]:
            _line("  (no stored context)")
            continue
        _line(f"  Newest memory: {entry['newest_item_at']}")
        for doc in entry["documents"]:
            label = doc.get("title") or doc.get("source_path") or doc["doc_id"]
            _line(f"  - doc {doc['doc_id']}: {label}")
        for node in entry["entities"]:
            _line(
                f"  - entity {node['entity_id']}:"
                f" {node.get('name') or node['entity_id']}"
            )
