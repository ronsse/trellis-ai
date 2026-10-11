"""``trellis admin settings set`` / ``unset`` — CLI parity for the governed
settings write (PR 3, ``Operation.SETTINGS_UPDATE``).

Both commands submit the same :class:`~trellis.mutate.commands.Command`
the REST route (``trellis_api/routes/settings.py``) and the MCP surface
do, through :func:`~trellis.mutate.build_curate_executor` — so a denying
policy, a degraded store, or an out-of-range value is refused identically
on every surface (Stage 2 always runs, CLAUDE.md). ``unset`` is not a
second operation: it submits ``value=None``, which the handler treats as
"remove the override" (see ``SettingsUpdateHandler``'s own docstring).

Exit codes follow :mod:`trellis_cli.exit_codes`, computed the same way
regardless of ``--format`` (``tests/unit/test_format_exit_parity_rule.py``):

* :data:`EXIT_OK` (0) — success or no-op (e.g. unsetting an already-unset
  name).
* :data:`EXIT_VALIDATION` (2) — unknown name, wrong type, out of range.
* :data:`EXIT_POLICY` (3) — a policy denies ``settings.update``.
* :data:`EXIT_STORE` (5) — the settings file is degraded or stale.
"""

from __future__ import annotations

import typer
from rich.markup import escape

from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis_cli.exit_codes import refusal_exit_code
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

settings_app = typer.Typer(
    no_args_is_help=True,
    help="Read and write operator-tunable settings (governed).",
)
console = build_console()


# ---------------------------------------------------------------------------
# Shared executor (mirrors trellis_cli.curate._execute_command)
# ---------------------------------------------------------------------------


def _execute(cmd: Command, output_format: str) -> None:
    """Submit *cmd*, render the result, and exit non-zero on refusal/failure."""
    result = build_curate_executor(_get_registry()).execute(cmd)

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "operation": result.operation,
                "message": result.message,
                "created_id": result.created_id,
                "warnings": list(result.warnings),
            }
        )
    else:
        if result.status == CommandStatus.SUCCESS:
            console.print(f"[green]✓ Command executed[/green]: {result.operation}")
        else:
            console.print(
                f"[red]✗ Command {result.status}[/red]: {result.operation}",
                soft_wrap=True,
            )
        console.print(f"  ID: {escape(result.command_id)}")
        console.print(
            f"  Message: {result.message}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    # Below the format branch, so both formats exit alike.
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise typer.Exit(code=refusal_exit_code(result))


# ---------------------------------------------------------------------------
# Command bodies (programmatic entry points, pulled out for testability)
# ---------------------------------------------------------------------------


def set_setting_command(
    *, name: str, value: str, requested_by: str, output_format: str
) -> None:
    """CLI body for ``settings set``."""
    cmd = Command(
        operation=Operation.SETTINGS_UPDATE,
        args={"name": name, "value": value},
        target_id=name,
        target_type="setting",
        requested_by=requested_by,
    )
    _execute(cmd, output_format)


def unset_setting_command(*, name: str, requested_by: str, output_format: str) -> None:
    """CLI body for ``settings unset``.

    Submits ``value=None`` — remove the override, per item 1's decision.
    """
    cmd = Command(
        operation=Operation.SETTINGS_UPDATE,
        args={"name": name, "value": None},
        target_id=name,
        target_type="setting",
        requested_by=requested_by,
    )
    _execute(cmd, output_format)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@settings_app.command("set")
def set_command(  # pragma: no cover — Typer wrapper only
    name: str = typer.Argument(..., help="Registered setting name (see the REST GET)."),
    value: str = typer.Argument(
        ..., help="New value. Coerced against the setting's declared type."
    ),
    requested_by: str = typer.Option(
        "cli:settings-set", "--by", help="Audit trail: who/what requested this write."
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Set one setting's override. Refused — not clamped — if invalid."""
    set_setting_command(
        name=name, value=value, requested_by=requested_by, output_format=output_format
    )


@settings_app.command("unset")
def unset_command(  # pragma: no cover — Typer wrapper only
    name: str = typer.Argument(
        ..., help="Registered setting name to remove the override for."
    ),
    requested_by: str = typer.Option(
        "cli:settings-unset", "--by", help="Audit trail: who/what requested this write."
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Remove a setting's override, reverting to env or the shipped default."""
    unset_setting_command(
        name=name, requested_by=requested_by, output_format=output_format
    )


def register(admin_app: typer.Typer) -> None:
    """Mount the ``settings`` sub-app onto the ``admin`` Typer app."""
    admin_app.add_typer(settings_app, name="settings")


__all__ = [
    "register",
    "set_setting_command",
    "unset_setting_command",
]
