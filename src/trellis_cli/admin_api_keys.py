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
* :data:`EXIT_VALIDATION` (2) — unknown scope on ``create``; unknown
  or already-revoked key id on ``revoke``. Operator input problem.
* :data:`EXIT_STORE` (5) — backend failure during read/write.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
import typer
from rich.console import Console
from rich.table import Table

from trellis.auth import ALL_SCOPES, generate_api_key
from trellis.stores.base.api_key import ApiKeyRecord
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.stores import get_api_key_store

logger = structlog.get_logger(__name__)
console = Console()

api_keys_app = typer.Typer(
    no_args_is_help=True,
    help="Manage scoped REST API credentials.",
)

# Operator guidance shown beside the freshly-minted token (not a secret).
_SHOWN_ONCE_WARNING = (
    "Store this token now - it is shown once and cannot be recovered. "
    "Only a hash of the secret is persisted."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_to_row(record: ApiKeyRecord) -> dict[str, Any]:
    """Project a record to the wire/list shape. Never includes the hash."""
    return {
        "key_id": record.key_id,
        "name": record.name,
        "scopes": list(record.scopes),
        "created_at": record.created_at.isoformat(),
        "revoked": record.revoked_at is not None,
        "revoked_at": (record.revoked_at.isoformat() if record.revoked_at else None),
    }


def _store_error(exc: Exception, output_format: str) -> typer.Exit:
    """Render a backend failure and return the EXIT_STORE exit."""
    logger.exception("api_keys_store_error")
    message = f"{type(exc).__name__}: {exc}"
    if output_format == "json":
        print(json.dumps({"error": "store_error", "message": message}))
    else:
        console.print(f"[red]store error: {message}[/red]")
    return typer.Exit(code=EXIT_STORE)


def _parse_scopes(scopes: str) -> list[str]:
    """Split + validate a comma-separated scope list; loud on unknowns."""
    parsed = [s.strip() for s in scopes.split(",") if s.strip()]
    unknown = [s for s in parsed if s not in ALL_SCOPES]
    if unknown or not parsed:
        msg = (
            f"Unknown or empty scope(s): {unknown or '(none given)'}; "
            f"valid scopes: {sorted(ALL_SCOPES)}"
        )
        raise ValueError(msg)
    return parsed


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
        scope_list = _parse_scopes(scopes)
        token, record = generate_api_key(name, scope_list)
    except ValueError as exc:
        if output_format == "json":
            print(json.dumps({"error": "validation_error", "message": str(exc)}))
        else:
            console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    try:
        store = get_api_key_store()
        store.create(record)
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc

    if output_format == "json":
        print(
            json.dumps(
                {
                    "token": token,
                    "warning": _SHOWN_ONCE_WARNING,
                    **_record_to_row(record),
                }
            )
        )
    else:
        console.print(f"[bold]Created API key[/bold] {record.key_id} ({record.name})")
        console.print(f"  scopes: {', '.join(record.scopes)}")
        console.print(f"  token:  [bold green]{token}[/bold green]")
        console.print(f"[yellow]{_SHOWN_ONCE_WARNING}[/yellow]")
    raise typer.Exit(code=EXIT_OK)


def list_api_keys_command(*, output_format: str) -> None:
    """CLI body for ``api-keys list``."""
    try:
        store = get_api_key_store()
        rows = [_record_to_row(r) for r in store.list()]
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
                row["key_id"],
                row["name"],
                ",".join(row["scopes"]),
                row["created_at"],
                "yes" if row["revoked"] else "no",
            )
        console.print(table)
    raise typer.Exit(code=EXIT_OK)


def revoke_api_key_command(*, key_id: str, output_format: str) -> None:
    """CLI body for ``api-keys revoke``."""
    try:
        store = get_api_key_store()
        existing = store.get(key_id)
        revoked = store.revoke(key_id)
    except typer.Exit:
        raise
    except Exception as exc:
        raise _store_error(exc, output_format) from exc

    if not revoked:
        # Loud: distinguish unknown vs already-revoked for the operator.
        reason = "already_revoked" if existing is not None else "unknown_key_id"
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "error": reason,
                        "key_id": key_id,
                        "message": (
                            f"Cannot revoke {key_id!r}: {reason.replace('_', ' ')}."
                        ),
                    }
                )
            )
        else:
            console.print(
                f"[red]Cannot revoke {key_id!r}: {reason.replace('_', ' ')}.[/red]"
            )
        raise typer.Exit(code=EXIT_VALIDATION)

    if output_format == "json":
        print(json.dumps({"status": "revoked", "key_id": key_id}))
    else:
        console.print(f"[green]Revoked API key {key_id}.[/green]")
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
            "mutate, admin (admin implies all others)."
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
