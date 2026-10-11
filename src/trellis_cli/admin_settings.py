"""``trellis admin settings set`` / ``unset`` — CLI parity for the governed
settings write (PR 3, ``Operation.SETTINGS_UPDATE``).

Both commands submit the same :class:`~trellis.mutate.commands.Command`
shape the admin-scoped REST route (``trellis_api/routes/settings.py``)
does, through :func:`~trellis.mutate.build_settings_executor` — so a
denying policy, a degraded store, or an out-of-range value is refused
identically on these two surfaces (Stage 2 always runs, CLAUDE.md).
``build_settings_executor`` wires ``Operation.SETTINGS_UPDATE`` into
*no other* executor: MCP's generic ``execute_mutation`` and REST's
generic ``POST /commands/batch`` build :func:`~trellis.mutate.build_curate_executor`
instead, which does not register this operation at all -- those two
surfaces have no admin-scope gate of their own, so neither has a
settings surface to be consistent with.
``unset`` is not a second operation: it submits ``value=None``, which the
handler treats as "remove the override" (see ``SettingsUpdateHandler``'s
own docstring).

Exit codes follow :mod:`trellis_cli.exit_codes`, computed the same way
regardless of ``--format`` (``tests/unit/test_format_exit_parity_rule.py``):

* :data:`EXIT_OK` (0) — success or no-op (e.g. unsetting an already-unset
  name).
* :data:`EXIT_VALIDATION` (2) — unknown name, wrong type, out of range.
* :data:`EXIT_POLICY` (3) — a policy denies ``settings.update``.
* :data:`EXIT_STORE` (5) — the settings file is degraded or stale.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape

from trellis.core.settings_registry import SettingSpec, get_setting, resolve_source
from trellis.core.write_config import WriteBehaviourConfig
from trellis.learning.candidate_auto_promote import (
    resolve_candidate_auto_promote_overrides,
)
from trellis.learning.tuners.auto_promote import resolve_auto_promote_overrides
from trellis.mutate import Command, CommandStatus, Operation, build_settings_executor
from trellis.retrieve.builder_factory import _graph_seeding_enabled
from trellis.retrieve.pack_builder import _filter_legacy_advisories_enabled
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path
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


def _settings_values(registry: StoreRegistry) -> dict[str, Any]:
    path = resolve_settings_path(registry.stores_dir)
    if path is None:
        return {}
    return SettingsStore(path).as_values()


#: Name prefixes of the two auto-promote settings families -- neither is
#: a :class:`WriteBehaviourConfig` field, so :func:`_env_effective_value`
#: resolves them through their own precedence functions below instead of
#: that dataclass's ``as_dict()``.
_AUTO_PROMOTE_PREFIX = "learning.auto_promote."
_CANDIDATE_AUTO_PROMOTE_PREFIX = "learning.candidate_auto_promote."


def _auto_promote_family_value(
    spec: SettingSpec, settings_values: dict[str, Any], *, prefix: str
) -> Any:
    """One field from whichever auto-promote family *prefix* names.

    A sibling knob's unparseable env value degrades this one to its
    registry default rather than raising past a successful write (the
    write itself already validated the value actually being set; a
    *different* knob in the same family having a bad raw env string is
    not this call's business to fail on).
    """
    field = spec.name[len(prefix) :]
    resolve = (
        resolve_auto_promote_overrides
        if prefix == _AUTO_PROMOTE_PREFIX
        else resolve_candidate_auto_promote_overrides
    )
    try:
        return resolve(settings=settings_values)[field]
    except (ValueError, TypeError):
        return spec.default


def _env_effective_value(spec: SettingSpec, settings_values: dict[str, Any]) -> Any:
    """The real environment-governed value for *spec*, env-only knobs included.

    ``graph_seeding``/``filter_legacy_advisories`` have their own
    asymmetric env semantics (see ``trellis_api.routes.settings``'s
    ``_effective_values`` docstring, which this mirrors rather than
    reimplements independently); the two ``learning.auto_promote.*`` /
    ``learning.candidate_auto_promote.*`` families resolve through
    :func:`_auto_promote_family_value`. Every other knob is a
    :class:`WriteBehaviourConfig` field.
    """
    if spec.name == "graph_seeding":
        return _graph_seeding_enabled()
    if spec.name == "filter_legacy_advisories":
        return _filter_legacy_advisories_enabled()
    if spec.name.startswith(_AUTO_PROMOTE_PREFIX):
        return _auto_promote_family_value(
            spec, settings_values, prefix=_AUTO_PROMOTE_PREFIX
        )
    if spec.name.startswith(_CANDIDATE_AUTO_PROMOTE_PREFIX):
        return _auto_promote_family_value(
            spec, settings_values, prefix=_CANDIDATE_AUTO_PROMOTE_PREFIX
        )
    return WriteBehaviourConfig.from_env_and_settings(
        settings=settings_values
    ).as_dict()[spec.name]


def _truthfulness_note(
    spec: SettingSpec, settings_values: dict[str, Any], *, reset: bool
) -> str | None:
    """What a caller would otherwise not learn from the success message alone.

    An env pin or a ``restart_required`` knob both report a plain "set to
    X" success with nothing to say the write has no, or not yet any,
    runtime effect -- this fills in that gap rather than letting the
    caller believe the printed value is already in force.

    ``reset`` (the submitted value was ``None``, i.e. ``unset``) branches
    the env-pinned wording the same way ``trellis_api.routes.settings``'s
    ``update_setting`` does: a reset leaves nothing stored behind the
    pin, so saying "the stored value takes effect once that variable is
    unset" would be false.
    """
    source = resolve_source(spec, settings_values)
    if source == "env":
        effective = _env_effective_value(spec, settings_values)
        if reset:
            return (
                f"{spec.env_var} is set, so it governs {spec.name!r} right "
                f"now (effective value: {effective!r}); the override was "
                "removed, and nothing is stored behind it -- the "
                "environment variable keeps winning once it stays set."
            )
        return (
            f"{spec.env_var} is set, so it governs {spec.name!r} right now "
            f"(effective value: {effective!r}); the stored value takes "
            "effect once that variable is unset."
        )
    if spec.restart_required:
        return (
            f"{spec.name!r} is restart_required: this change applies after "
            "a restart, not to the current process."
        )
    return None


def _execute(cmd: Command, output_format: str) -> None:
    """Submit *cmd*, render the result, and exit non-zero on refusal/failure."""
    registry = _get_registry()
    result = build_settings_executor(registry).execute(cmd)

    note: str | None = None
    spec: SettingSpec | None = None
    if cmd.target_id is not None:
        spec = get_setting(cmd.target_id)
    if result.status == CommandStatus.SUCCESS and spec is not None:
        note = _truthfulness_note(
            spec, _settings_values(registry), reset=cmd.args.get("value") is None
        )

    if output_format == "json":
        payload: dict[str, Any] = {
            "status": result.status.value,
            "command_id": result.command_id,
            "operation": result.operation,
            "message": result.message,
            "created_id": result.created_id,
            "warnings": list(result.warnings),
        }
        if spec is not None:
            payload["settings_live"] = spec.settings_live
            payload["restart_required"] = spec.restart_required
        if note is not None:
            payload["note"] = note
        emit_json(payload)
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
        if note is not None:
            console.print(
                f"  Note: {note}", markup=False, highlight=False, soft_wrap=True
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
