"""The ``api_key.mint``, ``api_key.revoke`` and ``api_key.update`` handlers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trellis.errors import ValidationError
from trellis.mutate.commands import Command
from trellis.mutate.executor import guarded_audit_emit
from trellis.stores.base.api_key import UNSET, ApiKeyDefaults
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


def _check_defaults(defaults: ApiKeyDefaults) -> None:
    """Refuse a defaults block holding a value a CLI or REST mint would refuse.

    The one check both :class:`ApiKeyMintHandler` and :class:`ApiKeyUpdateHandler`
    run, so a hand-built command cannot store a control character or a blank
    value through either.
    """
    from trellis.auth import key_default_refusal  # noqa: PLC0415

    for field, value in defaults.model_dump(exclude_none=True).items():
        refusal = key_default_refusal(field, value)
        if refusal is not None:
            raise ValidationError(refusal, errors=[refusal], code="invalid_defaults")


class ApiKeyMintHandler:
    """Persist one freshly generated API key and emit ``api_key.minted``.

    The caller generates the token (:func:`trellis.auth.generate_api_key`) and
    hands the handler only the :class:`~trellis.stores.base.api_key.ApiKeyRecord`,
    so the secret never enters the pipeline: the event, the result and the
    audit trail carry the key id, name and scopes, never the token or its hash.
    The scope vocabulary, the ``external``-only-as-``read,external`` rule and
    the ``defaults`` values are re-checked here with the same functions a mint
    uses, so a command built by hand cannot mint what the CLI would refuse.
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
        if record.defaults is not None:
            _check_defaults(record.defaults)
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
                    # Present only on a rotation, linking the new key to the
                    # one it replaces.
                    **(
                        {"rotated_from": command.metadata["rotated_from"]}
                        if command.metadata.get("rotated_from")
                        else {}
                    ),
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


class ApiKeyUpdateHandler:
    """Rewrite a live key's ``defaults`` and/or ``expires_at`` (``api_key.updated``).

    ``args["defaults"]`` (an :class:`~trellis.stores.base.api_key.ApiKeyDefaults`
    or ``None`` to clear) and ``args["expires_at"]`` (a ``datetime`` or ``None``
    to clear) are each optional, and an absent one is left alone; at least one
    must be present. The credential (scopes, name, secret) cannot be reached
    through this handler. An unknown or revoked key is a
    :class:`ValidationError`, like revoke. The values are checked here with the
    same function a mint uses, so a hand-built command cannot store a default
    the CLI would refuse, and an ``expires_at`` must be timezone-aware.

    ``args["only_if_no_expiry"]`` (``True``) makes setting ``expires_at``
    conditional on the key having none: a rotation's retire, which must not
    move a window another rotation already wrote. A key that already has one
    is refused as ``already_retired``.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        from trellis.auth import key_is_expired  # noqa: PLC0415

        key_id = command.args["key_id"]
        if not isinstance(key_id, str) or not key_id:
            msg = "api_key.update requires a non-empty string 'key_id'"
            raise ValidationError(msg, errors=[msg], code="invalid_key_id")
        changed = [name for name in ("defaults", "expires_at") if name in command.args]
        if not changed:
            msg = "api_key.update needs 'defaults' and/or 'expires_at'"
            raise ValidationError(msg, errors=[msg], code="nothing_to_update")
        defaults = command.args.get("defaults", UNSET)
        if defaults is not UNSET and defaults is not None:
            if not isinstance(defaults, ApiKeyDefaults):
                msg = "api_key.update requires args['defaults'] to be an ApiKeyDefaults"
                raise ValidationError(msg, errors=[msg], code="invalid_defaults")
            _check_defaults(defaults)
        expires_at = command.args.get("expires_at", UNSET)
        if (
            expires_at is not UNSET
            and expires_at is not None
            and not isinstance(expires_at, datetime)
        ):
            msg = "api_key.update requires args['expires_at'] to be a datetime"
            raise ValidationError(msg, errors=[msg], code="invalid_expiry")
        if isinstance(expires_at, datetime) and expires_at.utcoffset() is None:
            # A naive instant is ambiguous, and one in the store makes every
            # verify and every listing raise on the comparison with the clock.
            msg = "api_key.update requires args['expires_at'] to be timezone-aware"
            raise ValidationError(msg, errors=[msg], code="invalid_expiry")
        only_if_no_expiry = command.args.get("only_if_no_expiry", False)
        if only_if_no_expiry is not False and (
            only_if_no_expiry is not True or not isinstance(expires_at, datetime)
        ):
            msg = (
                "api_key.update accepts args['only_if_no_expiry'] only as True, "
                "and only together with an 'expires_at' datetime"
            )
            raise ValidationError(msg, errors=[msg], code="invalid_update")
        store = self._registry.operational.api_key_store
        existing = store.get(key_id)
        if existing is None:
            msg = f"Cannot update {key_id!r}: unknown key id."
            raise ValidationError(msg, errors=[msg], code="unknown_key_id")
        if (
            expires_at is not UNSET
            and existing.revoked_at is None
            and key_is_expired(existing)
        ):
            msg = (
                f"Cannot change the expiry of {key_id!r}: it expired at "
                f"{existing.expires_at.isoformat() if existing.expires_at else ''} "
                "and no longer verifies. An expired key is never revived; "
                "mint a new one."
            )
            raise ValidationError(msg, errors=[msg], code="key_expired")
        if existing.revoked_at is not None or not store.update(
            key_id,
            defaults=defaults,
            expires_at=expires_at,
            only_if_no_expiry=only_if_no_expiry,
        ):
            current = store.get(key_id) if only_if_no_expiry else None
            if (
                current is not None
                and current.revoked_at is None
                and current.expires_at is not None
            ):
                # Another retire won between this command's read and its write.
                msg = (
                    f"Cannot retire {key_id!r}: it is already retired, with an "
                    f"expiry of {current.expires_at.isoformat()} set by another "
                    "rotation or update. That expiry was left as it is."
                )
                raise ValidationError(msg, errors=[msg], code="already_retired")
            msg = f"Cannot update {key_id!r}: it is revoked."
            raise ValidationError(msg, errors=[msg], code="already_revoked")
        stored = store.get(key_id) or existing
        guarded_audit_emit(
            command,
            EventType.API_KEY_UPDATED,
            lambda: self._registry.operational.event_log.emit(
                EventType.API_KEY_UPDATED,
                source="mutation_executor",
                entity_id=key_id,
                entity_type="api_key",
                payload={
                    "key_id": key_id,
                    "name": existing.name,
                    "scopes": list(existing.scopes),
                    "changed": changed,
                    "defaults": (
                        stored.defaults.model_dump(exclude_none=True)
                        if stored.defaults is not None
                        else None
                    ),
                    "expires_at": (
                        stored.expires_at.isoformat() if stored.expires_at else None
                    ),
                    "updated_by": command.requested_by,
                    **_api_key_actor(command),
                },
            ),
        )
        return key_id, f"API key {key_id} updated"
