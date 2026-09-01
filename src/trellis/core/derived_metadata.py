"""Writing derived metadata back onto a row that slow work may have outrun.

A batch worker that computes derived metadata does three things in order:
reads a page of rows, runs something slow over each snapshot, then writes the
result back. ``DocumentStore.put`` has no partial-update form, so that last
step is a whole-row write — it carries ``content`` and the full ``metadata``
bag as they were **before** the slow work started.

Any write that lands inside that window is therefore silently reverted. #421
found it in ``worker enrich``, where the window is the whole batch of LLM
calls: minutes on a corpus-scale run, and the loser gets no error, no log
line and no reconciliation. Post-#406/#418 those writes correctly pass
``preserve_updated_at=True``, which makes the revert *worse* rather than
better — the row keeps the losing writer's ``updated_at`` while holding
pre-slow-work content, so it reports "current as of T" for content that never
existed at T, and
:func:`trellis.retrieve.file_context._newest_timestamp` hands that stamp to
the #307 read hook's staleness gate.

The invariant this module exists to make cheap:

    **Re-read immediately before the write whenever slow work separates the
    read from the write.** Then the write is genuinely metadata-only rather
    than metadata-only-in-intent, which is exactly what
    ``preserve_updated_at=True`` asserts.

Two things follow from doing it here rather than at each call site.

**The content lost update disappears by construction**, because the content
written is the one just read rather than the snapshot's. The cost is one
``get`` per *written* row, which is nothing against the LLM or embedder call
that opened the window — the ratio is the whole argument, and it is why this
is not applied to sites whose read and write happen in the same breath (see
the per-site notes in the #421 PR). Applying it to a fast path is pure cost.

**The metadata lost update disappears too**, and that half is easy to miss: a
concurrent *metadata* writer (a classify refresh, a lifecycle stamp) leaves
``content`` byte-identical, so a content-hash check sees nothing, yet the
snapshot write-back clobbers its keys just the same. That is why the update
is expressed as a callable over the **freshly read** metadata rather than as
a mapping computed from the snapshot: the caller merges its own derived keys
onto whatever the row says now, and every key it does not own is carried
through untouched.

Detection is kept as a **counter**, not as a refusal. A race that fires often
is a real signal about deployment concurrency that a silent merge would hide,
and a worker that skips instead of merging leaves the document unprocessed
while logging into a channel nobody reads. So the merge always happens and
:attr:`DerivedMetadataWrite.content_changed` is what a caller tallies.

The one case that is **not** merged is a row that vanished between the read
and the write. ``put`` on a missing id *inserts*, so the snapshot write-back
would resurrect a deleted document with stale content and a fresh
``created_at``. That is reported as
:attr:`DerivedMetadataWrite.vanished` and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)

__all__ = ["DerivedMetadataWrite", "apply_derived_metadata"]


@dataclass(frozen=True)
class DerivedMetadataWrite:
    """Outcome of one :func:`apply_derived_metadata` call."""

    doc_id: str
    #: ``True`` when the row was re-read and written.
    written: bool
    #: ``True`` when the row no longer existed at write time. Mutually
    #: exclusive with ``written`` — nothing is inserted in that case.
    vanished: bool = False
    #: ``True`` when the stored content differed from the snapshot the caller
    #: derived from, i.e. a concurrent write landed inside the slow window.
    #: The derived metadata is still written (onto the *current* content);
    #: this is the telemetry, not a refusal. Always ``False`` when the caller
    #: passed no ``snapshot_content`` to compare against.
    content_changed: bool = False
    #: The metadata bag as written — the merge result, not the caller's
    #: updates. ``None`` when nothing was written. Callers that mirror the
    #: write elsewhere (``sync_vector_metadata``) must forward *this*, not
    #: the snapshot bag they started from.
    metadata: dict[str, Any] | None = None


def apply_derived_metadata(
    document_store: DocumentStore,
    doc_id: str,
    build_updates: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    snapshot_content: str | None = None,
) -> DerivedMetadataWrite:
    """Merge derived metadata onto a row's **current** content and metadata.

    Args:
        document_store: Store holding the row.
        doc_id: Row to update.
        build_updates: Called with the freshly-read metadata (a private copy;
            return the keys to merge rather than mutating it) and returns the
            keys to merge over it. Taking the *current* metadata rather
            than the snapshot's is what stops a concurrent metadata write
            being clobbered — derive prior-value-dependent keys (merged tag
            bags, accumulated ``classified_by`` lists) from what this returns
            you, never from the snapshot.
        snapshot_content: The content the caller derived its updates from,
            when it has it. Supplied only to populate
            :attr:`DerivedMetadataWrite.content_changed`; it is never
            written.

    Returns:
        :class:`DerivedMetadataWrite`.

    Raises:
        Whatever the store raises. Read and write failures propagate — this
        is the authoritative write, and swallowing it would report a
        successful enrichment of a row that was never touched.
    """
    current = document_store.get(doc_id)
    if current is None:
        logger.warning("derived_metadata.row_vanished", doc_id=doc_id)
        return DerivedMetadataWrite(doc_id=doc_id, written=False, vanished=True)

    content = current.get("content", "")
    metadata: dict[str, Any] = dict(current.get("metadata") or {})
    content_changed = snapshot_content is not None and snapshot_content != content
    if content_changed:
        logger.warning(
            "derived_metadata.stale_snapshot",
            doc_id=doc_id,
            detail=(
                "content changed while derived metadata was being computed; "
                "merging onto the current content"
            ),
        )

    merged = {**metadata, **build_updates(metadata)}
    document_store.put(doc_id, content, merged, preserve_updated_at=True)
    return DerivedMetadataWrite(
        doc_id=doc_id,
        written=True,
        content_changed=content_changed,
        metadata=merged,
    )
