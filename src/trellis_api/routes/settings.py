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

from fastapi import APIRouter, Depends, HTTPException

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.settings_registry import (
    GROUP_LABELS,
    SETTINGS_REGISTRY,
    SettingSpec,
    get_setting,
    resolve_source,
)
from trellis.core.write_config import WriteBehaviourConfig
from trellis.learning.candidate_auto_promote import (
    resolve_candidate_auto_promote_overrides,
)
from trellis.learning.tuners.auto_promote import resolve_auto_promote_overrides
from trellis.mutate import Command, CommandStatus, Operation, build_settings_executor
from trellis.retrieve.builder_factory import _graph_seeding_enabled
from trellis.retrieve.pack_builder import _filter_legacy_advisories_enabled
from trellis.schemas.settings import SettingRow
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
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

    ``graph_seeding`` and ``filter_legacy_advisories`` are not
    :class:`WriteBehaviourConfig` fields (see ``settings_registry``'s
    module docstring), and each has its own real environment semantics
    that are asymmetric with every other boolean here -- unset *or
    unrecognised* means on, only an explicit falsey token turns it off
    (:func:`trellis.retrieve.builder_factory._graph_seeding_enabled`,
    :func:`trellis.retrieve.pack_builder._filter_legacy_advisories_enabled`).
    Reimplementing that asymmetry here would risk drifting from the one
    real implementation, so this calls each directly when the environment
    is what governs; when it is not, the stored/default value is reported
    the same way the other fields are (an optimistic preview -- see
    ``settings_live``, which is ``False`` for ``graph_seeding`` precisely
    because that preview is not yet wired to any real call site).
    """
    effective = WriteBehaviourConfig.from_env_and_settings(
        settings=settings_values
    ).as_dict()
    for name, env_fn in (
        ("graph_seeding", _graph_seeding_enabled),
        ("filter_legacy_advisories", _filter_legacy_advisories_enabled),
    ):
        spec = SETTINGS_REGISTRY[name]
        source = resolve_source(spec, settings_values)
        if source == "env":
            effective[name] = env_fn()
        elif source == "settings":
            effective[name] = bool(settings_values[name])
        else:
            effective[name] = spec.default

    # The two auto-promote families resolve env > settings > default here,
    # through the same two resolver functions every other caller of
    # AutoPromotePolicy and LearningCandidateAutoPromotePolicy routes
    # through, just without their own real caller's config.yaml layer
    # underneath: this route has no access to trellis_cli.worker's yaml
    # loader, and a third layer below "default" cannot change which of
    # env/settings/default resolve_source already reported. An
    # "effective_value" that silently ignored config.yaml would only
    # mislead when the stored/default value IS what's reported and
    # config.yaml sets something else -- the same honest-preview caveat
    # settings_live=False already carries for minhash_seed_max_docs.
    for field, value in resolve_auto_promote_overrides(
        settings=settings_values
    ).items():
        effective[f"learning.auto_promote.{field}"] = value
    for field, candidate_value in resolve_candidate_auto_promote_overrides(
        settings=settings_values
    ).items():
        effective[f"learning.candidate_auto_promote.{field}"] = candidate_value
    return effective


def _entry(
    spec: SettingSpec,
    settings_values: dict[str, Any],
    effective: dict[str, Any],
    stored_rows: dict[str, SettingRow],
) -> dict[str, Any]:
    stored_row = stored_rows.get(spec.name)
    return {
        "name": spec.name,
        "label": spec.label,
        "type": spec.type,
        "default": spec.default,
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        "enum": list(spec.enum) if spec.enum is not None else None,
        "description": spec.description,
        "risk_note": spec.risk_note,
        "surfaces": list(spec.surfaces),
        "restart_required": spec.restart_required,
        "risk": spec.risk,
        "group": spec.group,
        "settings_live": spec.settings_live,
        "env_var": spec.env_var,
        "effective_value": effective[spec.name],
        "overridden_by": resolve_source(spec, settings_values),
        # The raw override on disk, if any -- distinct from
        # effective_value, which may instead be the env or default value
        # while a stored override sits inert underneath it (brief e179
        # step 2's env-pin truthfulness).
        "stored_value": stored_row.value if stored_row is not None else None,
        # "Last changed ... when" (brief e179 step 1). The store never
        # records who changed a setting -- SettingRow carries no
        # changed_by field, and SettingsUpdateHandler's changed_by is
        # audit-event-only -- so this reports only what the store
        # actually knows rather than fabricating an attribution.
        "updated_at": (
            stored_row.updated_at.isoformat() if stored_row is not None else None
        ),
    }


def _execute_command(cmd: Command) -> CommandResponse:
    """Execute a command through the mutation pipeline.

    Mirrors ``trellis_api.routes.curate``'s own module-private helper of
    the same name exactly -- FAILED/REJECTED both read as a 400-class
    outcome to the caller, and a SUCCESS/DUPLICATE result is projected
    onto the wire through ``command_response``, the one allowed
    construction site (``tests/unit/api/test_command_response_rule.py``).
    """
    result = build_settings_executor(get_registry()).execute(cmd)
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        reason = result.metadata.get("rejection_reason")
        if reason in ("degraded_store_write", "stale_store_write"):
            # 409, matching routes/policies.py's precedent for the same
            # error class: the write did not happen and the file on disk
            # is unharmed, but "fix your input" (400) is the wrong
            # instruction. The recovery command IS folded into `message`
            # by StoreWriteRefusedError (trellis.errors) -- but
            # sanitize_error_message's 512-char cap can truncate it away
            # on a long stores_dir path, so `recovery` and (for a
            # degraded load) the real `store_degradation` are read fresh
            # from the store here and carried as their own fields,
            # uncapped, the way routes/policies.py's refusal does.
            store = _get_settings_store(get_registry())
            degradation = store.degradation
            recovery = (
                degradation.recovery
                if degradation is not None
                else store.stale_recovery
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": reason,
                    "message": sanitize_error_message(result.message),
                    "recovery": recovery,
                    "store_degradation": (
                        degradation.to_dict() if degradation is not None else None
                    ),
                },
            )
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
    stored_rows = {row.name: row for row in store.list()}
    degradation = store.degradation
    effective = _effective_values(settings_values)
    return {
        "settings": [
            _entry(spec, settings_values, effective, stored_rows)
            for spec in SETTINGS_REGISTRY.values()
        ],
        "store_degradation": (
            degradation.to_dict() if degradation is not None else None
        ),
        # This process's own read, not a fleet-wide value (plan
        # p1-ui-control-plane.md's PR 3/4 acceptance check): a
        # multi-container deployment has one settings.json per container
        # unless it is bind-mounted, and nothing about this route lets a
        # caller tell the two cases apart without this label.
        "read_by": "trellis-api",
        # Human-facing group labels, keyed the same way as each entry's
        # "group" field, so the UI's filter box never hardcodes a display
        # name (brief e179 step 8).
        "groups": dict(GROUP_LABELS),
    }


@router.put("/settings/{name}")
@router.post("/settings/{name}")
def update_setting(
    name: str,
    req: SettingUpdateRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> CommandResponse | dict[str, Any]:
    """Write (or, with ``value: null``, remove) one setting's override.

    Routes through ``Operation.SETTINGS_UPDATE`` -- unknown name, wrong
    type and out-of-range are all refused there, never clamped, and
    Stage 2 runs before the handler ever touches the file. A write for a
    setting whose environment variable is currently set still stores the
    override (so it is ready once the variable is unset); the response
    says so rather than letting the caller believe it just took effect.

    ``changed_by`` on the audit event is derived from ``ctx``, the
    *authenticated* principal that ``admin_auth`` already resolved for
    this request -- not ``req.requested_by``, which is free text the
    client chose and could set to anyone's name. ``ctx`` is declared as
    an explicit parameter here (rather than read off the request) so
    FastAPI's per-request dependency cache reuses the already-resolved
    context from the route's own scope gate instead of authenticating
    twice. ``req.requested_by`` is still recorded, but only as a
    separate *claimed* field (``args["requested_by_claim"]``), surfaced
    on the event as ``requested_by_claim`` rather than overwriting
    ``changed_by``.
    """
    principal = ctx.key_id or ctx.name or "unknown"
    cmd = Command(
        operation=Operation.SETTINGS_UPDATE,
        args={
            "name": name,
            "value": req.value,
            "requested_by_claim": req.requested_by,
        },
        target_id=name,
        target_type="setting",
        requested_by=f"api:settings:{principal}",
    )
    # Unknown name is NOT pre-checked here: it goes through the same
    # governed path as every other refusal reason (wrong type,
    # out-of-range, degraded store), so Stage 2 and the audit event cover
    # it too -- the handler raises ValidationError, which _execute_command
    # below turns into this 400 (or 409 for a degraded/stale store), not
    # a different code path.
    response = _execute_command(cmd)

    # A non-raising response above proves the handler accepted `name` as
    # registered -- an unknown name is rejected inside the governed write
    # itself, so `_execute_command` would have raised before this line.
    spec = get_setting(name)
    if spec is None:  # pragma: no cover -- see comment above; not reachable
        msg = f"{name!r} passed the governed write but is not registered"
        raise HTTPException(status_code=500, detail=msg)

    registry = get_registry()
    store = _get_settings_store(registry)
    settings_values = store.as_values()
    source = resolve_source(spec, settings_values)

    # The normalized, parsed value the handler actually stored -- never
    # the raw request body. A reset (`req.value is None`) leaves nothing
    # stored, so this reads back `None` too.
    stored_row = store.get(name)
    stored_value = stored_row.value if stored_row is not None else None

    restart_note = (
        f"{name!r} is restart_required: this change applies after a "
        "restart, not to the current process."
        if spec.restart_required
        else None
    )

    if source != "env":
        return {
            **response.model_dump(),
            "settings_live": spec.settings_live,
            "restart_required": spec.restart_required,
            "note": restart_note,
        }

    effective_value = _effective_values(settings_values)[name]
    if req.value is None:
        note = (
            f"{spec.env_var} is set, so it governs {name!r} right now; "
            "the override was removed, and nothing is stored behind it "
            "-- the environment variable keeps winning once it stays set."
        )
    else:
        note = (
            f"{spec.env_var} is set, so it governs {name!r} right now; "
            "the stored value takes effect once that variable is unset."
        )
    if spec.restart_required:
        note = f"{note} {restart_note}"
    return {
        "status": response.status,
        "command_id": response.command_id,
        "name": name,
        "accepted": True,
        "stored_value": stored_value,
        "effective_value": effective_value,
        "overridden_by": "env",
        "settings_live": spec.settings_live,
        "restart_required": spec.restart_required,
        "note": note,
    }
