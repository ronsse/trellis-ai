"""One resolver for a read-side integer knob: env beats settings beats default.

Five near-identical copies of this function accumulated across
``trellis.mcp.server``, ``trellis.ops.capture_health``,
``trellis.retrieve.builder_factory``, ``trellis_api.routes.retrieve`` and
``trellis.classify.demotion_gate`` (there as ``_resolve_int_threshold``).
Diffed side by side, the five bodies were identical in everything that is
*observable* — precedence (env > settings > module default), the bounds
check routed through the settings registry's own ``validate_value`` so an
out-of-bounds or non-finite value is rejected rather than silently
honoured, and what happens on a bad value (log a warning, fall back to
the default). The one difference was cosmetic: each module's warning
events were namespaced with that module's own name
(``mcp_server.bad_env_int`` vs. ``capture_health.bad_env_int`` and so on).
That difference is kept explicit here as the required ``warning_prefix``
keyword — callers are not changed to share one event name, only to share
one implementation.

Each former copy now keeps its own module-local wrapper (same name, same
signature, same call sites, same tests) delegating to
:func:`resolve_int_knob` below, so no caller's observable behaviour
changes: the warning event names, their fields, and what each caller
does with the final value are all exactly what they were before.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = ["resolve_int_knob"]


def resolve_int_knob(
    *,
    env_var: str,
    setting_name: str,
    default: int,
    settings: Mapping[str, Any] | None,
    warning_prefix: str,
) -> int:
    """Resolve one integer knob: env var, then a stored setting, then default.

    Both the env value and the stored value are coerced and bounds-checked
    through :func:`trellis.core.settings_registry.validate_value` — the
    same coercion the governed ``PUT /settings`` path uses — so a
    non-finite value (``nan``, ``inf``) is rejected instead of crashing
    later with an uncaught ``OverflowError``, and a value outside the
    setting's registered bounds is rejected rather than silently honoured.
    A bad value logs ``f"{warning_prefix}.bad_env_int"`` (fields: ``var``,
    ``error_type``) or ``f"{warning_prefix}.bad_settings_int"`` (fields:
    ``setting``, ``error_type``) and falls back to ``default`` — never the
    raw value, on either event.

    Imported lazily by every caller for the same import-cycle reason:
    ``trellis.core.settings_registry`` imports the learning tuners, which
    import ``trellis.ops.parameter_reachability``, which imports
    ``trellis.ops``'s ``__init__``.
    """
    from trellis.core.settings_registry import (  # noqa: PLC0415
        get_setting,
        validate_value,
    )

    bad_env_event = warning_prefix + ".bad_env_int"
    bad_settings_event = warning_prefix + ".bad_settings_int"

    spec = get_setting(setting_name)
    raw_env = os.environ.get(env_var, "").strip()
    if raw_env:
        try:
            return (
                int(validate_value(spec, raw_env)) if spec is not None else int(raw_env)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                bad_env_event,
                var=env_var,
                error_type=type(exc).__name__,
            )
            return default
    if settings is not None and setting_name in settings:
        raw_setting = settings[setting_name]
        try:
            return (
                int(validate_value(spec, raw_setting))
                if spec is not None
                else int(raw_setting)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                bad_settings_event,
                setting=setting_name,
                error_type=type(exc).__name__,
            )
            return default
    return default
