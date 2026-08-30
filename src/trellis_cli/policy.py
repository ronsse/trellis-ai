"""Policy commands — list, add, remove, show governance policies."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from trellis.mutate import resolve_policy_path
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis_cli.config import get_data_dir
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE

policy_app = typer.Typer(no_args_is_help=True)
console = Console()


def _get_policy_store() -> PolicyStore:
    """Get the policy store the mutation pipeline will actually read.

    Resolves through :func:`trellis.mutate.resolve_policy_path` rather
    than picking a path locally. This CLI used to write
    ``<data_dir>/policies.json`` while the REST API read
    ``<data_dir>/stores/policies.json`` — two surfaces, two files, and
    (until the gate was wired) nothing read either. Sharing the resolver
    is what guarantees the file this command writes is the file Stage 2
    enforces.
    """
    return PolicyStore(resolve_policy_path(get_data_dir() / "stores"))


def _print_json(obj: object) -> None:
    """Print a JSON-serialisable object as *parseable* JSON.

    ``typer.echo``, not ``console.print``. Rich soft-wraps at the terminal
    width, and a wrap lands inside a long string value — which makes the
    output something ``json.loads`` rejects, breaking the documented
    ``--format json`` contract. It was latent here only because every
    payload happened to be short; adding the policy file's path to
    ``policy list`` was enough to trip it at the default 80 columns. Same
    reasoning as :func:`trellis_cli.output.emit_json`, keeping this
    command group's ``indent=2`` / ``default=str`` rendering.
    """
    typer.echo(json.dumps(obj, indent=2, default=str))


def _render_degradation(degraded: dict[str, Any] | None) -> None:
    """Print the policy store's degraded state, or nothing at all.

    One renderer for every text surface here, so a warning cannot exist in
    ``--format json`` alone and the four commands cannot drift apart.

    **Every interpolated value is escaped.** ``detail`` is arbitrary
    exception text and ``path`` is an arbitrary filesystem path, and Rich
    reads ``[...]`` as markup: an unescaped detail of ``'no "policies" key
    (keys: [...])'`` loses the keys it exists to name, and a path under
    ``/tmp/my [staging] dir/`` turns the recovery line into a command that
    does not run. The recovery command is the entire justification for the
    refusal, so it is the one string that must survive rendering
    byte-for-byte.
    """
    if not degraded:
        return
    console.print(
        f"  [bold red]POLICY STORE DEGRADED[/bold red] — "
        f"{escape(str(degraded['reason']))}: {escape(str(degraded['detail']))}"
    )
    console.print(
        f"    file: [cyan]{escape(str(degraded['path']))}[/cyan] "
        f"({degraded['rows_loaded']} policy/policies readable, "
        f"{escape(str(degraded['rows_skipped_display']))} not)"
    )
    console.print(
        "    Writes are refused so the file is intact. This listing is a "
        "partial view, not the ruleset."
    )
    console.print(
        "    Enforcement reads this file separately and strictly: the "
        "mutation pipeline is failing closed on it."
    )
    console.print(f"    To reset: [bold]{escape(str(degraded['recovery']))}[/bold]")


def _exit_if_degraded(store: PolicyStore, output_format: str) -> None:
    """Stop a *write* command that must not act on a degraded store.

    The store refuses these writes unconditionally — this guard is not what
    makes them safe. It is what turns an unhandled
    ``DegradedStoreWriteError`` traceback (exit 1, "unexpected; file a
    bug") into a rendered refusal carrying the recovery command, at the
    canonical :data:`~trellis_cli.exit_codes.EXIT_STORE` a wrapper can
    branch on.
    """
    degradation = store.degradation
    if degradation is None:
        return
    degraded = degradation.to_dict()
    if output_format == "json":
        _print_json({"status": "degraded", "store_degradation": degraded})
    else:
        _render_degradation(degraded)
    raise typer.Exit(code=EXIT_STORE)


@policy_app.command("list")
def list_policies(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """List all governance policies.

    Works on a damaged file — that is the whole reason the CRUD reader is
    lenient — but says so, and exits
    :data:`~trellis_cli.exit_codes.EXIT_STORE` rather than 0. For an
    access-control question a partial answer presented as a complete one is
    the failure mode; a script verifying governance must not read a
    truncated list as the truth.
    """
    store = _get_policy_store()
    policies = store.list()
    degradation = store.degradation
    degraded = degradation.to_dict() if degradation else None
    # Distinguish the two ways of getting an empty answer. Enforcement
    # deliberately does not (see trellis.mutate.policy_source); here, where
    # a human is asking once, it is cheap and it is the question they mean.
    file_present = store.path.exists()

    if output_format == "json":
        payload: dict[str, Any] = {
            "count": len(policies),
            "policies": [p.model_dump(mode="json") for p in policies],
            "policy_file": str(store.path),
            "policy_file_present": file_present,
        }
        if degraded:
            payload["store_degradation"] = degraded
        _print_json(payload)
        if degraded:
            raise typer.Exit(code=EXIT_STORE)
        return

    # Banner above the listing: an operator who reads the first line and
    # stops must not stop on a reassuring one.
    _render_degradation(degraded)

    if not policies:
        console.print("[dim]No policies configured.[/dim]")
        if not degraded and file_present:
            console.print(
                f"[dim]  {escape(str(store.path))} declares an empty policy "
                "list. Stage 2 is transparent: every mutation is permitted.[/dim]"
            )
        if degraded:
            raise typer.Exit(code=EXIT_STORE)
        return

    table = Table(title="Governance Policies")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Type")
    table.add_column("Scope")
    table.add_column("Enforcement")
    table.add_column("Rules")

    for p in policies:
        scope_str: str = p.scope.level
        if p.scope.value:
            scope_str += f":{p.scope.value}"
        table.add_row(
            p.policy_id[:12] + "…",
            p.policy_type.value,
            scope_str,
            p.enforcement.value,
            str(len(p.rules)),
        )

    console.print(table)
    if degraded:
        raise typer.Exit(code=EXIT_STORE)


@policy_app.command("show")
def show_policy(
    policy_id: str = typer.Argument(..., help="Policy ID (or prefix)"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Show details of a specific policy."""
    store = _get_policy_store()
    degradation = store.degradation
    degraded = degradation.to_dict() if degradation else None
    # Support prefix matching
    match = _find_policy(store, policy_id)
    if match is None:
        # On a degraded store "not found" is not an answer: the row may
        # simply have failed to parse. Saying so — and exiting EXIT_STORE
        # rather than EXIT_INTERNAL — is the difference between "no such
        # policy" and "I could not read your policy file".
        if degraded:
            if output_format == "json":
                _print_json(
                    {
                        "status": "degraded",
                        "message": (
                            f"Policy not found: {policy_id} — but the store "
                            "loaded degraded, so this may mean the entry was "
                            "unreadable rather than absent."
                        ),
                        "store_degradation": degraded,
                    }
                )
            else:
                _render_degradation(degraded)
                console.print(
                    f"[red]Policy not found: {escape(policy_id)}[/red] "
                    "[yellow](the store is degraded — this may mean unreadable, "
                    "not absent)[/yellow]"
                )
            raise typer.Exit(code=EXIT_STORE)
        console.print(f"[red]Policy not found: {escape(policy_id)}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL)

    if output_format == "json":
        payload: dict[str, Any] = match.model_dump(mode="json")
        if degraded:
            payload["store_degradation"] = degraded
        _print_json(payload)
        if degraded:
            raise typer.Exit(code=EXIT_STORE)
        return

    _render_degradation(degraded)

    console.print(f"[bold]Policy:[/bold] {match.policy_id}")
    console.print(f"  Type: {match.policy_type.value}")
    console.print(
        f"  Scope: {match.scope.level}"
        + (f":{match.scope.value}" if match.scope.value else "")
    )
    console.print(f"  Enforcement: {match.enforcement.value}")
    console.print(f"  Rules ({len(match.rules)}):")
    for i, rule in enumerate(match.rules, 1):
        console.print(f"    {i}. [{rule.action}] {rule.operation} — {rule.condition}")
    if degraded:
        raise typer.Exit(code=EXIT_STORE)


@policy_app.command("add")
def add_policy(
    policy_type: str = typer.Option(
        "mutation", "--type", help="Policy type: mutation, access, retention, redaction"
    ),
    scope_level: str = typer.Option(
        "global", "--scope", help="Scope level: global, domain, team, entity_type"
    ),
    scope_value: str = typer.Option(
        None, "--scope-value", help="Scope value (required for non-global scopes)"
    ),
    operation: str = typer.Option(
        ..., "--operation", help="Operation pattern: e.g. entity.create, entity.*, *"
    ),
    action: str = typer.Option(
        "deny", "--action", help="Rule action: allow, deny, require_approval, warn"
    ),
    condition: str = typer.Option(
        "always", "--condition", help="Human-readable condition label"
    ),
    enforcement: str = typer.Option(
        "enforce", "--enforcement", help="Enforcement: enforce, warn, audit_only"
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Add a governance policy with a single rule."""
    store = _get_policy_store()
    _exit_if_degraded(store, output_format)

    policy = Policy(
        policy_type=PolicyType(policy_type),
        scope=PolicyScope(level=scope_level, value=scope_value),  # type: ignore[arg-type]
        rules=[
            PolicyRule(
                operation=operation,
                condition=condition,
                action=action,  # type: ignore[arg-type]
            )
        ],
        enforcement=Enforcement(enforcement),
    )

    store.add(policy)

    if output_format == "json":
        _print_json(
            {"status": "ok", "policy_id": policy.policy_id, "message": "Policy added"},
        )
    else:
        console.print(f"[green]✓ Policy added:[/green] {policy.policy_id}")
        console.print(
            f"  {action} {operation} (scope: {scope_level}"
            + (f":{scope_value}" if scope_value else "")
            + ")"
        )


@policy_app.command("remove")
def remove_policy(
    policy_id: str = typer.Argument(..., help="Policy ID (or prefix) to remove"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Remove a governance policy."""
    store = _get_policy_store()
    _exit_if_degraded(store, output_format)
    match = _find_policy(store, policy_id)
    if match is None:
        if output_format == "json":
            _print_json(
                {"status": "error", "message": f"Policy not found: {policy_id}"}
            )
        else:
            console.print(f"[red]Policy not found: {policy_id}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL)

    store.remove(match.policy_id)

    if output_format == "json":
        _print_json(
            {"status": "ok", "policy_id": match.policy_id, "message": "Policy removed"},
        )
    else:
        console.print(f"[green]✓ Policy removed:[/green] {match.policy_id}")


def _find_policy(store: PolicyStore, policy_id_or_prefix: str) -> Policy | None:
    """Find a policy by exact ID or prefix match."""
    # Exact match first
    exact = store.get(policy_id_or_prefix)
    if exact:
        return exact
    # Prefix match
    for p in store.list():
        if p.policy_id.startswith(policy_id_or_prefix):
            return p
    return None
