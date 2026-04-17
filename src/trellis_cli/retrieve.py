"""Retrieve commands — search and fetch from the experience graph."""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console

from trellis.retrieve.precedents import list_precedents as _list_precedents
from trellis_cli.output import format_output, truncate_values
from trellis_cli.stores import (
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


@retrieve_app.command()
def pack(
    intent: str = typer.Option(..., help="Intent for pack assembly"),
    domain: str = typer.Option(None, help="Domain scope"),
    agent: str = typer.Option(None, "--agent", help="Agent ID scope"),
    max_items: int = typer.Option(50, help="Maximum items in pack"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """Assemble a retrieval pack for a given intent."""
    store = get_document_store()
    filters = {}
    if domain:
        filters["domain"] = domain
    results = store.search(query=intent, limit=max_items, filters=filters)

    if output_format == "json":
        payload = json.dumps(
            {
                "status": "ok",
                "intent": intent,
                "domain": domain,
                "agent_id": agent,
                "count": len(results),
                "items": [r["doc_id"] for r in results],
            }
        )
        if quiet:
            sys.stdout.write(payload + "\n")
        else:
            console.print(payload)
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
            console.print(f"  - {r['doc_id']}")


@retrieve_app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, help="Maximum results"),
    domain: str = typer.Option(None, help="Domain scope"),
    output_format: str = typer.Option("text", "--format", help=_FMT_HELP),
    fields: str = typer.Option(None, "--fields", help=_FIELDS_HELP),
    truncate: int = typer.Option(None, "--truncate", help=_TRUNC_HELP),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """Search the experience graph."""
    store = get_document_store()
    filters = {}
    if domain:
        filters["domain"] = domain
    results = store.search(query=query, limit=limit, filters=filters)

    if output_format in ("json", "jsonl", "tsv"):
        if output_format == "json" and not fields:
            # Preserve backward-compatible JSON structure
            out_items = truncate_values(results, truncate)
            payload = json.dumps(
                {
                    "status": "ok",
                    "query": query,
                    "count": len(out_items),
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
        if quiet:
            sys.stdout.write(payload + "\n")
        else:
            console.print(payload)
    else:
        trunc = truncate or 80
        if not quiet:
            console.print(f"[green]Search results[/green] ({len(results)} found)")
        for r in results:
            snippet = r.get("snippet", "")[:trunc]
            if quiet:
                sys.stdout.write(f"{r['doc_id']}: {snippet}\n")
            else:
                console.print(f"  - {r['doc_id']}: {snippet}")


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
            console.print(json.dumps({"status": "not_found", "trace_id": trace_id}))
        else:
            console.print(f"[yellow]Trace not found[/yellow]: {trace_id}")
        raise typer.Exit(code=1)

    if output_format == "json":
        console.print(result.model_dump_json())
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

    if result is None:
        if output_format == "json":
            console.print(json.dumps({"status": "not_found", "entity_id": entity_id}))
        else:
            console.print(f"[yellow]Entity not found[/yellow]: {entity_id}")
        raise typer.Exit(code=1)

    if output_format == "json":
        console.print(json.dumps(result))
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
        if quiet:
            sys.stdout.write(payload + "\n")
        else:
            console.print(payload)
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
        if quiet:
            sys.stdout.write(output + "\n")
        else:
            console.print(output)
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
