"""Settings routes — list the catalog and write one governed override.

Mounted with the admin scope only (see ``trellis_api/app.py``), same as
``admin.router``: these are operator controls, not data a caller reads to
do its own job. ``GET /settings`` reports every
:data:`~trellis.core.settings_registry.SETTINGS_REGISTRY` entry's static
metadata plus its *resolved* ``effective_value``/``overridden_by``;
``PUT``/``POST /settings/{name}`` writes one value through
``Operation.SETTINGS_UPDATE`` -- the same governed path
``trellis admin settings set`` uses, so a denying policy blocks both
surfaces identically (Stage 2 always runs, CLAUDE.md).

Env-pinned writes still succeed
--------------------------------
Writing a value for a setting whose environment variable is currently
set is **not** a refusal: the governed write still stores the override
(so it is ready the moment the env var is unset), and this route answers
200 with ``accepted: true``, the environment value that actually governs
right now, and a note saying so -- rather than either silently losing the
write or claiming the stored value is already in force.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.settings_registry import (
    SETTINGS_REGISTRY,
    SettingSpec,
    get_setting,
    resolve_source,
)
from trellis.core.write_config import WriteBehaviourConfig
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.retrieve.builder_factory import _graph_seeding_enabled
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path
from trellis_api.app import get_registry
from trellis_api.routes._results import command_response
from trellis_wire.dtos import CommandResponse, SettingUpdateRequest

router = APIRouter()


def _get_settings_store(registry: StoreRegistry) -> SettingsStore:
    """Build a settings store from the registry's stores directory.

    Not cached, for the same reason ``policies.py``'s
    ``_get_policy_store`` is not: re-reading per call is what makes "an
    edit takes effect without a restart" true, and the alternative risks
    a stale in-memory copy clobbering a concurrent writer's row on this
    process's next write. :meth:`SettingsStore.refuse_if_stale` backstops
    the window this cannot close.
    """
    path = resolve_settings_path(registry.stores_dir)
    if path is None:
        msg = "stores_dir must be set on registry to use SettingsStore"
        raise ValueError(msg)
    return SettingsStore(path)


def _effective_values(settings_values: dict[str, Any]) -> dict[str, Any]:
    """Resolved value for every registry entry, env > settings > default.

    The twelve :class:`WriteBehaviourConfig` fields go through
    :meth:`WriteBehaviourConfig.from_env_and_settings` -- the exact
    resolution ``trellis admin write-config`` and ``GET /api/version``
    already report, so this route stays in sync with those two surfaces
    rather than adding a third, independently-drifting computation.

    ``graph_seeding`` is not a ``WriteBehaviourConfig`` field (see
    ``settings_registry``'s module docstring), and its real environment
    semantics are asymmetric with every other boolean here -- unset *or
    unrecognised* means on, only an explicit falsey token turns it off
    (:func:`trellis.retrieve.builder_factory._graph_seeding_enabled`).
    Reimplementing that asymmetry here would risk drifting from the one
    real implementation, so this calls it directly when the environment
    is what governs; when it is not, the stored/default value is reported
    the same way the twelve other fields are (an optimistic preview --
    see ``settings_live``, which is ``False`` for this entry precisely
    because that preview is not yet wired to any real call site).
    """
    effective = WriteBehaviourConfig.from_env_and_settings(
        settings=settings_values
    ).as_dict()
    spec = SETTINGS_REGISTRY["graph_seeding"]
    source = resolve_source(spec, settings_values)
    if source == "env":
        effective["graph_seeding"] = _graph_seeding_enabled()
    elif source == "settings":
        effective["graph_seeding"] = bool(settings_values["graph_seeding"])
    else:
        effective["graph_seeding"] = spec.default
    return effective


def _entry(
    spec: SettingSpec, settings_values: dict[str, Any], effective: dict[str, Any]
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "type": spec.type,
        "default": spec.default,
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        "enum": list(spec.enum) if spec.enum is not None else None,
        "description": spec.description,
        "surfaces": list(spec.surfaces),
        "restart_required": spec.restart_required,
        "risk": spec.risk,
        "settings_live": spec.settings_live,
        "env_var": spec.env_var,
        "effective_value": effective[spec.name],
        "overridden_by": resolve_source(spec, settings_values),
    }


def _execute_command(cmd: Command) -> CommandResponse:
    """Execute a command through the mutation pipeline.

    Mirrors ``trellis_api.routes.curate``'s own module-private helper of
    the same name exactly -- FAILED/REJECTED both read as a 400-class
    outcome to the caller, and a SUCCESS/DUPLICATE result is projected
    onto the wire through ``command_response``, the one allowed
    construction site (``tests/unit/api/test_command_response_rule.py``).
    """
    result = build_curate_executor(get_registry()).execute(cmd)
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(result.message)
        )
    return command_response(result)


@router.get("/settings")
def list_settings() -> dict[str, Any]:
    """Every registered setting's metadata, resolved value and provenance."""
    registry = get_registry()
    store = _get_settings_store(registry)
    settings_values = store.as_values()
    degradation = store.degradation
    effective = _effective_values(settings_values)
    return {
        "settings": [
            _entry(spec, settings_values, effective)
            for spec in SETTINGS_REGISTRY.values()
        ],
        "store_degradation": (
            degradation.to_dict() if degradation is not None else None
        ),
    }


@router.put("/settings/{name}")
@router.post("/settings/{name}")
def update_setting(
    name: str, req: SettingUpdateRequest
) -> CommandResponse | dict[str, Any]:
    """Write (or, with ``value: null``, remove) one setting's override.

    Routes through ``Operation.SETTINGS_UPDATE`` -- unknown name, wrong
    type and out-of-range are all refused there, never clamped, and
    Stage 2 runs before the handler ever touches the file. A write for a
    setting whose environment variable is currently set still stores the
    override (so it is ready once the variable is unset); the response
    says so rather than letting the caller believe it just took effect.
    """
    cmd = Command(
        operation=Operation.SETTINGS_UPDATE,
        args={"name": name, "value": req.value},
        target_id=name,
        target_type="setting",
        requested_by=req.requested_by,
    )
    # Unknown name is NOT pre-checked here: it goes through the same
    # governed path as every other refusal reason (wrong type,
    # out-of-range, degraded store), so Stage 2 and the audit event cover
    # it too -- the handler raises ValidationError, which _execute_command
    # below turns into this 400, not a different code path.
    response = _execute_command(cmd)

    # A non-raising response above proves the handler accepted `name` as
    # registered -- an unknown name is rejected inside the governed write
    # itself, so `_execute_command` would have raised before this line.
    spec = get_setting(name)
    if spec is None:  # pragma: no cover -- see comment above; not reachable
        msg = f"{name!r} passed the governed write but is not registered"
        raise HTTPException(status_code=500, detail=msg)

    registry = get_registry()
    settings_values = _get_settings_store(registry).as_values()
    source = resolve_source(spec, settings_values)
    if source != "env":
        return response

    effective_value = _effective_values(settings_values)[name]
    return {
        "status": response.status,
        "command_id": response.command_id,
        "name": name,
        "accepted": True,
        "stored_value": req.value,
        "effective_value": effective_value,
        "overridden_by": "env",
        "note": (
            f"{spec.env_var} is set, so it governs {name!r} right now; "
            "the stored value takes effect once that variable is unset."
        ),
    }
