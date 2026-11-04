"""``trellis admin api-keys``: create, list, revoke, set-defaults and rotate.

Operator surface for the scoped REST credentials introduced by
roadmap item E.5 (issue #191):

* ``create`` — mint a token with a name and a comma-separated scope
  list. The full token is printed **once** and never stored; only the
  SHA-256 of its secret half is persisted in the
  :class:`~trellis.stores.base.api_key.ApiKeyStore`.
* ``list`` — show key_id, name, scopes, created_at, and revocation
  state for every key. The secret hash is never printed.
* ``revoke`` — close a live key. Unknown or already-revoked key ids
  exit non-zero (loud), per the POC loud-on-misuse directive.
* ``set-defaults`` — replace (or ``--clear``) a live key's server-side defaults
  block: ``agent_id``, ``domain``, ``team`` and a ``session_id`` prefix filled
  into a call that omits them. A governed ``api_key.update``.
* ``rotate`` — mint a successor with the key's scopes and defaults, then retire
  the old key (revoked at once, or with ``--grace`` left valid for that long).
  Two audited operations, mint first: a failure between them leaves both keys
  valid, and the new token is still printed.

Exit codes follow :mod:`trellis_cli.exit_codes`:

* :data:`EXIT_OK` (0) — success.
* :data:`EXIT_VALIDATION` (2) — unknown scope on ``create``, or ``external``
  beside anything but ``read``; unknown
  or already-revoked key id on ``revoke``. Operator input problem.
* :data:`EXIT_STORE` (5) — backend failure during read/write, or a
  ``policies.json`` that will not load (Stage 2 fails closed, so ``create`` and
  ``revoke`` refuse until that file is fixed; the message names it).

``create`` and ``revoke`` never depend on the event log: when it cannot be opened
or written the key operation still runs, exits 0, and prints (text) or carries
(JSON ``warnings``) an ``audit_event_not_recorded`` warning.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
import typer
from rich.markup import escape
from rich.table import Table

from trellis.auth import (
    SCOPE_EXTERNAL,
    SHOWN_ONCE_WARNING,
    build_key_defaults,
    parse_grace,
    public_listing_row,
)
from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    build_api_keys_executor,
)
from trellis.mutate.api_key_commands import (
    RotationOutcome,
    build_mint_command,
    build_revoke_command,
    build_update_command,
    rotate_key,
)
from trellis_cli.exit_codes import (
    EXIT_OK,
    EXIT_STORE,
    EXIT_VALIDATION,
    refusal_exit_code,
)
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry, get_api_key_store

logger = structlog.get_logger(__name__)
console = build_console()

api_keys_app = typer.Typer(
    no_args_is_help=True,
    help="Manage scoped REST API credentials.",
)

#: The actor on the audit events the CLI's governed writes emit.
_CLI_ACTOR = "cli:api-keys"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_error(exc: Exception, output_format: str) -> typer.Exit:
    """Render a backend failure and return the EXIT_STORE exit."""
    logger.exception("api_keys_store_error")
    message = f"{type(exc).__name__}: {exc}"
    if output_format == "json":
        print(json.dumps({"error": "store_error", "message": message}))
    else:
        console.print(f"[red]store error: {escape(message)}[/red]", soft_wrap=True)
    return typer.Exit(code=EXIT_STORE)


def _execute(command: Command) -> CommandResult:
    """Submit *command* through the governed API-key executor."""
    return build_api_keys_executor(
        _get_registry(), tolerate_unopenable_event_log=True
    ).execute(command)


def _printable(text: str) -> str:
    """``text`` with control characters shown rather than sent to the terminal.

    A key name stored before names were validated may hold a newline or an
    escape sequence; it must list, and it must not reach the terminal raw.
    """
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def _print_warnings(result: CommandResult, output_format: str) -> None:
    """Text-mode warnings (JSON carries them in the payload; see ``_warnings``)."""
    if output_format == "json":
        return
    for warning in result.warnings:
        console.print(f"[yellow]warning: {escape(warning)}[/yellow]", soft_wrap=True)


def _warnings(result: CommandResult) -> dict[str, list[str]]:
    """The ``warnings`` key of a JSON payload, absent when there are none."""
    return {"warnings": list(result.warnings)} if result.warnings else {}


def _render_refusal(result: CommandResult, key_id: str, output_format: str) -> None:
    """Print why a governed write was refused or failed (exit is the caller's).

    A refusal the operator can fix (unknown or already-revoked key) keeps the
    ``{"error": reason, "key_id", "message"}`` shape this command has always
    printed, and the text this command has always printed (the handler's own
    message); anything else carries the executor's own message.
    """
    reason = str(result.metadata.get("rejection_reason") or result.status.value)
    message = sanitize_error_message(result.message)
    if output_format == "json":
        print(json.dumps({"error": reason, "key_id": key_id, "message": message}))
    else:
        console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)


# ---------------------------------------------------------------------------
# Command bodies (programmatic entry points, pulled out for testability)
# ---------------------------------------------------------------------------


def _validation_exit(message: str, output_format: str) -> None:
    """Print an operator-input problem (the caller raises ``EXIT_VALIDATION``)."""
    if output_format == "json":
        print(
            json.dumps(
                {
                    "error": "validation_error",
                    "message": sanitize_error_message(message),
                }
            )
        )
    else:
        console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)


def _defaults_lines(defaults: dict[str, str] | None) -> list[str]:
    """Text lines naming a defaults block, empty when there is none."""
    if not defaults:
        return []
    return [f"  default {field}: {escape(value)}" for field, value in defaults.items()]


def create_api_key_command(
    *,
    name: str,
    scopes: str,
    output_format: str,
    default_agent_id: str | None = None,
    default_domain: str | None = None,
    default_team: str | None = None,
    default_session_id_prefix: str | None = None,
) -> None:
    """CLI body for ``api-keys create``."""
    try:
        defaults = build_key_defaults(
            agent_id=default_agent_id,
            domain=default_domain,
            team=default_team,
            session_id_prefix=default_session_id_prefix,
        )
        token, record, command = build_mint_command(
            name, scopes, requested_by=_CLI_ACTOR, defaults=defaults
        )
    except ValueError as exc:
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "error": "validation_error",
                        "message": sanitize_error_message(str(exc)),
                    }
                )
            )
        else:
            console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    try:
        result = _execute(command)
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc
    if result.status != CommandStatus.SUCCESS:
        _render_refusal(result, record.key_id, output_format)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        print(
            json.dumps(
                {
                    "token": token,
                    "warning": SHOWN_ONCE_WARNING,
                    **public_listing_row(record),
                    **_warnings(result),
                }
            )
        )
    else:
        console.print(
            f"[bold]Created API key[/bold] {escape(record.key_id)} "
            f"({escape(_printable(record.name))})"
        )
        console.print(f"  scopes: {', '.join(record.scopes)}")
        for line in _defaults_lines(public_listing_row(record).get("defaults")):
            console.print(line, soft_wrap=True)
        if SCOPE_EXTERNAL in record.scopes:
            console.print(
                "  [dim]external: reads only documents someone labelled public; "
                "it does not hide unlabelled content from other keys.[/dim]"
            )
        console.print(f"  token:  [bold green]{token}[/bold green]")
        console.print(f"[yellow]{SHOWN_ONCE_WARNING}[/yellow]")
        _print_warnings(result, output_format)
    raise typer.Exit(code=EXIT_OK)


def list_api_keys_command(*, output_format: str) -> None:
    """CLI body for ``api-keys list``."""
    try:
        store = get_api_key_store()
        rows = [public_listing_row(r) for r in store.list()]
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc

    if output_format == "json":
        print(json.dumps({"keys": rows, "count": len(rows)}))
    elif not rows:
        console.print(
            "[dim]No API keys. Mint one with "
            "'trellis admin api-keys create --name NAME --scopes read'.[/dim]"
        )
    else:
        table = Table(title=f"API keys ({len(rows)})")
        table.add_column("key_id")
        table.add_column("name")
        table.add_column("scopes")
        table.add_column("created_at")
        table.add_column("revoked")
        table.add_column("expired")
        table.add_column("defaults")
        for row in rows:
            table.add_row(
                escape(_printable(row["key_id"])),
                escape(_printable(row["name"])),
                ",".join(row["scopes"]),
                row["created_at"],
                "yes" if row["revoked"] else "no",
                "yes" if row["expired"] else "no",
                escape(
                    _printable(
                        ", ".join(
                            f"{k}={v}" for k, v in row.get("defaults", {}).items()
                        )
                    )
                ),
            )
        console.print(table)
    raise typer.Exit(code=EXIT_OK)


def revoke_api_key_command(*, key_id: str, output_format: str) -> None:
    """CLI body for ``api-keys revoke``."""
    try:
        result = _execute(build_revoke_command(key_id, requested_by=_CLI_ACTOR))
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc

    if result.status != CommandStatus.SUCCESS:
        _render_refusal(result, key_id, output_format)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        print(json.dumps({"status": "revoked", "key_id": key_id, **_warnings(result)}))
    else:
        console.print(f"[green]Revoked API key {escape(key_id)}.[/green]")
        _print_warnings(result, output_format)
    raise typer.Exit(code=EXIT_OK)


def set_defaults_command(
    *,
    key_id: str,
    agent_id: str | None,
    domain: str | None,
    team: str | None,
    session_id_prefix: str | None,
    clear: bool,
    output_format: str,
) -> None:
    """CLI body for ``api-keys set-defaults``.

    The block is replaced, not merged: a field not named is cleared, so the
    command line is the whole truth about the key's defaults afterwards.
    """
    try:
        defaults = build_key_defaults(
            agent_id=agent_id,
            domain=domain,
            team=team,
            session_id_prefix=session_id_prefix,
        )
        if clear and defaults is not None:
            msg = "--clear cannot be combined with a default; pass one or the other"
            raise ValueError(msg)  # noqa: TRY301
        if not clear and defaults is None:
            msg = (
                "Nothing to set. Pass at least one of --agent-id, --domain, --team, "
                "--session-id-prefix, or --clear to remove the key's defaults."
            )
            raise ValueError(msg)  # noqa: TRY301
    except ValueError as exc:
        _validation_exit(str(exc), output_format)
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    try:
        result = _execute(
            build_update_command(key_id, defaults=defaults, requested_by=_CLI_ACTOR)
        )
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc
    if result.status != CommandStatus.SUCCESS:
        _render_refusal(result, key_id, output_format)
        raise typer.Exit(code=refusal_exit_code(result))

    shown = defaults.model_dump(exclude_none=True) if defaults is not None else {}
    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": "updated",
                    "key_id": key_id,
                    "defaults": shown or None,
                    **_warnings(result),
                }
            )
        )
    else:
        if shown:
            console.print(f"[green]Set defaults on API key {escape(key_id)}.[/green]")
            for line in _defaults_lines(shown):
                console.print(line, soft_wrap=True)
        else:
            console.print(
                f"[green]Cleared defaults on API key {escape(key_id)}.[/green]"
            )
        _print_warnings(result, output_format)
    raise typer.Exit(code=EXIT_OK)


def _rotation_payload(outcome: RotationOutcome, key_id: str) -> dict[str, Any]:
    """The JSON a rotation that minted a key prints."""
    assert outcome.token is not None
    assert outcome.record is not None
    failure = outcome.failure
    payload: dict[str, Any] = {
        "status": "rotated" if failure is None else "partial",
        "token": outcome.token,
        "warning": SHOWN_ONCE_WARNING,
        "old_key_id": key_id,
        "retired": outcome.retired,
        "old_expires_at": (
            outcome.old_expires_at.isoformat() if outcome.old_expires_at else None
        ),
        **public_listing_row(outcome.record),
        **({"warnings": list(outcome.warnings)} if outcome.warnings else {}),
    }
    if failure is not None:
        payload["error"] = str(
            failure.metadata.get("rejection_reason") or failure.status.value
        )
        payload["message"] = sanitize_error_message(failure.message)
    return payload


def _print_rotation_text(outcome: RotationOutcome, key_id: str) -> None:
    """Text output for a rotation that minted a key."""
    assert outcome.token is not None
    assert outcome.record is not None
    record = outcome.record
    console.print(
        f"[bold]Created API key[/bold] {escape(record.key_id)} "
        f"({escape(_printable(record.name))}), replacing {escape(key_id)}"
    )
    console.print(f"  scopes: {', '.join(record.scopes)}")
    for line in _defaults_lines(public_listing_row(record).get("defaults")):
        console.print(line, soft_wrap=True)
    console.print(f"  token:  [bold green]{outcome.token}[/bold green]")
    console.print(f"[yellow]{SHOWN_ONCE_WARNING}[/yellow]")
    if outcome.failure is not None:
        console.print(
            f"[red]The old key {escape(key_id)} was NOT retired and is still "
            f"valid: {escape(sanitize_error_message(outcome.failure.message))} "
            "Revoke it with 'trellis admin api-keys revoke'.[/red]",
            soft_wrap=True,
        )
    elif outcome.old_expires_at is not None:
        console.print(
            f"  old key {escape(key_id)} stays valid until "
            f"{outcome.old_expires_at.isoformat()}"
        )
    else:
        console.print(f"  old key {escape(key_id)} revoked")
    for warning in outcome.warnings:
        console.print(f"[yellow]warning: {escape(warning)}[/yellow]", soft_wrap=True)


def _render_rotation(outcome: RotationOutcome, key_id: str, output_format: str) -> None:
    """Print a rotation's outcome (the exit code is the caller's)."""
    if outcome.refusal is not None:
        reason, message = outcome.refusal
        if output_format == "json":
            print(json.dumps({"error": reason, "key_id": key_id, "message": message}))
        else:
            console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)
    elif outcome.token is None:
        # Nothing was minted: the mint itself was refused or failed.
        assert outcome.failure is not None
        _render_refusal(outcome.failure, key_id, output_format)
    elif output_format == "json":
        print(json.dumps(_rotation_payload(outcome, key_id)))
    else:
        _print_rotation_text(outcome, key_id)


def rotate_api_key_command(
    *,
    key_id: str,
    grace: str | None,
    name: str | None,
    output_format: str,
) -> None:
    """CLI body for ``api-keys rotate``."""
    try:
        grace_delta = parse_grace(grace) if grace is not None else None
    except ValueError as exc:
        _validation_exit(str(exc), output_format)
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    try:
        outcome = rotate_key(
            get_api_key_store(),
            _execute,
            key_id,
            grace=grace_delta,
            new_name=name,
            requested_by=_CLI_ACTOR,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc

    # One exit code per outcome, decided before the format branch.
    code = EXIT_OK
    if outcome.refusal is not None:
        code = EXIT_VALIDATION
    elif outcome.failure is not None:
        code = refusal_exit_code(outcome.failure)
    _render_rotation(outcome, key_id, output_format)
    raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@api_keys_app.command("create")
def create(  # pragma: no cover — Typer wrapper only
    name: str = typer.Option(
        ...,
        "--name",
        help="Human-readable label for the key (shown in list/audit output).",
    ),
    scopes: str = typer.Option(
        ...,
        "--scopes",
        help=(
            "Comma-separated scope list. Valid scopes: read, ingest, "
            "mutate, admin (admin implies all others), confidential, "
            "restricted, and external. external is only valid as "
            "'read,external': a read-only key that sees only documents "
            "labelled public."
        ),
    ),
    default_agent_id: str | None = typer.Option(
        None,
        "--default-agent-id",
        help="Server-side default for agent_id on a call that omits it.",
    ),
    default_domain: str | None = typer.Option(
        None,
        "--default-domain",
        help="Server-side default for domain on a call that omits it.",
    ),
    default_team: str | None = typer.Option(
        None,
        "--default-team",
        help="Server-side team the key's writes are attributed to (pinned).",
    ),
    default_session_id_prefix: str | None = typer.Option(
        None,
        "--default-session-id-prefix",
        help="Prefix of the session_id minted for a call that omits one.",
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """Mint a new scoped API key. The token is printed exactly once."""
    create_api_key_command(
        name=name,
        scopes=scopes,
        output_format=output_format,
        default_agent_id=default_agent_id,
        default_domain=default_domain,
        default_team=default_team,
        default_session_id_prefix=default_session_id_prefix,
    )


@api_keys_app.command("list")
def list_keys(  # pragma: no cover — Typer wrapper only
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """List every API key (live and revoked). Never prints the hash."""
    list_api_keys_command(output_format=output_format)


@api_keys_app.command("revoke")
def revoke(  # pragma: no cover — Typer wrapper only
    key_id: str = typer.Argument(
        ...,
        help="The 12-hex-char key_id to revoke (see 'api-keys list').",
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """Revoke a live API key. Errors loudly if unknown or already revoked."""
    revoke_api_key_command(key_id=key_id, output_format=output_format)


@api_keys_app.command("set-defaults")
def set_defaults(  # pragma: no cover — Typer wrapper only
    key_id: str = typer.Argument(
        ..., help="The key_id to update (see 'api-keys list')."
    ),
    agent_id: str | None = typer.Option(None, "--agent-id", help="Default agent_id."),
    domain: str | None = typer.Option(None, "--domain", help="Default domain."),
    team: str | None = typer.Option(None, "--team", help="Pinned team."),
    session_id_prefix: str | None = typer.Option(
        None, "--session-id-prefix", help="Prefix of a minted session_id."
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove every default from the key."
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """Replace a live key's server-side defaults (fields not named are cleared)."""
    set_defaults_command(
        key_id=key_id,
        agent_id=agent_id,
        domain=domain,
        team=team,
        session_id_prefix=session_id_prefix,
        clear=clear,
        output_format=output_format,
    )


@api_keys_app.command("rotate")
def rotate(  # pragma: no cover — Typer wrapper only
    key_id: str = typer.Argument(..., help="The key_id to rotate."),
    grace: str | None = typer.Option(
        None,
        "--grace",
        help=(
            "Keep the old key valid for this long (30m, 24h, 7d; at most 90d). "
            "Omit to revoke it at once."
        ),
    ),
    name: str | None = typer.Option(
        None, "--name", help="Name for the new key (default: the old key's name)."
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """Mint a successor with the same scopes and defaults, then retire the old key."""
    rotate_api_key_command(
        key_id=key_id, grace=grace, name=name, output_format=output_format
    )


def register(admin_app: typer.Typer) -> None:
    """Mount the ``api-keys`` sub-app onto the ``admin`` Typer app.

    Mirrors :mod:`trellis_cli.admin_proposals` — registration hook so
    the import order in :mod:`trellis_cli.admin` stays explicit.
    """
    admin_app.add_typer(api_keys_app, name="api-keys")


__all__ = [
    "create_api_key_command",
    "list_api_keys_command",
    "register",
    "revoke_api_key_command",
    "rotate_api_key_command",
    "set_defaults_command",
]
