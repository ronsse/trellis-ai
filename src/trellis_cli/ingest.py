"""Ingest commands -- import traces, evidence, and external sources."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer
from rich.console import Console

from trellis.extract.commands import result_to_batch
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.mutate.commands import (
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.schemas.evidence import Evidence
from trellis.schemas.extraction import ExtractionResult
from trellis.schemas.trace import Trace
from trellis.stores.registry import StoreRegistry
from trellis_cli.stores import _get_registry, get_document_store, get_trace_store

ingest_app = typer.Typer(no_args_is_help=True)
console = Console()


@ingest_app.command("trace")
def ingest_trace(
    file: str = typer.Argument(None, help="Path to trace JSON file, or '-' for stdin"),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Ingest a trace from a JSON file or stdin."""
    # Read input
    if file == "-" or file is None:
        raw = sys.stdin.read()
    else:
        path = Path(file)
        if not path.exists():
            console.print(f"[red]File not found: {file}[/red]")
            raise typer.Exit(code=1)
        raw = path.read_text()

    # Parse and validate
    try:
        data = json.loads(raw)
        trace = Trace.model_validate(data)
    except Exception as exc:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            console.print(f"[red]Invalid trace: {exc}[/red]")
        raise typer.Exit(code=1) from None

    # Persist to store
    store = get_trace_store()
    store.append(trace)

    if output_format == "json":
        console.print(
            json.dumps(
                {
                    "status": "ingested",
                    "trace_id": trace.trace_id,
                    "source": trace.source,
                    "intent": trace.intent,
                }
            )
        )
    else:
        console.print(f"[green]Trace ingested[/green]: {trace.trace_id}")
        console.print(f"  Source: {trace.source}")
        console.print(f"  Intent: {trace.intent}")


@ingest_app.command("evidence")
def ingest_evidence(
    file: str = typer.Argument(..., help="Path to evidence JSON file"),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Ingest evidence from a JSON file."""
    path = Path(file)
    if not path.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(code=1)

    try:
        data = json.loads(path.read_text())
        evidence = Evidence.model_validate(data)
    except Exception as exc:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            console.print(f"[red]Invalid evidence: {exc}[/red]")
        raise typer.Exit(code=1) from None

    # Persist to document store
    store = get_document_store()
    store.put(
        doc_id=evidence.evidence_id,
        content=evidence.content or "",
        metadata={
            "evidence_type": evidence.evidence_type,
            "source_origin": evidence.source_origin,
        },
    )

    if output_format == "json":
        console.print(
            json.dumps(
                {
                    "status": "ingested",
                    "evidence_id": evidence.evidence_id,
                    "evidence_type": evidence.evidence_type,
                }
            )
        )
    else:
        console.print(f"[green]Evidence ingested[/green]: {evidence.evidence_id}")
        console.print(f"  Type: {evidence.evidence_type}")


# ---------------------------------------------------------------------------
# Extraction pipeline helpers
# ---------------------------------------------------------------------------


def _run_extraction(
    registry: StoreRegistry,
    *,
    extractor: object,
    raw_input: object,
    source_hint: str,
) -> ExtractionResult:
    """Register the extractor, dispatch, and return the result."""
    ext_registry = ExtractorRegistry()
    ext_registry.register(extractor)  # type: ignore[arg-type]
    dispatcher = ExtractionDispatcher(
        ext_registry, event_log=registry.operational.event_log
    )
    return asyncio.run(
        dispatcher.dispatch(raw_input, source_hint=source_hint),
    )


def _execute_batch(
    registry: StoreRegistry,
    batch: CommandBatch,
) -> tuple[int, int]:
    """Submit the batch and return ``(nodes_created, edges_created)``."""
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=handlers,
    )
    results = executor.execute_batch(batch)
    nodes = sum(
        1
        for r in results
        if r.operation == Operation.ENTITY_CREATE and r.status == CommandStatus.SUCCESS
    )
    edges = sum(
        1
        for r in results
        if r.operation == Operation.LINK_CREATE and r.status == CommandStatus.SUCCESS
    )
    return nodes, edges


@ingest_app.command("dbt-manifest")
def ingest_dbt_manifest(
    manifest_path: str = typer.Argument(
        ..., help="Path to dbt manifest.json or project dir"
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Ingest a dbt manifest through the governed extraction pipeline."""
    path = Path(manifest_path)
    if not path.exists():
        console.print(f"[red]Path not found: {manifest_path}[/red]")
        raise typer.Exit(code=1)

    manifest_file = path / "target" / "manifest.json" if path.is_dir() else path

    try:
        manifest = json.loads(manifest_file.read_text())
    except Exception as exc:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            console.print(f"[red]Could not read manifest: {exc}[/red]")
        raise typer.Exit(code=1) from None

    from trellis_workers.extract import DbtManifestExtractor  # noqa: PLC0415

    registry = _get_registry()
    try:
        result = _run_extraction(
            registry,
            extractor=DbtManifestExtractor(),
            raw_input=manifest,
            source_hint="dbt-manifest",
        )
        nodes, edges = _execute_batch(
            registry,
            result_to_batch(result, requested_by="cli:dbt-manifest"),
        )
    except Exception as exc:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            console.print(f"[red]dbt ingest failed: {exc}[/red]")
        raise typer.Exit(code=1) from None

    # Index descriptions into the document store (dbt-specific side-channel
    # that used to live inside the worker's load() override).
    doc_store = registry.knowledge.document_store
    doc_count = 0
    for entity in result.entities:
        desc = entity.properties.get("description", "")
        if desc:
            doc_store.put(
                doc_id=f"dbt:{entity.entity_id}",
                content=desc,
                metadata={
                    "source": "dbt",
                    "node_type": entity.entity_type,
                    "name": entity.properties.get("name", entity.name),
                    "unique_id": entity.entity_id,
                },
            )
            doc_count += 1

    counts = {"nodes": nodes, "edges": edges, "documents": doc_count}
    if output_format == "json":
        console.print(json.dumps({"status": "ingested", **counts}))
    else:
        console.print("[green]dbt manifest ingested[/green]")
        console.print(f"  Nodes: {counts['nodes']}")
        console.print(f"  Edges: {counts['edges']}")
        console.print(f"  Documents: {counts['documents']}")


@ingest_app.command("openlineage")
def ingest_openlineage(
    events_path: str = typer.Argument(..., help="Path to OpenLineage events JSON file"),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Ingest OpenLineage events through the governed extraction pipeline."""
    path = Path(events_path)
    if not path.exists():
        console.print(f"[red]File not found: {events_path}[/red]")
        raise typer.Exit(code=1)

    # Support JSON array and NDJSON — CLI owns file I/O.
    try:
        raw = path.read_text().strip()
        events: list[dict[str, object]]
        if raw.startswith("["):
            events = json.loads(raw)
        else:
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except Exception as exc:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            console.print(f"[red]Could not read events file: {exc}[/red]")
        raise typer.Exit(code=1) from None

    from trellis_workers.extract import OpenLineageExtractor  # noqa: PLC0415

    registry = _get_registry()
    try:
        result = _run_extraction(
            registry,
            extractor=OpenLineageExtractor(),
            raw_input=events,
            source_hint="openlineage",
        )
        nodes, edges = _execute_batch(
            registry,
            result_to_batch(result, requested_by="cli:openlineage"),
        )
    except Exception as exc:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            console.print(f"[red]OpenLineage ingest failed: {exc}[/red]")
        raise typer.Exit(code=1) from None

    counts = {"nodes": nodes, "edges": edges}
    if output_format == "json":
        console.print(json.dumps({"status": "ingested", **counts}))
    else:
        console.print("[green]OpenLineage events ingested[/green]")
        console.print(f"  Nodes: {counts['nodes']}")
        console.print(f"  Edges: {counts['edges']}")
