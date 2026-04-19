"""Entry-point plugin discovery.

Single source of truth for every registry that wants to load
third-party contributions.  The shadowing policy lives here too
so it applies uniformly: ``merge_with_builtins`` returns the
final ``name -> value`` mapping after applying the built-in wins
rule, with optional override via :data:`OVERRIDE_ENV`.

Consumers call :func:`discover` for the raw plugin list and
:func:`merge_with_builtins` when there's a built-in namespace to
defend against.  For one-off pluggable *instances* (classifiers,
rerankers, search strategies) where there's no built-in namespace
of names to defend, plain :func:`discover` is enough.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint

logger = structlog.get_logger(__name__)

# ----- Entry-point group names -----
#
# Stable strings; bumping a group name is a plugin-ecosystem break
# on par with a major version bump.  Adding a new group is
# backwards compatible — plugins that don't declare it simply
# aren't loaded for the new capability.

GROUP_STORES = "trellis.stores"  # prefix; actual groups are GROUP_STORES + "." + type
GROUP_EXTRACTORS = "trellis.extractors"
GROUP_CLASSIFIERS = "trellis.classifiers"
GROUP_RERANKERS = "trellis.rerankers"
GROUP_POLICIES = "trellis.policies"
GROUP_SEARCH_STRATEGIES = "trellis.search_strategies"
GROUP_LLM_PROVIDERS = "trellis.llm.providers"
GROUP_LLM_EMBEDDERS = "trellis.llm.embedders"

# Per-store-type subgroups.  Exposed as a helper so the diagnostic
# CLI and StoreRegistry stay in lockstep without hardcoding the list
# in two places.
_STORE_TYPES = ("trace", "document", "graph", "vector", "event_log", "blob")


def store_backend_groups() -> tuple[str, ...]:
    """Return the full set of per-store-type entry-point group names.

    ``("trellis.stores.trace", "trellis.stores.document", ...)``.
    Keeps the six store types in one place.
    """
    return tuple(f"{GROUP_STORES}.{t}" for t in _STORE_TYPES)


# ----- Override env var -----

OVERRIDE_ENV = "TRELLIS_PLUGIN_OVERRIDE"


def _override_enabled() -> bool:
    return os.environ.get(OVERRIDE_ENV, "").lower() in ("1", "true", "yes", "on")


# ----- Plugin specs -----


@dataclass(frozen=True)
class PluginSpec:
    """A resolved plugin entry point.

    Kept as a light dataclass rather than a Pydantic model so it's
    free to construct and compare in tight discovery loops.  Unlike
    wire DTOs this is an internal detail — no serialization contract
    to uphold.

    ``value`` is the raw entry-point value (``"pkg.mod:Class"``).
    ``module`` / ``attr`` are the parsed halves, populated during
    discovery so consumers don't have to re-parse.
    """

    group: str
    name: str
    value: str
    module: str
    attr: str
    distribution: str | None = None  # e.g. "trellis-unity-catalog"
    distribution_version: str | None = None


def _parse_ep_value(value: str) -> tuple[str, str] | None:
    """Parse ``"pkg.mod:Class"`` into ``("pkg.mod", "Class")``.

    Entry points can also use ``"pkg.mod.Class"`` form; we support
    both.  Returns ``None`` on malformed values so the caller can
    log + skip rather than raise.
    """
    if ":" in value:
        module, _, attr = value.partition(":")
        if module and attr:
            return module, attr
        return None
    # Dotted form.
    if "." in value:
        module, _, attr = value.rpartition(".")
        if module and attr:
            return module, attr
    return None


def _spec_from_ep(group: str, ep: EntryPoint) -> PluginSpec | None:
    parsed = _parse_ep_value(ep.value)
    if parsed is None:
        logger.warning(
            "plugin_entry_point_malformed",
            group=group,
            name=ep.name,
            value=ep.value,
        )
        return None
    module, attr = parsed
    dist_name: str | None = None
    dist_version: str | None = None
    dist = getattr(ep, "dist", None)
    if dist is not None:
        dist_name = getattr(dist, "name", None)
        dist_version = getattr(dist, "version", None)
    return PluginSpec(
        group=group,
        name=ep.name,
        value=ep.value,
        module=module,
        attr=attr,
        distribution=dist_name,
        distribution_version=dist_version,
    )


def discover(group: str) -> list[PluginSpec]:
    """Return plugin specs advertised under ``group``.

    Never raises.  Malformed entries are logged and dropped so one
    broken plugin doesn't take down the registry.  The returned
    list is in the order the installed-packages metadata
    iterator emits (usually stable but not guaranteed — callers
    should not depend on order).
    """
    specs: list[PluginSpec] = []
    try:
        eps = entry_points(group=group)
    except Exception:
        logger.exception("plugin_entry_points_lookup_failed", group=group)
        return []
    for ep in eps:
        spec = _spec_from_ep(group, ep)
        if spec is not None:
            specs.append(spec)
    return specs


def load_class(spec: PluginSpec) -> type[Any] | None:
    """Import the module and resolve the attribute from a ``PluginSpec``.

    Returns ``None`` on any import / attribute error — logged at
    warning level with full context so operators can diagnose
    broken plugins without the SDK failing hard.
    """
    try:
        module = importlib.import_module(spec.module)
    except Exception:
        logger.exception(
            "plugin_module_import_failed",
            group=spec.group,
            name=spec.name,
            module=spec.module,
        )
        return None
    target = getattr(module, spec.attr, None)
    if target is None:
        logger.warning(
            "plugin_attr_missing",
            group=spec.group,
            name=spec.name,
            module=spec.module,
            attr=spec.attr,
        )
        return None
    return target  # type: ignore[no-any-return]


# ----- Built-in merging -----


def merge_with_builtins(
    group: str,
    builtins: dict[str, tuple[str, str]],
    specs: list[PluginSpec] | None = None,
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Merge plugin specs with a built-in ``name -> (module, attr)`` map.

    Built-ins win unless :data:`OVERRIDE_ENV` is truthy.  Shadowing
    is always logged at ``warning``.  Returns the merged map plus
    a list of shadowed plugin names (useful for the diagnostic
    CLI — the shadow itself is already logged).

    Usage::

        merged, shadowed = merge_with_builtins(
            "trellis.stores.graph",
            {"sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore"),
             "postgres": ("trellis.stores.postgres.graph", "PostgresGraphStore")},
        )

    Plugin specs that are malformed are silently dropped (already
    logged by :func:`discover`).
    """
    if specs is None:
        specs = discover(group)
    override = _override_enabled()
    merged = dict(builtins)
    shadowed: list[str] = []
    for spec in specs:
        if spec.name in builtins and not override:
            logger.warning(
                "plugin_shadows_builtin",
                group=group,
                name=spec.name,
                builtin=builtins[spec.name],
                plugin=spec.value,
                override_env=OVERRIDE_ENV,
            )
            shadowed.append(spec.name)
            continue
        if spec.name in builtins and override:
            logger.info(
                "plugin_overrides_builtin",
                group=group,
                name=spec.name,
                plugin=spec.value,
                override_env=OVERRIDE_ENV,
            )
        merged[spec.name] = (spec.module, spec.attr)
    return merged, shadowed


__all__ = [
    "GROUP_CLASSIFIERS",
    "GROUP_EXTRACTORS",
    "GROUP_LLM_EMBEDDERS",
    "GROUP_LLM_PROVIDERS",
    "GROUP_POLICIES",
    "GROUP_RERANKERS",
    "GROUP_SEARCH_STRATEGIES",
    "GROUP_STORES",
    "OVERRIDE_ENV",
    "PluginSpec",
    "discover",
    "load_class",
    "merge_with_builtins",
    "store_backend_groups",
]
