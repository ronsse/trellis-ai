"""Classify overrides store — JSON file-based persistence for applied rules.

Same failure posture as :class:`~trellis.stores.policy_store.PolicyStore`,
:class:`~trellis.stores.advisory_store.AdvisoryStore`,
:class:`~trellis.stores.settings_store.SettingsStore` and
:class:`~trellis.stores.schedule_store.ScheduleStore`, all built on
:class:`~trellis.stores.degradable_json_store.DegradableJsonStore`: **the
read degrades and the write refuses.** A whole-file rewrite from a
partially-read view would silently drop whatever rule did not parse — for
this store that means a previously-applied ``domain`` promotion quietly
stops being seen by the ingestion pipeline and the miners, with nothing on
disk to say so. This is a third such file (CLAUDE.md: "a third such file
gets a subclass, never a copy").

Precedence
----------
This store holds the *overlay* layer only — see
:mod:`trellis.classify.factory`'s ``effective_domain_keywords`` /
``effective_domain_aliases`` for the full merge: built-in defaults <
``config.yaml`` < this overlay. A row here always wins over the same key
in ``config.yaml``, because an operator approving a mined candidate through
the governed path is a more specific, more recent decision than whatever
is sitting in a host-local file Trellis never writes.

Per-row, not per-file
----------------------
One unparseable row costs that row, not the whole file, for the same
reason :class:`~trellis.stores.settings_store.SettingsStore` chose this:
an operator whose overrides file just broke still gets every other
applied rule.

Duplicate ``(kind, key)`` is not row-level rejection here — it is the
store's own primary key, so :meth:`ClassifyOverridesStore._row_id` colliding
on two file entries means the later one in file order wins, the same
last-one-wins posture :class:`~trellis.stores.advisory_store.AdvisoryStore`
takes (nothing here reads duplicates as a list the way
:class:`~trellis.stores.policy_store.PolicyStore` does).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import structlog

from trellis.schemas.classify_overrides import ClassifyOverrideKind, ClassifyOverrideRow
from trellis.stores.degradable_json_store import DegradableJsonStore, LoadDegradation

logger = structlog.get_logger(__name__)

#: Filename holding applied tag/domain rules, under ``stores_dir``
#: (``<data_dir>/stores``) — co-located with ``settings.json``,
#: ``policies.json``, ``advisories.json`` and ``schedule.json``.
CLASSIFY_OVERRIDES_FILENAME = "classify_overrides.json"


def resolve_classify_overrides_path(stores_dir: Path | None) -> Path | None:
    """``<stores_dir>/classify_overrides.json``, or ``None`` with no ``stores_dir``.

    ``None`` is a legitimate answer (an in-memory or storeless deployment),
    not an error — callers treat it the way
    :func:`trellis.stores.settings_store.resolve_settings_path` treats its
    own ``None``: no overlay, not a missing file.
    """
    if stores_dir is None:
        return None
    return Path(stores_dir) / CLASSIFY_OVERRIDES_FILENAME


def _row_key(kind: ClassifyOverrideKind, key: str) -> str:
    """The store's own primary key -- shared by read and write call sites.

    A free function, not a method, so :meth:`ClassifyOverridesStore.get`
    and :meth:`ClassifyOverridesStore.remove` build the exact same id
    :meth:`ClassifyOverridesStore._row_id` does, rather than each
    reimplementing the ``f"{kind}:{key}"`` join.
    """
    return f"{kind}:{key}"


#: ``list[str]`` under another name, needed only so
#: :meth:`ClassifyOverridesStore.domain_keywords_map`'s return annotation
#: doesn't spell ``list`` -- the class below also defines a method named
#: ``list``, which shadows the builtin generic within the class body's
#: own scope (where a ``def``'s signature is evaluated) and makes mypy
#: read a bare ``list[str]`` there as "call the method, not the type".
_StrList = list[str]


class ClassifyOverridesStore(DegradableJsonStore[ClassifyOverrideRow]):
    """Load and save applied tag/domain rules from a JSON file.

    Backs ``trellis classify apply-rule`` / ``revoke-rule`` and the REST
    ``POST /api/v1/classify/rules`` / ``DELETE /api/v1/classify/rules/{kind}/{key}``
    routes, both reached only through
    ``Operation.CLASSIFY_RULE_APPLY`` / ``Operation.CLASSIFY_RULE_REVOKE``
    (:mod:`trellis.mutate.handlers`). Nothing else writes this file.

    File format::

        {"rules": [<ClassifyOverrideRow.model_dump()>, ...]}
    """

    _envelope_key: ClassVar[str] = "rules"
    _store_label: ClassVar[str] = "classify_overrides"
    _loaded_event: ClassVar[str] = "classify_overrides_loaded"
    _degraded_event: ClassVar[str] = "classify_overrides_load_degraded"
    _degraded_impact: ClassVar[str] = (
        "Rules that parsed are still in force and still seed the ingestion "
        "pipeline and the tag/domain miners; every write is refused so the "
        "unreadable file cannot be replaced by the partial view, which "
        "would silently un-apply whatever rule didn't parse with nothing "
        "on disk to say so."
    )
    _stale_recovery: ClassVar[str] = "trellis classify effective-rules --format json"

    # -- Row handling --

    @staticmethod
    def _parse_row(entry: Any) -> ClassifyOverrideRow:
        return ClassifyOverrideRow.model_validate(entry)

    @staticmethod
    def _row_id(row: ClassifyOverrideRow) -> str:
        return _row_key(row.kind, row.key)

    # -- Public API --

    def list(self) -> list[ClassifyOverrideRow]:
        """Return all applied rules, in file order.

        Works on a degraded store, serving whatever parsed. **A degraded
        store's list is not the whole overlay** — callers rendering it
        must say so (:attr:`degradation`).
        """
        return list(self._rows.values())

    def get(self, kind: ClassifyOverrideKind, key: str) -> ClassifyOverrideRow | None:
        """Get one applied rule by ``(kind, key)``, or ``None`` if unset.

        **A ``None`` from a degraded store does not mean "not applied"**
        — it may mean "the row was unreadable". Callers reporting absence
        to a human must check :attr:`is_degraded` first.
        """
        return self._rows.get(_row_key(kind, key))

    def put(self, row: ClassifyOverrideRow) -> ClassifyOverrideRow:
        """Add or replace one applied rule. Persists immediately."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        self._rows[_row_key(row.kind, row.key)] = row
        self._save_or_roll_back(restore)
        logger.info("classify_override_stored", kind=row.kind, key=row.key)
        return row

    def remove(self, kind: ClassifyOverrideKind, key: str) -> bool:
        """Remove an applied rule by ``(kind, key)``. Returns ``True`` if found.

        Refuses *before* the membership check — on a degraded or stale
        store the check would answer from a partial or outdated view and
        report ``False`` ("no such rule") for a row that exists and
        merely failed to parse, or was removed by another process.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        row_id = _row_key(kind, key)
        if row_id not in self._rows:
            return False
        restore = self._snapshot()
        del self._rows[row_id]
        self._save_or_roll_back(restore)
        logger.info("classify_override_removed", kind=kind, key=key)
        return True

    def domain_keywords_map(self) -> dict[str, _StrList]:
        """Applied ``tag_keyword`` rules as ``domain -> [keyword, ...]``.

        The shape :func:`trellis.classify.classifiers.keyword.build_domain_keyword_map`
        takes as its highest-precedence ``extra_domains`` layer -- see
        :mod:`trellis.classify.factory`. Degrades the same way :meth:`list`
        does: a row that failed to parse simply is not in the map, the
        same as a rule that was never applied.
        """
        result: dict[str, list[str]] = {}
        for row in self._rows.values():
            if row.kind != "tag_keyword":
                continue
            result.setdefault(row.target, []).append(row.key)
        return result

    def domain_aliases_map(self) -> dict[str, str]:
        """Applied ``domain_alias`` rules as ``alias -> canonical``.

        The shape :func:`trellis.classify.factory.effective_domain_aliases`
        merges on top of the ``config.yaml``-derived alias map. Degrades
        the same way :meth:`list` does.
        """
        return {
            row.key: row.target
            for row in self._rows.values()
            if row.kind == "domain_alias"
        }

    # -- Refusal messages --

    def _degraded_write_message(self, degradation: LoadDegradation) -> str:
        return (
            f"Refusing to write the Trellis classify-overrides file at "
            f"{degradation.path}: it loaded degraded ({degradation.reason}: "
            f"{degradation.detail}). {degradation.rows_loaded} rule(s) "
            f"parsed and are being applied; "
            f"{degradation.rows_skipped_display} could not be read. "
            "Writing would replace the file with only what parsed, silently "
            "un-applying every rule that didn't. To reset:"
        )

    def _stale_write_message(self) -> str:
        return (
            f"Refusing to write the Trellis classify-overrides file at "
            f"{self._path}: it changed after this process read it, so "
            "writing would replace whatever landed in between — silently "
            "dropping a rule applied or revoked concurrently. Re-read and "
            "retry:"
        )

    def _unreadable_write_message(self, detail: str) -> str:
        return (
            f"Refusing to write the Trellis classify-overrides file at "
            f"{self._path}: its identity could not be read ({detail}), so "
            "this process cannot tell whether the file changed after it "
            "read it. Writing anyway would replace a file it never saw. "
            "Check the path, then retry:"
        )
