"""A chunk inherits its parent document's archived and noise verdicts.

A long document is stored as one parent row plus N chunk rows
(:mod:`trellis.ingest_corpus.chunker`), and every chunk carries its own
``content_tags``/``Lifecycle`` metadata written at ingest time. The collect
seam's archived gate (:func:`trellis.retrieve.lifecycle.partition_archived`)
and noise gate (:func:`trellis.retrieve.noise.partition_by_signal_quality`)
read only that per-item metadata — so a verdict recorded against the
*parent* (:func:`trellis.classify.feedback.apply_noise_tags`, the retention
prune in :mod:`trellis.mutate.retention`, both of which act per document id)
never reaches chunks that were never themselves re-tagged.

Measured on the reference deployment (2026-10-18): 67 noise-tagged,
non-archived parent documents have chunk rows; those parents have 704
chunks, 581 tagged ``standard`` on their own row, served 727 times in the
60 days after the parent's ``classified_at``.

This module adds a second, parent-scoped check of the *same* predicates
(:func:`~trellis.retrieve.lifecycle.is_archived`,
:func:`~trellis.retrieve.noise.passes_signal_quality`) alongside the
item-level ones — it does not change what either predicate means, only
what metadata bag a chunk is additionally checked against.

**Default-pass, like the gates it extends.** A parent the document store
does not have is no verdict: :class:`ParentVerdictCache` returns ``None``
and the chunk is served. A store error while fetching a parent must not
fail the pack either — the error is caught whole (not filtered by
exception type, the same posture
:func:`trellis.core.write_provenance.resolve_stamp_staleness` takes for an
advisory step on the write path), logged at warning level once per failed
parent, and the cache is marked :attr:`ParentVerdictCache.degraded` so the
caller can record the condition instead of serving silently-unfiltered
chunks with no trace anywhere.

**One fetch per distinct parent per pack.** :class:`ParentVerdictCache` is
constructed once per :meth:`~trellis.retrieve.pack_builder.PackBuilder.build`
/ :meth:`~trellis.retrieve.pack_builder.PackBuilder.build_sectioned` call
and threaded through every strategy's pass over the collect-seam gates, so
three chunks sharing a parent cost one
:meth:`~trellis.stores.base.document.DocumentStore.get` regardless of how
many strategies or how many gates consult it. A non-chunk item — its own
id fails :func:`~trellis.ingest_corpus.models.is_chunk_doc_id`, or (should
that check somehow pass) :func:`~trellis.retrieve.concentration.resolve_parent_id`
still returns the item's own id — never reaches the store at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.retrieve.concentration import resolve_parent_id
from trellis.retrieve.lifecycle import is_archived
from trellis.retrieve.noise import (
    NOISE_SIGNAL_QUALITY,
    _facet_value,
    passes_signal_quality,
)
from trellis.schemas.document_metadata import document_form_of

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.pack import PackItem
    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)


class ParentVerdictCache:
    """Per-pack memoized lookup of a chunk's parent document metadata.

    Not a general document cache: :meth:`resolve` only ever returns
    something non-``None`` for an item whose own id is a real chunk id
    (:func:`~trellis.ingest_corpus.models.is_chunk_doc_id`) *and* whose
    :func:`~trellis.retrieve.concentration.resolve_parent_id` differs from
    its own ``item_id``. A non-chunk item is its own parent, and its own
    metadata already went through the gate directly, so looking it up
    again here would be redundant, not merely wasteful — and a non-chunk
    item cannot borrow a verdict via a caller-set ``parent_doc_id`` either,
    since the id check runs first.
    """

    def __init__(self, document_store: DocumentStore | None) -> None:
        #: ``None`` when no builder is wired with a document store at all
        #: (e.g. a test double) — treated as "every parent lookup misses",
        #: not as a store error, so it never sets :attr:`degraded`.
        self._document_store = document_store
        self._cache: dict[str, dict[str, Any] | None] = {}
        #: Set once any parent fetch raises; sticky for the cache's whole
        #: lifetime (one pack build). Only the failed parent is cached as a
        #: miss: every other distinct parent is still fetched, so one
        #: unreadable row cannot switch the check off for the whole pack.
        #: The caller reports the condition once per build, not per chunk.
        self.degraded = False

    def resolve(self, item: PackItem) -> dict[str, Any] | None:
        """The parent document's metadata for ``item``, or ``None``.

        ``None`` covers three cases a caller must treat identically — no
        verdict, serve the item unaffected by this gate: ``item`` is not a
        chunk, the store has no row for its parent id, or the parent's
        ``metadata`` is not a dict (defensive — see
        :func:`~trellis.retrieve.lifecycle.is_archived`'s own fallback for
        why malformed data reads as absent rather than excluding).
        """
        if self._document_store is None:
            return None
        # Deferred like the matching import in
        # ``trellis.retrieve.concentration.resolve_parent_id``:
        # ``trellis.ingest_corpus.__init__`` pulls ``sync``, which imports
        # back into ``trellis.retrieve`` (the embed hook), so a module-level
        # import here would make the two packages mutually importing.
        from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

        if not is_chunk_doc_id(item.item_id):
            # Only a real chunk id may inherit a parent verdict. Without
            # this, a non-chunk item whose metadata happens to carry a
            # caller-set ``parent_doc_id`` (``resolve_parent_id`` trusts
            # that key unconditionally) would have this cache fetch and
            # apply another document's verdict to it.
            return None
        parent_id = resolve_parent_id(item)
        if parent_id == item.item_id:
            return None
        if parent_id in self._cache:
            return self._cache[parent_id]
        try:
            parent_doc = self._document_store.get(parent_id)
        except Exception:
            logger.warning(
                "parent_verdict_fetch_failed",
                parent_id=parent_id,
            )
            self.degraded = True
            self._cache[parent_id] = None
            return None
        metadata = parent_doc.get("metadata") if parent_doc else None
        resolved = metadata if isinstance(metadata, dict) else None
        self._cache[parent_id] = resolved
        return resolved


def parent_document_form(item: PackItem, cache: ParentVerdictCache) -> str | None:
    """The ``document_form`` of the document ``item`` is a chunk of, or ``None``.

    Rides :meth:`ParentVerdictCache.resolve`, so it is the *same* lookup the
    archived and noise gates make and obeys the same guard: only an item whose
    own id is a chunk id (``<parent>#chunk-N``) inherits through a parent, and
    a non-chunk item cannot borrow a form via a caller-set ``parent_doc_id``.
    The parent id is read as the concentration measure reads it — the item's
    ``parent_doc_id``, else the chunk id's own prefix for a vector snapshot
    embedded before that key existed. A parent the build's cache already
    fetched costs no document-store read. A non-chunk item, a parent the store
    lacks, a store error and a parent that carries no form all read as
    ``None``: no verdict, serve the item. This is
    :func:`trellis.retrieve.class_prior.effective_form`'s injected resolver
    under ``scope="parents_and_chunks"``.
    """
    return document_form_of(cache.resolve(item))


def partition_by_parent_archived(
    items: Iterable[PackItem],
    cache: ParentVerdictCache,
) -> tuple[list[PackItem], list[PackItem]]:
    """Split ``items`` into ``(kept, withheld)`` on the *parent's* archived stamp.

    The item's own archived stamp is not re-checked here — call this after
    :func:`~trellis.retrieve.lifecycle.partition_archived` has already
    applied it, over the survivors.
    """
    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in items:
        parent_metadata = cache.resolve(item)
        if parent_metadata is not None and is_archived(parent_metadata):
            withheld.append(item)
        else:
            kept.append(item)
    return kept, withheld


def partition_by_parent_signal_quality(
    items: Iterable[PackItem],
    spec: dict[str, Any],
    cache: ParentVerdictCache,
) -> tuple[list[PackItem], list[PackItem]]:
    """Split ``items`` into ``(kept, withheld)`` on the *parent's* ``signal_quality``.

    The parent contributes only a noise verdict, never the caller's whole
    ``spec``: a chunk is withheld through its parent only when the
    parent's own ``signal_quality`` is literally ``"noise"`` *and* that
    value fails ``spec`` — the same ``spec``
    :func:`~trellis.retrieve.noise.partition_by_signal_quality` applies to
    the item's own metadata. Applying the whole ``spec`` to the parent,
    rather than just its noise-ness, misreads a caller-supplied allowlist
    such as curation's ``{"in": ["noise"]}``: a parent tagged ``standard``
    fails that allowlist too, which would withhold a noise-tagged chunk
    the caller explicitly asked to see, via a parent that was never noise
    at all.
    """
    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in items:
        parent_metadata = cache.resolve(item)
        if _facet_value(
            parent_metadata
        ) == NOISE_SIGNAL_QUALITY and not passes_signal_quality(parent_metadata, spec):
            withheld.append(item)
        else:
            kept.append(item)
    return kept, withheld


__all__ = [
    "ParentVerdictCache",
    "parent_document_form",
    "partition_by_parent_archived",
    "partition_by_parent_signal_quality",
]
