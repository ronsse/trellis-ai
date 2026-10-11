"""Settings store — JSON file-based persistence for operator overrides.

Failure posture — read leniently, refuse to write
-------------------------------------------------
Same posture as :class:`~trellis.stores.policy_store.PolicyStore` and
:class:`~trellis.stores.advisory_store.AdvisoryStore`, for the same reason
(#413, generalised in :mod:`trellis.stores.degradable_json_store`): a
whole-file rewrite from a partially-read in-memory view silently replaces
whatever did not parse. For a settings file the laundering is not an
access-control bypass — it is a silent reversion of every knob whose
override failed to survive the read, back through the precedence chain
(``env`` > ``settings`` > default) to whichever of the other two is in
force. That is still a change an operator did not make and would not see:
``trellis admin write-config`` would keep reporting the setting as
overridden right up until the next write replaces the file without it.

So: the read degrades (serves what parsed) and every write raises
:class:`~trellis.errors.DegradedStoreWriteError`. The damaged bytes stay on
disk for an operator to look at;
:func:`trellis.core.write_config.WriteBehaviourConfig.from_env_and_settings`
reads through this store and must see the degradation to report it rather
than silently treating a degraded (zero-row) load as "no overrides set" —
see that function's docstring.

Per-row, not per-file
---------------------
One unparseable row costs that row, not the whole file — the same
reasoning as the other two stores: an operator whose settings file just
broke still gets ``trellis admin settings list`` (or the equivalent read)
telling them what *did* survive, rather than losing visibility into every
other override at once.

No row-level rejection rule is added here. Unlike :class:`PolicyStore`,
nothing yet reads ``settings.json`` as an evaluated list where a duplicate
``name`` would mean something different from a last-one-wins dict — so,
like :class:`AdvisoryStore`, a duplicate is just an overwrite, not a
degradation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import structlog

from trellis.schemas.settings import SettingRow
from trellis.stores.degradable_json_store import DegradableJsonStore, LoadDegradation

logger = structlog.get_logger(__name__)

#: Filename holding a deployment's setting overrides, under ``stores_dir``
#: (``<data_dir>/stores``) — co-located with ``policies.json`` and
#: ``advisories.json``. Unlike those two, there is no legacy path to honour:
#: this file has never shipped under any other name.
SETTINGS_FILENAME = "settings.json"


def resolve_settings_path(stores_dir: Path | None) -> Path | None:
    """``<stores_dir>/settings.json``, or ``None`` with no ``stores_dir``.

    ``None`` is a legitimate answer (an in-memory or storeless
    deployment), not an error — callers treat it the way
    :func:`trellis.mutate.policy_source.resolve_policy_path` treats its own
    ``None``: no overrides, not a missing file.
    """
    if stores_dir is None:
        return None
    return Path(stores_dir) / SETTINGS_FILENAME


def load_settings_values(
    stores_dir: Path | None,
) -> dict[str, float | int | str | bool]:
    """Effective setting overrides for ``stores_dir``, as a plain mapping.

    The shape
    :func:`trellis.core.write_config.WriteBehaviourConfig.from_env_and_settings`
    and its sibling :func:`~trellis.core.write_config.resolve_overridden_by`
    both take as ``settings=``. ``{}`` for a ``None``/absent ``stores_dir``
    — an ordinary, non-degraded empty read, not a warning.

    A **degraded** file still contributes whatever rows parsed
    (:meth:`SettingsStore.as_values` degrades the same way
    :meth:`SettingsStore.list` does) — this function does not hide that
    from the caller, it simply has no channel to report it through; a
    caller that must surface degradation to an operator should construct
    its own :class:`SettingsStore` and read :attr:`SettingsStore.is_degraded`
    directly instead of calling this convenience wrapper.
    """
    path = resolve_settings_path(stores_dir)
    if path is None:
        return {}
    return SettingsStore(path).as_values()


class SettingsStore(DegradableJsonStore[SettingRow]):
    """Load and save operator setting overrides from a JSON file.

    This is the **CRUD** store behind ``trellis admin settings`` and (in a
    follow-up PR) its REST/mutation surface. It holds overrides only — a
    tunable with no row here is simply unset, and resolves through
    :func:`trellis.core.write_config.WriteBehaviourConfig.from_env_and_settings`
    to its environment value or, failing that, its shipped default.

    File format::

        {"settings": [<SettingRow.model_dump()>, ...]}

    A store whose file could not be read in full is **degraded**: reads
    serve what parsed, writes raise
    :class:`~trellis.errors.DegradedStoreWriteError`.
    """

    _envelope_key: ClassVar[str] = "settings"
    _store_label: ClassVar[str] = "setting"
    _loaded_event: ClassVar[str] = "settings_loaded"
    _degraded_event: ClassVar[str] = "settings_load_degraded"
    _degraded_impact: ClassVar[str] = (
        "Settings that parsed are still in force; every write is refused "
        "so the unreadable file cannot be replaced by the partial view. "
        "Every tunable's precedence resolver (env > settings > default) "
        "falls back to env or default for a row that failed to parse, "
        "exactly as it would for a row that was never set."
    )
    _stale_recovery: ClassVar[str] = "trellis admin settings list"

    # -- Row handling --

    @staticmethod
    def _parse_row(entry: Any) -> SettingRow:
        return SettingRow.model_validate(entry)

    @staticmethod
    def _row_id(row: SettingRow) -> str:
        return row.name

    # -- Public API --

    def list(self) -> list[SettingRow]:
        """Return all overrides, in file order.

        Works on a degraded store, serving whatever parsed. **A degraded
        store's list is not the whole override set** — callers rendering
        it must say so (:attr:`degradation`).
        """
        return list(self._rows.values())

    def get(self, name: str) -> SettingRow | None:
        """Get one override by name, or ``None`` if unset.

        **A ``None`` from a degraded store does not mean "no override"** —
        it may mean "the row was unreadable". Callers that report absence
        as "using the default" must check :attr:`is_degraded` first.
        """
        return self._rows.get(name)

    def as_values(self) -> dict[str, float | int | str | bool]:
        """Plain ``name -> value`` mapping, for a precedence resolver.

        Convenience over :meth:`list`/:meth:`get` for a caller (such as
        :func:`trellis.core.write_config.WriteBehaviourConfig.from_env_and_settings`)
        that only wants the effective override values, not the rows
        carrying them. Degrades the same way :meth:`list` does.
        """
        return {name: row.value for name, row in self._rows.items()}

    def set(self, name: str, value: float | int | str | bool) -> SettingRow:
        """Set (or replace) one override. Persists immediately."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        row = SettingRow(name=name, value=value)
        self._rows[name] = row
        self._save_or_roll_back(restore)
        logger.info("setting_stored", name=name)
        return row

    def remove(self, name: str) -> bool:
        """Remove an override by name. Returns ``True`` if found.

        Refuses *before* the membership check — on a degraded store the
        check would answer from a partial view and report ``False`` ("no
        such override") for a row that exists in the file and merely
        failed to parse, and on a **stale** store it would do the same for
        an override another process set since this one loaded.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        if name not in self._rows:
            return False
        restore = self._snapshot()
        del self._rows[name]
        self._save_or_roll_back(restore)
        logger.info("setting_removed", name=name)
        return True

    # -- Refusal messages --

    def _degraded_write_message(self, degradation: LoadDegradation) -> str:
        return (
            f"Refusing to write the Trellis settings file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} setting(s) parsed and are being "
            f"shown; {degradation.rows_skipped_display} could not be read. "
            "Writing would replace the file with only what parsed, silently "
            "reverting every missing override to its environment value or "
            "default. To reset:"
        )

    def _stale_write_message(self) -> str:
        return (
            f"Refusing to write the Trellis settings file at {self._path}: it "
            "changed after this process read it, so writing would replace "
            "whatever landed in between. Re-read and retry:"
        )

    def _unreadable_write_message(self, detail: str) -> str:
        return (
            f"Refusing to write the Trellis settings file at {self._path}: its "
            f"identity could not be read ({detail}), so this process cannot "
            "tell whether the file changed after it read it. Writing anyway "
            "would replace a file it never saw. Check the path, then retry:"
        )
