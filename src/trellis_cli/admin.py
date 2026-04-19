"""Admin commands for Trellis CLI."""

from __future__ import annotations

import json
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from trellis_cli.claude_integration import (
    get_claude_settings_path,
    merge_mcp_server,
    read_claude_settings,
    write_claude_settings,
)
from trellis_cli.config import TrellisConfig, get_config_dir, get_data_dir
from trellis_cli.stores import (
    _get_registry,
    get_document_store,
    get_event_log,
    get_graph_store,
    get_trace_store,
)

# Environment variable names used by the memory-extraction pipeline.
_MEMORY_FLAG_ENV = "TRELLIS_ENABLE_MEMORY_EXTRACTION"
_LLM_API_KEY_ENVS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_TRUTHY = {"1", "true", "yes", "on"}

# Commented-out ``llm:`` block appended to a freshly-initialized
# ``config.yaml`` so operators have an in-place template for enabling
# the memory-extraction pipeline. Uncomment + fill to activate; keep
# secrets out of the file by preferring ``api_key_env`` over ``api_key``.
_LLM_CONFIG_TEMPLATE = """
# llm:
#   # LLM provider for memory extraction + enrichment workers.
#   # Enabling this block unlocks the AliasMatch + LLMExtractor pipeline
#   # in MCP save_memory when TRELLIS_ENABLE_MEMORY_EXTRACTION=1 is also set.
#   provider: openai              # or "anthropic"
#   api_key_env: OPENAI_API_KEY   # env var name (preferred — keeps secrets out of file)
#   # api_key: sk-...             # OR literal value (discouraged)
#   model: gpt-4o-mini            # default model for generate() calls
#   # base_url: https://...       # optional, for proxies / self-hosted
#   # embedding:                  # optional sub-block; inherits provider/key from llm
#   #   provider: openai
#   #   model: text-embedding-3-small
"""

admin_app = typer.Typer(no_args_is_help=True)
console = Console()


@admin_app.command()
def init(
    data_dir: str = typer.Option(None, help="Custom data directory path"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config"),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Initialize Trellis stores and configuration."""
    config_dir = get_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists() and not force:
        if output_format == "json":
            console.print(
                json.dumps({"status": "exists", "config_dir": str(config_dir)})
            )
        else:
            console.print(
                f"[yellow]Config already exists at {config_path}."
                " Use --force to overwrite.[/yellow]"
            )
        raise typer.Exit(code=0)

    # Set up data directory
    actual_data_dir = Path(data_dir) if data_dir else get_data_dir()
    actual_data_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories for stores
    (actual_data_dir / "stores").mkdir(exist_ok=True)

    # Save config
    config = TrellisConfig(data_dir=str(actual_data_dir))
    config.save()

    # Append a commented-out ``llm:`` block so operators have an
    # in-place template for enabling the memory-extraction pipeline.
    # See docs/agent-guide/playbooks.md "Configuring LLM extraction".
    config_path.write_text(config_path.read_text() + _LLM_CONFIG_TEMPLATE)

    if output_format == "json":
        # Plain ``print`` (not ``console.print``) so Rich's terminal
        # width soft-wrap never splits the JSON across lines — long
        # Windows paths can push the payload past 80 chars.
        print(
            json.dumps(
                {
                    "status": "initialized",
                    "config_dir": str(config_dir),
                    "data_dir": str(actual_data_dir),
                }
            )
        )
    else:
        console.print("[green]Initialized Trellis[/green]")
        console.print(f"  Config: {config_path}")
        console.print(f"  Data:   {actual_data_dir}")


def _load_config_for_migration(
    config_path: Path, output_format: str
) -> dict[str, Any]:
    """Load + validate config for migration; exit nonzero on failure."""
    import yaml  # noqa: PLC0415

    if not config_path.exists():
        _report_migrate(
            output_format,
            status="missing",
            message=f"No config found at {config_path}",
        )
        raise typer.Exit(code=1)

    try:
        raw = config_path.read_text()
        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        _report_migrate(
            output_format,
            status="error",
            message=f"Failed to parse {config_path}: {exc}",
        )
        raise typer.Exit(code=1) from exc

    if not isinstance(data, dict):
        _report_migrate(
            output_format,
            status="error",
            message="config.yaml root is not a mapping",
        )
        raise typer.Exit(code=1)

    return data


def _partition_stores_by_plane(
    flat: dict[str, Any], output_format: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a flat ``stores:`` block into (knowledge, operational).

    Exits nonzero if unknown store types are present.
    """
    from trellis.stores.registry import _PLANE_OF  # noqa: PLC0415

    knowledge: dict[str, Any] = {}
    operational: dict[str, Any] = {}
    unknown: list[str] = []
    for store_type, store_cfg in flat.items():
        plane = _PLANE_OF.get(store_type)
        if plane == "knowledge":
            knowledge[store_type] = store_cfg
        elif plane == "operational":
            operational[store_type] = store_cfg
        else:
            unknown.append(store_type)

    if unknown:
        _report_migrate(
            output_format,
            status="error",
            message=f"Unknown store types in 'stores:' block: {unknown}",
        )
        raise typer.Exit(code=1)

    return knowledge, operational


def _build_migrated_config(
    data: dict[str, Any],
    knowledge: dict[str, Any],
    operational: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild config dict with planes replacing ``stores:`` — preserves key order."""
    migrated: dict[str, Any] = {}
    for key, value in data.items():
        if key == "stores":
            if knowledge:
                migrated["knowledge"] = knowledge
            if operational:
                migrated["operational"] = operational
        else:
            migrated[key] = value
    return migrated


@admin_app.command("migrate-config")
def migrate_config(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview changes without writing"
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite without creating a backup"
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Migrate a legacy flat ``stores:`` config to the plane-split shape.

    Rewrites ``~/.trellis/config.yaml`` so the ``stores:`` block is
    replaced by ``knowledge:`` (graph, vector, document, blob) and
    ``operational:`` (trace, event_log) blocks. A timestamped backup
    is written to ``config.yaml.bak.<ts>`` unless ``--force`` is used.

    See docs/design/adr-planes-and-substrates.md.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    config_path = get_config_dir() / "config.yaml"
    data = _load_config_for_migration(config_path, output_format)

    flat = data.get("stores")
    has_planes = "knowledge" in data or "operational" in data

    if not flat and has_planes:
        _report_migrate(
            output_format,
            status="already-migrated",
            message="config.yaml already uses plane-split shape",
        )
        return

    if not flat:
        _report_migrate(
            output_format,
            status="nothing-to-do",
            message="No 'stores:' block found",
        )
        return

    if not isinstance(flat, dict):
        _report_migrate(
            output_format,
            status="error",
            message="'stores:' block is not a mapping",
        )
        raise typer.Exit(code=1)

    knowledge, operational = _partition_stores_by_plane(flat, output_format)
    migrated = _build_migrated_config(data, knowledge, operational)
    new_text = yaml.safe_dump(migrated, sort_keys=False)

    if dry_run:
        _report_migrate(
            output_format,
            status="dry-run",
            message="Would rewrite config.yaml",
            diff_preview=new_text,
            backup_path=None,
        )
        return

    backup_path: Path | None = None
    if not force:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = config_path.with_name(f"config.yaml.bak.{ts}")
        shutil.copy2(config_path, backup_path)

    config_path.write_text(new_text)

    _report_migrate(
        output_format,
        status="migrated",
        message=f"Rewrote {config_path}",
        backup_path=str(backup_path) if backup_path else None,
    )


def _report_migrate(
    output_format: str,
    *,
    status: str,
    message: str,
    backup_path: str | None = None,
    diff_preview: str | None = None,
) -> None:
    """Emit migrate-config output in text or json."""
    if output_format == "json":
        payload: dict[str, Any] = {"status": status, "message": message}
        if backup_path is not None:
            payload["backup"] = backup_path
        if diff_preview is not None:
            payload["preview"] = diff_preview
        # Plain ``print`` (not ``console.print``) so Rich's terminal-
        # width soft-wrap never splits the JSON across lines. Machine
        # consumers expect single-line JSON per the project rule in
        # CLAUDE.md (`parse JSON output, not human-readable text`).
        print(json.dumps(payload))
        return

    colors = {
        "migrated": "green",
        "already-migrated": "cyan",
        "nothing-to-do": "cyan",
        "dry-run": "yellow",
        "missing": "red",
        "error": "red",
    }
    color = colors.get(status, "white")
    console.print(f"[{color}]{status}[/{color}] {message}")
    if backup_path:
        console.print(f"  Backup: {backup_path}")
    if diff_preview:
        console.print("\n[dim]--- Preview ---[/dim]")
        console.print(diff_preview)


@admin_app.command()
def health(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Check health of Trellis stores."""
    config = TrellisConfig.load()
    data_dir = Path(config.data_dir) if config.data_dir else get_data_dir()
    stores_dir = data_dir / "stores"

    checks: dict[str, bool] = {
        "config": get_config_dir().exists(),
        "data_dir": data_dir.exists(),
        "stores_dir": stores_dir.exists(),
    }

    # Check for store files
    store_files = [
        "documents.db",
        "graph.db",
        "vectors.db",
        "events.db",
        "traces.db",
    ]
    for sf in store_files:
        checks[sf] = (stores_dir / sf).exists()

    if output_format == "json":
        console.print(json.dumps(checks))
    else:
        table = Table(title="Trellis Health")
        table.add_column("Component", style="cyan")
        table.add_column("Status")
        for name, ok in checks.items():
            status = "[green]OK[/green]" if ok else "[red]MISSING[/red]"
            table.add_row(name, status)
        console.print(table)


@admin_app.command()
def version(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Print API / wire-schema / SDK version info.

    Mirrors the ``GET /api/version`` handshake endpoint so operators
    can see what a deployed server is advertising without an HTTP
    round-trip.  Pulls from :mod:`trellis.api_version` — the single
    source of truth shared by the CLI and the API.
    """
    from trellis.api_version import (  # noqa: PLC0415
        API_MAJOR,
        API_MINOR,
        MCP_TOOLS_VERSION,
        SDK_MIN,
        WIRE_SCHEMA,
        api_version_string,
    )
    from trellis.core.base import get_version  # noqa: PLC0415
    from trellis_api.deprecation import ROUTE_DEPRECATIONS  # noqa: PLC0415

    info: dict[str, Any] = {
        "api_major": API_MAJOR,
        "api_minor": API_MINOR,
        "api_version": api_version_string(),
        "wire_schema": WIRE_SCHEMA,
        "sdk_min": SDK_MIN,
        "package_version": get_version(),
        "mcp_tools_version": MCP_TOOLS_VERSION,
        "deprecations": [
            {
                "path": path,
                "deprecated_since": entry.deprecated_since.isoformat(),
                "sunset_on": entry.sunset_on.isoformat(),
                "replacement": entry.replacement,
                "reason": entry.reason,
            }
            for path, entry in ROUTE_DEPRECATIONS.items()
        ],
    }

    if output_format == "json":
        typer.echo(json.dumps(info, indent=2))
        return

    table = Table(title="Trellis API Version")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("api_version", info["api_version"])
    table.add_row("wire_schema", info["wire_schema"])
    table.add_row("sdk_min", info["sdk_min"])
    table.add_row("package_version", info["package_version"])
    table.add_row("mcp_tools_version", str(info["mcp_tools_version"]))
    table.add_row("deprecations", str(len(info["deprecations"])))
    console.print(table)
    if info["deprecations"]:
        dep_table = Table(title="Deprecated routes")
        dep_table.add_column("Path", style="yellow")
        dep_table.add_column("Sunset on")
        dep_table.add_column("Replacement")
        for d in info["deprecations"]:
            dep_table.add_row(
                d["path"],
                d["sunset_on"],
                d["replacement"] or "-",
            )
        console.print(dep_table)


@admin_app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Host to bind"),  # noqa: S104
    port: int = typer.Option(8420, help="Port to bind"),
) -> None:
    """Start the XPG REST API server."""
    try:
        import uvicorn  # noqa: PLC0415

        from trellis_api.app import create_app  # noqa: PLC0415
    except ImportError:
        console.print(
            "[red]FastAPI/uvicorn not installed."
            " Install with: pip install fastapi uvicorn[/red]"
        )
        raise typer.Exit(code=1)  # noqa: B904

    console.print(f"[green]Starting XPG API server on {host}:{port}[/green]")
    app = create_app()
    uvicorn.run(app, host=host, port=port)


@admin_app.command()
def stats(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Show store statistics."""
    counts: dict[str, int] = {}

    counts["traces"] = get_trace_store().count()
    counts["documents"] = get_document_store().count()
    gstore = get_graph_store()
    counts["nodes"] = gstore.count_nodes()
    counts["edges"] = gstore.count_edges()
    counts["events"] = get_event_log().count()

    if output_format == "json":
        console.print(json.dumps({"status": "ok", **counts}))
    else:
        table = Table(title="Store Statistics")
        table.add_column("Store", style="cyan")
        table.add_column("Count", justify="right")
        for name, count in counts.items():
            table.add_row(name, str(count))
        console.print(table)


@admin_app.command("graph-health")
def graph_health(  # noqa: PLR0912, PLR0915
    entity_type: str = typer.Option(
        None, "--entity-type", help="Scope to one entity type"
    ),
    role: str = typer.Option(
        None, "--role", help="Filter by node_role: structural/semantic/curated"
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Analyse graph health: role distribution, type balance, leaf nodes, orphans."""
    # Threshold constants for warning signals
    structural_warn_pct = 70
    curated_warn_pct = 30
    type_imbalance_pct = 70
    leaf_warn_pct = 90
    leaf_min_group_size = 5
    curated_min_graph_size = 100

    gstore = get_graph_store()
    total_nodes = gstore.count_nodes()
    total_edges = gstore.count_edges()

    if total_nodes == 0:
        if output_format == "json":
            typer.echo(
                json.dumps({"status": "empty", "warnings": [], "total_nodes": 0})
            )
        else:
            console.print("[yellow]Graph is empty — nothing to analyse.[/yellow]")
        raise typer.Exit(code=0)

    # Fetch all current nodes (up to 10K for analysis).
    all_nodes = gstore.query(
        node_type=entity_type,
        properties={"node_role": role} if role else None,
        limit=10000,
    )

    warnings: list[dict[str, Any]] = []

    # ── Report 1: role distribution ──
    role_counts: Counter[str] = Counter()
    for n in all_nodes:
        role_counts[n.get("node_role", "semantic")] += 1

    total_sampled = len(all_nodes)

    role_dist: list[dict[str, Any]] = []
    for r in ("structural", "semantic", "curated"):
        count = role_counts.get(r, 0)
        pct = (count / total_sampled * 100) if total_sampled else 0.0
        role_dist.append({"role": r, "count": count, "pct": round(pct, 1)})

    # Warning signals
    structural_pct = (
        role_counts.get("structural", 0) / total_sampled * 100 if total_sampled else 0
    )
    curated_pct = (
        role_counts.get("curated", 0) / total_sampled * 100 if total_sampled else 0
    )
    if structural_pct > structural_warn_pct:
        warnings.append(
            {
                "severity": "warning",
                "signal": "structural_dominant",
                "message": (
                    f"Structural nodes are {structural_pct:.0f}%"
                    " of total — likely over-modeling"
                ),
            }
        )
    if curated_pct > curated_warn_pct:
        warnings.append(
            {
                "severity": "warning",
                "signal": "curated_heavy",
                "message": (
                    f"Curated nodes are {curated_pct:.0f}%"
                    " — may indicate stale generators"
                ),
            }
        )
    if total_sampled > curated_min_graph_size and role_counts.get("curated", 0) == 0:
        warnings.append(
            {
                "severity": "info",
                "signal": "no_curated_nodes",
                "message": "No curated nodes — consider running precedent promotion",
            }
        )

    # ── Report 2: top entity types ──
    type_counts: Counter[str] = Counter()
    for n in all_nodes:
        type_counts[n.get("node_type", "unknown")] += 1

    type_dist = [
        {"entity_type": t, "count": c, "pct": round(c / total_sampled * 100, 1)}
        for t, c in type_counts.most_common(15)
    ]

    for t, c in type_counts.most_common(3):
        pct = c / total_sampled * 100
        if pct > type_imbalance_pct:
            warnings.append(
                {
                    "severity": "warning",
                    "signal": "type_imbalance",
                    "message": f"Entity type '{t}' is {pct:.0f}% of all nodes",
                }
            )

    # ── Report 3: leaf-node analysis ──
    leaf_analysis: list[dict[str, Any]] = []
    node_ids = [n["node_id"] for n in all_nodes]
    # Build outbound edge counts
    outbound: Counter[str] = Counter()
    for nid in node_ids[:2000]:  # cap for performance
        try:
            edges = gstore.get_edges(nid, direction="outgoing")
            outbound[nid] = len(edges)
        except Exception:  # noqa: S112
            continue

    # Group by (entity_type, node_role) for leaf analysis
    type_role_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for n in all_nodes:
        if n["node_id"] in outbound:
            key = (n.get("node_type", "unknown"), n.get("node_role", "semantic"))
            type_role_groups[key].append(n["node_id"])

    for (nt, nr), nids in sorted(type_role_groups.items()):
        total_in_group = len(nids)
        leaves = sum(1 for nid in nids if outbound.get(nid, 0) == 0)
        leaf_pct = (leaves / total_in_group * 100) if total_in_group else 0.0
        leaf_analysis.append(
            {
                "entity_type": nt,
                "node_role": nr,
                "total": total_in_group,
                "leaves": leaves,
                "leaf_pct": round(leaf_pct, 1),
            }
        )
        is_semantic_leaf_heavy = (
            nr == "semantic"
            and leaf_pct > leaf_warn_pct
            and total_in_group >= leaf_min_group_size
        )
        if is_semantic_leaf_heavy:
            warnings.append(
                {
                    "severity": "warning",
                    "signal": "semantic_mostly_leaves",
                    "message": (
                        f"Semantic type '{nt}' has {leaf_pct:.0f}%"
                        " leaves — consider reclassifying to structural"
                    ),
                }
            )

    # ── Report 4: orphan detection ──
    orphans: list[str] = []
    for n in all_nodes[:2000]:
        nid = n["node_id"]
        try:
            edges = gstore.get_edges(nid, direction="both")
            if not edges:
                orphans.append(nid)
        except Exception:  # noqa: S112
            continue

    if orphans:
        warnings.append(
            {
                "severity": "info",
                "signal": "orphan_nodes",
                "message": (
                    f"{len(orphans)} orphan nodes (no edges in either direction)"
                ),
            }
        )

    # ── Output ──
    report = {
        "status": "ok",
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "sampled_nodes": total_sampled,
        "role_distribution": role_dist,
        "top_entity_types": type_dist,
        "leaf_analysis": leaf_analysis,
        "orphan_count": len(orphans),
        "orphan_sample": orphans[:10],
        "warnings": warnings,
    }

    if output_format == "json":
        typer.echo(json.dumps(report, indent=2))
    else:
        _print_graph_health_report(report)

    # Exit code: 0 ok, 1 warnings, 2 critical
    severity_levels = {w.get("severity") for w in warnings}
    if "critical" in severity_levels:
        raise typer.Exit(code=2)
    if "warning" in severity_levels:
        raise typer.Exit(code=1)


def _print_graph_health_report(report: dict[str, Any]) -> None:
    """Rich-formatted graph health output."""
    n_nodes = report["total_nodes"]
    n_edges = report["total_edges"]
    console.print(
        f"\n[bold]Graph Health Report[/bold]  ({n_nodes} nodes, {n_edges} edges)\n"
    )

    # Role distribution
    table = Table(title="Role Distribution")
    table.add_column("Role", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("%", justify="right")
    for r in report["role_distribution"]:
        table.add_row(r["role"], str(r["count"]), f"{r['pct']}%")
    console.print(table)

    # Top entity types
    table = Table(title="Top Entity Types")
    table.add_column("Type", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("%", justify="right")
    for t in report["top_entity_types"]:
        table.add_row(t["entity_type"], str(t["count"]), f"{t['pct']}%")
    console.print(table)

    # Leaf analysis
    if report["leaf_analysis"]:
        table = Table(title="Leaf Node Analysis")
        table.add_column("Type", style="cyan")
        table.add_column("Role")
        table.add_column("Total", justify="right")
        table.add_column("Leaves", justify="right")
        table.add_column("Leaf %", justify="right")
        for la in report["leaf_analysis"]:
            table.add_row(
                la["entity_type"],
                la["node_role"],
                str(la["total"]),
                str(la["leaves"]),
                f"{la['leaf_pct']}%",
            )
        console.print(table)

    # Orphans
    if report["orphan_count"]:
        console.print(f"\n[yellow]Orphan nodes:[/yellow] {report['orphan_count']}")
        if report["orphan_sample"]:
            console.print(f"  Sample: {', '.join(report['orphan_sample'][:5])}")

    # Warnings
    if report["warnings"]:
        console.print("\n[bold]Warnings:[/bold]")
        for w in report["warnings"]:
            color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(
                w["severity"], "white"
            )
            console.print(
                f"  [{color}]{w['severity'].upper()}[/{color}]: {w['message']}"
            )
    else:
        console.print("\n[green]No warnings — graph looks healthy.[/green]")


def _init_stores_if_needed(config_path: Path) -> str:
    """Initialize stores if not already done. Returns step name."""
    if config_path.exists():
        return "stores_already_initialized"
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stores").mkdir(exist_ok=True)
    TrellisConfig(data_dir=str(data_dir)).save()
    return "stores_initialized"


def _check_lancedb(output_format: str) -> None:
    """Verify lancedb is importable; exit with error if not."""
    try:
        import lancedb  # noqa: F401, PLC0415
    except ImportError:
        hint = 'pip install lancedb  # or: uv pip install "trellis[vectors]"'
        if output_format == "json":
            typer.echo(
                json.dumps(
                    {
                        "status": "error",
                        "error": "lancedb not installed",
                        "hint": hint,
                    }
                )
            )
        else:
            console.print(
                f"[red]lancedb is not installed.[/red]\n  Install with: {hint}"
            )
        raise typer.Exit(code=1)  # noqa: B904


def _ensure_gitignore(project_dir: Path) -> str | None:
    """Add .trellis/ to .gitignore if missing. Returns step name or None."""
    gitignore = project_dir / ".gitignore"
    xpg_line = ".trellis/"
    try:
        content = gitignore.read_text()
    except FileNotFoundError:
        gitignore.write_text(xpg_line + "\n")
        return "gitignore_created"
    if xpg_line in content.splitlines():
        return None
    with gitignore.open("a") as f:
        if not content.endswith("\n"):
            f.write("\n")
        f.write(xpg_line + "\n")
    return "gitignore_updated"


def _print_quickstart_summary(
    steps: list[str],
    config_path: Path,
    settings_path: Path,
    mcp_on_path: bool,
) -> None:
    """Print human-readable quickstart summary."""
    console.print("[green]Quickstart complete![/green]\n")
    if "stores_initialized" in steps:
        console.print(f"  [cyan]Stores initialized:[/cyan] {config_path}")
    else:
        console.print(f"  [dim]Stores already initialized:[/dim] {config_path}")
    if "mcp_registered" in steps:
        console.print(f"  [cyan]MCP server registered:[/cyan] {settings_path}")
    else:
        console.print(
            f"  [dim]MCP server already registered:[/dim]"
            f" {settings_path} (use --force to overwrite)"
        )
    if not mcp_on_path:
        console.print(
            "\n  [yellow]Warning:[/yellow] trellis-mcp not found on PATH."
            '\n  Run: uv pip install -e ".[dev]"'
        )
    console.print("\n[bold]Next steps:[/bold]")
    console.print("  1. Restart Claude Code to pick up the new MCP server")
    console.print("  2. Ask Claude to use get_context or save_experience")
    console.print(
        "\n[dim]Tip: Add experience graph usage guidance to your"
        " project's CLAUDE.md.[/dim]"
    )


@admin_app.command()
def quickstart(
    scope: str = typer.Option("root", help="root (global) or project (local)"),
    with_vectors: bool = typer.Option(
        False, "--with-vectors", help="Enable LanceDB vector store"
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing MCP server entry"
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Initialize stores and register MCP server with Claude Code."""
    config_path = get_config_dir() / "config.yaml"
    steps: list[str] = [_init_stores_if_needed(config_path)]

    if with_vectors:
        _check_lancedb(output_format)
        steps.append("lancedb_available")

    # Build MCP server entry
    project_dir = Path.cwd()
    mcp_entry: dict = {"command": "trellis-mcp", "args": []}
    if scope == "project":
        mcp_entry["env"] = {
            "TRELLIS_CONFIG_DIR": str(project_dir / ".trellis"),
        }

    # Merge into Claude settings
    settings_path = get_claude_settings_path(
        scope,
        project_dir=project_dir if scope == "project" else None,
    )
    settings = read_claude_settings(settings_path)
    settings, changed = merge_mcp_server(
        settings,
        "trellis",
        mcp_entry,
        force=force,
    )
    if changed:
        write_claude_settings(settings_path, settings)
        steps.append("mcp_registered")
    else:
        steps.append("mcp_already_registered")

    # Project scope extras
    if scope == "project":
        gi_step = _ensure_gitignore(project_dir)
        if gi_step:
            steps.append(gi_step)

    mcp_on_path = shutil.which("trellis-mcp") is not None

    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "scope": scope,
                    "steps": steps,
                    "settings_path": str(settings_path),
                    "mcp_on_path": mcp_on_path,
                }
            )
        )
    else:
        _print_quickstart_summary(
            steps,
            config_path,
            settings_path,
            mcp_on_path,
        )


def _memory_prompt_available() -> bool:
    """Verify the memory-extraction prompt template is importable.

    This is a forward-compat sentinel. Today the import always succeeds
    because the prompt ships with core; the check exists so a future
    refactor that moves the prompt behind an extra surfaces here, not
    as a runtime ``ImportError`` inside the MCP server.
    """
    try:
        from trellis.extract.prompts.extraction import (  # noqa: F401, PLC0415
            MEMORY_EXTRACTION_V1,
        )
    except ImportError:
        return False
    return True


def _build_check_extractors_report() -> dict[str, Any]:
    """Collect readiness signals for the ``save_memory`` extractor.

    Returns a structured report with the ``status``/``exit_code`` pair
    already filled in so the caller only renders it.
    """
    registry = _get_registry()

    llm_client = registry.build_llm_client()
    llm_cfg: dict[str, Any] = dict(registry._llm_config or {})
    provider = llm_cfg.get("provider")
    model = llm_cfg.get("model")

    env_fallback_available = any(os.environ.get(v) for v in _LLM_API_KEY_ENVS)
    flag_raw = os.environ.get(_MEMORY_FLAG_ENV, "").strip().lower()
    flag_set = flag_raw in _TRUTHY

    config_buildable = llm_client is not None
    alias_resolver_ok = registry.graph_store is not None
    memory_prompt_ok = _memory_prompt_available()

    warnings: list[dict[str, str]] = []

    # Status logic:
    # * BLOCKED (exit 2) — flag set AND no LLM client obtainable from
    #   either config or env. Extraction would silently skip in prod.
    # * WARN (exit 1) — suboptimal but not fatal. Two sub-cases:
    #     (a) LLM buildable but flag unset.
    #     (b) Flag set, config-path unbuildable, but env fallback present.
    # * READY (exit 0) — flag set AND config-buildable LLM.
    if flag_set and not config_buildable and not env_fallback_available:
        status = "blocked"
        exit_code = 2
        warnings.append(
            {
                "severity": "critical",
                "signal": "no_llm_client",
                "message": (
                    "Feature flag is set but no LLM client can be built from"
                    " config or env — memory extraction will silently skip."
                ),
            }
        )
    elif not flag_set and config_buildable:
        status = "warn"
        exit_code = 1
        warnings.append(
            {
                "severity": "warning",
                "signal": "flag_unset",
                "message": (
                    f"{_MEMORY_FLAG_ENV} is not set — memory extraction will"
                    " not run. Set it to '1' to opt in."
                ),
            }
        )
    elif flag_set and not config_buildable and env_fallback_available:
        status = "warn"
        exit_code = 1
        warnings.append(
            {
                "severity": "warning",
                "signal": "env_fallback_only",
                "message": (
                    "LLM client is not buildable from config; only env-var"
                    " fallback is available. Consider adding an llm: block"
                    " to ~/.config/trellis/config.yaml."
                ),
            }
        )
    elif flag_set and config_buildable:
        status = "ready"
        exit_code = 0
    else:
        # Flag unset AND no LLM configured — inert but not wrong.
        status = "warn"
        exit_code = 1
        warnings.append(
            {
                "severity": "warning",
                "signal": "flag_unset",
                "message": (
                    f"{_MEMORY_FLAG_ENV} is not set — memory extraction will not run."
                ),
            }
        )

    return {
        "status": status,
        "exit_code": exit_code,
        "llm_client": {
            "config_buildable": config_buildable,
            "provider": provider,
            "model": model,
            "env_fallback_available": env_fallback_available,
        },
        "feature_flag": {
            "name": _MEMORY_FLAG_ENV,
            "set": flag_set,
        },
        "dependencies": {
            "alias_resolver": alias_resolver_ok,
            "llm_client": config_buildable or env_fallback_available,
            "memory_prompt": memory_prompt_ok,
        },
        "warnings": warnings,
    }


def _print_check_extractors_report(report: dict[str, Any]) -> None:
    """Rich-formatted readiness report."""
    console.print("\n[bold]Tiered Extraction — Readiness Report[/bold]\n")

    llm = report["llm_client"]
    console.print("[bold]LLM client:[/bold]")
    if llm["config_buildable"]:
        provider = llm.get("provider") or "?"
        model = llm.get("model") or "(default)"
        console.print(
            f"  [green]OK[/green] configurable from ~/.config/trellis/config.yaml"
            f" (provider={provider}, model={model})"
        )
    else:
        console.print(
            "  [red]MISSING[/red] not configurable from ~/.config/trellis/config.yaml"
        )
    if llm["env_fallback_available"]:
        console.print(
            "  [green]OK[/green] OPENAI_API_KEY/ANTHROPIC_API_KEY env var is set"
            " (env fallback available)"
        )
    else:
        console.print(
            "  [yellow]--[/yellow] no OPENAI_API_KEY/ANTHROPIC_API_KEY env var"
            " (no env fallback)"
        )

    flag = report["feature_flag"]
    console.print("\n[bold]Memory-extraction feature flag:[/bold]")
    if flag["set"]:
        console.print(f"  [green]OK[/green] {flag['name']}=1")
    else:
        console.print(f"  [yellow]--[/yellow] {flag['name']} is not set")

    deps = report["dependencies"]
    console.print("\n[bold]Dependencies for save_memory extractor:[/bold]")
    dep_msgs = [
        ("alias_resolver", "alias resolver (graph_store — always available)"),
        ("llm_client", "LLM client (via registry config or env)"),
        ("memory_prompt", "memory prompt template"),
    ]
    for key, label in dep_msgs:
        if deps[key]:
            console.print(f"  [green]OK[/green] {label}")
        else:
            console.print(f"  [red]MISSING[/red] {label}")

    status_color = {
        "ready": "green",
        "warn": "yellow",
        "blocked": "red",
    }.get(report["status"], "white")
    console.print(
        f"\nStatus: [{status_color}]{report['status'].upper()}[/{status_color}]"
    )

    if report["warnings"]:
        console.print("\n[bold]Warnings:[/bold]")
        for w in report["warnings"]:
            color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(
                w["severity"], "white"
            )
            console.print(
                f"  [{color}]{w['severity'].upper()}[/{color}]: {w['message']}"
            )


@admin_app.command("check-extractors")
def check_extractors(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Report readiness of the ``save_memory`` tiered-extraction pipeline.

    Exit codes:

    * ``0`` — READY (LLM client buildable AND feature flag set)
    * ``1`` — WARN (buildable but flag unset; or flag set with only
      env-var fallback available)
    * ``2`` — BLOCKED (flag set AND no LLM client obtainable anywhere)
    """
    report = _build_check_extractors_report()
    if output_format == "json":
        typer.echo(json.dumps(report, indent=2))
    else:
        _print_check_extractors_report(report)
    if report["exit_code"] != 0:
        raise typer.Exit(code=report["exit_code"])


@admin_app.command("check-plugins")
def check_plugins(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Report discovered plugins (entry-point extensions) and their status.

    Walks every known entry-point group (``trellis.stores.*``,
    ``trellis.llm.providers``, ``trellis.extractors``,
    ``trellis.classifiers``, ``trellis.rerankers``,
    ``trellis.policies``, ``trellis.search_strategies``,
    ``trellis.llm.embedders``) and reports each plugin's status:

    * ``LOADED`` — plugin imported cleanly; will be available at
      runtime.
    * ``SHADOWED`` — plugin uses the same name as a built-in; the
      built-in wins unless ``TRELLIS_PLUGIN_OVERRIDE=1`` is set.
    * ``BLOCKED`` — plugin is declared but the module or class
      couldn't be imported.  Silent in prod — this is the case the
      probe most wants to catch.

    Exit codes: ``0`` clean, ``1`` shadowing only, ``2`` any blocked.
    """
    from trellis.plugins import collect_plugin_report  # noqa: PLC0415

    report = collect_plugin_report()
    if output_format == "json":
        payload = {
            "loaded": report.loaded_count,
            "blocked": report.blocked_count,
            "shadowed": report.shadowed_count,
            "exit_code": report.exit_code,
            "groups_checked": report.groups_checked,
            "plugins": [p.to_dict() for p in report.plugins],
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        _print_check_plugins_report(report)
    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


def _print_check_plugins_report(report: Any) -> None:
    """Pretty-print a :class:`trellis.plugins.PluginReport`."""
    summary = Table(title="Trellis Plugins")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Count")
    summary.add_row("Groups checked", str(len(report.groups_checked)))
    summary.add_row("Loaded", f"[green]{report.loaded_count}[/green]")
    summary.add_row("Shadowed", f"[yellow]{report.shadowed_count}[/yellow]")
    summary.add_row("Blocked", f"[red]{report.blocked_count}[/red]")
    console.print(summary)

    if not report.plugins:
        console.print(
            "\n[dim]No plugins discovered.  This is expected for a "
            "stock install; see docs/design/adr-plugin-contract.md for "
            "the contract.[/dim]"
        )
        return

    table = Table(title="Discovered plugins")
    table.add_column("Group", style="cyan")
    table.add_column("Name")
    table.add_column("Target")
    table.add_column("Package")
    table.add_column("Status")
    table.add_column("Reason")
    for p in report.plugins:
        status_color = {
            "LOADED": "green",
            "SHADOWED": "yellow",
            "BLOCKED": "red",
        }.get(p.status, "white")
        dist = p.distribution or "-"
        if p.distribution_version:
            dist = f"{dist} {p.distribution_version}"
        table.add_row(
            p.group,
            p.name,
            p.value,
            dist,
            f"[{status_color}]{p.status}[/{status_color}]",
            p.reason or "-",
        )
    console.print(table)
