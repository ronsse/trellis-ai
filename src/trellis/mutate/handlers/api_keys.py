"""The ``api_key.mint`` and ``api_key.revoke`` handlers."""

from __future__ import annotations

from typing import Any

from trellis.errors import ValidationError
from trellis.mutate.commands import Command
from trellis.mutate.executor import guarded_audit_emit
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


def _api_key_actor(command: Command) -> dict[str, Any]:
    """The audit identity an API-key command carries: who asked, and which key.

    ``requested_by`` is what the surface derived (``api:<key>`` on REST,
    ``cli:...``, ``mcp:...``); ``actor_key_id`` / ``actor_key_name`` name the
    authenticated key when there is one, and are ``None`` otherwise.
    """
    return {
        "actor_key_id": command.metadata.get("actor_key_id"),
        "actor_key_name": command.metadata.get("actor_key_name"),
    }


class ApiKeyMintHandler:
    """Persist one freshly generated API key and emit ``api_key.minted``.

    The caller generates the token (:func:`trellis.auth.generate_api_key`) and
    hands the handler only the :class:`~trellis.stores.base.api_key.ApiKeyRecord`,
    so the secret never enters the pipeline: the event, the result and the
    audit trail carry the key id, name and scopes, never the token or its hash.
    The scope vocabulary and the ``external``-only-as-``read,external`` rule are
    re-checked here with the same functions a mint uses, so a command built by
    hand cannot mint what the CLI would refuse.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        from trellis.auth import (  # noqa: PLC0415
            KEY_SCOPES,
            api_key_name_refusal,
            external_scope_refusal,
        )
        from trellis.stores.base.api_key import ApiKeyRecord  # noqa: PLC0415

        record = command.args["record"]
        if not isinstance(record, ApiKeyRecord):
            msg = "api_key.mint requires args['record'] to be an ApiKeyRecord"
            raise ValidationError(msg, errors=[msg], code="invalid_record")
        name_refusal = api_key_name_refusal(record.name)
        if name_refusal is not None:
            raise ValidationError(
                name_refusal, errors=[name_refusal], code="invalid_name"
            )
        if not record.scopes:
            msg = "API key must carry at least one scope"
            raise ValidationError(msg, errors=[msg], code="invalid_scopes")
        unknown = [s for s in record.scopes if s not in KEY_SCOPES]
        refusal = external_scope_refusal(record.scopes)
        if unknown or refusal is not None:
            msg = refusal or f"Unknown scope(s) {unknown!r}"
            raise ValidationError(msg, errors=[msg], code="invalid_scopes")
        self._registry.operational.api_key_store.create(record)
        # The key is stored: from here the mint has happened, so a dead event
        # log is a warning on the result and never a failure (the token the
        # caller holds is for a key that exists).
        guarded_audit_emit(
            command,
            EventType.API_KEY_MINTED,
            lambda: self._registry.operational.event_log.emit(
                EventType.API_KEY_MINTED,
                source="mutation_executor",
                entity_id=record.key_id,
                entity_type="api_key",
                payload={
                    "key_id": record.key_id,
                    "name": record.name,
                    "scopes": list(record.scopes),
                    "minted_by": command.requested_by,
                    **_api_key_actor(command),
                },
            ),
        )
        return record.key_id, f"API key {record.key_id} minted"


class ApiKeyRevokeHandler:
    """Revoke one live API key and emit ``api_key.revoked``.

    An unknown or already-revoked key id is a :class:`ValidationError` (the
    operator error the store and the CLI have always called it), and a key
    revoking itself is refused: a caller holding a single admin key would
    otherwise lock itself out with one request. The refusal rests on
    ``command.metadata["actor_key_id"]``, which each authenticated surface sets
    from its own principal, so it holds on REST and MCP alike. The CLI has no
    key to compare against and may revoke any key.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        key_id = command.args["key_id"]
        if not isinstance(key_id, str) or not key_id:
            msg = "api_key.revoke requires a non-empty string 'key_id'"
            raise ValidationError(msg, errors=[msg], code="invalid_key_id")
        store = self._registry.operational.api_key_store
        existing = store.get(key_id)
        if existing is None:
            msg = f"Cannot revoke {key_id!r}: unknown key id."
            raise ValidationError(msg, errors=[msg], code="unknown_key_id")
        if existing.revoked_at is not None:
            msg = f"Cannot revoke {key_id!r}: already revoked."
            raise ValidationError(msg, errors=[msg], code="already_revoked")
        if command.metadata.get("actor_key_id") == key_id:
            msg = (
                f"Cannot revoke {key_id!r}: it is the key this request is "
                "authenticated with. Revoke it with another admin key or "
                "'trellis admin api-keys revoke'."
            )
            raise ValidationError(msg, errors=[msg], code="cannot_revoke_own_key")
        if not store.revoke(key_id):
            msg = f"Cannot revoke {key_id!r}: already revoked."
            raise ValidationError(msg, errors=[msg], code="already_revoked")
        guarded_audit_emit(
            command,
            EventType.API_KEY_REVOKED,
            lambda: self._registry.operational.event_log.emit(
                EventType.API_KEY_REVOKED,
                source="mutation_executor",
                entity_id=key_id,
                entity_type="api_key",
                payload={
                    "key_id": key_id,
                    "name": existing.name,
                    "scopes": list(existing.scopes),
                    "revoked_by": command.requested_by,
                    **_api_key_actor(command),
                },
            ),
        )
        return key_id, f"API key {key_id} revoked"
