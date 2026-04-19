"""ParameterRegistry — runtime resolver for tuneable component parameters.

A thin read-only facade over :class:`ParameterStore`.  Components call
:meth:`get` with a scope, a key, and a hardcoded default; the registry
walks the precedence chain and returns the first override it finds,
falling back to the caller's default when no snapshot exists.

The registry is deliberately a lightweight wrapper rather than a
dependency-injected global.  Tuneable components accept an optional
``registry`` argument in their constructor or factory; when absent,
they use hardcoded defaults unchanged — making the migration
behaviour-preserving at every step.

Caching
-------

Resolved snapshots are memoised per exact scope key.  Mutations to
:class:`ParameterStore` go through the governed mutation pipeline,
which calls :meth:`invalidate` after a successful promotion so the
registry re-fetches on the next ``get``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.schemas.parameters import ParameterScope, ParameterSet

if TYPE_CHECKING:
    from trellis.stores.base.parameter import ParameterStore

logger = structlog.get_logger(__name__)


# Sentinel used to distinguish "no snapshot resolved" from "snapshot with
# value equal to the caller's default" in the _cache.  We can't use None
# because an active snapshot genuinely resolves to a ParameterSet.
_MISS: ParameterSet | None = None


class ParameterRegistry:
    """Resolve tuneable parameters from a :class:`ParameterStore`.

    Callers build a :class:`ParameterScope` with the component's
    learning axes, call :meth:`get` with a key and hardcoded default,
    and receive either the overridden value (when an active snapshot
    exists) or the default.  A ``None`` ``store`` yields a no-op
    registry that always returns defaults — useful for unit tests and
    deployments that haven't opted in to parameter tuning.
    """

    def __init__(self, store: ParameterStore | None = None) -> None:
        self._store = store
        self._cache: dict[
            tuple[str, str | None, str | None, str | None], ParameterSet | None
        ] = {}

    def get(self, scope: ParameterScope, key: str, default: Any) -> Any:
        """Resolve ``key`` for ``scope`` or fall back to ``default``.

        The resolve call walks the precedence chain (narrowest scope
        first) and returns the first snapshot found.  Missing keys in
        the resolved snapshot fall back to ``default`` without emitting
        a warning — components carry the canonical defaults.
        """
        snapshot = self._resolve(scope)
        if snapshot is None:
            return default
        return snapshot.values.get(key, default)

    def get_values(self, scope: ParameterScope) -> dict[str, Any]:
        """Return the full values dict for the scope, or an empty dict.

        Useful when a component wants to merge overrides over a default
        dict in one step.  Unlike :meth:`get`, this does not apply any
        defaults — callers must combine with their own default dict.
        """
        snapshot = self._resolve(scope)
        if snapshot is None:
            return {}
        return dict(snapshot.values)

    def params_version(self, scope: ParameterScope) -> str | None:
        """Return the active ``params_version`` for the scope, or ``None``.

        Components record this on :class:`OutcomeEvent` so tuners can
        correlate outcomes with the exact snapshot in effect.
        """
        snapshot = self._resolve(scope)
        return snapshot.params_version if snapshot is not None else None

    def invalidate(self, scope: ParameterScope | None = None) -> None:
        """Drop cached snapshots.  Clears entire cache when ``scope`` is ``None``.

        Called after a governed mutation promotes a new snapshot so the
        next :meth:`get` re-resolves.
        """
        if scope is None:
            self._cache.clear()
            return
        # Invalidate the exact scope and any broader scope it might fall
        # back to — simplest correct choice is to clear all entries that
        # share the same component_id.
        component_id = scope.component_id
        to_drop = [k for k in self._cache if k[0] == component_id]
        for k in to_drop:
            del self._cache[k]

    # -- internal ------------------------------------------------------------

    def _resolve(self, scope: ParameterScope) -> ParameterSet | None:
        if self._store is None:
            return None
        key = scope.key()
        if key in self._cache:
            return self._cache[key]
        try:
            snapshot = self._store.resolve(scope)
        except Exception:
            logger.warning(
                "parameter_registry.resolve_failed",
                scope=key,
                exc_info=True,
            )
            snapshot = None
        self._cache[key] = snapshot
        return snapshot
