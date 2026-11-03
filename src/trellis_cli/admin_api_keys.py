"""``trellis admin api-keys create`` / ``list`` / ``revoke``.

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

import structlog
import typer
from rich.markup import escape
from rich.table import Table

from trellis.auth import (
    SCOPE_EXTERNAL,
    SHOWN_ONCE_WARNING,
    public_listing_row,
)
from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    build_api_keys_executor,
)
from trellis.mutate.api_key_commands import build_mint_command, build_revoke_command
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


def create_api_key_command(
    *,
    name: str,
    scopes: str,
    output_format: str,
) -> None:
    """CLI body for ``api-keys create``."""
    try:
        token, record, command = build_mint_command(
            name, scopes, requested_by=_CLI_ACTOR
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
        for row in rows:
            table.add_row(
                escape(_printable(row["key_id"])),
                escape(_printable(row["name"])),
                ",".join(row["scopes"]),
                row["created_at"],
                "yes" if row["revoked"] else "no",
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
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: text or json.",
    ),
) -> None:
    """Mint a new scoped API key. The token is printed exactly once."""
    create_api_key_command(name=name, scopes=scopes, output_format=output_format)


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
]
