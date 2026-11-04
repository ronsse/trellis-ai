"""Build the governed commands that mint, revoke, update and rotate an API key.

One construction site for every surface (``trellis admin api-keys``, the admin
REST routes, the MCP revoke tool), so the scope check, the token generation and
the audit identity are the same wherever a key is minted or revoked. The caller
submits the result to :func:`trellis.mutate.build_api_keys_executor`.

The token is generated here and returned beside the command, never inside it:
only the hashed :class:`~trellis.stores.base.api_key.ApiKeyRecord` enters the
pipeline, so no event, result or log line the executor writes can hold it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from trellis.auth import generate_api_key, key_is_expired, parse_scope_list
from trellis.core.base import utc_now
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.stores.base.api_key import UNSET, ApiKeyDefaults, Unset

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.base.api_key import ApiKeyRecord, ApiKeyStore

#: ``target_type`` on both commands; the policy gate scopes on it.
API_KEY_TARGET_TYPE = "api_key"


def _metadata(
    actor_key_id: str | None,
    actor_key_name: str | None,
    rotated_from: str | None = None,
) -> dict[str, str]:
    return {
        k: v
        for k, v in (
            ("actor_key_id", actor_key_id),
            ("actor_key_name", actor_key_name),
            ("rotated_from", rotated_from),
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
    defaults: ApiKeyDefaults | None = None,
    rotated_from: str | None = None,
) -> tuple[str, ApiKeyRecord, Command]:
    """``(token, record, command)`` for minting ``name`` with ``scopes``.

    ``defaults`` is the key's optional server-side defaults block (already
    validated by :func:`trellis.auth.build_key_defaults`); ``rotated_from`` names
    the key a rotation replaces and is carried on the audit event.

    Raises :class:`ValueError` on an empty name, an unknown or empty scope, or
    ``external`` beside anything but ``read``; nothing has been written then.
    """
    token, record = generate_api_key(name, parse_scope_list(scopes))
    if defaults is not None and not defaults.is_empty():
        record = record.model_copy(update={"defaults": defaults})
    return (
        token,
        record,
        Command(
            operation=Operation.API_KEY_MINT,
            args={"record": record},
            target_id=record.key_id,
            target_type=API_KEY_TARGET_TYPE,
            requested_by=requested_by,
            metadata=_metadata(actor_key_id, actor_key_name, rotated_from),
        ),
    )


def build_update_command(
    key_id: str,
    *,
    defaults: ApiKeyDefaults | Unset | None = UNSET,
    expires_at: datetime | Unset | None = UNSET,
    requested_by: str,
    actor_key_id: str | None = None,
    actor_key_name: str | None = None,
) -> Command:
    """The command rewriting ``key_id``'s defaults block and/or expiry.

    A field left at :data:`~trellis.stores.base.api_key.UNSET` is not sent, so
    the handler leaves it alone; ``None`` clears it.
    """
    args: dict[str, Any] = {"key_id": key_id}
    if not isinstance(defaults, Unset):
        args["defaults"] = defaults
    if not isinstance(expires_at, Unset):
        args["expires_at"] = expires_at
    return Command(
        operation=Operation.API_KEY_UPDATE,
        args=args,
        target_id=key_id,
        target_type=API_KEY_TARGET_TYPE,
        requested_by=requested_by,
        metadata=_metadata(actor_key_id, actor_key_name),
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


#: How a rotation left the old key. ``revoked`` and ``grace`` are the two clean
#: endings; ``still_valid`` means the new key exists and the old one was NOT
#: retired (both work); ``none`` means nothing was minted (the old key is the
#: only one).
Retired = Literal["revoked", "grace", "still_valid", "none"]


@dataclass(frozen=True)
class RotationOutcome:
    """What :func:`rotate_key` did, for a surface to render.

    ``token`` / ``record`` are set whenever a new key was minted, and the token
    must be shown then even if the old key could not be retired: it belongs to a
    key that exists. ``failure`` is the refused or failed step's result.
    """

    old_key_id: str
    retired: Retired
    token: str | None = None
    record: ApiKeyRecord | None = None
    old_expires_at: datetime | None = None
    failure: CommandResult | None = None
    #: Warnings from the executor, mint first.
    warnings: tuple[str, ...] = ()
    #: A pre-mint refusal that is not a command result (reason, message).
    refusal: tuple[str, str] | None = None


def _preflight_refusal(
    old: ApiKeyRecord, *, grace: timedelta | None, actor_key_id: str | None
) -> tuple[str, str] | None:
    """``(reason, message)`` when ``old`` must not be rotated, else ``None``.

    Everything here is decided before anything is minted.
    """
    key_id = old.key_id
    if old.revoked_at is not None:
        return ("already_revoked", f"Cannot rotate {key_id!r}: it is revoked.")
    if old.expires_at is not None:
        # A key with an expiry is already ending: rotating it would either
        # revive a key that no longer verifies (the retire step would write a
        # later expiry over the past one) or silently push out a grace period
        # somebody chose. Refuse both and say which it is.
        when = old.expires_at.isoformat()
        if key_is_expired(old):
            return (
                "already_expired",
                (
                    f"Cannot rotate {key_id!r}: it expired at {when} and no longer "
                    "verifies. Mint a new key instead."
                ),
            )
        return (
            "already_in_grace",
            (
                f"Cannot rotate {key_id!r}: it is already in a grace period that "
                f"ends at {when}. Rotating it again would extend that window, so "
                "it is refused; revoke it to end the window early."
            ),
        )
    if grace is None and actor_key_id == key_id:
        return (
            "cannot_revoke_own_key",
            (
                f"Cannot rotate {key_id!r} without a grace period: it is the "
                "key this request is authenticated with, and rotating retires it. "
                "Pass a grace period, or rotate it with another admin key or "
                "'trellis admin api-keys rotate'."
            ),
        )
    return None


def rotate_key(
    store: ApiKeyStore,
    execute: Callable[[Command], CommandResult],
    old_key_id: str,
    *,
    grace: timedelta | None = None,
    new_name: str | None = None,
    requested_by: str,
    actor_key_id: str | None = None,
    actor_key_name: str | None = None,
) -> RotationOutcome:
    """Mint a successor with the old key's scopes and defaults, then retire the old.

    Two audited governed operations, mint then retire, never one: a crash or a
    refusal between them leaves BOTH keys valid and never zero, and the outcome
    says so (``retired="still_valid"``). Retiring is a revoke, or, with
    ``grace``, an ``expires_at`` on the old row so it keeps verifying until the
    window closes (nothing is revoked, so there is no second event to forget).
    Rotating the key a request is authenticated with is refused up front when it
    would revoke it (the revoke handler's own rule), before anything is minted.
    A revoked key, an expired key (rotation never revives one) and a key already
    in a grace period (a second rotation would mint another successor and move
    the window) are refused the same way, with nothing minted.
    """
    old = store.get(old_key_id)
    if old is None:
        return RotationOutcome(
            old_key_id=old_key_id,
            retired="none",
            refusal=(
                "unknown_key_id",
                f"Cannot rotate {old_key_id!r}: unknown key id.",
            ),
        )
    refusal = _preflight_refusal(old, grace=grace, actor_key_id=actor_key_id)
    if refusal is not None:
        return RotationOutcome(old_key_id=old_key_id, retired="none", refusal=refusal)
    token, record, mint = build_mint_command(
        new_name or old.name,
        list(old.scopes),
        requested_by=requested_by,
        actor_key_id=actor_key_id,
        actor_key_name=actor_key_name,
        defaults=old.defaults,
        rotated_from=old_key_id,
    )
    minted = execute(mint)
    if minted.status != CommandStatus.SUCCESS:
        return RotationOutcome(old_key_id=old_key_id, retired="none", failure=minted)
    warnings = tuple(minted.warnings)
    expires_at = utc_now() + grace if grace is not None else None
    retire = (
        build_update_command(
            old_key_id,
            expires_at=expires_at,
            requested_by=requested_by,
            actor_key_id=actor_key_id,
            actor_key_name=actor_key_name,
        )
        if expires_at is not None
        else build_revoke_command(
            old_key_id,
            requested_by=requested_by,
            actor_key_id=actor_key_id,
            actor_key_name=actor_key_name,
        )
    )
    try:
        retired = execute(retire)
    except Exception as exc:
        # The new key exists and its token must reach the operator, so a retire
        # that raises is an outcome, not an escape. Only the type is kept: a
        # driver's message can hold a DSN.
        retired = CommandResult(
            command_id=retire.command_id,
            status=CommandStatus.FAILED,
            operation=retire.operation,
            message=f"retiring the old key raised {type(exc).__name__}",
        )
    if retired.status != CommandStatus.SUCCESS:
        return RotationOutcome(
            old_key_id=old_key_id,
            retired="still_valid",
            token=token,
            record=record,
            failure=retired,
            warnings=warnings,
        )
    return RotationOutcome(
        old_key_id=old_key_id,
        retired="grace" if grace is not None else "revoked",
        token=token,
        record=record,
        old_expires_at=expires_at,
        warnings=(*warnings, *retired.warnings),
    )
