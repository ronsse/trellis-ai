"""Keeping a vector row's metadata agreeing with the document behind it.

**A vector row's ``metadata`` is a snapshot taken at embed time.** Nothing
refreshes it when the backing document's metadata changes, and
:class:`~trellis.retrieve.strategies.SemanticSearch` builds its
:class:`~trellis.schemas.pack.PackItem` from that snapshot rather than from
the document store. So any tag written *after* the document was embedded is
invisible to semantic retrieval until something re-embeds it.

That is not hypothetical twice over. #337 hit it for ``Lifecycle``: archival
written only through ``document_store.put`` left the semantic path serving
the item because its vector row had no lifecycle key. #338 is the same root
cause for ``ContentTags``: :func:`~trellis.classify.feedback.apply_noise_tags`
— the demote half of the feedback loop — wrote ``signal_quality="noise"`` to
the document store alone, and a production join of ``documents`` to
``vectors`` found **45 noise-tagged documents and not one whose vector row
agreed** (28 carried no ``signal_quality`` at all, 17 still read
``"standard"``).

This module is the one writer for the repair: a **metadata-only re-upsert**
that carries the existing embedding through unchanged, so nothing is
re-embedded and no embedding cost is incurred. It is deliberately narrow —
see :data:`SYNCED_METADATA_KEYS` for which keys are mirrored and why the
rest are not.

It lives in :mod:`trellis.core` rather than beside either caller because
both the classify layer (:mod:`trellis.classify.feedback`,
:mod:`trellis.classify.refresh`) and the CLI backfill need it, and
:mod:`trellis.retrieve` is a heavy package that the classify write path has
no other reason to import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from trellis.stores.base.vector import VectorStore
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Document-metadata keys mirrored onto the vector row by
#: :func:`sync_vector_metadata`.
#:
#: Scoped to the **classify layer's output**, which is the pair of keys the
#: post-embed tag writers touch:
#:
#: * ``content_tags`` — the facet bag. ``signal_quality`` is the facet the
#:   noise filter acts on; ``domain`` is what
#:   ``SemanticSearch``'s default-pass domain scoping reads; and the
#:   ``importance_scored_at`` stamp rides here too.
#: * ``auto_importance`` — read by ``_apply_importance`` on the semantic
#:   axis, and **coupled to the stamp inside ``content_tags``**: an
#:   ``auto_importance`` above the decay threshold with no
#:   ``importance_scored_at`` beside it raises ``ValueError`` by the
#:   greenfield writer contract (adr-importance-score-freshness §3.5).
#:   Syncing the two together is therefore not a convenience — mirroring
#:   either one alone could manufacture exactly the broken pair that
#:   contract exists to catch.
#:
#: Everything else a vector row carries is deliberately excluded. ``content``
#: is the row's *own* excerpt, cut at embed time by ``build_vector_row``
#: because that is the last point holding the full document; ``doc_id`` and
#: ``created_at`` are the row's identity and recency stamp. Copying the
#: document bag wholesale would clobber all three. ``lifecycle`` has its own
#: writer on the retention path (#337). Shadow tags
#: (:data:`~trellis.schemas.classification.SHADOW_TAGS_KEY`) are excluded for
#: the reason ``build_vector_row`` already excludes them: a measurement-only
#: record duplicated into a store with no shadow awareness can only drift.
SYNCED_METADATA_KEYS: tuple[str, ...] = ("content_tags", "auto_importance")


def sync_vector_metadata(
    vector_store: VectorStore | None,
    item_id: str,
    document_metadata: dict[str, Any] | None,
    *,
    keys: tuple[str, ...] = SYNCED_METADATA_KEYS,
) -> bool:
    """Mirror ``keys`` from a document's metadata onto its vector row.

    The vector row is keyed by ``doc_id`` (``build_vector_row`` writes the
    two 1:1), so ``item_id`` is the document id.

    Each key is mirrored in both directions: present on the document means
    written to the row, **absent on the document means removed from the
    row**. "Agreeing" has to mean agreeing, or a key deleted from a document
    would live on in the snapshot forever — the same staleness one level
    down.

    Args:
        vector_store: The vector store, or ``None`` on a deployment that has
            none configured (in which case this is a no-op).
        item_id: The document id, which is also the vector row's key.
        document_metadata: The document's metadata bag — the authority.
        keys: Which keys to mirror. Defaults to :data:`SYNCED_METADATA_KEYS`.

    Returns:
        ``True`` iff a row existed and was actually rewritten. A row already
        in agreement returns ``False``, so a re-run over a synced corpus
        reports zero work rather than churning every row.

    Never raises. A missing vector row is normal — structural nodes and
    documents ingested before embed-on-ingest was enabled have none — and a
    vector-backend failure is logged rather than propagated, because every
    caller has *already* written the authoritative document row by the time
    it gets here. Failing the tag write to report a mirror failure would
    lose the tag; failing silently would hide the divergence. So: fail soft
    and loud, exactly as :func:`trellis.mutate.handlers._sync_vector_lifecycle`
    does for the lifecycle stamp.
    """
    if vector_store is None:
        return False
    try:
        row = vector_store.get(item_id)
        if row is None:
            return False
        metadata = dict(row.get("metadata") or {})
        source = document_metadata or {}
        changed = False
        for key in keys:
            if key in source:
                if metadata.get(key) != source[key] or key not in metadata:
                    metadata[key] = source[key]
                    changed = True
            elif key in metadata:
                del metadata[key]
                changed = True
        if not changed:
            return False
        vector_store.upsert(item_id, row["vector"], metadata)
    except Exception:
        logger.warning(
            "vector_metadata_sync_failed",
            item_id=item_id,
            keys=list(keys),
            exc_info=True,
        )
        return False
    logger.debug("vector_metadata_synced", item_id=item_id, keys=list(keys))
    return True


def resolve_vector_store(registry: StoreRegistry) -> VectorStore | None:
    """The registry's vector store, or ``None`` on a deployment without one.

    Wiring helper for the surfaces that *drive* a tag write (the CLI and
    REST entry points to the feedback loop) rather than perform it. A
    deployment with no usable vector store must still be able to demote a
    document — the document store is the authority — but it must not do so
    quietly, because the consequence is precisely the divergence #338 is
    about. So the failure degrades to ``None`` and says what that costs.
    """
    try:
        return registry.knowledge.vector_store
    except Exception:
        logger.warning(
            "vector_store_unavailable_for_metadata_sync",
            consequence=(
                "tag writes will not reach vector rows; the semantic axis "
                "will keep serving pre-write metadata"
            ),
            exc_info=True,
        )
        return None


def vector_metadata_diverges(
    document_metadata: dict[str, Any] | None,
    vector_metadata: dict[str, Any] | None,
    *,
    keys: tuple[str, ...] = SYNCED_METADATA_KEYS,
) -> bool:
    """Whether a document and its vector row disagree on any of ``keys``.

    The read-only half of :func:`sync_vector_metadata`, split out so the
    backfill can count divergent rows in a dry run without writing, and so
    the invariant a test asserts is the same predicate the writer enforces.
    """
    source = document_metadata or {}
    target = vector_metadata or {}
    return any(
        (key in source and (key not in target or target[key] != source[key]))
        or (key not in source and key in target)
        for key in keys
    )


__all__ = [
    "SYNCED_METADATA_KEYS",
    "resolve_vector_store",
    "sync_vector_metadata",
    "vector_metadata_diverges",
]
