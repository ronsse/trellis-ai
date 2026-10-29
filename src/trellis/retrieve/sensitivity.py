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
merged pool after supersession. Supersession itself skips a successor the
caller cannot read (:func:`reader_for`), so a restricted successor withholds
no loser the caller may read and a below-clearance pack is the pack built
without it.

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

import structlog

from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    SENSITIVITY_ORDER,
    DataClassification,
    sensitivity_of,
    sensitivity_rank,
)
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

    from trellis.schemas.classification import Sensitivity
    from trellis.schemas.pack import RejectedItem
    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)

#: ``RejectedItem.reason`` recorded when this gate removes an item, so the
#: withholding report (:mod:`trellis.retrieve.withholding`) can name it.
SENSITIVITY_REJECTION_REASON = "sensitivity"

#: ``Pack.metadata`` / ``SectionedPack.metadata`` key holding the ids a surface
#: must not name to a below-``restricted`` caller: the ids this gate withheld,
#: and every other rejected candidate the caller may not read
#: (:func:`concealed_ids_metadata`). In-memory only: it rides the returned pack
#: so a surface can :func:`conceal_withheld_ids`, and it is never written to
#: ``PACK_ASSEMBLED``.
SENSITIVITY_WITHHELD_IDS_KEY = "sensitivity_withheld_ids"


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


def reader_for(
    clearance: Sensitivity, parent_verdict_cache: ParentVerdictCache | None = None
) -> Callable[[PackItem], bool] | None:
    """The predicate "may a caller at ``clearance`` read this item", or ``None``.

    :func:`partition_by_clearance`'s test, for a gate that must ask the same
    question before the withholding step (supersession: an item the caller
    cannot read must not suppress one it can). ``None`` for ``restricted``,
    which reads everything, so the trusted-local path pays no parent reads.
    """
    if clearance == "restricted":
        return None

    def _readable(item: PackItem) -> bool:
        return permits(clearance, effective_sensitivity(item, parent_verdict_cache))

    return _readable


class DocumentClearance:
    """:func:`partition_by_clearance`'s predicate over a stored document.

    For the read surfaces that fetch documents directly rather than through a
    pack. One instance per request: it carries a per-request parent cache.
    """

    def __init__(self, document_store: DocumentStore, clearance: Sensitivity) -> None:
        self.clearance = clearance
        self._cache = ParentVerdictCache(document_store)

    def sensitivity(
        self, doc_id: str, metadata: Mapping[str, Any] | None
    ) -> Sensitivity:
        """The document's effective label: its own, raised to its parent's."""
        item = PackItem(
            item_id=doc_id, item_type="document", metadata=dict(metadata or {})
        )
        return effective_sensitivity(item, self._cache)

    def permits(self, doc_id: str, metadata: Mapping[str, Any] | None) -> bool:
        if self.clearance == "restricted":
            return True
        return permits(self.clearance, self.sensitivity(doc_id, metadata))


def readable_ids(
    document_store: DocumentStore, ids: Iterable[str], clearance: Sensitivity
) -> list[str]:
    """The ``ids`` a caller at ``clearance`` may read, in order.

    For a surface that would otherwise hand a caller ids lifted from a pack it
    did not build (the feedback attribution check): the same predicate the
    pack gate and ``get_items`` use, a chunk judged by its parent. An id with
    no document row is no verdict (a graph node, a trace), as it is for
    ``get_items``. A store that raises is unreadable: an outage must not read
    as permission. ``restricted`` reads everything without touching the store.
    """
    if clearance == "restricted":
        return list(ids)
    gate = DocumentClearance(document_store, clearance)
    readable: list[str] = []
    for item_id in ids:
        try:
            doc = document_store.get(item_id)
        except Exception:
            # GRACEFUL-DEGRADATION: fail closed. An id the store cannot answer
            # for is withheld from the caller; the surface's own check still
            # fails open on an empty answer, so no write is refused for it.
            logger.warning("readable_ids_fetch_failed", item_id=item_id)
            continue
        if gate.permits(item_id, (doc or {}).get("metadata")):
            readable.append(item_id)
    return readable


class OverwriteRefusedError(ValueError):
    """A write at an existing document id that the caller may not make.

    ``message`` is text Trellis wrote (the id and a fixed sentence), so a
    surface can show it without the sanitizing a foreign exception needs.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def refuse_overwrite(
    document_store: DocumentStore,
    doc_id: str,
    *,
    clearance: Sensitivity,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Refuse a re-put of ``doc_id`` that would destroy or relabel content.

    A write at a caller-chosen id replaces the stored row, metadata included,
    so without this a key that cannot read a labelled document could overwrite
    it (and, since the new row carries no label, publish its chunks). Two
    refusals, both before any write:

    * the existing document's label is above the caller's ``clearance``;
    * the existing label is above the one being written (``metadata``), which
      would be a silent downgrade. Re-labelling is a separate operation.

    A missing document is no verdict. A write at the same or a higher label by
    a cleared caller passes. The first message does not name the label.
    """
    existing = document_store.get(doc_id)
    if existing is None:
        return
    current = DocumentClearance(document_store, clearance).sensitivity(
        doc_id, existing.get("metadata")
    )
    if not permits(clearance, current):
        msg = (
            f"document {doc_id!r} already exists and this credential's clearance "
            "does not cover it; nothing was written"
        )
        raise OverwriteRefusedError(msg)
    if sensitivity_rank(current) > sensitivity_rank(sensitivity_of(metadata)):
        msg = (
            f"document {doc_id!r} already exists labelled {current!r}; a write may "
            "not lower its label, so nothing was written (relabelling is not "
            "supported here)"
        )
        raise OverwriteRefusedError(msg)


def withheld_ids(rejected: Iterable[RejectedItem]) -> list[str]:
    """The ids this gate withheld, from the build's ``rejected`` rows."""
    return sorted(
        {r.item_id for r in rejected if r.reason == SENSITIVITY_REJECTION_REASON}
    )


def withheld_ids_metadata(rejected: Iterable[RejectedItem]) -> dict[str, list[str]]:
    """The pack-metadata entry naming what the gate withheld, or ``{}`` for none.

    Absent rather than empty when nothing was withheld, so a pack that found
    nothing and a held-out pack (:mod:`trellis.core.pack_holdout`) carry the same
    metadata keys and stay indistinguishable.
    """
    ids = withheld_ids(rejected)
    return {SENSITIVITY_WITHHELD_IDS_KEY: ids} if ids else {}


def conceal_withheld_ids(
    report: Mapping[str, Any] | None,
    withholding: Mapping[str, Any] | None,
    hidden_ids: Collection[str],
    clearance: Sensitivity,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(report, withholding)`` as a caller at ``clearance`` may be shown them.

    For a response only; the ``PACK_ASSEMBLED`` payload keeps full detail for
    its admin readers. A below-``restricted`` caller gets neither the
    ``sensitivity`` rows of ``rejected_items`` nor the ids in
    ``withheld_item_ids``: a per-query ``(id, score)`` for a document it may not
    read is an oracle on that document's content. Every count survives
    (``by_reason``, ``total``), so the note can still say how many.
    """
    if clearance == "restricted":
        return (
            dict(report) if report is not None else None,
            dict(withholding) if withholding is not None else None,
        )
    hidden = set(hidden_ids)
    shown_report: dict[str, Any] | None = None
    if report is not None:
        shown_report = {
            **report,
            "rejected_items": [
                row
                for row in report.get("rejected_items") or []
                if row.get("reason") != SENSITIVITY_REJECTION_REASON
                and row.get("item_id") not in hidden
            ],
        }
    shown_withholding: dict[str, Any] | None = None
    if withholding is not None:
        shown_withholding = {
            **withholding,
            "withheld_item_ids": [
                item_id
                for item_id in withholding.get("withheld_item_ids") or []
                if item_id not in hidden
            ],
        }
    return shown_report, shown_withholding


class CandidateIndex:
    """Every candidate a build collected, by id, so a rejected row can be judged.

    A ``RejectedItem`` carries an id and a score but no metadata, so the label
    of a document an *earlier* gate (noise, archived, demoted, superseded)
    rejected is unknown by the time the pack is assembled. The builder adds
    each strategy's candidates here before any gate runs. Holds references
    only, and nothing at all for a ``restricted`` caller, who is shown every id.
    """

    def __init__(self, clearance: Sensitivity) -> None:
        self._clearance = clearance
        self._by_id: dict[str, list[PackItem]] = {}

    def add(self, items: Iterable[PackItem]) -> None:
        if self._clearance == "restricted":
            return
        for item in items:
            self._by_id.setdefault(item.item_id, []).append(item)

    def get(self, item_id: str) -> Sequence[PackItem]:
        return self._by_id.get(item_id, ())


def unreadable_rejected_ids(
    rejected: Iterable[RejectedItem],
    candidates: CandidateIndex,
    clearance: Sensitivity,
    parent_verdict_cache: ParentVerdictCache | None,
) -> list[str]:
    """Ids of every rejected candidate a caller at ``clearance`` may not read.

    The ``sensitivity`` rows (:func:`withheld_ids`) plus any row another gate
    rejected, whatever its reason, whose candidate is above the clearance:
    a chunk by its parent's label too, a failed parent lookup as unreadable.
    An id counts when *any* of its collected copies is above the clearance.
    """
    ids = set(withheld_ids(rejected))
    if clearance == "restricted":
        return sorted(ids)
    for row in rejected:
        if row.item_id in ids:
            continue
        if any(
            not permits(clearance, effective_sensitivity(item, parent_verdict_cache))
            for item in candidates.get(row.item_id)
        ):
            ids.add(row.item_id)
    return sorted(ids)


def concealed_ids_metadata(
    rejected: Iterable[RejectedItem],
    candidates: CandidateIndex,
    clearance: Sensitivity,
    parent_verdict_cache: ParentVerdictCache | None,
) -> dict[str, list[str]]:
    """The pack-metadata entry naming what a surface must not show, or ``{}``.

    :func:`withheld_ids_metadata` widened from the rows this gate rejected to
    every rejected candidate the caller cannot read (#1022), so a restricted
    document that a per-strategy gate or supersession removed first does not
    surface its id and score under that other reason. Absent rather than empty
    when there is nothing to hide, for the reason given there.
    """
    ids = unreadable_rejected_ids(rejected, candidates, clearance, parent_verdict_cache)
    return {SENSITIVITY_WITHHELD_IDS_KEY: ids} if ids else {}


class SensitivityLabelError(ValueError):
    """A ``sensitivity`` label a producer may not write.

    ``message`` is text Trellis wrote, safe to show. ``data`` names the field
    at fault (and the caller's clearance, when that is the reason), for a
    surface that wants structured detail. ``above_clearance`` separates a
    label the caller may not set (a permission) from a malformed request.
    """

    def __init__(
        self,
        message: str,
        *,
        data: dict[str, Any],
        above_clearance: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.data = data
        self.above_clearance = above_clearance


def stamp_sensitivity(
    metadata: Mapping[str, Any] | None,
    sensitivity: str | None,
    clearance: Sensitivity,
) -> dict[str, Any]:
    """The metadata to store, carrying the caller's ``sensitivity`` label (#194).

    The one producer rule, shared by every write surface (MCP ``save_memory``
    and ``save_knowledge``, REST ingest). No label leaves the bag untouched: an
    unlabelled save is byte-identical to one before the parameter existed. A
    label above the caller's own ``clearance`` is refused, since the caller
    could then store content it cannot read back. A raw ``data_classification``
    in ``metadata`` is refused for the same reason: it would be a second,
    unchecked way to write the label.
    """
    stamped = dict(metadata or {})
    if DATA_CLASSIFICATION_KEY in stamped:
        msg = (
            f"metadata may not set {DATA_CLASSIFICATION_KEY!r}; use the "
            "sensitivity parameter"
        )
        raise SensitivityLabelError(
            msg, data={"field": f"metadata.{DATA_CLASSIFICATION_KEY}"}
        )
    if sensitivity is None:
        return stamped
    if sensitivity not in SENSITIVITY_ORDER:
        msg = (
            f"sensitivity must be one of {list(SENSITIVITY_ORDER)}, got {sensitivity!r}"
        )
        raise SensitivityLabelError(msg, data={"field": "sensitivity"})
    if sensitivity_rank(sensitivity) > sensitivity_rank(clearance):  # type: ignore[arg-type]
        msg = (
            f"sensitivity {sensitivity!r} is above this credential's clearance "
            f"({clearance!r}); nothing was written. A key needs the "
            f"{sensitivity!r} (or admin) scope to label content that high."
        )
        raise SensitivityLabelError(
            msg,
            data={"field": "sensitivity", "clearance": clearance},
            above_clearance=True,
        )
    stamped[DATA_CLASSIFICATION_KEY] = DataClassification(
        sensitivity=sensitivity,  # type: ignore[arg-type]
        classified_by=["caller"],
    ).model_dump(mode="json")
    return stamped


__all__ = [
    "SENSITIVITY_REJECTION_REASON",
    "SENSITIVITY_WITHHELD_IDS_KEY",
    "CandidateIndex",
    "DocumentClearance",
    "OverwriteRefusedError",
    "SensitivityLabelError",
    "conceal_withheld_ids",
    "concealed_ids_metadata",
    "effective_sensitivity",
    "partition_by_clearance",
    "permits",
    "readable_ids",
    "reader_for",
    "refuse_overwrite",
    "stamp_sensitivity",
    "unreadable_rejected_ids",
    "withheld_ids",
    "withheld_ids_metadata",
]
