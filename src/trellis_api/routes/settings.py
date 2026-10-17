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

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.settings_registry import (
    GROUP_LABELS,
    SETTINGS_REGISTRY,
    SettingSpec,
    get_setting,
    resolve_source,
    validate_value,
)
from trellis.core.write_config import WriteBehaviourConfig
from trellis.errors import ConfigError
from trellis.learning.candidate_auto_promote import (
    resolve_candidate_auto_promote_overrides,
)
from trellis.learning.precedent_auto_demote import (
    resolve_precedent_auto_demote_overrides,
)
from trellis.learning.tuners.auto_promote import resolve_auto_promote_overrides
from trellis.mutate import Command, CommandStatus, Operation, build_settings_executor
from trellis.ops.capture_health import resolve_capture_thresholds
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


def resolve_env_pinned_bool_setting(
    name: str, env_fn: Callable[[], bool], settings_values: dict[str, Any]
) -> bool:
    """One env-pinned boolean registry entry's effective value.

    env > settings > default -- the loop inside :func:`_effective_values`
    below (``GET /settings``'s ``effective_value``) and the Advisories
    route's "withheld (legacy)" badge both call this, so neither can
    drift from the other's precedence for the same setting.
    """
    spec = SETTINGS_REGISTRY[name]
    source = resolve_source(spec, settings_values)
    if source == "env":
        return env_fn()
    if source == "settings":
        return bool(settings_values[name])
    return bool(spec.default)


def _effective_values(
    settings_values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
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

    Returns ``(effective, parse_errors)``. ``WriteBehaviourConfig``'s own
    twelve fields can't land in ``parse_errors``: every one of its
    per-field helpers (``_truthy``, ``_min_confidence``, the clamps)
    already degrades a garbage value to a default with a warning log,
    never a raised exception. The three resolvers below make no such
    promise -- ``resolve_auto_promote_overrides``,
    ``resolve_candidate_auto_promote_overrides`` and
    ``resolve_precedent_auto_demote_overrides`` do ``int(float(raw))`` (or
    raise a value-free ``ValueError`` on a bad ``mode``) on a non-blank
    env value and let ``ValueError``/``TypeError``/``OverflowError`` fly,
    by design, for their real caller (the CLI / worker), which wants a
    loud failure. This route is read by an operator who did not necessarily
    set the bad value and must still see every *other* setting, so a
    family that fails to resolve degrades to each of its entries'
    registry defaults here, with a FIXED, value-free reason recorded
    against each affected entry name (never the raw exception text,
    which can echo the offending stored/env value) rather than raised
    past this function.

    The remaining registry entries each have a dedicated resolver with
    the exact same env-beats-settings-beats-default precedence, built
    for the production call site rather than for this route -- calling
    it here, instead of re-deriving the value from ``spec`` generically,
    is what keeps this list from drifting from what a request actually
    gets:

    * :func:`trellis.classify.demotion_gate.resolve_demotion_thresholds`
      for ``noise_demotion_min_attributed_packs`` /
      ``noise_demotion_min_unhelpful_citations``.
    * :func:`trellis.retrieve.advisory_generator.resolve_advisory_thresholds`
      for ``advisory_min_sample_size`` / ``advisory_min_effect_size``.
    * :func:`trellis.ops.capture_health.resolve_capture_thresholds` for
      ``capture_warn_threshold`` / ``capture_warn_window_hours``.
    * :func:`trellis_workers.session_capture.sweep.strict_mode` for
      ``capture_strict`` (imported locally -- ``trellis_api`` otherwise
      has no reason to load the worker package at import time).
    * :func:`trellis.retrieve.class_prior.resolve_class_prior_config` for
      ``class_prior_mode`` -- like the knobs above, it never raises, so it
      needs no degrade-to-default handling.

    ``graph_max_subgraph_depth`` is the one entry in this second group
    that is *not* settings-live: :data:`trellis.stores.base.graph.MAX_SUBGRAPH_DEPTH`
    is resolved once at import, from the environment only -- the
    settings store is never consulted, so, unlike every env-pinned knob
    above, a restart does not make a stored override take effect either.
    ``SettingsUpdateHandler`` refuses a write that would set one
    (``settings_writable=False``); this function still never echoes a
    stray legacy row as if it were effective, always calling the real
    resolver below regardless of what ``settings_values`` holds.
    """
    effective = WriteBehaviourConfig.from_env_and_settings(
        settings=settings_values
    ).as_dict()
    for name, env_fn in (
        ("graph_seeding", _graph_seeding_enabled),
        ("filter_legacy_advisories", _filter_legacy_advisories_enabled),
    ):
        effective[name] = resolve_env_pinned_bool_setting(name, env_fn, settings_values)

    from trellis.classify.demotion_gate import (  # noqa: PLC0415
        resolve_demotion_thresholds,
    )
    from trellis.retrieve.advisory_generator import (  # noqa: PLC0415
        resolve_advisory_thresholds,
    )
    from trellis_workers.session_capture.sweep import strict_mode  # noqa: PLC0415

    (
        effective["noise_demotion_min_attributed_packs"],
        effective["noise_demotion_min_unhelpful_citations"],
    ) = resolve_demotion_thresholds(settings_values)
    (
        effective["advisory_min_sample_size"],
        effective["advisory_min_effect_size"],
    ) = resolve_advisory_thresholds(settings_values)
    (
        effective["capture_warn_threshold"],
        effective["capture_warn_window_hours"],
    ) = resolve_capture_thresholds(settings_values)
    effective["capture_strict"] = strict_mode(settings_values)

    # Always the real resolver, regardless of what settings_values holds:
    # no code path reads a stored override for this knob (B3), so echoing
    # one back here as "effective" would be exactly the lie this route
    # exists to avoid. _resolve_max_subgraph_depth() already falls back
    # to the spec default (6) on an unset or invalid env var.
    from trellis.stores.base.graph import (  # noqa: PLC0415
        _resolve_max_subgraph_depth,
    )

    effective["graph_max_subgraph_depth"] = _resolve_max_subgraph_depth()

    # resolve_class_prior_config degrades an unrecognised env value to the
    # shipped default with a logged warning -- it never raises -- so this
    # needs no try/except, unlike the three families below.
    from trellis.retrieve.class_prior import (  # noqa: PLC0415
        resolve_class_prior_config,
    )

    class_prior_config = resolve_class_prior_config(settings=settings_values)
    effective["class_prior_mode"] = class_prior_config.mode

    # The three auto-promote/demote families resolve env > settings >
    # default here, through the same resolver functions every other
    # caller of AutoPromotePolicy, LearningCandidateAutoPromotePolicy and
    # PrecedentAutoDemotePolicy routes through, just without their own
    # real caller's config.yaml layer underneath: this route has no
    # access to trellis_cli.worker's yaml loader, and a third layer below
    # "default" cannot change which of env/settings/default resolve_source
    # already reported. An
    # "effective_value" that silently ignored config.yaml would only
    # mislead when the stored/default value IS what's reported and
    # config.yaml sets something else -- the same honest-preview caveat
    # settings_live=False already carries for minhash_seed_max_docs.
    parse_errors: dict[str, str] = {}
    for prefix, resolve in (
        (
            "learning.auto_promote.",
            lambda: resolve_auto_promote_overrides(settings=settings_values),
        ),
        (
            "learning.candidate_auto_promote.",
            lambda: resolve_candidate_auto_promote_overrides(settings=settings_values),
        ),
        (
            "learning.precedent_auto_demote.",
            lambda: resolve_precedent_auto_demote_overrides(settings=settings_values),
        ),
    ):
        try:
            resolved = resolve()
        except (ValueError, TypeError, OverflowError, ConfigError):
            # ``ConfigError`` is a stored value the setting refuses
            # (a hand-edited ``mode: commit``); it degrades the same way.
            # One bad knob in a family must not blank every other
            # setting off the response -- degrade every entry in *this*
            # family to its registry default and record a FIXED,
            # value-free reason (never the raw exception text, which
            # can echo the offending stored/env value back through this
            # route) against each affected entry, rather than letting
            # the exception reach list_settings() and turn the whole
            # route into a 500. A typo'd
            # TRELLIS_CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT must not
            # take down every other operator control. ``OverflowError``
            # is a real member of this family, not a defensive extra:
            # a stored or env ``Infinity``/``1e999`` round-trips through
            # ``int(float(raw))`` inside the two resolvers and raises
            # ``OverflowError``, not ``ValueError`` (see
            # ``parse_finite_int``'s docstring in ``write_config.py``).
            for spec in SETTINGS_REGISTRY.values():
                if spec.name.startswith(prefix):
                    effective[spec.name] = spec.default
                    parse_errors[spec.name] = _family_parse_error_reason()
            continue
        for field, value in resolved.items():
            effective[f"{prefix}{field}"] = value
    return effective, parse_errors


def _entry(
    spec: SettingSpec,
    settings_values: dict[str, Any],
    effective: dict[str, Any],
    stored_rows: dict[str, SettingRow],
    parse_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    stored_row = stored_rows.get(spec.name)
    return {
        "name": spec.name,
        "label": spec.label,
        "type": spec.type,
        "default": spec.default,
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        # Whether ``minimum`` excludes the boundary itself (``value >
        # minimum`` required, not ``>=``) -- the UI renders "> N" instead
        # of "min N" when this is set, since "min 0" reads as 0 being a
        # valid value for a knob that actually refuses it.
        "minimum_exclusive": spec.minimum_exclusive,
        "enum": list(spec.enum) if spec.enum is not None else None,
        # Members of ``enum`` that are structurally valid but refused at
        # write time (e.g. ``"commit"`` before auto-demotion ships) --
        # kept in ``enum`` so the catalog still documents them as real
        # future options, but named here so a renderer can grey one out
        # instead of offering a value every save of it will refuse.
        "refused_values": list(spec.refused_values),
        "refused_reason": spec.refused_reason,
        "description": spec.description,
        "risk_note": spec.risk_note,
        "surfaces": list(spec.surfaces),
        "restart_required": spec.restart_required,
        "risk": spec.risk,
        "group": spec.group,
        "settings_live": spec.settings_live,
        "settings_writable": spec.settings_writable,
        "env_var": spec.env_var,
        "effective_value": effective[spec.name],
        # For a non-writable knob, ``resolve_source`` would still answer
        # "settings" off a stray row on disk even though effective_value
        # (above) never reads it -- the two fields would then name
        # different sources for the same row. Collapse that one case to
        # "default" so the two agree: a non-writable knob's effective
        # value only ever comes from its environment variable or the
        # shipped default, never a stored row.
        "overridden_by": (
            "default"
            if not spec.settings_writable
            and resolve_source(spec, settings_values) == "settings"
            else resolve_source(spec, settings_values)
        ),
        # The raw override on disk, if any -- distinct from
        # effective_value, which may instead be the env or default value
        # while a stored override sits inert underneath it.
        "stored_value": stored_row.value if stored_row is not None else None,
        # Re-gate #867 B4/B6: a malformed stored row (``1e999``, ``nan``,
        # a string where an int belongs) makes every resolver above fall
        # back to the default silently -- correct for the knob's own
        # behaviour, but invisible here, where ``overridden_by`` would
        # still read "settings" off the row's mere presence
        # (``resolve_source`` checks presence, not validity) while
        # ``effective_value`` quietly differs from ``stored_value``. This
        # re-runs the same ``validate_value`` the governed write path
        # already enforces, against the row already on disk, so a value
        # that predates a tightened bound or was written outside the
        # governed path (direct settings.json edit) surfaces as a fact
        # the UI can show next to the mismatched pair above, not
        # something a caller has to notice by diffing two fields
        # themselves. ``None`` when there is no stored row, or it is
        # valid. A FIXED reason string otherwise -- never the raw
        # ``stored_value`` above, which this field exists to flag as
        # untrustworthy, not re-echo under a new name.
        # Checked in this order because a family-level failure already
        # won: it means the environment or settings resolver for the
        # whole family (learning.auto_promote.*,
        # learning.candidate_auto_promote.*) raised, so every entry in
        # it -- not just one with a bad stored row -- has already had
        # its effective_value forced to the registry default above. The
        # stored-row check only runs once that is ruled out, since a
        # row can be individually invalid even when its family resolved
        # fine overall (e.g. one other entry's env var is what's bad).
        # Either branch is a FIXED, value-free reason string -- never
        # the raw stored or env value, which is what this field exists
        # to flag as untrustworthy, not re-echo under a new name.
        "parse_error": (
            (parse_errors or {}).get(spec.name)
            or (
                _stored_value_parse_error(spec, stored_row.value)
                if stored_row is not None
                else None
            )
        ),
        # "Last changed ... when". The store never
        # records who changed a setting -- SettingRow carries no
        # changed_by field, and SettingsUpdateHandler's changed_by is
        # audit-event-only -- so this reports only what the store
        # actually knows rather than fabricating an attribution.
        "updated_at": (
            stored_row.updated_at.isoformat() if stored_row is not None else None
        ),
    }


def _stored_value_parse_error(spec: SettingSpec, raw: Any) -> str | None:
    """``None`` if the registry's own coercion accepts ``raw``, else a
    FIXED reason string -- never the value itself.

    Mirrors the ``except (TypeError, ValueError)`` every dedicated
    resolver in this PR uses around the same ``validate_value`` call
    (``demotion_gate``, ``advisory_generator``, ``capture_health``) --
    ``validate_value`` itself never raises ``OverflowError`` (its
    ``_coerce_int``/``_coerce_float`` check finiteness before casting),
    so that is not a gap here either.

    The reason text is keyed only on ``spec.type``/``spec.enum`` -- never
    on ``raw`` -- so a stored secret-shaped string that fails validation
    (a mistyped knob holding a credential, say) cannot resurface through
    this field. That is narrower than it may sound: ``_entry`` above
    already puts the raw override on the same response, unconditionally,
    as ``stored_value`` -- a different field built for a different job
    (showing an operator what is actually on disk). This field's job is
    only to say that row is unusable, never to be a second, redundant
    echo of it.
    """
    try:
        validate_value(spec, raw)
    except (TypeError, ValueError):
        return _parse_error_reason(spec)
    return None


def _parse_error_reason(spec: SettingSpec) -> str:
    """The fixed, value-free reason text for ``spec``'s type.

    States only that ``validate_value`` rejects the stored row, never
    what happens next. An earlier wording appended "; using the default"
    unconditionally, which does not hold for every knob this field
    covers: an env-pinned knob ignores the stored row either way (the
    environment variable wins, not a default); ``WriteBehaviourConfig``'s
    own boolean fields accept a stored ``1``/``1.0`` that this registry's
    stricter ``validate_value`` rejects; and ``minhash_seed_max_docs``,
    ``reconcile_timeout_s`` and ``reconcile_model`` are resolved by
    dedicated code that is unbounded, accepts ``inf``, or never checks
    the type the way this field's probe does. The field still does its
    job -- it flags that the registry's own validation disagrees with
    what is on disk -- without asserting a specific resolution this
    function cannot see.
    """
    if spec.type == "bool":
        return "stored value is not a recognized boolean"
    if spec.type == "int":
        return "stored value is not an integer in range"
    if spec.type == "float":
        return "stored value is not a finite number in range"
    if spec.enum is not None:
        return "stored value is not one of the allowed options"
    return "stored value failed validation"


def _family_parse_error_reason() -> str:
    """The fixed, value-free reason for a whole-family resolution failure.

    Used when ``resolve_auto_promote_overrides``,
    ``resolve_candidate_auto_promote_overrides`` or
    ``resolve_precedent_auto_demote_overrides`` raises: unlike
    :func:`_parse_error_reason`, the culprit is not necessarily *this*
    entry's own stored row -- it can be another entry in the same
    family whose environment variable is the one that is malformed, so
    the wording does not name "stored value" specifically. Still never
    the raw exception text or the offending value itself, which can
    carry the bad env var's contents. Drops the same retired "; using
    the default" suffix :func:`_parse_error_reason` explains above, kept
    parallel across both reasons rather than correct for one and stale
    on the other -- this one states only that resolution failed, not
    which of this family's several entries the caller should expect to
    see at their registry default because of it.
    """
    return (
        "this setting's family could not be resolved from the "
        "environment or stored overrides"
    )


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
    effective, parse_errors = _effective_values(settings_values)
    return {
        "settings": [
            _entry(spec, settings_values, effective, stored_rows, parse_errors)
            for spec in SETTINGS_REGISTRY.values()
        ],
        "store_degradation": (
            degradation.to_dict() if degradation is not None else None
        ),
        # This process's own read, not a fleet-wide value: a
        # multi-container deployment has one settings.json per container
        # unless it is bind-mounted, and nothing about this route lets a
        # caller tell the two cases apart without this label.
        "read_by": "trellis-api",
        # Human-facing group labels, keyed the same way as each entry's
        # "group" field, so the UI's filter box never hardcodes a display
        # name.
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

    effective_value = _effective_values(settings_values)[0][name]
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
