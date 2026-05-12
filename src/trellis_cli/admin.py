"""Admin commands for Trellis CLI."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter, defaultdict
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
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
            # Plain ``print`` (not ``console.print``) so Rich's terminal-
            # width soft-wrap never splits the JSON across lines — long
            # config paths can push the payload past 80 chars.
            print(json.dumps({"status": "exists", "config_dir": str(config_dir)}))
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
        # Plain ``print`` (not ``console.print``) so Rich's terminal-
        # width soft-wrap never splits the JSON across lines. Machine
        # consumers expect single-line JSON per the project rule in
        # CLAUDE.md (`parse JSON output, not human-readable text`).
        print(json.dumps(checks))
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

    info: dict[str, Any] = {
        "api_major": API_MAJOR,
        "api_minor": API_MINOR,
        "api_version": api_version_string(),
        "wire_schema": WIRE_SCHEMA,
        "sdk_min": SDK_MIN,
        "package_version": get_version(),
        "mcp_tools_version": MCP_TOOLS_VERSION,
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
    console.print(table)


@admin_app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        help=(
            "Bind address. Loopback by default; set TRELLIS_API_HOST=0.0.0.0 "
            "or pass --host 0.0.0.0 for container deployments."
        ),
    ),
    port: int = typer.Option(8420, help="Port to bind"),
) -> None:
    """Start the Trellis REST API server."""
    try:
        import uvicorn  # noqa: PLC0415

        from trellis_api.app import create_app  # noqa: PLC0415
    except ImportError:
        console.print(
            "[red]FastAPI/uvicorn not installed."
            " Install with: pip install fastapi uvicorn[/red]"
        )
        raise typer.Exit(code=1)  # noqa: B904

    console.print(f"[green]Starting Trellis API server on {host}:{port}[/green]")
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
        # Plain ``print`` (not ``console.print``) so Rich's terminal-
        # width soft-wrap never splits the JSON across lines. Machine
        # consumers expect single-line JSON per the project rule in
        # CLAUDE.md (`parse JSON output, not human-readable text`).
        print(json.dumps({"status": "ok", **counts}))
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
    alias_resolver_ok = registry.knowledge.graph_store is not None
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
                    " to ~/.trellis/config.yaml."
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
            f"  [green]OK[/green] configurable from ~/.trellis/config.yaml"
            f" (provider={provider}, model={model})"
        )
    else:
        console.print(
            "  [red]MISSING[/red] not configurable from ~/.trellis/config.yaml"
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


def _load_graph_store_from_yaml(path: Path) -> Any:
    """Build a ``GraphStore`` from a single-block YAML config file.

    The file must contain a ``graph:`` block with ``backend`` plus
    backend-specific kwargs (``uri``, ``user``, ``password`` for Neo4j;
    ``dsn`` for Postgres; ``db_path`` for SQLite). Used by
    ``migrate-graph`` so the source and destination can be specified
    independently of the operator's main config.
    """
    import yaml  # noqa: PLC0415

    from trellis.stores.registry import StoreRegistry  # noqa: PLC0415

    if not path.exists():
        console.print(f"[red]Config file not found: {path}[/red]")
        raise typer.Exit(code=1)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        console.print(f"[red]Invalid YAML in {path}: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    graph_block = data.get("graph")
    if not isinstance(graph_block, dict) or "backend" not in graph_block:
        console.print(
            f"[red]{path} must contain a 'graph:' block with a 'backend' key[/red]"
        )
        raise typer.Exit(code=1)

    config: dict[str, Any] = {"graph": graph_block}
    # SQLite backend needs stores_dir for default db_path resolution.
    stores_dir: Path | None = None
    if graph_block.get("backend") == "sqlite" and "db_path" not in graph_block:
        stores_dir = path.parent / "migrate-stores"
    registry = StoreRegistry(config=config, stores_dir=stores_dir)
    return registry, registry.knowledge.graph_store


@admin_app.command("migrate-graph")
def migrate_graph(
    from_config: Path = typer.Option(  # noqa: B008
        ...,
        "--from-config",
        "-f",
        help="YAML file with a single 'graph:' block describing the source store.",
    ),
    to_config: Path = typer.Option(  # noqa: B008
        ...,
        "--to-config",
        "-t",
        help="YAML file with a single 'graph:' block describing the destination store.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Walk and count, don't write to the destination."
    ),
    max_nodes: int = typer.Option(
        100_000,
        "--max-nodes",
        help="Safety cap. Source graphs above this need paginated iteration.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Copy current graph data (nodes + edges + aliases) between backends.

    POC scope: current versions only, in-memory snapshot up to
    ``--max-nodes``. Idempotent on retry — a destination that already
    has a row with the same ``node_id`` (or alias source/raw_id) is
    skipped, not overwritten.

    Both ``--from-config`` and ``--to-config`` accept a YAML file with
    a single ``graph:`` block matching the shape used in
    ``docs/deployment/recommended-config.yaml``. Example for migrating
    SQLite (default install) to a Neo4j AuraDB instance::

        # /tmp/from.yaml
        graph:
          backend: sqlite
          db_path: ~/.trellis/data/stores/graph.db

        # /tmp/to.yaml
        graph:
          backend: neo4j
          uri: neo4j+s://abcd1234.databases.neo4j.io
          user: abcd1234
          password: <from console>
          database: abcd1234

        trellis admin migrate-graph -f /tmp/from.yaml -t /tmp/to.yaml --dry-run
    """
    from trellis.migrate import (  # noqa: PLC0415
        GraphMigrator,
        MigrationCapacityExceededError,
    )

    src_registry, src_store = _load_graph_store_from_yaml(from_config)
    dst_registry, dst_store = _load_graph_store_from_yaml(to_config)

    try:
        migrator = GraphMigrator(src_store, dst_store, max_nodes=max_nodes)
        try:
            report = migrator.run(dry_run=dry_run)
        except MigrationCapacityExceededError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        # Close in dest-first order so the source connection survives if
        # the dest close blows up — diagnostics may need to re-read source.
        dst_registry.close()
        src_registry.close()

    if output_format == "json":
        from dataclasses import asdict  # noqa: PLC0415

        # Errors are list[tuple] which json doesn't serialize directly.
        payload = asdict(report)
        payload["errors"] = [
            {"target": target, "message": msg} for target, msg in payload["errors"]
        ]
        console.print(json.dumps(payload, indent=2))
    else:
        if report.dry_run:
            console.print(f"[yellow]{report.summary()}[/yellow]")
        else:
            console.print(f"[green]{report.summary()}[/green]")
        if report.errors:
            console.print()
            console.print("[red]Errors:[/red]")
            for target, msg in report.errors:
                console.print(f"  [red]{target}[/red]: {msg}")
            raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# smoke-test — operationalises the validation checklist in
# docs/deployment/runbook.md "Validating a deployment". Run after a
# fresh deploy or before sending real traffic; exits non-zero on any
# fail so a CI / k8s init-container hook can gate on it.
# ---------------------------------------------------------------------------


_SMOKE_AUTH_PROBE_PATH = "/api/v1/advisories"


def _resolve_smoke_url() -> str:
    host = os.environ.get("TRELLIS_API_HOST", "127.0.0.1")
    port = os.environ.get("TRELLIS_API_PORT", "8420")
    return f"http://{host}:{port}"


def _record_check(
    name: str, status: str, started_ns: int, **extra: Any
) -> dict[str, Any]:
    latency_ms = round((time.monotonic_ns() - started_ns) / 1_000_000, 2)
    return {"name": name, "status": status, "latency_ms": latency_ms, **extra}


def _check_healthz(client: httpx.Client) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        response = client.get("/healthz")
    except httpx.HTTPError as exc:
        return _record_check("healthz", "fail", started, error=str(exc))
    if response.status_code != HTTPStatus.OK:
        return _record_check(
            "healthz",
            "fail",
            started,
            error=f"expected 200, got {response.status_code}",
        )
    return _record_check("healthz", "pass", started)


def _check_readyz(client: httpx.Client) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        response = client.get("/readyz")
    except httpx.HTTPError as exc:
        return _record_check("readyz", "fail", started, error=str(exc))
    body = _safe_json(response)
    backends = body.get("backends") if isinstance(body, dict) else None
    if response.status_code != HTTPStatus.OK:
        return _record_check(
            "readyz",
            "fail",
            started,
            error=f"expected 200, got {response.status_code}",
            backends=backends,
        )
    return _record_check("readyz", "pass", started, backends=backends)


def _check_auth_rejects_missing(client: httpx.Client) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        response = client.get(_SMOKE_AUTH_PROBE_PATH)
    except httpx.HTTPError as exc:
        return _record_check("auth_rejects_missing", "fail", started, error=str(exc))
    if response.status_code != HTTPStatus.UNAUTHORIZED:
        return _record_check(
            "auth_rejects_missing",
            "fail",
            started,
            error=f"expected 401, got {response.status_code}",
        )
    return _record_check("auth_rejects_missing", "pass", started)


def _check_auth_accepts_valid(client: httpx.Client, api_key: str) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        response = client.get(_SMOKE_AUTH_PROBE_PATH, headers={"X-API-Key": api_key})
    except httpx.HTTPError as exc:
        return _record_check("auth_accepts_valid", "fail", started, error=str(exc))
    if response.status_code != HTTPStatus.OK:
        return _record_check(
            "auth_accepts_valid",
            "fail",
            started,
            error=f"expected 200, got {response.status_code}",
        )
    return _record_check("auth_accepts_valid", "pass", started)


def _check_metrics(client: httpx.Client) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        response = client.get("/metrics")
    except httpx.HTTPError as exc:
        return _record_check("metrics", "fail", started, error=str(exc))
    # 404 means the [observability] extra isn't installed — that's a
    # legitimate deploy choice, not a smoke-test failure. Treat as info.
    if response.status_code == HTTPStatus.NOT_FOUND:
        return _record_check(
            "metrics",
            "info",
            started,
            note="not wired (install trellis-ai[observability] to enable)",
        )
    if response.status_code != HTTPStatus.OK:
        return _record_check(
            "metrics",
            "fail",
            started,
            error=f"expected 200, got {response.status_code}",
        )
    # Prometheus exposition format starts every metric with a # HELP
    # comment; bail if the body doesn't look like one.
    if "# HELP" not in response.text[:4096]:
        return _record_check(
            "metrics",
            "fail",
            started,
            error="response is not Prometheus format (no '# HELP' line in first 4KB)",
        )
    return _record_check("metrics", "pass", started)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _summarize(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(c["status"] for c in checks)
    return {
        "pass": counts.get("pass", 0),
        "fail": counts.get("fail", 0),
        "info": counts.get("info", 0),
        "skip": counts.get("skip", 0),
    }


def _render_smoke_text(
    base_url: str, checks: list[dict[str, Any]], summary: dict[str, int]
) -> None:
    style_for = {
        "pass": "green",
        "fail": "red",
        "info": "yellow",
        "skip": "dim",
    }
    label_for = {
        "pass": "PASS",
        "fail": "FAIL",
        "info": "INFO",
        "skip": "SKIP",
    }
    console.print(f"[bold]Trellis API smoke test[/bold] → {base_url}")
    console.print()
    for check in checks:
        status = check["status"]
        style = style_for.get(status, "white")
        label = label_for.get(status, status.upper())
        line = f"  [{style}]{label}[/{style}]  {check['name']:<24}"
        if "latency_ms" in check:
            line += f"  ({check['latency_ms']}ms)"
        console.print(line)
        if check.get("error"):
            console.print(f"        [red]{check['error']}[/red]")
        if check.get("note"):
            console.print(f"        [dim]{check['note']}[/dim]")
        if check.get("reason"):
            console.print(f"        [dim]{check['reason']}[/dim]")
        if check["name"] == "readyz" and check.get("backends"):
            for backend, info in check["backends"].items():
                if not isinstance(info, dict):
                    continue
                b_status = info.get("status", "unknown")
                b_latency = info.get("latency_ms")
                b_style = "green" if b_status == "ok" else "red"
                detail = f"{b_status}"
                if b_latency is not None:
                    detail += f" ({b_latency}ms)"
                console.print(f"        [{b_style}]{backend}[/{b_style}]: {detail}")
                if info.get("error"):
                    console.print(f"          [red]{info['error']}[/red]")
    console.print()
    total = sum(summary.values())
    console.print(
        f"{total} checks · "
        f"[green]{summary['pass']} pass[/green] · "
        f"[yellow]{summary['info']} info[/yellow] · "
        f"[dim]{summary['skip']} skip[/dim] · "
        f"[red]{summary['fail']} fail[/red]"
    )


@admin_app.command("smoke-test")
def smoke_test(
    url: str = typer.Option(
        None,
        "--url",
        help=(
            "Base URL of the API. Defaults to "
            "http://$TRELLIS_API_HOST:$TRELLIS_API_PORT or "
            "http://127.0.0.1:8420 if those env vars are unset."
        ),
    ),
    api_key: str = typer.Option(
        None,
        "--api-key",
        help=(
            "X-API-Key value. Defaults to $TRELLIS_API_KEY. When neither "
            "is set, the auth checks skip rather than fail — useful for "
            "smoke-testing a dev instance running without auth."
        ),
    ),
    timeout: float = typer.Option(
        10.0, "--timeout", help="Per-request timeout in seconds."
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Validate a running Trellis deployment end-to-end.

    Hits ``/healthz``, ``/readyz``, an authenticated ``/api/v1`` route
    with and without the API key, and ``/metrics``. Exits 0 when every
    required check passes; 1 when any check fails. Operationalises the
    "Validating a deployment" checklist in
    ``docs/deployment/runbook.md``.
    """
    base_url = url or _resolve_smoke_url()
    key = api_key if api_key is not None else os.environ.get("TRELLIS_API_KEY")

    checks: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        checks.append(_check_healthz(client))
        checks.append(_check_readyz(client))
        if key:
            checks.append(_check_auth_rejects_missing(client))
            checks.append(_check_auth_accepts_valid(client, key))
        else:
            checks.append(
                {
                    "name": "auth_rejects_missing",
                    "status": "skip",
                    "reason": "no API key configured (TRELLIS_API_KEY unset)",
                }
            )
            checks.append(
                {
                    "name": "auth_accepts_valid",
                    "status": "skip",
                    "reason": "no API key configured (TRELLIS_API_KEY unset)",
                }
            )
        checks.append(_check_metrics(client))

    summary = _summarize(checks)
    ok = summary["fail"] == 0

    if output_format == "json":
        print(
            json.dumps(
                {
                    "url": base_url,
                    "checks": checks,
                    "summary": summary,
                    "ok": ok,
                },
                indent=2,
            )
        )
    else:
        _render_smoke_text(base_url, checks, summary)

    if not ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# draft-promotion-adr — self-improvement item 5, ADR scaffold for a
# WELL_KNOWN_CANDIDATE. Reads the most recent surfaced candidate from
# the EventLog, renders the markdown template, and writes the ADR.
# ---------------------------------------------------------------------------


_PROMOTION_ADR_TEMPLATE_NAME = "promotion_adr.md"


def _load_promotion_adr_template() -> str:
    """Read the promotion-ADR template from package data.

    Templates use :meth:`str.format` (no Jinja). Reading from package
    data keeps the file alongside the source so it ships with the
    wheel — no separate distribution concern.
    """
    from importlib.resources import files  # noqa: PLC0415

    return (files("trellis.templates") / _PROMOTION_ADR_TEMPLATE_NAME).read_text(
        encoding="utf-8"
    )


def _lookup_candidate_payload(event_log: Any, candidate_id: str) -> dict[str, Any]:
    """Return the most recent ``WELL_KNOWN_CANDIDATE`` payload for ``candidate_id``.

    Raises :class:`typer.Exit` with a clear error message when the id
    doesn't match any emitted event — the operator must run ``trellis
    analyze schema-evolution`` first.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    events = event_log.get_events(
        event_type=EventType.WELL_KNOWN_CANDIDATE,
        limit=1000,
        order="desc",
    )
    for event in events:
        if event.payload.get("candidate_id") == candidate_id:
            payload = dict(event.payload)
            payload["_event_recorded_at"] = event.recorded_at.isoformat()
            return payload
    msg = (
        f"No WELL_KNOWN_CANDIDATE event found with candidate_id="
        f"{candidate_id!r}. Run 'trellis analyze schema-evolution' first."
    )
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(code=1)


def _render_promotion_adr(  # noqa: PLR0913
    *,
    candidate: dict[str, Any],
    canonical_name_override: str | None,
    drafted_date: str,
    count_threshold: int,
    distinct_extractors_threshold: int,
    distinct_domains_threshold: int,
    min_signal_quality_threshold: str,
    window_days_threshold: int,
) -> str:
    """Substitute the candidate payload into the markdown template.

    The template uses ``str.format``-style placeholders; everything
    that varies per-candidate is computed here so the template itself
    stays a near-pure scaffold.
    """
    canonical_name = (
        canonical_name_override
        if canonical_name_override
        else candidate.get("suggested_canonical_name", "")
    )

    # Re-run collision detection against the (possibly overridden)
    # canonical name so the rendered ADR carries the correct warning.
    from trellis.learning.schema_evolution import (  # noqa: PLC0415
        _detect_naming_collision,
    )

    kind: str = candidate.get("candidate_kind", "entity_type")
    naming_collision = _detect_naming_collision(canonical_name, kind)  # type: ignore[arg-type]
    if naming_collision:
        msg = (
            f"Suggested canonical name {canonical_name!r} collides with an "
            f"existing canonical or alias in trellis.schemas.well_known. "
            "Rename via --canonical-name <new_name> or alias explicitly "
            "rather than overwriting."
        )
        raise typer.BadParameter(msg)

    kind_label = "entity type" if kind == "entity_type" else "edge kind"
    kind_upper = "ENTITY_TYPE" if kind == "entity_type" else "EDGE_KIND"
    well_known_constant_name = _well_known_constant_name(canonical_name, kind)  # type: ignore[arg-type]

    first_seen = str(candidate.get("first_seen", ""))
    last_seen = str(candidate.get("last_seen", ""))
    evidence_span_days = _evidence_span_days(first_seen, last_seen)

    extractors = candidate.get("distinct_extractors") or []
    domains = candidate.get("distinct_domains") or []
    extractors_block = "\n".join(f"- `{e}`" for e in extractors) or "- _none recorded_"
    domains_block = "\n".join(f"- `{d}`" for d in domains) or "- _none recorded_"

    alignment_uri = candidate.get("suggested_alignment_uri")
    alignment_uri_label = (
        f"`{alignment_uri}` _(advisory; verify the URI resolves to a real "
        "published schema before accepting)_"
        if alignment_uri
        else "_(none suggested — pick one only if it corresponds to a real "
        "schema.org / PROV-O term)_"
    )
    if alignment_uri:
        alignment_diff_block = (
            "Add to `_ENTITY_SCHEMA_ALIGNMENT` (or `_EDGE_SCHEMA_ALIGNMENT`):\n"
            "\n```python\n"
            f"{well_known_constant_name}: \"{alignment_uri}\",\n"
            "```"
        )
    else:
        alignment_diff_block = (
            "_No alignment URI suggested. Omit the `_*_SCHEMA_ALIGNMENT` entry "
            "unless the ADR author identifies a real published schema URI._"
        )

    # `naming_collision` is always False here because we reject above on
    # collision; keep the placeholder for template stability.
    naming_collision_block = ""

    return _load_promotion_adr_template().format(
        candidate_id=candidate.get("candidate_id", ""),
        candidate_kind=kind,
        candidate_kind_label=kind_label,
        candidate_kind_upper=kind_upper,
        open_string_value=candidate.get("open_string_value", ""),
        count=candidate.get("count", 0),
        count_threshold=count_threshold,
        distinct_extractors_count=len(extractors),
        distinct_extractors_threshold=distinct_extractors_threshold,
        distinct_extractors_block=extractors_block,
        distinct_domains_count=len(domains),
        distinct_domains_threshold=distinct_domains_threshold,
        distinct_domains_block=domains_block,
        avg_signal_quality=candidate.get("avg_signal_quality", ""),
        min_signal_quality_threshold=min_signal_quality_threshold,
        evidence_window_days_observed=evidence_span_days,
        window_days_threshold=window_days_threshold,
        first_seen=first_seen,
        last_seen=last_seen,
        recurrence_count=candidate.get("recurrence_count", 0),
        suggested_canonical_name=canonical_name,
        well_known_constant_name=well_known_constant_name,
        alignment_uri_label=alignment_uri_label,
        alignment_diff_block=alignment_diff_block,
        naming_collision_block=naming_collision_block,
        drafted_date=drafted_date,
    )


def _well_known_constant_name(canonical_name: str, kind: str) -> str:
    """Generate the ``UPPER_SNAKE_CASE`` constant name for ``well_known.py``.

    Mirrors the convention used by existing canonicals (``PERSON``,
    ``WAS_GENERATED_BY``, ``ATTACHED_TO``). camelCase and PascalCase
    both split on capital-letter boundaries.
    """
    import re  # noqa: PLC0415

    tokens = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", canonical_name)
    if not tokens:
        return canonical_name.upper().replace("-", "_").replace(" ", "_")
    return "_".join(t.upper() for t in tokens)


def _evidence_span_days(first_seen: str, last_seen: str) -> int:
    """Compute the day-count between two ISO-format timestamps.

    Returns ``0`` on any parsing failure — the rendered ADR shows
    ``0 day(s)`` and the ADR author can correct manually. Failing
    silently is the right shape here because the timestamps are
    informational, not load-bearing.
    """
    from datetime import datetime  # noqa: PLC0415

    try:
        f = datetime.fromisoformat(first_seen)
        l_ = datetime.fromisoformat(last_seen)
    except (TypeError, ValueError):
        return 0
    delta = l_ - f
    return max(delta.days, 0)


@admin_app.command("draft-promotion-adr")
def draft_promotion_adr(
    candidate_id: str = typer.Argument(
        ..., help="The candidate_id from a WELL_KNOWN_CANDIDATE event."
    ),
    output: Path = typer.Option(  # noqa: B008 — Typer option default
        None,
        "--output",
        "-o",
        help=(
            "Output markdown path. Defaults to "
            "docs/design/adr-promote-<open-string>.md."
        ),
    ),
    canonical_name: str = typer.Option(
        None,
        "--canonical-name",
        help=(
            "Override the analyzer's suggested canonical name. "
            "Useful when the heuristic guess is wrong."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite an existing ADR file. Emits a WARN log line "
            "noting the prior content was discarded."
        ),
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """Draft a promotion ADR for an open-string type that crossed thresholds.

    Reads the most recent ``WELL_KNOWN_CANDIDATE`` event with the
    given ``candidate_id``, renders the markdown template
    (``src/trellis/templates/promotion_adr.md``), and writes the ADR
    to ``--output``. The promotion ADR is the human-gated step in the
    well-known promotion loop — the file produced here is a *draft*,
    not a decision; the ADR author fills in the "Decision" section
    before requesting review.

    Refuses to overwrite an existing file unless ``--force`` is set.
    Raises if the (possibly overridden) canonical name collides with
    an existing canonical or alias in
    :mod:`trellis.schemas.well_known` — the ADR amendment cannot
    silently rename through collisions.

    See ``docs/design/adr-well-known-promotion-loop.md``.
    """
    import logging  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    from trellis.learning.schema_evolution import (  # noqa: PLC0415
        RECOMMENDED_SEED_VALUES,
    )

    event_log = get_event_log()
    candidate = _lookup_candidate_payload(event_log, candidate_id)

    # Default output path: docs/design/adr-promote-<safe-name>.md where
    # safe-name is the open-string value lowercased + underscored. ADR
    # filenames in the repo follow lowercase-hyphenated convention; we
    # use underscores in the file slug to avoid masking the open string
    # with hyphenation noise (``"my-edge-kind"`` -> ``"my_edge_kind"``).
    open_string = candidate.get("open_string_value", "unknown")
    slug = "".join(c if c.isalnum() else "_" for c in str(open_string)).strip("_").lower()
    output_path = (
        output if output is not None else Path("docs/design") / f"adr-promote-{slug}.md"
    )

    if output_path.exists() and not force:
        msg = (
            f"Refusing to overwrite existing ADR at {output_path}. "
            "Pass --force to overwrite (the prior content will be replaced)."
        )
        console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)

    if output_path.exists() and force:
        # Loud-on-misuse: an operator overwriting a previously-drafted
        # ADR is doing something destructive; surface it as a WARN.
        log = logging.getLogger("trellis.admin")
        log.warning(
            "draft_promotion_adr.overwriting_existing_file",
            extra={
                "path": str(output_path),
                "candidate_id": candidate_id,
            },
        )
        console.print(
            f"[yellow]Overwriting existing file at {output_path}.[/yellow]"
        )

    drafted_date = datetime.now(tz=UTC).date().isoformat()
    rendered = _render_promotion_adr(
        candidate=candidate,
        canonical_name_override=canonical_name,
        drafted_date=drafted_date,
        count_threshold=int(RECOMMENDED_SEED_VALUES["well_known_count_threshold"]),
        distinct_extractors_threshold=int(
            RECOMMENDED_SEED_VALUES["well_known_distinct_extractors"]
        ),
        distinct_domains_threshold=int(
            RECOMMENDED_SEED_VALUES["well_known_distinct_domains"]
        ),
        min_signal_quality_threshold=str(
            RECOMMENDED_SEED_VALUES["well_known_min_signal_quality"]
        ),
        window_days_threshold=int(
            RECOMMENDED_SEED_VALUES["well_known_window_days"]
        ),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")

    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "candidate_id": candidate_id,
                    "output_path": str(output_path),
                    "bytes_written": len(rendered.encode("utf-8")),
                }
            )
        )
    else:
        console.print(
            f"[green]Drafted promotion ADR for candidate {candidate_id} ->"
            f" {output_path}[/green]"
        )
        console.print(
            "[dim]Fill in the 'Decision' section before requesting review.[/dim]"
        )
