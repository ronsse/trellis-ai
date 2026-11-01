"""The sensitivity boundary: a document above the caller's clearance is not served.

:mod:`trellis.retrieve.lifecycle` and :mod:`trellis.retrieve.noise` answer "is
this item fit to serve". This module answers "may *this caller* read it". The
label is :class:`~trellis.schemas.classification.DataClassification`, stamped
under ``metadata["data_classification"]`` by the save surfaces, and read with
:func:`~trellis.schemas.classification.sensitivity_of`. The clearance is the
caller's: ``clearance_of(scopes)`` on a credentialed surface, ``"restricted"``
on a trusted-local one (stdio, auth off, the CLI).

**Transparent by default.** A missing label reads ``internal`` and the lowest
clearance a key reaches without opting in is ``internal``, so a store with no
labels serves every such caller exactly what it served before this gate
existed. Only a document someone labelled ``confidential`` or ``restricted`` is
withheld, and only from a caller who lacks the scope. A *malformed* label reads
``restricted`` (:func:`sensitivity_of`): unparseable is withheld, never served.

**The one opt-in is a ``public`` caller** (an ``external`` key,
:func:`is_public_only`), and it inverts the default: an unlabelled document
reads ``internal``, so it is withheld. It is also shown only what *can* carry a
label. A graph item, trace, precedent or observation has none, and its metadata
is free-form, so :func:`item_readable` serves a ``public`` caller only a pack
item that is a stored whole document row carrying the exact public record
(:func:`~trellis.schemas.classification.is_public_label`: whole-record
equality, the record a person's labelling writes, never a test of the
``sensitivity`` value alone). One predicate decides "public" on every surface,
so a bare ``{"sensitivity": "public"}`` is withheld from a list, a by-id read,
a search and a pack alike. An error while looking the row up withholds, and a
clearance outside the vocabulary ranks as ``public``, not above it.

**What a public caller receives is the stored row, and only a window onto it.**
The governing rule is that nothing it is sent depends on anything it may not
read. So a served item's text and metadata are re-read from the stored document
row (:meth:`~trellis.retrieve.parent_verdicts.ParentVerdictCache.stored_row`),
never taken from the strategy that found it (a vector row's text is an
embed-time snapshot an overwrite does not touch), and the metadata is cut down
to :data:`PUBLIC_METADATA_KEYS`. No count that looks past the filter reaches it
either: :func:`conceal_withheld_ids` returns the empty report.

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
permission. A ``public`` caller does not take part in that: it is never served a
chunk at all (:func:`item_readable`, :meth:`DocumentClearance.permits`), and a
chunk id answers as a missing id does.

Non-pack read surfaces (``get_items``, ``get_file_context``) use
:class:`DocumentClearance`, the same predicate over a stored document. A by-id
read of a withheld document answers exactly as not-found.

Graph nodes, entities, precedents, advisories and observations carry no label
in this slice; ``tests/unit/test_sensitivity_surface_rule.py`` rosters every MCP
tool as gated or free of document content so a new one cannot slip in unread.
A ``public`` caller is refused those surfaces outright instead.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import structlog

from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.retrieve.withholding import summarize_withheld
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    LIFECYCLE_KEY,
    SENSITIVITY_ORDER,
    DataClassification,
    is_public_label,
    sensitivity_of,
    sensitivity_rank,
)
from trellis.schemas.document_metadata import document_form_of
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


#: The clearances a caller holds without being public-only. Spelled out so that
#: a value outside it (a typo, a future rung, ``None`` from a bug) lands on the
#: narrow side of :func:`is_public_only` rather than the wide one.
_NON_PUBLIC_CLEARANCES = frozenset({"internal", "confidential", "restricted"})


def is_public_only(clearance: Sensitivity) -> bool:
    """Whether ``clearance`` reads only stored documents labelled ``public``.

    ``True`` for ``public`` and for anything that is not one of the three higher
    rungs, so an unknown clearance fails closed.
    """
    return clearance not in _NON_PUBLIC_CLEARANCES


def permits(clearance: Sensitivity, sensitivity: Sensitivity) -> bool:
    """Whether a caller holding ``clearance`` may read ``sensitivity`` content.

    A ``clearance`` outside the vocabulary ranks as ``public``, the lowest, so an
    unrecognised value never reads above it.
    """
    try:
        ceiling = sensitivity_rank(clearance)
    except ValueError:
        ceiling = sensitivity_rank("public")
    return sensitivity_rank(sensitivity) <= ceiling


#: The ``PackItem.item_type`` values that can be a stored document row: the
#: keyword axis emits ``document``, the semantic axis ``vector``. Anything else
#: (``entity``, ``observation``, and a trace or precedent as the graph axis
#: carries them) has no label to compare.
_PUBLIC_ITEM_TYPES = frozenset({"document", "vector"})

#: The metadata a ``public`` caller is shown for a document: a title to name it
#: by and the document form, and nothing else. Everything else a store keeps on
#: a row (the entity and trace ids a save stamps, the pointers between
#: documents, tags, lifecycle, classification) is for a caller who may read the
#: rest of the corpus. Each value must be a non-empty string.
PUBLIC_METADATA_KEYS: tuple[str, ...] = ("title", "document_form")


def public_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """``metadata`` cut down to :data:`PUBLIC_METADATA_KEYS`, as a new dict.

    A key that is absent, or whose value is not a non-empty string, is left out,
    so the result never carries a shape a reader would have to guard against.
    The form is read as :func:`~trellis.schemas.document_metadata.document_form_of`
    reads it, so a document written before ``document_form`` existed still
    shows one.
    """
    bag = metadata or {}
    shown: dict[str, Any] = {}
    title = bag.get("title")
    if isinstance(title, str) and title.strip():
        shown["title"] = title
    form = document_form_of(bag)
    if form is not None:
        shown["document_form"] = form
    return shown


def public_withholding(served_count: int = 0) -> dict[str, Any]:
    """The withholding block of a public-only pack: the empty shape, same keys.

    Every count that would see past the filter is zero or empty, so the block
    reads the same whatever the corpus holds. ``served_count`` is the one number
    that stays, because it counts what the caller was sent.
    """
    return {**summarize_withheld([], []).as_telemetry(), "served_count": served_count}


def _is_chunk_id(item_id: str) -> bool:
    """Whether ``item_id`` has the shape of a chunk row's id.

    Deferred for the reason ``ParentVerdictCache.resolve`` defers it:
    ``trellis.ingest_corpus`` imports back into ``trellis.retrieve``.
    """
    from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

    return is_chunk_doc_id(item_id)


def _public_document_readable(
    item: PackItem, parent_verdict_cache: ParentVerdictCache | None
) -> bool:
    """Whether ``item`` is a stored whole document carrying the public label.

    ``False`` when ``item`` is not something a ``public`` caller may be shown
    at all: not a document or vector row, a chunk, no cache to look the row up
    with, no stored row under its id, or a failed lookup. The label is read off
    the stored row, never off ``item.metadata``, and judged by
    :func:`~trellis.schemas.classification.is_public_label`, the same
    predicate :class:`DocumentClearance` and the store filter use.
    """
    if item.item_type not in _PUBLIC_ITEM_TYPES or parent_verdict_cache is None:
        return False
    if _is_chunk_id(item.item_id):
        return False
    stored = parent_verdict_cache.stored_metadata(item.item_id)
    return stored is not None and is_public_label(stored)


def item_readable(
    clearance: Sensitivity,
    item: PackItem,
    parent_verdict_cache: ParentVerdictCache | None,
) -> bool:
    """Whether a caller at ``clearance`` may be served ``item``.

    The one predicate every pack gate asks. Above ``public`` it is exactly
    ``permits(clearance, effective_sensitivity(item, cache))``. For a public-only
    caller (:func:`is_public_only`) the item must also be a stored whole document
    row (:func:`_public_document_readable`) carrying the exact public record: an
    unlabelled document reads ``internal`` and is withheld, a bare
    ``{"sensitivity": "public"}`` is withheld, a chunk is withheld whatever its
    parent says, and an item that cannot carry a label is withheld.
    """
    if not is_public_only(clearance):
        return permits(clearance, effective_sensitivity(item, parent_verdict_cache))
    return _public_document_readable(item, parent_verdict_cache)


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
        if item_readable(clearance, item, parent_verdict_cache):
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
        return item_readable(clearance, item, parent_verdict_cache)

    return _readable


class DocumentClearance:
    """:func:`partition_by_clearance`'s predicate over a stored document.

    For the read surfaces that fetch documents directly rather than through a
    pack. One instance per request: it carries a per-request parent cache. For a
    public-only clearance a document is permitted only when it carries the exact
    public record (:func:`~trellis.schemas.classification.is_public_label`): an
    unlabelled document reads ``internal`` and is not permitted, a bare
    ``{"sensitivity": "public"}`` is not permitted, and neither is an id with no
    stored row (``metadata`` is ``None``). A public-only clearance is also
    refused every chunk id: only a whole document is ever served to it, so a
    chunk answers as a missing id does. Every other clearance judges the
    effective sensitivity value, as before.
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
        if is_public_only(self.clearance):
            return not _is_chunk_id(doc_id) and is_public_label(metadata)
        return permits(self.clearance, self.sensitivity(doc_id, metadata))


#: What CPython's ``sqlite3`` raises when a stored text column is not UTF-8.
_SQLITE_UNDECODABLE = "Could not decode to UTF-8"


def _row_cannot_be_parsed(exc: BaseException) -> bool:
    """Whether ``exc`` is a stored row's own bytes failing to parse.

    The shapes reproduced against a raw row, and nothing else: ``metadata_json``
    that is not JSON (``json.JSONDecodeError``, which also covers empty text and a
    control character in a string), JSON nested past the recursion limit
    (``RecursionError``), and a SQLite text column that is not UTF-8 (a
    ``sqlite3.OperationalError`` whose text names the decode; any other
    ``OperationalError`` is a store fault and is not matched). All four depend on the
    row, never on the store, so none of them is an outage.
    """
    if isinstance(exc, json.JSONDecodeError | RecursionError):
        return True
    return isinstance(exc, sqlite3.OperationalError) and str(exc).startswith(
        _SQLITE_UNDECODABLE
    )


def read_document_row(
    document_store: DocumentStore, doc_id: str, clearance: Sensitivity
) -> dict[str, Any] | None:
    """``document_store.get(doc_id)``, for a by-id read at ``clearance``.

    A public-only caller must receive nothing that depends on a row it may not
    read, and a hidden row whose stored metadata cannot be parsed would otherwise
    answer a guessed id with a 500 where a missing id answers 404. So for that
    clearance alone, a row that fails to parse (:func:`_row_cannot_be_parsed`)
    reads as ``None``, exactly as an absent id does. Any other failure is a store
    fault, does not depend on the id, and propagates; every other clearance
    propagates the parse failure too, as it always did.
    """
    try:
        return document_store.get(doc_id)
    except (json.JSONDecodeError, RecursionError, sqlite3.OperationalError) as exc:
        if not is_public_only(clearance) or not _row_cannot_be_parsed(exc):
            raise
        logger.warning(
            "public_read_unparseable_row", doc_id=doc_id, error=type(exc).__name__
        )
        return None


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

#: The metadata key path that names the item a learning-sourced precedent was
#: promoted from: ``source_item_id``, the id of a served item that is often a
#: document (``trellis/learning/scoring.py`` writes it into the precedent node's
#: ``properties``). The graph axis spreads a node's properties into its pack
#: item's metadata, so it reaches a pack exactly as ``evidence_ref`` does. Kept
#: apart from :data:`SUPERSESSION_POINTER_PATHS` (pinned to the reconcile
#: writers' keys) and :data:`EVIDENCE_POINTER_PATHS`; every concealing walk
#: covers all three.
SOURCE_ITEM_POINTER_PATHS: tuple[tuple[str, ...], ...] = (("source_item_id",),)

_CONCEALED_POINTER_PATHS: tuple[tuple[str, ...], ...] = (
    SUPERSESSION_POINTER_PATHS + EVIDENCE_POINTER_PATHS + SOURCE_ITEM_POINTER_PATHS
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

    Those are :data:`SUPERSESSION_POINTER_PATHS`,
    :data:`EVIDENCE_POINTER_PATHS` and :data:`SOURCE_ITEM_POINTER_PATHS`.
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

    A graph-axis item carries two more: the node's ``evidence_ref`` property is
    spread into the item's metadata (:data:`EVIDENCE_POINTER_PATHS`) and names
    its evidence document, and a precedent's ``source_item_id`` is spread the
    same way (:data:`SOURCE_ITEM_POINTER_PATHS`) and names the item it was
    promoted from. Both are dropped on the same terms, in the same batch.
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

    A public-only caller's rows keep only the allowlisted metadata
    (:func:`public_metadata`) and no ``rank``: a stored row's other keys name
    entities, traces and documents it may not read, and the full-text rank is
    computed from term statistics over every document, hidden ones included.
    """
    rows = list(docs)
    if is_public_only(clearance):
        return [
            {
                **{k: v for k, v in row.items() if k != "rank"},
                "metadata": public_metadata(row.get("metadata")),
            }
            for row in rows
        ]
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
    beside ``document_ids``, the ``lifecycle.superseded_by`` that
    ``supersede_entity`` stamps, and a precedent's ``source_item_id``. Only a
    non-empty string names an id, so a ``document_ids`` that is not a list, and
    a member that is not a string, name nothing.
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
    can leak is the id of a document the caller may not read, in four places:
    its ``document_ids``, a string ``properties["evidence_ref"]``
    (``save_knowledge`` writes both, the same id, and the document may have
    been labelled up, or the node written by a cleared caller, after the write
    check), the ``properties["lifecycle"]["superseded_by"]`` that
    ``supersede_entity`` stamps and, on a learning-sourced precedent, the
    ``properties["source_item_id"]`` it was promoted from
    (:data:`SOURCE_ITEM_POINTER_PATHS`). Each drops an unreadable id; a pointer
    key is removed rather than blanked, ``lifecycle.state`` and every other
    property stay, and a ``document_ids`` member that is not a string, an
    ``evidence_ref`` or ``source_item_id`` that is not a string, and a
    ``document_ids`` that is not a list are left as stored.

    The follow-on to :func:`conceal_document_rows` for the pointers a node
    carries, with the same batching (:func:`_unreadable_pointers`): the
    distinct ids named across *all* the nodes are judged in one pass, so a
    response costs one store read per distinct id however many nodes name it,
    and none when no node names one. A node that names only readable ids, or
    none, comes back as the same object. ``restricted`` does no reads, and a
    missing store leaves every node as it is. A response that serves edges too
    passes both to :func:`conceal_graph_pointers`, to be judged once.
    """
    return conceal_graph_pointers(nodes, (), document_store, clearance)[0]


def _edge_pointers(edge: Mapping[str, Any]) -> set[str]:
    """Every document id ``edge`` names, in its ``evidence_ref`` or properties.

    The provenance column is a top-level key of the edge row
    (:data:`EVIDENCE_POINTER_PATHS` read against the row itself), and the
    caller-supplied ``properties`` are walked as a node's are
    (:func:`_stored_pointers`). Only a non-empty string names an id.
    """
    named = {
        pointer
        for path in EVIDENCE_POINTER_PATHS
        if (pointer := _pointer_at(edge, path))
    }
    return named | _stored_pointers(edge.get("properties"))


def _edge_without_pointers(
    edge: dict[str, Any], hidden: Collection[str]
) -> dict[str, Any]:
    """``edge`` without any ``hidden`` id in its ``evidence_ref`` or properties.

    A hidden top-level ``evidence_ref`` is blanked to ``None``, not removed:
    every stored edge row carries that column (null when unset), so a missing
    key would tell the caller the edge cites a document it may not read. A
    pointer inside ``properties`` is removed, since those keys are absent when
    unset and so carry no tell.

    Copy-on-write: ``edge`` itself comes back when nothing is dropped, and the
    stored edge (and its ``properties``) is never mutated.
    """
    out: Any = edge
    for (column,) in EVIDENCE_POINTER_PATHS:
        value = out.get(column)
        if isinstance(value, str) and value in hidden:
            out = {**out, column: None}
    props = out.get("properties")
    shown = _without_pointers(props, hidden) if isinstance(props, dict) else props
    return out if shown is props else {**out, "properties": shown}  # type: ignore[no-any-return]


def conceal_graph_pointers(
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(nodes, edges)`` as a caller at ``clearance`` may be shown them.

    The one judgement a graph read shares: the distinct ids named across *all*
    the nodes (:func:`conceal_node_pointers`) and *all* the edges
    (:func:`conceal_edge_pointers`) are judged in a single pass, so a response
    costs one store read per distinct id however many rows, of either kind,
    name it, and none when no row names one. A row that names only readable
    ids, or none, comes back as the same object. ``restricted`` does no reads,
    and a missing store leaves every row as it is.
    """
    node_pool = list(nodes)
    edge_pool = list(edges)
    if clearance == "restricted" or document_store is None:
        return node_pool, edge_pool
    named: set[str] = set()
    for node in node_pool:
        named |= _node_pointers(node)
    for edge in edge_pool:
        named |= _edge_pointers(edge)
    if not named:
        return node_pool, edge_pool
    hidden = _unreadable_pointers(document_store, named, clearance)
    if not hidden:
        return node_pool, edge_pool
    return (
        [_node_without_pointers(node, hidden) for node in node_pool],
        [_edge_without_pointers(edge, hidden) for edge in edge_pool],
    )


def conceal_edge_pointers(
    edges: Iterable[dict[str, Any]],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> list[dict[str, Any]]:
    """``edges`` as a caller at ``clearance`` may be shown them by a graph read.

    An edge carries no label either, so it is served whole; what it can leak is
    the id of a document the caller may not read, in a string top-level
    ``evidence_ref`` (the provenance column) or at a pointer path of its
    ``properties`` (``evidence_ref``, ``lifecycle.superseded_by``,
    ``supersedes_doc_id``, ``updates_doc_id``, ``source_item_id``). No shipped
    writer puts a document id in either, but the schema allows it and
    ``link.create`` stores ``properties`` verbatim. Each drops an unreadable
    id: the top-level ``evidence_ref`` reads ``None`` (the column is on every
    stored row, so removing the key would tell), and a ``properties`` pointer is
    removed (those keys are absent when unset). A value that is not a string is
    left as stored.

    A surface that serves nodes beside its edges passes both to
    :func:`conceal_graph_pointers` instead, so the response is judged once.
    """
    return conceal_graph_pointers((), edges, document_store, clearance)[1]


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

    A public-only caller (:func:`is_public_only`) gets no count either. A count
    of what a query matched and the filter removed is an oracle over the very
    corpus the filter hides: an intent naming a word found only in a hidden
    document moves it, and so does a hidden tag or an archived, noise or
    superseded state. So ``rejected_items`` is emptied and ``withholding``
    becomes the empty summary, the keys all still present (zeros and empty
    lists), which renders no ``**Withheld:**`` line anywhere.
    """
    if clearance == "restricted":
        return (
            dict(report) if report is not None else None,
            dict(withholding) if withholding is not None else None,
        )
    if is_public_only(clearance):
        served = withholding.get("served_count") if withholding is not None else 0
        return (
            {**report, "rejected_items": []} if report is not None else None,
            public_withholding(served if isinstance(served, int) else 0)
            if withholding is not None
            else None,
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
            not item_readable(clearance, item, parent_verdict_cache)
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
    "PUBLIC_METADATA_KEYS",
    "SOURCE_ITEM_POINTER_PATHS",
    "SUPERSESSION_POINTER_PATHS",
    "SensitivityLabelError",
    "conceal_document_rows",
    "conceal_document_successor",
    "conceal_edge_pointers",
    "conceal_graph_pointers",
    "conceal_node_pointers",
    "conceal_withheld_ids",
    "conceal_unreadable_successors",
    "concealed_ids_metadata",
    "effective_sensitivity",
    "is_public_only",
    "item_readable",
    "partition_by_clearance",
    "permits",
    "public_metadata",
    "public_withholding",
    "readable_ids",
    "reader_for",
    "refuse_overwrite",
    "stamp_sensitivity",
    "unreadable_rejected_ids",
    "withheld_ids",
    "withheld_ids_metadata",
]
