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

The seven pack-size / excerpt / disclosure knobs the settings route
reports live here too, as public ``resolve_*`` functions with their env
var, settings key and default beside them. They used to sit in three
places -- ``trellis.mcp.server``, ``trellis_api.routes.retrieve`` and
``trellis.retrieve.builder_factory`` -- and ``trellis_api.routes.settings``
imported the first one's *private* names to report them, a layering
inversion (a REST route reaching into the MCP server). Every call site and
the settings route now import from this module. Each knob keeps the
``warning_prefix`` of the module it came from, so a bad env or settings
value logs the same event name as before.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "BODY_ITEMS_ENV",
    "BODY_ITEMS_SETTING",
    "EXCERPT_MAX_CHARS_ENV",
    "EXCERPT_MAX_CHARS_SETTING",
    "GET_CONTEXT_MAX_ITEMS_DEFAULT",
    "GET_CONTEXT_MAX_ITEMS_ENV",
    "GET_CONTEXT_MAX_ITEMS_SETTING",
    "GET_CONTEXT_MAX_TOKENS_DEFAULT",
    "GET_CONTEXT_MAX_TOKENS_ENV",
    "GET_CONTEXT_MAX_TOKENS_SETTING",
    "REST_PACK_MAX_ITEMS_DEFAULT",
    "REST_PACK_MAX_ITEMS_ENV",
    "REST_PACK_MAX_ITEMS_SETTING",
    "REST_PACK_MAX_TOKENS_DEFAULT",
    "REST_PACK_MAX_TOKENS_ENV",
    "REST_PACK_MAX_TOKENS_SETTING",
    "SEARCH_MAX_TOKENS_DEFAULT",
    "SEARCH_MAX_TOKENS_ENV",
    "SEARCH_MAX_TOKENS_SETTING",
    "resolve_body_items",
    "resolve_excerpt_max_chars",
    "resolve_get_context_max_items",
    "resolve_get_context_max_tokens",
    "resolve_int_knob",
    "resolve_rest_pack_max_items",
    "resolve_rest_pack_max_tokens",
    "resolve_search_max_tokens",
]


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


# ---------------------------------------------------------------------------
# The seven pack-size / excerpt / disclosure knobs
# ---------------------------------------------------------------------------
#
# Each is ``env > stored setting > default``, registered in
# :mod:`trellis.core.settings_registry` under the same name, env var and
# default (``tests/unit/core/test_knobs.py`` pins the two against each other).
# Read-side knobs, so -- like ``graph_seeding`` -- they stay out of
# :mod:`trellis.core.write_config`.

#: MCP ``get_context``, flat (non-sectioned) layout. The ``sections=``
#: layout resolves its budget from ``registry.budget_config`` instead and is
#: deliberately not wired to these three.
GET_CONTEXT_MAX_TOKENS_ENV = "TRELLIS_GET_CONTEXT_MAX_TOKENS"
GET_CONTEXT_MAX_TOKENS_SETTING = "get_context_max_tokens"
GET_CONTEXT_MAX_TOKENS_DEFAULT = 2000
GET_CONTEXT_MAX_ITEMS_ENV = "TRELLIS_GET_CONTEXT_MAX_ITEMS"
GET_CONTEXT_MAX_ITEMS_SETTING = "get_context_max_items"
#: Item ceiling for a flat pack: bounds the candidate list the token-budget
#: walk considers. ``trellis.mcp.tools.context._FLAT_MAX_ITEMS`` is this value.
GET_CONTEXT_MAX_ITEMS_DEFAULT = 50

#: MCP ``search``. Same shipped default as ``get_context`` by coincidence,
#: not unification: the two are resolved and tuned independently.
SEARCH_MAX_TOKENS_ENV = "TRELLIS_SEARCH_MAX_TOKENS"
SEARCH_MAX_TOKENS_SETTING = "search_max_tokens"
SEARCH_MAX_TOKENS_DEFAULT = 2000

#: ``POST /packs`` when the request body omits the field. The Pydantic
#: field defaults on ``PackRequest`` (50 / 8000) are unchanged, so the
#: committed OpenAPI schema is too; the route reads ``model_fields_set``
#: to tell an omitted field from a supplied one.
REST_PACK_MAX_ITEMS_ENV = "TRELLIS_REST_PACK_MAX_ITEMS"
REST_PACK_MAX_ITEMS_SETTING = "rest_pack_max_items"
REST_PACK_MAX_ITEMS_DEFAULT = 50
REST_PACK_MAX_TOKENS_ENV = "TRELLIS_REST_PACK_MAX_TOKENS"
REST_PACK_MAX_TOKENS_SETTING = "rest_pack_max_tokens"
REST_PACK_MAX_TOKENS_DEFAULT = 8000

#: Pack shaping, applied by every surface through
#: :func:`trellis.retrieve.builder_factory.build_pack_builder`. Their
#: defaults are not restated here: they are owned by the modules whose
#: behaviour they set (``trellis.retrieve.disclosure.DEFAULT_BODY_ITEMS``
#: and ``trellis.retrieve.excerpts.EXCERPT_MAX_CHARS``, both also used as
#: plain function/dataclass defaults elsewhere) and read lazily, so that
#: importing this core module never loads ``trellis.retrieve``. No import
#: cycle forces this today; it keeps the dependency pointing one way.
BODY_ITEMS_ENV = "TRELLIS_DISCLOSURE_BODY_ITEMS"
BODY_ITEMS_SETTING = "disclosure_body_items"
EXCERPT_MAX_CHARS_ENV = "TRELLIS_EXCERPT_MAX_CHARS"
EXCERPT_MAX_CHARS_SETTING = "excerpt_max_chars"


def resolve_get_context_max_tokens(settings: Mapping[str, Any] | None) -> int:
    """Resolve ``get_context``'s flat-path ``max_tokens`` default (2000)."""
    return resolve_int_knob(
        env_var=GET_CONTEXT_MAX_TOKENS_ENV,
        setting_name=GET_CONTEXT_MAX_TOKENS_SETTING,
        default=GET_CONTEXT_MAX_TOKENS_DEFAULT,
        settings=settings,
        warning_prefix="mcp_server",
    )


def resolve_get_context_max_items(settings: Mapping[str, Any] | None) -> int:
    """Resolve ``get_context``'s flat-path ``max_items`` default (50)."""
    return resolve_int_knob(
        env_var=GET_CONTEXT_MAX_ITEMS_ENV,
        setting_name=GET_CONTEXT_MAX_ITEMS_SETTING,
        default=GET_CONTEXT_MAX_ITEMS_DEFAULT,
        settings=settings,
        warning_prefix="mcp_server",
    )


def resolve_search_max_tokens(settings: Mapping[str, Any] | None) -> int:
    """Resolve ``search``'s ``max_tokens`` default (2000)."""
    return resolve_int_knob(
        env_var=SEARCH_MAX_TOKENS_ENV,
        setting_name=SEARCH_MAX_TOKENS_SETTING,
        default=SEARCH_MAX_TOKENS_DEFAULT,
        settings=settings,
        warning_prefix="mcp_server",
    )


def resolve_rest_pack_max_items(settings: Mapping[str, Any] | None) -> int:
    """Resolve ``POST /packs``'s ``max_items`` default (50) when omitted."""
    return resolve_int_knob(
        env_var=REST_PACK_MAX_ITEMS_ENV,
        setting_name=REST_PACK_MAX_ITEMS_SETTING,
        default=REST_PACK_MAX_ITEMS_DEFAULT,
        settings=settings,
        warning_prefix="retrieve_route",
    )


def resolve_rest_pack_max_tokens(settings: Mapping[str, Any] | None) -> int:
    """Resolve ``POST /packs``'s ``max_tokens`` default (8000) when omitted."""
    return resolve_int_knob(
        env_var=REST_PACK_MAX_TOKENS_ENV,
        setting_name=REST_PACK_MAX_TOKENS_SETTING,
        default=REST_PACK_MAX_TOKENS_DEFAULT,
        settings=settings,
        warning_prefix="retrieve_route",
    )


def resolve_body_items(settings: Mapping[str, Any] | None) -> int:
    """Resolve the disclosure body-item cap: env > settings > default."""
    from trellis.retrieve.disclosure import DEFAULT_BODY_ITEMS  # noqa: PLC0415

    return resolve_int_knob(
        env_var=BODY_ITEMS_ENV,
        setting_name=BODY_ITEMS_SETTING,
        default=DEFAULT_BODY_ITEMS,
        settings=settings,
        warning_prefix="builder_factory",
    )


def resolve_excerpt_max_chars(settings: Mapping[str, Any] | None) -> int:
    """Resolve the per-item excerpt character cap: env > settings > default."""
    from trellis.retrieve.excerpts import EXCERPT_MAX_CHARS  # noqa: PLC0415

    return resolve_int_knob(
        env_var=EXCERPT_MAX_CHARS_ENV,
        setting_name=EXCERPT_MAX_CHARS_SETTING,
        default=EXCERPT_MAX_CHARS,
        settings=settings,
        warning_prefix="builder_factory",
    )
