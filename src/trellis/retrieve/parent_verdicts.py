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
advisory step on the write path), logged once at warning level, and the
cache is marked :attr:`ParentVerdictCache.degraded` so the caller can
record the condition instead of serving silently-unfiltered chunks with no
trace anywhere.

**One fetch per distinct parent per pack.** :class:`ParentVerdictCache` is
constructed once per :meth:`~trellis.retrieve.pack_builder.PackBuilder.build`
/ :meth:`~trellis.retrieve.pack_builder.PackBuilder.build_sectioned` call
and threaded through every strategy's pass over the collect-seam gates, so
three chunks sharing a parent cost one
:meth:`~trellis.stores.base.document.DocumentStore.get` regardless of how
many strategies or how many gates consult it. A non-chunk item (where
:func:`~trellis.retrieve.concentration.resolve_parent_id` returns the
item's own id) never reaches the store at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.retrieve.concentration import resolve_parent_id
from trellis.retrieve.lifecycle import is_archived
from trellis.retrieve.noise import passes_signal_quality

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.pack import PackItem
    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)


class ParentVerdictCache:
    """Per-pack memoized lookup of a chunk's parent document metadata.

    Not a general document cache: :meth:`resolve` only ever returns
    something non-``None`` for an item whose
    :func:`~trellis.retrieve.concentration.resolve_parent_id` differs from
    its own ``item_id`` — i.e. a chunk. A non-chunk item is its own parent,
    and its own metadata already went through the gate directly, so
    looking it up again here would be redundant, not merely wasteful.
    """

    def __init__(self, document_store: DocumentStore | None) -> None:
        #: ``None`` when no builder is wired with a document store at all
        #: (e.g. a test double) — treated as "every parent lookup misses",
        #: not as a store error, so it never sets :attr:`degraded`.
        self._document_store = document_store
        self._cache: dict[str, dict[str, Any] | None] = {}
        #: Set once a parent fetch raises. Sticky for the cache's whole
        #: lifetime (one pack build): a store that failed once for this
        #: build is not worth retrying for every subsequent chunk, and the
        #: caller reports this once, not per occurrence.
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

    Applies the caller's own ``spec`` — the same one
    :func:`~trellis.retrieve.noise.partition_by_signal_quality` applied to
    the item's own metadata — to the parent's, so a caller-supplied spec
    that admits noise (e.g. curation tooling) admits a noisy parent's
    chunks too, rather than this gate silently overriding it.
    """
    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in items:
        parent_metadata = cache.resolve(item)
        if parent_metadata is not None and not passes_signal_quality(
            parent_metadata, spec
        ):
            withheld.append(item)
        else:
            kept.append(item)
    return kept, withheld


__all__ = [
    "ParentVerdictCache",
    "partition_by_parent_archived",
    "partition_by_parent_signal_quality",
]
