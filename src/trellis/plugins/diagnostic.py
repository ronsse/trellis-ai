"""Structured report for ``trellis admin check-plugins``.

The CLI command in ``trellis_cli.admin`` consumes these types and
renders them for humans / JSON consumers.  Plugin status is checked
by actually importing each advertised class — a plugin that installs
but imports broken is LOADED-then-BLOCKED, and the diagnostic surfaces
that cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import structlog

from trellis.plugins.loader import (
    GROUP_CLASSIFIERS,
    GROUP_EXTRACTORS,
    GROUP_LLM_EMBEDDERS,
    GROUP_LLM_PROVIDERS,
    GROUP_POLICIES,
    GROUP_RERANKERS,
    GROUP_SEARCH_STRATEGIES,
    discover,
    load_class,
    store_backend_groups,
)

logger = structlog.get_logger(__name__)

PluginStatus = Literal["LOADED", "BLOCKED", "SHADOWED"]


# Built-in backend names per store-type group (just the name, not the
# module/class — this module only needs shadowing detection, it doesn't
# resolve the classes).  Intentionally duplicated here rather than
# imported from stores.registry to avoid a layering-reversal where
# ``trellis.plugins`` depends on ``trellis.stores``.
_BUILTIN_STORE_NAMES: dict[str, set[str]] = {
    "trellis.stores.trace": {"sqlite", "postgres"},
    "trellis.stores.document": {"sqlite", "postgres"},
    "trellis.stores.graph": {"sqlite", "postgres"},
    "trellis.stores.vector": {"sqlite", "pgvector", "lancedb"},
    "trellis.stores.event_log": {"sqlite", "postgres"},
    "trellis.stores.blob": {"local", "s3"},
}

_BUILTIN_LLM_PROVIDER_NAMES: set[str] = {"openai", "anthropic"}
_BUILTIN_LLM_EMBEDDER_NAMES: set[str] = {"openai"}


@dataclass(frozen=True)
class PluginEntry:
    """One discovered plugin + its load outcome."""

    group: str
    name: str
    value: str
    distribution: str | None
    distribution_version: str | None
    status: PluginStatus
    reason: str | None = None  # populated for BLOCKED / SHADOWED

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group,
            "name": self.name,
            "value": self.value,
            "distribution": self.distribution,
            "distribution_version": self.distribution_version,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class PluginReport:
    """Aggregated plugin diagnostic report.

    Groups without any discovered plugins are still listed (with an
    empty ``entries`` list) so operators can quickly see which
    extension points are available vs. in use.
    """

    plugins: list[PluginEntry] = field(default_factory=list)
    groups_checked: list[str] = field(default_factory=list)

    @property
    def loaded_count(self) -> int:
        return sum(1 for p in self.plugins if p.status == "LOADED")

    @property
    def blocked_count(self) -> int:
        return sum(1 for p in self.plugins if p.status == "BLOCKED")

    @property
    def shadowed_count(self) -> int:
        return sum(1 for p in self.plugins if p.status == "SHADOWED")

    @property
    def exit_code(self) -> int:
        """CI-friendly exit code: 0 clean, 1 warnings, 2 blocked.

        * ``0`` — every discovered plugin loaded cleanly (or no
          plugins present).
        * ``1`` — at least one plugin is shadowed (non-fatal — the
          built-in is serving the role, but the operator should know).
        * ``2`` — at least one plugin is blocked (would have served
          a role but couldn't import — silent in prod if not caught).
        """
        if self.blocked_count > 0:
            return 2
        if self.shadowed_count > 0:
            return 1
        return 0


def _check_one(
    group: str,
    builtin_names: set[str],
) -> list[PluginEntry]:
    """Discover and status-check every plugin in ``group``.

    A plugin name that collides with a built-in is SHADOWED — the
    built-in wins (consistent with the loader's shadowing policy).
    Import failures mark the plugin BLOCKED.  Everything else is
    LOADED.
    """
    entries: list[PluginEntry] = []
    for spec in discover(group):
        if spec.name in builtin_names:
            entries.append(
                PluginEntry(
                    group=group,
                    name=spec.name,
                    value=spec.value,
                    distribution=spec.distribution,
                    distribution_version=spec.distribution_version,
                    status="SHADOWED",
                    reason=f"builtin '{spec.name}' takes precedence",
                )
            )
            continue
        # Attempt actual import to catch broken plugins early.
        cls = load_class(spec)
        if cls is None:
            entries.append(
                PluginEntry(
                    group=group,
                    name=spec.name,
                    value=spec.value,
                    distribution=spec.distribution,
                    distribution_version=spec.distribution_version,
                    status="BLOCKED",
                    reason="module import or attribute resolution failed",
                )
            )
            continue
        entries.append(
            PluginEntry(
                group=group,
                name=spec.name,
                value=spec.value,
                distribution=spec.distribution,
                distribution_version=spec.distribution_version,
                status="LOADED",
            )
        )
    return entries


def collect_plugin_report() -> PluginReport:
    """Walk every known plugin group and return a combined report.

    Used by ``trellis admin check-plugins``.  Safe to call at any
    time — does not instantiate plugins, only imports the declared
    module + class.
    """
    report = PluginReport()

    # Store backends — per-type subgroups.
    for group in store_backend_groups():
        report.groups_checked.append(group)
        report.plugins.extend(_check_one(group, _BUILTIN_STORE_NAMES.get(group, set())))

    # LLM providers + embedders.
    report.groups_checked.append(GROUP_LLM_PROVIDERS)
    report.plugins.extend(_check_one(GROUP_LLM_PROVIDERS, _BUILTIN_LLM_PROVIDER_NAMES))
    report.groups_checked.append(GROUP_LLM_EMBEDDERS)
    report.plugins.extend(_check_one(GROUP_LLM_EMBEDDERS, _BUILTIN_LLM_EMBEDDER_NAMES))

    # Instance-contributor groups — no built-in name namespace, so
    # shadowing doesn't apply; plugins are LOADED or BLOCKED.
    for group in (
        GROUP_EXTRACTORS,
        GROUP_CLASSIFIERS,
        GROUP_RERANKERS,
        GROUP_POLICIES,
        GROUP_SEARCH_STRATEGIES,
    ):
        report.groups_checked.append(group)
        report.plugins.extend(_check_one(group, set()))

    return report


__all__ = [
    "PluginEntry",
    "PluginReport",
    "PluginStatus",
    "collect_plugin_report",
]
