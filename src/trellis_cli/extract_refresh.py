"""``trellis extract refresh`` — re-run an extractor and diff vs prior state.

Two invocation forms:

* ``trellis extract refresh --source <name>`` looks up ``<name>`` in a
  ``sources.yaml`` registry and runs the matching extractor. The default
  registry location is ``./sources.yaml`` relative to the current working
  directory; override with ``--sources-file``.
* ``trellis extract refresh --type <type> --path <path>`` runs the
  extractor for ``--type`` directly against ``--path`` without consulting
  any registry. Useful for one-shot operator commands.

For each entity the refresh touches, the CLI computes a property-level
diff against the entity's state immediately before the refresh and emits
a :attr:`~trellis.stores.base.event_log.EventType.TAGS_REFRESHED` event
with the structured before/after payload. Agents watching the EventLog
read these events to know which cached pack content is stale.

Trellis is not an orchestrator — this CLI is invoked from your existing
scheduler (cron, GitHub Actions, Airflow, K8s CronJob). See
[freshness-and-curation.md](../../docs/agent-guide/freshness-and-curation.md).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from trellis.extract.commands import result_to_batch
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.extract.sources import SourceEntry, load_sources
from trellis.mutate import build_curate_executor
from trellis.schemas.extraction import ExtractionResult
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.stores import _get_registry

extract_app = typer.Typer(no_args_is_help=True)
console = Console()


# Registry of built-in extractor types. Plugin-loaded extractors are
# resolved via the entry-points group ``trellis.extractors`` at runtime;
# this mapping handles the in-tree references the CLI ships with.
def _resolve_extractor(extractor_type: str) -> object:
    """Instantiate the extractor whose ``supported_sources`` claims ``extractor_type``.

    Built-ins live in ``trellis_workers.extract`` and ``trellis.extract``;
    third-party extractors register themselves via ``trellis.extractors``
    entry points and are discovered automatically.
    """
    registry = ExtractorRegistry()
    registry.load_entry_points()
    # Built-ins not yet entry-point-registered get added explicitly.
    try:
        from trellis_workers.extract import (  # noqa: PLC0415
            DbtManifestExtractor,
            OpenLineageExtractor,
        )

        for ext in (DbtManifestExtractor(), OpenLineageExtractor()):
            if registry.get(ext.name) is None:
                registry.register(ext)
    except ImportError:
        pass

    candidates = registry.candidates_for(extractor_type)
    if candidates:
        return candidates[0]
    available_sources = sorted(
        {s for name in registry.names() for s in registry.get(name).supported_sources}  # type: ignore[union-attr]
    )
    msg = (
        f"No extractor registered for type {extractor_type!r}. "
        f"Registered sources: {available_sources}"
    )
    raise typer.BadParameter(msg)


def _read_raw_input(path: Path, source_type: str) -> Any:
    """Parse an extractor input file based on the source type.

    dbt manifests are a single JSON object; OpenLineage events are either
    a JSON array or NDJSON. Other extractors are responsible for
    documenting their accepted input shapes — they receive the parsed
    JSON when the file is JSON-shaped, otherwise the raw text.
    """
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        msg = f"Input file is empty: {path}"
        raise typer.BadParameter(msg)
    if source_type == "openlineage":
        if text.startswith("["):
            return json.loads(text)
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def _snapshot_entities(
    registry: StoreRegistry, entity_ids: list[str]
) -> dict[str, dict[str, Any] | None]:
    """Read the current state of each entity_id from the graph store.

    Returns a dict mapping entity_id to the latest node row (or ``None``
    when the entity hasn't been ingested yet). Caller passes the post-
    snapshot through the same function to compute the diff.
    """
    graph = registry.knowledge.graph_store
    snapshot: dict[str, dict[str, Any] | None] = {}
    for entity_id in entity_ids:
        try:
            snapshot[entity_id] = graph.get_node(entity_id)
        except Exception:
            # Backend errors are non-fatal for the diff path. We just
            # report missing snapshot for that id and continue.
            snapshot[entity_id] = None
    return snapshot


def _property_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute the property-level diff between two node snapshots.

    Returns ``{}`` when nothing changed. The diff structure:

    .. code-block:: python

        {
            "added": {"key": value, ...},      # keys only in after
            "removed": {"key": value, ...},    # keys only in before
            "changed": {"key": [before, after], ...},
            "new_entity": True,                # before snapshot was None
            "deleted_entity": True,            # after snapshot is None
        }
    """
    before_props = (before or {}).get("properties") or {}
    after_props = (after or {}).get("properties") or {}

    if before is None and after is not None:
        return {"new_entity": True, "added": dict(after_props)}
    if after is None and before is not None:
        return {"deleted_entity": True, "removed": dict(before_props)}
    if before is None and after is None:
        return {}

    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, list[Any]] = {}

    for key, val in after_props.items():
        if key not in before_props:
            added[key] = val
        elif before_props[key] != val:
            changed[key] = [before_props[key], val]
    removed = {key: val for key, val in before_props.items() if key not in after_props}

    if not added and not removed and not changed:
        return {}
    diff: dict[str, Any] = {}
    if added:
        diff["added"] = added
    if removed:
        diff["removed"] = removed
    if changed:
        diff["changed"] = changed
    return diff


def _emit_refresh_event(
    registry: StoreRegistry,
    entity_id: str,
    entity_type: str,
    diff: dict[str, Any],
    *,
    source_name: str,
    extractor_used: str,
) -> None:
    """Fail-soft TAGS_REFRESHED emission with the structural diff payload."""
    try:
        registry.operational.event_log.emit(
            EventType.TAGS_REFRESHED,
            source=f"extract.refresh:{source_name}",
            entity_id=entity_id,
            entity_type=entity_type,
            payload={
                "extractor_used": extractor_used,
                "source_name": source_name,
                "diff": diff,
            },
        )
    except Exception:
        # Log via structlog rather than failing the refresh. EventLog
        # writes are best-effort — operators can still consume the CLI
        # summary if the audit channel is down.
        import structlog  # noqa: PLC0415

        structlog.get_logger(__name__).exception(
            "extract_refresh_emit_failed",
            entity_id=entity_id,
            source_name=source_name,
        )


def _run_refresh(
    registry: StoreRegistry,
    *,
    extractor: object,
    raw_input: Any,
    source_hint: str,
    source_name: str,
) -> dict[str, Any]:
    """Core refresh logic: extract, snapshot, execute, diff, emit.

    Returns a summary dict suitable for direct JSON serialization.
    """
    ext_registry = ExtractorRegistry()
    ext_registry.register(extractor)  # type: ignore[arg-type]
    dispatcher = ExtractionDispatcher(
        ext_registry, event_log=registry.operational.event_log
    )

    result: ExtractionResult = asyncio.run(
        dispatcher.dispatch(raw_input, source_hint=source_hint),
    )
    entity_ids = [e.entity_id for e in result.entities if e.entity_id]
    entity_types = {e.entity_id: e.entity_type for e in result.entities if e.entity_id}

    before_snapshot = _snapshot_entities(registry, entity_ids)
    batch = result_to_batch(result, requested_by=f"cli:extract-refresh:{source_name}")
    executor = build_curate_executor(registry)
    executor.execute_batch(batch)
    after_snapshot = _snapshot_entities(registry, entity_ids)

    new_entities = 0
    changed_entities = 0
    unchanged_entities = 0
    diffs: list[dict[str, Any]] = []

    for entity_id in entity_ids:
        diff = _property_diff(
            before_snapshot.get(entity_id), after_snapshot.get(entity_id)
        )
        if not diff:
            unchanged_entities += 1
            continue
        if diff.get("new_entity"):
            new_entities += 1
        else:
            changed_entities += 1
        diffs.append(
            {
                "entity_id": entity_id,
                "entity_type": entity_types.get(entity_id, ""),
                "diff": diff,
            }
        )
        _emit_refresh_event(
            registry,
            entity_id,
            entity_types.get(entity_id, ""),
            diff,
            source_name=source_name,
            extractor_used=result.extractor_used,
        )

    return {
        "source": source_name,
        "extractor_used": result.extractor_used,
        "tier": result.tier,
        "entities_scanned": len(entity_ids),
        "new_entities": new_entities,
        "changed_entities": changed_entities,
        "unchanged_entities": unchanged_entities,
        "edges_emitted": len(result.edges),
        "diffs": diffs,
    }


def _refresh_entry(
    entry: SourceEntry,
    *,
    sources_root: Path,
) -> dict[str, Any]:
    """Run refresh for a single SourceEntry."""
    if entry.endpoint is not None:
        # Endpoint-form sources are pushed at the REST API, not pulled by
        # this CLI. Refusing here forces the operator to use the right
        # mechanism rather than failing silently with an empty diff.
        msg = (
            f"Source {entry.name!r} uses 'endpoint', not 'path'. "
            "Endpoint-form sources are push-driven via POST "
            "/api/v1/extract/drafts; trellis extract refresh only pulls "
            "from path-based sources."
        )
        raise typer.BadParameter(msg)
    if entry.path is None:  # pragma: no cover — XOR validator prevents this
        msg = f"Source {entry.name!r} has neither path nor endpoint."
        raise typer.BadParameter(msg)
    raw_path = Path(entry.path)
    if not raw_path.is_absolute():
        raw_path = (sources_root / raw_path).resolve()
    if not raw_path.exists():
        msg = f"Source {entry.name!r}: path {raw_path} does not exist."
        raise typer.BadParameter(msg)
    extractor = _resolve_extractor(entry.type)
    raw_input = _read_raw_input(raw_path, entry.type)
    registry = _get_registry()
    return _run_refresh(
        registry,
        extractor=extractor,
        raw_input=raw_input,
        source_hint=entry.type,
        source_name=entry.name,
    )


@extract_app.command("refresh")
def refresh(  # noqa: PLR0912, PLR0915 - CLI dispatch with explicit branching by intent
    source: str = typer.Option(
        None,
        "--source",
        help="Source name from sources.yaml. Mutually exclusive with --type.",
    ),
    extractor_type: str = typer.Option(
        None,
        "--type",
        help=(
            "Extractor type for one-shot invocation (e.g., 'dbt-manifest'). "
            "Requires --path. Mutually exclusive with --source."
        ),
    ),
    path: str = typer.Option(
        None,
        "--path",
        help="Input file path. Required with --type; ignored with --source.",
    ),
    sources_file: str = typer.Option(
        "sources.yaml",
        "--sources-file",
        help="Path to sources.yaml registry. Defaults to ./sources.yaml.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Re-run an extractor and emit a diff vs prior state.

    For each entity touched by the refresh, a TAGS_REFRESHED event is
    emitted into the EventLog with the structured property diff.
    """
    if (source is None) == (extractor_type is None):
        console.print(
            "[red]Specify exactly one of --source or --type.[/red]"
        )
        raise typer.Exit(code=1)

    if extractor_type is not None and path is None:
        console.print("[red]--type requires --path.[/red]")
        raise typer.Exit(code=1)

    summary: dict[str, Any]
    if source is not None:
        sources_path = Path(sources_file)
        if not sources_path.exists():
            console.print(
                f"[red]sources.yaml not found: {sources_path}[/red]"
            )
            raise typer.Exit(code=1)
        config = load_sources(sources_path)
        entry = config.find(source)
        if entry is None:
            console.print(
                f"[red]Source {source!r} not declared in {sources_path}[/red]"
            )
            raise typer.Exit(code=1)
        if not entry.enabled:
            console.print(
                f"[yellow]Source {source!r} is disabled in {sources_path} — "
                f"refusing to refresh. Remove enabled: false to proceed.[/yellow]"
            )
            raise typer.Exit(code=1)
        try:
            summary = _refresh_entry(entry, sources_root=sources_path.parent.resolve())
        except typer.BadParameter as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        except Exception as exc:
            if output_format == "json":
                print(json.dumps({"status": "error", "message": str(exc)}))
            else:
                console.print(f"[red]Refresh failed: {exc}[/red]")
            raise typer.Exit(code=1) from None
    else:
        # --type path
        type_path = Path(path)  # type: ignore[arg-type]
        if not type_path.exists():
            console.print(f"[red]Path not found: {type_path}[/red]")
            raise typer.Exit(code=1)
        try:
            extractor = _resolve_extractor(extractor_type)  # type: ignore[arg-type]
            raw_input = _read_raw_input(type_path, extractor_type)  # type: ignore[arg-type]
            registry = _get_registry()
            summary = _run_refresh(
                registry,
                extractor=extractor,
                raw_input=raw_input,
                source_hint=extractor_type,  # type: ignore[arg-type]
                source_name=f"<adhoc:{extractor_type}>",
            )
        except typer.BadParameter as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        except Exception as exc:
            if output_format == "json":
                print(json.dumps({"status": "error", "message": str(exc)}))
            else:
                console.print(f"[red]Refresh failed: {exc}[/red]")
            raise typer.Exit(code=1) from None

    if output_format == "json":
        # Use plain print to avoid Rich's word-wrapping on long JSON, which
        # injects newlines mid-value and breaks downstream parsers.
        print(json.dumps({"status": "refreshed", **summary}))
        return

    console.print(f"[green]Refreshed {summary['source']}[/green]")
    console.print(f"  Extractor: {summary['extractor_used']} ({summary['tier']})")
    console.print(f"  Entities scanned:  {summary['entities_scanned']}")
    console.print(f"  New:               {summary['new_entities']}")
    console.print(f"  Changed:           {summary['changed_entities']}")
    console.print(f"  Unchanged:         {summary['unchanged_entities']}")
    console.print(f"  Edges emitted:     {summary['edges_emitted']}")
    if summary["diffs"]:
        console.print()
        console.print("  Per-entity diffs:")
        for d in summary["diffs"]:
            console.print(f"    - {d['entity_id']} ({d['entity_type']})")
            diff = d["diff"]
            if diff.get("new_entity"):
                console.print("      [cyan]new entity[/cyan]")
            for key in diff.get("added", {}):
                console.print(f"      [green]+[/green] {key}")
            for key in diff.get("removed", {}):
                console.print(f"      [red]-[/red] {key}")
            for key, (b, a) in (diff.get("changed") or {}).items():
                console.print(f"      [yellow]~[/yellow] {key}: {b!r} -> {a!r}")


if __name__ == "__main__":  # pragma: no cover
    extract_app()
