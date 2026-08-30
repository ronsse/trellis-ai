"""Policy commands — list, add, remove, show governance policies."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from trellis.mutate import resolve_policy_path
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis_cli.config import get_data_dir
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.output import emit_json

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


def _policy_not_found(policy_id: str) -> dict[str, str]:
    """The not-found error envelope, shaped like ``sanitized_error_payload``.

    ``error_type`` is present so a consumer can branch on it the same way it
    does for every other JSON error surface in the CLI; ``show`` and
    ``remove`` share the helper so the two cannot drift apart again — they
    already had, with ``show`` emitting Rich prose on stdout under
    ``--format json`` while ``remove`` emitted JSON (#403).
    """
    return {
        "status": "error",
        "error_type": "PolicyNotFound",
        "message": f"Policy not found: {policy_id}",
    }


def _print_json(obj: object) -> None:
    """Emit a pretty-printed, ``str``-coerced JSON object to stdout.

    Goes through ``emit_json``, so it never reaches Rich — the
    ``highlight=False`` this used to pass became meaningless when the
    Rich path was removed (#403).
    """
    emit_json(obj, indent=2, default=str)


@policy_app.command("list")
def list_policies(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """List all governance policies."""
    store = _get_policy_store()
    policies = store.list()

    if output_format == "json":
        _print_json(
            {
                "count": len(policies),
                "policies": [p.model_dump(mode="json") for p in policies],
            },
        )
        return

    if not policies:
        console.print("[dim]No policies configured.[/dim]")
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


@policy_app.command("show")
def show_policy(
    policy_id: str = typer.Argument(..., help="Policy ID (or prefix)"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Show details of a specific policy."""
    store = _get_policy_store()
    # Support prefix matching
    match = _find_policy(store, policy_id)
    if match is None:
        if output_format == "json":
            _print_json(_policy_not_found(policy_id))
        else:
            console.print(f"[red]Policy not found: {policy_id}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL)

    if output_format == "json":
        _print_json(match.model_dump(mode="json"))
        return

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
    match = _find_policy(store, policy_id)
    if match is None:
        if output_format == "json":
            _print_json(_policy_not_found(policy_id))
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
