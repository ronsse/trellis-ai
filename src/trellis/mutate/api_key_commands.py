"""Build the governed commands that mint and revoke an API key.

One construction site for every surface (``trellis admin api-keys``, the admin
REST routes, the MCP revoke tool), so the scope check, the token generation and
the audit identity are the same wherever a key is minted or revoked. The caller
submits the result to :func:`trellis.mutate.build_api_keys_executor`.

The token is generated here and returned beside the command, never inside it:
only the hashed :class:`~trellis.stores.base.api_key.ApiKeyRecord` enters the
pipeline, so no event, result or log line the executor writes can hold it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.auth import generate_api_key, parse_scope_list
from trellis.mutate.commands import Command, Operation

if TYPE_CHECKING:
    from trellis.stores.base.api_key import ApiKeyRecord

#: ``target_type`` on both commands; the policy gate scopes on it.
API_KEY_TARGET_TYPE = "api_key"


def _metadata(actor_key_id: str | None, actor_key_name: str | None) -> dict[str, str]:
    return {
        k: v
        for k, v in (
            ("actor_key_id", actor_key_id),
            ("actor_key_name", actor_key_name),
        )
        if v is not None
    }


def build_mint_command(
    name: str,
    scopes: str | list[str],
    *,
    requested_by: str,
    actor_key_id: str | None = None,
    actor_key_name: str | None = None,
) -> tuple[str, ApiKeyRecord, Command]:
    """``(token, record, command)`` for minting ``name`` with ``scopes``.

    Raises :class:`ValueError` on an empty name, an unknown or empty scope, or
    ``external`` beside anything but ``read``; nothing has been written then.
    """
    token, record = generate_api_key(name, parse_scope_list(scopes))
    return (
        token,
        record,
        Command(
            operation=Operation.API_KEY_MINT,
            args={"record": record},
            target_id=record.key_id,
            target_type=API_KEY_TARGET_TYPE,
            requested_by=requested_by,
            metadata=_metadata(actor_key_id, actor_key_name),
        ),
    )


def build_revoke_command(
    key_id: str,
    *,
    requested_by: str,
    actor_key_id: str | None = None,
    actor_key_name: str | None = None,
) -> Command:
    """The command revoking ``key_id``.

    ``actor_key_id`` arms the refusal to revoke the caller's own key.
    """
    return Command(
        operation=Operation.API_KEY_REVOKE,
        args={"key_id": key_id},
        target_id=key_id,
        target_type=API_KEY_TARGET_TYPE,
        requested_by=requested_by,
        metadata=_metadata(actor_key_id, actor_key_name),
    )
