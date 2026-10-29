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
without it. A loser so kept still stores its own ``lifecycle.superseded_by``, and a
readable successor its ``supersedes_doc_id`` (and an UPDATE addendum its
``updates_doc_id``): :func:`conceal_unreadable_successors` drops each from the
served copy (and :func:`conceal_document_rows` from a REST document row) when the
store says the caller cannot read the id it names.

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
    LIFECYCLE_KEY,
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


#: Every metadata key path that stores a reconcile pointer, i.e. names the other
#: end of a SUPERSEDE or UPDATE decision by document id. Three exist, each a
#: single id string: ``lifecycle.superseded_by`` on a loser (naming its
#: successor; ``Lifecycle``, stamped by ``mark_document_superseded`` and the
#: ``supersedes=`` save path), ``supersedes_doc_id`` on a successor (naming its
#: loser) and ``updates_doc_id`` on an UPDATE addendum (naming the annotated
#: candidate); the last two are written by the reconcile-on-write path and by
#: session capture. They are spelled here rather than imported because
#: ``trellis.mcp`` imports this package; ``tests/unit/retrieve/test_sensitivity.py``
#: pins them to ``trellis.mcp.reconcile.SUPERSEDES_DOC_KEY`` / ``UPDATES_DOC_KEY``
#: and to every pointer key the writers emit.
SUPERSESSION_POINTER_PATHS: tuple[tuple[str, ...], ...] = (
    (LIFECYCLE_KEY, "superseded_by"),
    ("supersedes_doc_id",),
    ("updates_doc_id",),
)


#: The metadata key path that names a document id without being a reconcile
#: pointer: ``evidence_ref``, which the graph axis lifts from a node's
#: ``properties`` into the pack item's metadata (``save_knowledge`` writes the
#: node's evidence document id there). Kept apart from
#: :data:`SUPERSESSION_POINTER_PATHS` because that tuple is pinned to the
#: reconcile writers' keys; every concealing walk covers both.
EVIDENCE_POINTER_PATHS: tuple[tuple[str, ...], ...] = (("evidence_ref",),)

_CONCEALED_POINTER_PATHS: tuple[tuple[str, ...], ...] = (
    SUPERSESSION_POINTER_PATHS + EVIDENCE_POINTER_PATHS
)


def _pointer_at(
    metadata: Mapping[str, Any] | None, path: tuple[str, ...]
) -> str | None:
    """The id stored at ``path`` in ``metadata``, whatever the record's state.

    Only a non-empty string names an id; a missing, empty or malformed value
    (including a list, which no writer stores) names nothing and is left as
    stored.
    """
    node: Any = metadata
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def _stored_pointers(metadata: Mapping[str, Any] | None) -> set[str]:
    """Every id ``metadata`` names at a concealed pointer path.

    Those are :data:`SUPERSESSION_POINTER_PATHS` and
    :data:`EVIDENCE_POINTER_PATHS`.
    """
    return {
        pointer
        for path in _CONCEALED_POINTER_PATHS
        if (pointer := _pointer_at(metadata, path))
    }


def _dropping(node: Any, path: tuple[str, ...], hidden: Collection[str]) -> Any:
    """``node`` without the pointer at ``path`` when it names a ``hidden`` id.

    Copy-on-write along the path only: the stored bag, and a pool's shared
    one, are never mutated, and ``node`` itself comes back when nothing is
    dropped. A record keeps every other key, ``lifecycle.state`` included.
    """
    if not isinstance(node, dict):
        return node
    key, *rest = path
    if key not in node:
        return node
    if not rest:
        value = node[key]
        if isinstance(value, str) and value in hidden:
            return {k: v for k, v in node.items() if k != key}
        return node
    child = _dropping(node[key], tuple(rest), hidden)
    return node if child is node[key] else {**node, key: child}


def _without_pointers(
    metadata: Mapping[str, Any], hidden: Collection[str]
) -> Mapping[str, Any]:
    """``metadata`` with every pointer that names a ``hidden`` id dropped."""
    out: Any = metadata
    for path in _CONCEALED_POINTER_PATHS:
        out = _dropping(out, path, hidden)
    return out  # type: ignore[no-any-return]


def _unreadable_pointers(
    document_store: DocumentStore, named: Collection[str], clearance: Sensitivity
) -> set[str]:
    """The ids in ``named`` that a caller at ``clearance`` may not read.

    Judged by the store, as :func:`readable_ids` judges a by-id read (a chunk
    by its parent, a store error as unreadable), and never by whether the id is
    in a pack's candidate pool: a pool test would answer only for a document
    that matched the query, which is an oracle on what matched.
    """
    return set(named) - set(readable_ids(document_store, sorted(named), clearance))


def _concealed_bags(
    bags: Sequence[Mapping[str, Any] | None],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> list[Mapping[str, Any] | None]:
    """Each metadata bag, or a copy without the pointers the caller cannot read.

    The one batched step every surface shares: the distinct ids named across
    *all* the bags are judged in a single :func:`readable_ids` call, so a
    response costs one store read per distinct id however many rows name it,
    and none when no row names one. A bag that names only readable ids, or
    none, comes back as the same object. ``restricted`` does no reads, and a
    missing store (no label to judge, so no verdict, the parent cache's own
    rule) leaves every bag as it is.
    """
    if clearance == "restricted" or document_store is None:
        return list(bags)
    named: set[str] = set()
    for bag in bags:
        named |= _stored_pointers(bag)
    if not named:
        return list(bags)
    hidden = _unreadable_pointers(document_store, named, clearance)
    if not hidden:
        return list(bags)
    return [_without_pointers(bag, hidden) if bag else bag for bag in bags]


def conceal_unreadable_successors(
    items: Iterable[PackItem],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> list[PackItem]:
    """``items`` with every supersession pointer the caller cannot read dropped.

    A loser whose successor is above the caller's clearance is served (see
    :func:`~trellis.retrieve.lifecycle.partition_superseded`), and it still
    carries its own stored ``lifecycle.superseded_by`` naming that successor.
    The reverse holds too: a readable successor's ``supersedes_doc_id`` may
    name a loser the caller cannot read, and an UPDATE addendum's
    ``updates_doc_id`` the candidate it annotates. Any such id would reach the
    caller in the item's metadata, so it is dropped from the served copy
    (:data:`SUPERSESSION_POINTER_PATHS`). The state and the ``reconciliation``
    marker stay; the item reads superseded or updated, by nothing the caller
    may name.

    A graph-axis item carries one more: the node's ``evidence_ref`` property is
    spread into the item's metadata (:data:`EVIDENCE_POINTER_PATHS`) and names
    its evidence document. It is dropped on the same terms, in the same batch.
    Supersession is resolved over the pool first (``partition_superseded``
    reads ``evidence_ref`` itself), so which items are served, and in what
    order, does not change; only the served copy's metadata does.

    Applies to every item whatever its ``lifecycle.state``, since the id is the
    leak. See :func:`_concealed_bags` for the batching and the no-read cases.
    An item that names readable ids, or none, is passed through unchanged
    rather than copied.
    """
    pool = list(items)
    bags = _concealed_bags([item.metadata for item in pool], document_store, clearance)
    return [
        item if bag is item.metadata else item.model_copy(update={"metadata": bag})
        for item, bag in zip(pool, bags, strict=True)
    ]


def conceal_document_rows(
    docs: Iterable[dict[str, Any]],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> list[dict[str, Any]]:
    """``docs`` as a caller at ``clearance`` may be shown them by a REST read.

    The same drop as :func:`conceal_unreadable_successors`, for stored document
    rows the caller already passed the read gate for (``GET /documents`` and
    its search, ``GET /search``, ``GET /documents/{id}``). One batched store
    judgement per call, so a response passes all its rows in one call rather
    than one per row. A row that names nothing unreadable is returned as the
    same object, else as a copy.
    """
    rows = list(docs)
    bags = _concealed_bags(
        [row.get("metadata") for row in rows], document_store, clearance
    )
    return [
        row if bag is row.get("metadata") else {**row, "metadata": bag}
        for row, bag in zip(rows, bags, strict=True)
    ]


def conceal_document_successor(
    doc: dict[str, Any], document_store: DocumentStore, clearance: Sensitivity
) -> dict[str, Any]:
    """``doc`` as a caller at ``clearance`` may be shown it by a by-id read.

    :func:`conceal_document_rows` for one row: returns ``doc`` itself when
    nothing is dropped, else a copy.
    """
    (out,) = conceal_document_rows([doc], document_store, clearance)
    return out


def _node_pointers(node: Mapping[str, Any]) -> set[str]:
    """Every document id ``node`` names, in ``document_ids`` or its properties.

    The properties are walked as a pack item's metadata is
    (:func:`_stored_pointers`): the ``evidence_ref`` ``save_knowledge`` writes
    beside ``document_ids``, and the ``lifecycle.superseded_by`` that
    ``supersede_entity`` stamps. Only a non-empty string names an id, so a
    ``document_ids`` that is not a list, and a member that is not a string,
    name nothing.
    """
    ids = node.get("document_ids")
    named = (
        {i for i in ids if isinstance(i, str) and i} if isinstance(ids, list) else set()
    )
    return named | _stored_pointers(node.get("properties"))


def _node_without_pointers(
    node: dict[str, Any], hidden: Collection[str]
) -> dict[str, Any]:
    """``node`` without any ``hidden`` id in ``document_ids`` or its properties.

    Copy-on-write: ``node`` itself comes back when nothing is dropped, and the
    stored node (and its ``properties``) is never mutated. A member of
    ``document_ids`` that is not a string is kept as stored.
    """
    props = node.get("properties")
    shown = _without_pointers(props, hidden) if isinstance(props, dict) else props
    out = node if shown is props else {**node, "properties": shown}
    ids = out.get("document_ids")
    if isinstance(ids, list):
        kept = [i for i in ids if not (isinstance(i, str) and i in hidden)]
        if len(kept) != len(ids):
            out = {**out, "document_ids": kept}
    return out


def conceal_node_pointers(
    nodes: Iterable[dict[str, Any]],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> list[dict[str, Any]]:
    """``nodes`` as a caller at ``clearance`` may be shown them by a graph read.

    A node carries no label of its own (#1036), so it is served whole; what it
    can leak is the id of a document the caller may not read, in three places:
    its ``document_ids``, a string ``properties["evidence_ref"]``
    (``save_knowledge`` writes both, the same id, and the document may have
    been labelled up, or the node written by a cleared caller, after the write
    check) and the ``properties["lifecycle"]["superseded_by"]`` that
    ``supersede_entity`` stamps. Each drops an unreadable id; a pointer key is
    removed rather than blanked, ``lifecycle.state`` and every other property
    stay, and a ``document_ids`` member that is not a string, an
    ``evidence_ref`` that is not a string, and a ``document_ids`` that is not a
    list are left as stored.

    The follow-on to :func:`conceal_document_rows` for the pointers a node
    carries, with the same batching (:func:`_unreadable_pointers`): the
    distinct ids named across *all* the nodes are judged in one pass, so a
    response costs one store read per distinct id however many nodes name it,
    and none when no node names one. A node that names only readable ids, or
    none, comes back as the same object. ``restricted`` does no reads, and a
    missing store leaves every node as it is.
    """
    pool = list(nodes)
    if clearance == "restricted" or document_store is None:
        return pool
    named: set[str] = set()
    for node in pool:
        named |= _node_pointers(node)
    if not named:
        return pool
    hidden = _unreadable_pointers(document_store, named, clearance)
    if not hidden:
        return pool
    return [_node_without_pointers(node, hidden) for node in pool]


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
    "EVIDENCE_POINTER_PATHS",
    "SENSITIVITY_REJECTION_REASON",
    "SENSITIVITY_WITHHELD_IDS_KEY",
    "CandidateIndex",
    "DocumentClearance",
    "OverwriteRefusedError",
    "SUPERSESSION_POINTER_PATHS",
    "SensitivityLabelError",
    "conceal_document_rows",
    "conceal_document_successor",
    "conceal_node_pointers",
    "conceal_withheld_ids",
    "conceal_unreadable_successors",
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
