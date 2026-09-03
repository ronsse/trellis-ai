"""Retrieve commands — search and fetch from the experience graph."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.console import Console

from trellis.retrieve.builder_factory import (
    SEMANTIC_AXIS_NOTES,
    build_pack_builder,
    describe_axes,
)
from trellis.retrieve.file_context import build_file_context
from trellis.retrieve.precedents import list_precedents as _list_precedents
from trellis.retrieve.withholding import (
    format_withholding_note,
    withholding_from_payload,
)
from trellis.schemas.pack import PackBudget
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_VALIDATION
from trellis_cli.output import (
    emit_json,
    emit_machine_text,
    format_output,
    truncate_values,
)
from trellis_cli.stores import (
    LOCAL_SOURCE_SYSTEM,
    _get_registry,
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
#: ``retrieve search`` hands back whole document rows, so it excludes
#: ``<parent>#chunk-N`` fragments by default — see
#: :data:`trellis.ingest_corpus.models.CHUNK_ID_SEPARATOR` for the rule.
#: The exclusion is pushed into the store rather than applied to the printed
#: list, so the row cap (``--limit``) refills with the documents the
#: fragments were sliced from instead of the list simply getting shorter. A
#: short list therefore still means "that is all there is", which is exactly
#: what a post-hoc filter would have destroyed.
#:
#: ``retrieve pack`` used to share this flag and no longer has one. #396
#: classified it as a row surface *because* it bypassed ``PackBuilder``, and
#: said the flag should be removed rather than inverted once that was fixed;
#: #410 fixed it. On a pack surface the chunk is the retrievable unit and
#: the excerpt is what the token budget prices, so suppressing chunks there
#: would make the operator preview diverge from the agent-facing path in the
#: opposite direction.
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
    max_tokens: int = typer.Option(8000, help="Maximum tokens in pack"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help=_QUIET_HELP),
) -> None:
    """Assemble a retrieval pack for a given intent.

    A real pack, since #410: this runs the same
    :class:`~trellis.retrieve.pack_builder.PackBuilder` that MCP
    ``get_context`` and ``POST /api/v1/packs`` run — keyword + graph +
    semantic axes, RRF fusion, recency/importance decay, the collect-seam
    noise and lifecycle gates, advisories, the two-stage budget, graduated
    disclosure — and emits the ``PACK_ASSEMBLED`` event that carries the
    ``pack_id``.

    Before #410 it called ``DocumentStore.search`` directly and printed
    doc ids: one keyword axis, no budget, no event, no ``pack_id``, and
    therefore nothing the learning loop could ever grade. It was named,
    documented and reached for as a preview of what an agent gets, while
    returning a materially different result set from every surface an
    agent actually uses.

    Two consequences of becoming a pack surface:

    * ``--include-chunks`` is **gone**, not inverted. #396 classified this
      command as a whole-row surface *because of* the bypass, and said in
      as many words that routing it through ``PackBuilder`` would make
      chunks the retrievable unit. They are: the keyword axis serves
      chunk rows on purpose, the excerpt is what the budget prices, and
      the flag would now suppress candidates the agent-facing path keeps.
    * ``--format json`` returns the pack, not a list of ids — same shape
      as ``POST /api/v1/packs`` plus the axis report, so an operator's
      preview and an agent's pack are directly comparable.
    """
    registry = _get_registry()
    builder = build_pack_builder(registry, surface="cli.retrieve")
    pack_result = builder.build(
        intent=intent,
        domain=domain or None,
        agent_id=agent or None,
        budget=PackBudget(max_items=max_items, max_tokens=max_tokens),
        # Match the MCP surface: fetch at least as many candidates per axis
        # as the item budget allows, so raising --max-items buys recall
        # instead of being silently capped at the 20-per-axis default.
        limit_per_strategy=max(20, max_items),
    )

    # Which axes this deployment has, which ran, and which did not. A pack
    # assembled without the semantic axis is a materially different pack,
    # and ``build_strategies`` drops that axis with a log line the CLI's
    # WARNING default never prints. Reporting the result without reporting
    # the gap would reproduce #410 one layer up.
    axes = describe_axes(
        builder,
        pack_result.retrieval_report.strategies_used,
        embedder_configured=registry.embedding_fn is not None,
    )
    axis_note = SEMANTIC_AXIS_NOTES.get(axes["semantic"], "")

    # #404: read the summary the builder stamped, do not re-derive one.
    # The JSON arm emits the stamped payload verbatim rather than
    # re-serialising a parsed copy, so the two surfaces cannot disagree.
    withholding_payload = pack_result.metadata.get("withholding")
    withholding = withholding_from_payload(withholding_payload)

    if output_format == "json":
        payload = json.dumps(
            {
                "status": "ok",
                # The whole point of the change: this pack is citable.
                "pack_id": pack_result.pack_id,
                "intent": pack_result.intent,
                "domain": pack_result.domain,
                "agent_id": pack_result.agent_id,
                "intent_family": pack_result.intent_family,
                "count": len(pack_result.items),
                "items": [item.model_dump(mode="json") for item in pack_result.items],
                "advisories": [
                    a.model_dump(mode="json") for a in pack_result.advisories
                ],
                "retrieval_report": pack_result.retrieval_report.model_dump(
                    mode="json"
                ),
                "budget": pack_result.budget.model_dump(mode="json"),
                # The builder's stamped summary, verbatim — counts,
                # reasons **and** ``withheld_item_ids``. #404's
                # counts-and-reasons-only rule scopes the rendered *note*,
                # whose audience is an agent's context window; it does not
                # scope this payload, whose audience is an operator who
                # already holds the stores and needs the ids to go look.
                # ``POST /api/v1/packs`` hands back the same ids under
                # ``retrieval_report.rejected_items``.
                "withholding": withholding_payload,
                "axes": axes,
            }
        )
        emit_machine_text(payload)
    elif quiet:
        for item in pack_result.items:
            sys.stdout.write(item.item_id + "\n")
    else:
        console.print(f"[green]Pack assembled[/green] ({len(pack_result.items)} items)")
        console.print(f"  pack_id: {pack_result.pack_id}")
        console.print(f"  Intent: {intent}")
        if domain:
            console.print(f"  Domain: {domain}")
        if agent:
            console.print(f"  Agent: {agent}")
        console.print(f"  Axes ran: {', '.join(axes['ran']) or '(none)'}")
        # Header, above the item blocks — never appended after them. The
        # same rule the pack formatters follow (#404): a note printed after
        # a list is a note the reader of a long list never reaches.
        if axis_note:
            console.print(f"  [yellow]{axis_note}[/yellow]")
        note = format_withholding_note(withholding)
        if note:
            console.print(f"  [yellow]{note}[/yellow]")
        for item in pack_result.items:
            preview = " ".join(item.excerpt.split())[:80]
            source = item.strategy_source or "?"
            # ``markup=False, emoji=False`` for the same reason #403 gave
            # for the JSON arm: Rich reads ``[document]`` as a style tag
            # and eats it, and rewrites ``:snowflake:`` inside a real
            # ``dataset:snowflake://…`` item_id as an emoji. Both were
            # live in this output before the flags were added — an id an
            # operator cannot copy is worse than no id.
            console.print(
                f"  - [{item.item_type}] {item.item_id} ({source}): {preview}",
                markup=False,
                emoji=False,
                highlight=False,
            )


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
