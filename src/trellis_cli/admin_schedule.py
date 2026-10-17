"""``trellis admin schedule set`` / ``run`` — CLI parity for the admin REST surface.

Mirrors ``trellis_api.routes.admin``'s ``patch_admin_job`` / ``run_admin_job``:
the same writable fields (``enabled``, ``cadence``, ``timeout_seconds`` via
``set``; ``run_requested_at`` via ``run``), the same catalog-name
validation, the same refusal when the schedule store is degraded or
stale, and the same audit events (``SCHEDULE_UPDATED`` /
``JOB_RUN_REQUESTED``). Both surfaces build a
:class:`~trellis.mutate.commands.Command` and execute it through
:func:`trellis.mutate.build_schedule_executor` — the only code that
calls :meth:`ScheduleStore.put` is
:mod:`trellis.mutate.schedule_handlers`, never this module directly.
This module exists only because a host operator needs a terminal
command, not a browser, to flip a job's cadence.

``set`` tracks which options were actually passed on the command line
(via ``ctx.get_parameter_source``, compared by ``.name`` rather than by
identity against ``click.ParameterSource`` — Typer calls this callback
through its own vendored click fork, whose ``ParameterSource`` enum is a
distinct class from the top-level ``click`` package's, so an identity
comparison silently reads every option as "not passed"), the CLI's
equivalent of the REST body's ``model_fields_set`` — so omitting
``--cadence`` leaves cadence unchanged rather than reading as "clear
it", and passing ``--cadence none`` is how an operator clears it to
manual-only (the ``ScheduledJob.cadence`` schema already treats
``None`` as that state).
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.markup import escape

from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
    build_schedule_executor,
)
from trellis.mutate.commands import STORE_REFUSAL_REASONS
from trellis.schedule.catalog import JOB_CATALOG
from trellis.schemas.schedule import ScheduledJob
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry, get_schedule_store

console = build_console()

schedule_app = typer.Typer(
    no_args_is_help=True,
    help="Edit a catalog job's schedule, or trigger an out-of-band run.",
)

#: Case-insensitive sentinel for "clear cadence" on ``--cadence``, matching
#: the REST body's explicit ``"cadence": null`` (there is no null on a
#: command line, so this is its CLI spelling).
_CLEAR_CADENCE_SENTINEL = "none"


def _audit_identity() -> dict[str, str | None]:
    """CLI-actor identity stamped on a schedule audit event.

    A local CLI invocation carries no authenticated credential — the
    REST route stamps ``key_id``/``key_name`` from the caller's API key;
    this stamps the OS user instead, so an audit row is attributable
    either way and a reader never has to guess which surface wrote it.
    """
    import getpass  # noqa: PLC0415

    try:
        user: str | None = getpass.getuser()
    except Exception:  # pragma: no cover - exotic/no-passwd environments
        user = None
    return {"key_id": None, "key_name": f"cli:{user}" if user else "cli"}


def _cli_result_failure(result: CommandResult) -> tuple[int, str, str]:
    """Map a non-SUCCESS schedule-admin ``CommandResult`` to (exit_code, code, message).

    Deliberately **not** :func:`trellis_cli.exit_codes.refusal_exit_code`:
    that shared helper maps every non-policy ``REJECTED`` reason to
    ``EXIT_VALIDATION`` and has no store-refusal case at all, so a
    degraded/stale store would read as a validation failure rather than
    ``EXIT_STORE`` — the same split
    ``trellis_api.routes.admin._schedule_result_http_error`` makes for
    409 vs. 422. The outward ``code`` for every other ``REJECTED`` reason
    is the literal ``"validation_error"`` regardless of the handler's own
    ``rejection_reason`` (``invalid_field``, ``unknown_job``,
    ``schedule_validation``, ...): the CLI's pre-existing contract names
    one code for "the request you typed was bad" and a store refusal is
    the only ``REJECTED`` case that gets its own.
    """
    reason = result.metadata.get("rejection_reason")
    if result.status == CommandStatus.REJECTED and reason in STORE_REFUSAL_REASONS:
        return EXIT_STORE, str(reason), result.message
    if result.status == CommandStatus.REJECTED:
        return EXIT_VALIDATION, "validation_error", result.message
    return (
        EXIT_INTERNAL,
        "execution_failed",
        sanitize_error_message(result.message),
    )


def _emit_result(output_format: str, payload: dict[str, Any], text: str) -> None:
    if output_format == "json":
        print(json.dumps(payload))
    else:
        console.print(text, soft_wrap=True)


def _emit_failure(output_format: str, code: str, message: str) -> None:
    if output_format == "json":
        print(json.dumps({"status": "error", "code": code, "message": message}))
    else:
        console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)


# ---------------------------------------------------------------------------
# Command bodies (programmatic entry points, pulled out for testability)
# ---------------------------------------------------------------------------


def set_job_command(
    *,
    name: str,
    enabled_set: bool,
    enabled: bool,
    cadence_set: bool,
    cadence: str | None,
    timeout_set: bool,
    timeout_seconds: int | None,
    output_format: str,
) -> None:
    """CLI body for ``admin schedule set``."""
    if output_format not in ("text", "json"):
        _emit_failure(
            output_format,
            "invalid_format",
            f"Unsupported --format {output_format!r}; expected one of: text, json",
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    spec = JOB_CATALOG.get(name)
    if spec is None:
        _emit_failure(
            output_format,
            "unknown_job",
            f"Unknown job: {name!r}. See 'trellis admin due-jobs' for valid names.",
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    if not (enabled_set or cadence_set or timeout_set):
        _emit_failure(
            output_format,
            "no_fields",
            "Nothing to change: pass at least one of --enabled/--disabled, "
            "--cadence or --timeout.",
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    args: dict[str, Any] = {}
    if enabled_set:
        args["enabled"] = enabled
    if cadence_set:
        args["cadence"] = (
            None
            if cadence is None or cadence.strip().lower() == _CLEAR_CADENCE_SENTINEL
            else cadence
        )
    if timeout_set:
        args["timeout_seconds"] = timeout_seconds

    command = Command(
        operation=Operation.SCHEDULE_UPDATE,
        target_type="schedule",
        target_id=name,
        args=args,
        requested_by="cli:admin.schedule-set",
        metadata=_audit_identity(),
    )
    result = build_schedule_executor(_get_registry()).execute(command)
    if result.status != CommandStatus.SUCCESS:
        exit_code, code, message = _cli_result_failure(result)
        _emit_failure(output_format, code, message)
        raise typer.Exit(code=exit_code)

    # Re-read: no store instance was built in this function before the
    # executor's own handler committed the write.
    updated = get_schedule_store().get(name) or ScheduledJob(
        name=spec.name, cadence=spec.default_cadence
    )

    effective_timeout = (
        updated.timeout_seconds
        if updated.timeout_seconds is not None
        else spec.default_timeout_seconds
    )
    row = {
        "name": updated.name,
        "enabled": updated.enabled,
        "cadence": updated.cadence,
        "timeout_seconds": effective_timeout,
    }
    _emit_result(
        output_format,
        {"status": "ok", "job": row},
        f"[green]Updated[/green] {escape(name)}: enabled={updated.enabled} "
        f"cadence={updated.cadence!r} timeout_seconds={effective_timeout}",
    )
    raise typer.Exit(code=EXIT_OK)


def run_job_command(*, name: str, output_format: str) -> None:
    """CLI body for ``admin schedule run``.

    Writes only ``run_requested_at`` — never executes the job inline.
    See :func:`trellis_api.routes.admin.run_admin_job`'s docstring for
    the dispatcher-pickup argument this mirrors.
    """
    if output_format not in ("text", "json"):
        _emit_failure(
            output_format,
            "invalid_format",
            f"Unsupported --format {output_format!r}; expected one of: text, json",
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    spec = JOB_CATALOG.get(name)
    if spec is None:
        _emit_failure(
            output_format,
            "unknown_job",
            f"Unknown job: {name!r}. See 'trellis admin due-jobs' for valid names.",
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    command = Command(
        operation=Operation.JOB_RUN_REQUEST,
        target_type="schedule",
        target_id=name,
        requested_by="cli:admin.schedule-run",
        metadata=_audit_identity(),
    )
    result = build_schedule_executor(_get_registry()).execute(command)
    if result.status != CommandStatus.SUCCESS:
        exit_code, code, message = _cli_result_failure(result)
        _emit_failure(output_format, code, message)
        raise typer.Exit(code=exit_code)

    # Re-read rather than trust a locally-computed timestamp: the
    # handler, not this command, is what decided and wrote ``now``. A
    # successful command always writes ``run_requested_at``, but we
    # report exactly what is on the row — never a value fabricated here
    # — so a future handler change can't make this response lie (gate
    # #860, F6).
    updated = get_schedule_store().get(name) or ScheduledJob(
        name=spec.name, cadence=spec.default_cadence
    )
    run_requested_at = (
        updated.run_requested_at.isoformat()
        if updated.run_requested_at is not None
        else None
    )

    hint = (
        "Accepted. A host dispatcher polling 'due-jobs' picks this up, "
        "typically within ~5 minutes, if one is installed."
    )
    when_text = run_requested_at if run_requested_at is not None else "an unknown time"
    _emit_result(
        output_format,
        {
            "status": "ok",
            "name": name,
            "run_requested_at": run_requested_at,
            "dispatcher_hint": hint,
        },
        f"[green]Run requested[/green] for {escape(name)} at {escape(when_text)}.\n"
        f"[dim]{escape(hint)}[/dim]",
    )
    raise typer.Exit(code=EXIT_OK)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@schedule_app.command("set")
def set_job(  # pragma: no cover — Typer wrapper only
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., help="Job name (a trellis.schedule.catalog.JOB_CATALOG key)."
    ),
    enabled: bool = typer.Option(
        False,
        "--enabled/--disabled",
        help="Enable or disable the job. Omit to leave unchanged.",
    ),
    cadence: str | None = typer.Option(
        None,
        "--cadence",
        help=(
            "5-field cron expression. Omit to leave unchanged; pass the "
            "literal 'none' to clear it (manual-only, triggered only by "
            "'run')."
        ),
    ),
    timeout_seconds: int | None = typer.Option(
        None,
        "--timeout",
        min=1,
        max=86400,
        help="Per-job timeout override in seconds (1-86400). Omit to leave unchanged.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Edit ``enabled``, ``cadence`` and/or ``timeout_seconds`` for one catalog job."""

    def _was_passed(param_name: str) -> bool:
        # Compare by *name*, not identity/``==``: Typer invokes this
        # callback through its own vendored click fork
        # (``typer._click.core.Context``), whose ``get_parameter_source``
        # returns a ``ParameterSource`` member from that fork's own enum
        # class — a different object than ``click.ParameterSource``
        # despite the same name, so ``src == click.ParameterSource.
        # COMMANDLINE`` is False even when the flag WAS passed on the
        # command line. Measured directly: identity comparison silently
        # returns False for ``--cadence <anything>``, making every
        # ``set`` option in this command permanently a no-op.
        source = ctx.get_parameter_source(param_name)
        return source is not None and source.name == "COMMANDLINE"

    set_job_command(
        name=name,
        enabled_set=_was_passed("enabled"),
        enabled=enabled,
        cadence_set=_was_passed("cadence"),
        cadence=cadence,
        timeout_set=_was_passed("timeout_seconds"),
        timeout_seconds=timeout_seconds,
        output_format=output_format,
    )


@schedule_app.command("run")
def run_job(  # pragma: no cover — Typer wrapper only
    name: str = typer.Argument(
        ..., help="Job name (a trellis.schedule.catalog.JOB_CATALOG key)."
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Request an out-of-band run ("Run now"). Never executes the job inline."""
    run_job_command(name=name, output_format=output_format)


def register(admin_app: typer.Typer) -> None:
    """Mount the ``schedule`` sub-app onto the ``admin`` Typer app.

    Mirrors :mod:`trellis_cli.admin_api_keys` — registration hook so the
    import order in :mod:`trellis_cli.admin` stays explicit.
    """
    admin_app.add_typer(schedule_app, name="schedule")


__all__ = [
    "register",
    "run_job_command",
    "set_job_command",
]
