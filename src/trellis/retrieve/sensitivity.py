"""The sensitivity boundary: a document above the caller's clearance is not served.

:mod:`trellis.retrieve.lifecycle` and :mod:`trellis.retrieve.noise` answer "is
this item fit to serve". This module answers "may *this caller* read it". The
label is :class:`~trellis.schemas.classification.DataClassification`, stamped
under ``metadata["data_classification"]`` by the save surfaces, and read with
:func:`~trellis.schemas.classification.sensitivity_of`. The clearance is the
caller's: ``clearance_of(scopes)`` on a credentialed surface, ``"restricted"``
on a trusted-local one (stdio, auth off, the CLI).

**Transparent by default.** A missing label reads ``internal`` and the lowest
clearance is ``internal``, so a store with no labels serves every caller
exactly what it served before this gate existed. Only a document someone
labelled ``confidential`` or ``restricted`` is withheld, and only from a caller
who lacks the scope. A *malformed* label reads ``restricted``
(:func:`sensitivity_of`): unparseable is withheld, never served.

**Enforced where PackBuilder collects, not per strategy**, for the reason in
:mod:`trellis.retrieve.lifecycle`: the strategy set is open. It runs over the
merged pool after supersession, so a withheld successor does not un-supersede
its loser.

**A chunk inherits its parent's label.** A chunk row carries its own metadata
and none of the parent's later labels, so :func:`effective_sensitivity` takes
the higher of the two, through the same
:class:`~trellis.retrieve.parent_verdicts.ParentVerdictCache` the archived and
noise gates share. A parent that is *missing* is no verdict (served); a parent
whose fetch *raised* withholds its chunks, because an outage must not read as
permission.

Non-pack read surfaces (``get_items``, ``get_file_context``) use
:class:`DocumentClearance`, the same predicate over a stored document. A by-id
read of a withheld document answers exactly as not-found.

Graph nodes, entities, precedents, advisories and observations carry no label
in this slice; ``tests/unit/test_sensitivity_surface_rule.py`` rosters every MCP
tool as gated or free of document content so a new one cannot slip in unread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.schemas.classification import sensitivity_of, sensitivity_rank
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore

#: ``RejectedItem.reason`` recorded when this gate removes an item, so the
#: withholding report (:mod:`trellis.retrieve.withholding`) can name it.
SENSITIVITY_REJECTION_REASON = "sensitivity"


def permits(clearance: Sensitivity, sensitivity: Sensitivity) -> bool:
    """Whether a caller holding ``clearance`` may read ``sensitivity`` content."""
    return sensitivity_rank(sensitivity) <= sensitivity_rank(clearance)


def effective_sensitivity(
    item: PackItem, parent_verdict_cache: ParentVerdictCache | None
) -> Sensitivity:
    """The item's own label, raised to its parent's when it is a chunk."""
    own = sensitivity_of(item.metadata)
    if parent_verdict_cache is None:
        return own
    parent_metadata = parent_verdict_cache.resolve(item)
    if parent_metadata is None:
        return "restricted" if parent_verdict_cache.unreadable(item) else own
    inherited = sensitivity_of(parent_metadata)
    return inherited if sensitivity_rank(inherited) > sensitivity_rank(own) else own


def partition_by_clearance(
    items: Iterable[PackItem],
    clearance: Sensitivity,
    parent_verdict_cache: ParentVerdictCache | None = None,
) -> tuple[list[PackItem], list[PackItem]]:
    """Split ``items`` into ``(kept, withheld)`` on the caller's clearance.

    ``clearance="restricted"`` keeps everything without reading a parent row:
    a trusted-local caller pays nothing for a gate it cannot trip.
    """
    if clearance == "restricted":
        return list(items), []
    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in items:
        if permits(clearance, effective_sensitivity(item, parent_verdict_cache)):
            kept.append(item)
        else:
            withheld.append(item)
    return kept, withheld


class DocumentClearance:
    """:func:`partition_by_clearance`'s predicate over a stored document.

    For the read surfaces that fetch documents directly rather than through a
    pack. One instance per request: it carries a per-request parent cache.
    """

    def __init__(self, document_store: DocumentStore, clearance: Sensitivity) -> None:
        self.clearance = clearance
        self._cache = ParentVerdictCache(document_store)

    def permits(self, doc_id: str, metadata: Mapping[str, Any] | None) -> bool:
        if self.clearance == "restricted":
            return True
        item = PackItem(
            item_id=doc_id, item_type="document", metadata=dict(metadata or {})
        )
        return permits(self.clearance, effective_sensitivity(item, self._cache))


__all__ = [
    "SENSITIVITY_REJECTION_REASON",
    "DocumentClearance",
    "effective_sensitivity",
    "partition_by_clearance",
    "permits",
]
