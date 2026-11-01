"""API-key management routes: list, mint and revoke.

Mounted with the admin scope only (see ``trellis_api/app.py``), so an
``external`` key, which satisfies ``read`` and nothing else, is refused on
every route here before the handler runs.

Mint and revoke go through the governed ``MutationExecutor``
(``Operation.API_KEY_MINT`` / ``API_KEY_REVOKE``, the same path
``trellis admin api-keys`` takes), so a denying policy blocks every surface
alike and each write emits an ``api_key.minted`` / ``api_key.revoked`` event.
The token is generated outside the pipeline: only the hashed record enters a
command, so no event or result can carry the secret. The full token appears
in the mint response and nowhere else; the list never returns a hash.

A caller cannot revoke the key it is authenticated with. Mint and revoke also
require an *identified* credential that holds ``admin`` itself, whatever the
auth mode: an anonymous caller (mode ``off``, or ``optional`` with no key) is
granted every scope by ``authenticate`` but may not mint a durable credential or
revoke one, and is pointed at ``trellis admin api-keys``. The CLI has no
principal and remains the bootstrap and recovery path: with a live admin key
required and self-revoke refused, REST and MCP cannot remove the last admin.

A key operation that took effect always answers success. If its audit event
could not be written the response carries ``warnings`` (``audit_event_not_recorded``)
rather than an error, because a mint's token is for a key that now exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from trellis.auth import (
    KEY_PRESETS,
    SCOPE_ADMIN,
    SHOWN_ONCE_WARNING,
    api_key_public_row,
    scopes_satisfy,
)
from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate import CommandResult, CommandStatus, build_api_keys_executor
from trellis.mutate.api_key_commands import build_mint_command, build_revoke_command
from trellis.mutate.commands import STORE_REFUSAL_REASONS
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
from trellis_api.routes._principal import resolve_requested_by
from trellis_wire.dtos import ApiKeyMintRequest

router = APIRouter()

_DEFAULT_ACTOR = "api:api-keys"

#: Refusal reasons that name a missing or already-closed key rather than a
#: malformed request, mapped to the status a REST client expects.
_REVOKE_STATUS = {
    "unknown_key_id": 404,
    "already_revoked": 409,
    "cannot_revoke_own_key": 409,
}


def _actor(ctx: AuthContext) -> str:
    return resolve_requested_by(ctx, _DEFAULT_ACTOR, claim_was_set=False)[0]


#: The one text a server fault carries. No exception text: a store driver's
#: message can hold query text or values.
_SERVER_FAULT = (
    "The API key operation failed on the server and its outcome is unknown. "
    "List the keys before retrying, and see the server log."
)

#: ``AuthContext.name`` of the legacy shared secret (``trellis_api.auth``).
_SHARED_SECRET_NAME = "shared-secret"  # noqa: S105 -- a label, not a credential


def _require_identified_admin(ctx: AuthContext, action: str) -> None:
    """Refuse (403) a mint or revoke from anything but an identified admin.

    Checked here, not left to the router's scope dependency, because ``off`` and
    ``optional`` grant an anonymous caller every scope and ``off`` ignores a
    presented key altogether. Identified means a stored key, or the legacy
    shared secret, which the server treats as admin in every mode that accepts
    it; both are named on the audit event.
    """
    identified = ctx.key_id is not None or ctx.name == _SHARED_SECRET_NAME
    if identified and scopes_satisfy(ctx.scopes, SCOPE_ADMIN):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"{action} needs an identified credential that holds the admin scope: "
            "present an admin API key. This server cannot tell who is asking "
            "otherwise, so to bootstrap or recover use 'trellis admin api-keys' "
            "on the server host."
        ),
    )


def _refuse(result: CommandResult, statuses: dict[str, int]) -> HTTPException:
    """The HTTP error for a command that did not succeed.

    A REJECTED command is the caller's to fix (400, or the mapped 404/409; a
    policy denial is 400 like every other governed route). A FAILED one is the
    server's: 500 with a fixed text. A DUPLICATE is not an outcome either route
    can answer, because no key was written.
    """
    if result.status == CommandStatus.FAILED:
        return HTTPException(status_code=500, detail=_SERVER_FAULT)
    if result.status == CommandStatus.DUPLICATE:
        return HTTPException(
            status_code=409, detail="This command was already submitted."
        )
    reason = result.metadata.get("rejection_reason")
    code = 409 if reason in STORE_REFUSAL_REASONS else statuses.get(str(reason), 400)
    return HTTPException(
        status_code=code, detail=sanitize_error_message(result.message)
    )


@router.get("/api-keys")
async def list_api_keys() -> dict[str, Any]:
    """Every key, live and revoked, plus the scope presets a UI can offer."""
    store = get_registry().operational.api_key_store
    rows = [api_key_public_row(r) for r in store.list()]
    return {
        "keys": rows,
        "count": len(rows),
        "presets": [
            {"label": label, "scopes": list(scopes)} for label, scopes in KEY_PRESETS
        ],
    }


@router.post("/api-keys", status_code=201)
async def mint_api_key(
    req: ApiKeyMintRequest,
    response: Response,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 -- FastAPI DI idiom
) -> dict[str, Any]:
    """Mint a key. The full token is in this response and never again."""
    _require_identified_admin(ctx, "Minting an API key")
    try:
        token, record, command = build_mint_command(
            req.name,
            req.scopes,
            requested_by=_actor(ctx),
            actor_key_id=ctx.key_id,
            actor_key_name=ctx.name,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(str(exc))
        ) from exc
    result = build_api_keys_executor(get_registry()).execute(command)
    if result.status != CommandStatus.SUCCESS:
        raise _refuse(result, {})
    response.headers["Cache-Control"] = "no-store"
    return {
        "token": token,
        "warning": SHOWN_ONCE_WARNING,
        **api_key_public_row(record),
        **({"warnings": list(result.warnings)} if result.warnings else {}),
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 -- FastAPI DI idiom
) -> dict[str, Any]:
    """Revoke a live key. Unknown, already-revoked and own-key are refused."""
    _require_identified_admin(ctx, "Revoking an API key")
    command = build_revoke_command(
        key_id,
        requested_by=_actor(ctx),
        actor_key_id=ctx.key_id,
        actor_key_name=ctx.name,
    )
    result = build_api_keys_executor(get_registry()).execute(command)
    if result.status != CommandStatus.SUCCESS:
        raise _refuse(result, _REVOKE_STATUS)
    return {
        "status": "revoked",
        "key_id": key_id,
        **({"warnings": list(result.warnings)} if result.warnings else {}),
    }
