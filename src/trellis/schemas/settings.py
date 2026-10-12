"""Settings schema for Trellis.

A :class:`SettingRow` is one operator override of one tunable declared in
:mod:`trellis.core.settings_registry`. The store that persists these
(:class:`trellis.stores.settings_store.SettingsStore`) does not itself
validate ``value`` against the registry's declared type, range or enum —
that belongs to the governed mutation handler that will gate writes
(tracked for a follow-up PR); this schema only pins the *shape* a row must
have to round-trip through JSON.

``value`` is a plain ``float | int | str | bool`` union rather than
``Any``, mirroring :class:`trellis.schemas.parameters.ParameterSet`:
freeform enough that a new tunable needs no schema change, typed enough
that a row surviving ``model_validate`` is usable without a second check.
"""

from __future__ import annotations

from trellis.core.base import TimestampedModel, VersionedModel


class SettingRow(TimestampedModel, VersionedModel):
    """One named override, as stored in ``settings.json``.

    ``name`` is the :class:`~trellis.core.settings_registry.SettingSpec`
    name it overrides (e.g. ``"memory_extraction"``, ``"graph_seeding"``),
    not an environment variable spelling — the registry maps one to the
    other.
    """

    name: str
    value: float | int | str | bool
