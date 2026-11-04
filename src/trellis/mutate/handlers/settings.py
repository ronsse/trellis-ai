"""The ``settings.update`` handler."""

from __future__ import annotations

from typing import Any

from trellis.core.settings_registry import get_setting, resolve_source, validate_value
from trellis.errors import (
    DegradedStoreWriteError,
    StaleStoreWriteError,
    ValidationError,
)
from trellis.mutate.commands import Command
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path


class SettingsUpdateHandler:
    """Validate and persist one settings-store override via SettingsStore.

    This is the governed write path the settings registry
    (:mod:`trellis.core.settings_registry`) was always meant to gate: a
    name/value pair is checked against the registry's declared
    ``type``/``minimum``/``maximum``/``enum`` before anything is written.

    ``args["value"] is None`` means **remove the override** — there is no
    separate ``SETTINGS_RESET`` operation. This keeps one verb for the
    knob's whole lifecycle (set, change, clear) instead of two operations
    that would need to agree on the same registry lookup and the same
    audit event shape; a caller already has to pass a literal ``null``
    to clear a JSON field everywhere else in this API, so this is not a
    new idiom.

    Every refusal — unknown name, wrong type, out of range, not in the
    declared enum, or the store itself degraded/stale — is raised as a
    :class:`~trellis.errors.ValidationError` so the executor's normal
    ``REJECTED`` path records the real reason. Nothing here clamps a bad
    value to the nearest bound or to the default: a bad write is refused,
    never silently corrected.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        name = command.args["name"]
        raw_value = command.args.get("value")

        # Defence in depth: this handler is only ever registered via
        # create_settings_handlers, reachable only through the admin
        # route/CLI, which both stamp target_type and
        # target_id correctly -- but an untargeted or mistargeted Command
        # reaching here by some future wiring mistake must still refuse
        # rather than write, exactly as an entity_type="setting" policy
        # expects to be able to gate this operation on those two fields.
        if command.target_type != "setting" or command.target_id != name:
            msg = (
                "settings.update requires target_type='setting' and "
                f"target_id={name!r}; got target_type={command.target_type!r}, "
                f"target_id={command.target_id!r}"
            )
            raise ValidationError(msg, errors=[msg], code="untargeted_command")

        spec = get_setting(name)
        if spec is None:
            msg = f"Unknown setting: {name!r}"
            raise ValidationError(msg, errors=[msg], code="unknown_setting")

        # settings_writable=False means no code path ever reads this
        # knob's stored value -- not even after a restart (that is
        # restart_required, which stays writable). Storing one anyway
        # would be accepted and echoed back by GET /settings as if it
        # mattered, with nothing that could ever make it real. Clearing
        # a stray legacy row (raw_value is None) is still allowed.
        if raw_value is not None and not spec.settings_writable:
            msg = (
                f"{name} is read from {spec.env_var} only, once at "
                "process start; the settings store is never consulted, "
                "so a stored value here would never take effect. Set "
                f"{spec.env_var} instead."
            )
            raise ValidationError(msg, errors=[msg], code="settings_write_refused")

        path = resolve_settings_path(self._registry.stores_dir)
        if path is None:
            # Same precedent as trellis_api.routes.policies._get_policy_store:
            # no stores_dir configured means no file-backed settings store
            # exists to write to. Not a caller-input problem (ValidationError
            # would mislabel it as one the caller can fix by retrying with a
            # different value), so this is left as an ordinary exception for
            # the executor's handler-panic path to turn into a FAILED result.
            msg = "stores_dir must be set on registry to use SettingsStore"
            raise ValueError(msg)
        store = SettingsStore(path)
        existing = store.get(name)
        old_value = existing.value if existing is not None else None

        if raw_value is None:
            try:
                removed = store.remove(name)
            except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
                raise ValidationError(
                    exc.message, errors=[exc.message], code=exc.code.lower()
                ) from exc
            new_value: bool | int | float | str | None = None
            action = "reset to default/env" if removed else "already unset"
        else:
            try:
                new_value = validate_value(spec, raw_value)
            except ValueError as exc:
                # validate_value always raises ValueError(msg) with a single
                # string argument composed by _coerce_*/_check_bounds, never
                # a wrapped driver/library exception, so args[0] is exactly
                # that message -- this is a structured-field read, not a
                # bare str(exc) stringification of the caught exception.
                msg = exc.args[0] if exc.args else "invalid value"
                raise ValidationError(msg, errors=[msg], code="invalid_value") from exc
            try:
                store.set(name, new_value)
            except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
                raise ValidationError(
                    exc.message, errors=[exc.message], code=exc.code.lower()
                ) from exc
            action = f"set to {new_value!r}"

        overridden_by = resolve_source(spec, store.as_values())

        payload: dict[str, Any] = {
            "name": name,
            "old_value": old_value,
            "new_value": new_value,
            "overridden_by_at_write": overridden_by,
            "changed_by": command.requested_by,
            "settings_live": spec.settings_live,
            "restart_required": spec.restart_required,
        }
        requested_by_claim = command.args.get("requested_by_claim")
        if requested_by_claim is not None:
            payload["requested_by_claim"] = requested_by_claim
        self._registry.operational.event_log.emit(
            EventType.SETTINGS_UPDATED,
            source="mutation_executor",
            entity_id=name,
            payload=payload,
        )
        return name, f"Setting {name!r} {action}"
