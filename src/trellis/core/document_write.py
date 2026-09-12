"""The document-plane write seam: a row and its vector mirror, written together.

**A vector row's metadata is a snapshot taken at embed time**, so a document
write that lands after the embed is invisible to
:class:`~trellis.retrieve.strategies.SemanticSearch` until something mirrors
it across. That is the root cause of both #337 (a prune archived 35 documents
and every one kept a vector row reading ``signal_quality="standard"``) and
#338 (45 noise-tagged documents, not one whose vector row agreed).

Both were fixed. Neither fix removed the way they happened. The guarantee
afterwards was *"every writer calls the right mirror helper"* — and there
were **two** helpers with **disjoint** key sets, so a writer had to pick:
:func:`~trellis.core.vector_metadata.sync_vector_metadata` for the classify
keys, ``_sync_vector_lifecycle`` for the lifecycle stamp. A guarantee held by
each caller remembering which of two helpers to call is a convention, and
ledger **T-3**'s panel chose option B precisely because *"a guarantee held by
ten callers remembering to mirror is a convention, not a guarantee."*

This module is the other answer to that sentence — the one T-3's own reopen
clause names:

    *Reopen if … a store-layer transactional mirror lands that makes a direct
    write structurally incapable of forgetting the vector row — ``kimi-k3``
    named that as the alternative that would change its answer, and it would
    make B redundant rather than wrong.*

:func:`put_document` is that mirror. The bag it mirrors **is the bag it
wrote** — the same object, not one the caller selects afterwards — so there
is no partial-bag hazard and no call to forget. Forgetting the mirror now
requires not calling the seam at all, which is a single, greppable,
AST-checkable fact rather than a per-key judgement; that check is
``tests/unit/core/test_document_write_rule.py``.

**There are two mirror mechanisms, and this is only one of them.** A
*metadata-only* write (``preserve_updated_at=True``) mirrors by key: the
vector row keeps its embedding and gains the new bag. A *content* write
re-embeds, and ``run_embed_on_ingest`` / ``build_vector_row`` rebuild the
whole row from that same bag — a superset of what this does, and the only
correct answer when the text changed, because the embedding is stale too.
So a content writer still calls the embed **after** the seam, and the two
are not alternatives: the seam cannot refresh an embedding and the embed
cannot run on a deployment that has it switched off.

That second clause is why the rule here is a **routing** rule and not the
weaker *pairing* rule this module was first written with ("a ``put`` must be
accompanied, in its own function, by a mirror **or** an embed"). Two
measurements killed the pairing form:

* ``run_embed_on_ingest`` is gated on ``TRELLIS_ENABLE_EMBED_ON_INGEST``
  (:mod:`trellis.core.write_config`), and that flag genuinely differs per
  surface on the reference deployment. A ``put`` paired only with an embed
  therefore mirrors **nothing at all** wherever the flag is off. A mirror
  conditional on a runtime flag is not a guarantee, which is the whole
  sentence T-3 was written against.
* Routing a content write through here is close to free.
  :func:`~trellis.core.vector_metadata.sync_vector_metadata_outcome`
  short-circuits: when the mirrored keys on the row already agree with the
  bag it returns ``"unchanged"`` **without an upsert**. The cost of the
  redundant-upsert worry — one extra upsert per chunk on the corpus-sync hot
  path — is really one extra ``vector_store.get``, and on the re-embed branch
  the row is rewritten wholesale a line later regardless.

So **every** document-store write goes through here, content and
metadata-only alike, and the invariant is a single greppable fact rather than
a per-call-site judgement: no ``document_store.put(...)`` anywhere in ``src/``
outside this module and ``stores/``. ``ingest_corpus.sync._write_chunks`` is
the reference shape for the content case — one :func:`put_document`, then the
embed on the branch whose text changed.

What this is **not**: it is not the governed pipeline. A metadata-only tag
write does not become a :class:`~trellis.mutate.command.Command` here, gets
no policy check and emits no event. See T-3's amendment for what routing the
rest of option B would buy, and #360 for the half that is still open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.vector_metadata import (
    MIRRORED_METADATA_KEYS,
    VectorSyncOutcome,
    sync_vector_metadata_outcome,
)

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DocumentWriteResult:
    """What one :func:`put_document` call did to each plane."""

    #: The row written. Always the ``doc_id`` passed in — returned so a
    #: caller aggregating a batch does not have to carry it alongside.
    doc_id: str
    #: What the mirror did. ``"absent"`` is the common, correct case for a
    #: document that was never embedded; ``"failed"`` means the document row
    #: landed and the vector row did not, which is the divergence #337/#338
    #: are about and is why this is reported rather than swallowed.
    mirror: VectorSyncOutcome


def put_document(
    document_store: DocumentStore,
    vector_store: VectorStore | None,
    doc_id: str,
    content: str,
    metadata: dict[str, Any],
    *,
    preserve_updated_at: bool = False,
) -> DocumentWriteResult:
    """Write a document row and mirror it onto the vector row.

    The document store is **authoritative** and is written first. The mirror
    is fail-soft for the reason it has always been: by the time it runs the
    caller's write has already landed, so failing the call to report a
    mirror failure would lose the write, while failing silently would hide
    the divergence. It therefore fails soft and *loud*, and the outcome
    rides back on :class:`DocumentWriteResult` so a caller that reports
    coverage can distinguish "never embedded" from "mirror threw".

    Args:
        document_store: The authority. Written first, unconditionally.
        vector_store: The mirror, or ``None`` on a deployment with no vector
            store configured — in which case the mirror is a no-op and the
            result reads ``"no_store"``. Use
            :func:`~trellis.core.vector_metadata.resolve_vector_store` to
            get one from a registry without raising on a broken config.
        doc_id: Row id. Also the vector row's ``item_id`` —
            ``build_vector_row`` writes the two 1:1.
        content: The row's content. Pass the row's **own** content on a
            metadata-only write; there is no metadata-only overload,
            because one would need a read the caller has already done.
        metadata: The complete metadata bag being written. Complete is the
            precondition that makes the mirror safe: mirroring is
            bidirectional (a key absent here is *removed* from the vector
            row), so a partial bag would strip keys it never meant to
            touch. Passing the same object to both planes is what makes
            that precondition structural instead of documented.
        preserve_updated_at: Forwarded to the document store. ``True`` on a
            metadata-only write, so a tag refresh does not re-stamp the row
            to the sweep's own clock and hand ``KeywordSearch``'s recency
            decay a false age (#406).

    Returns:
        A :class:`DocumentWriteResult`. Never raises for a mirror failure;
        a *document*-store failure propagates, because that write is the
        authoritative one and a caller must not read a lost write as done.
    """
    document_store.put(
        doc_id, content, metadata, preserve_updated_at=preserve_updated_at
    )
    outcome = sync_vector_metadata_outcome(
        vector_store, doc_id, metadata, keys=MIRRORED_METADATA_KEYS
    )
    if outcome == "failed":
        logger.warning(
            "document_write_mirror_failed",
            doc_id=doc_id,
            consequence=(
                "document row written; vector row still carries pre-write "
                "metadata, so the semantic axis will serve the stale snapshot "
                "until `trellis admin resync-vector-metadata` runs"
            ),
        )
    return DocumentWriteResult(doc_id=doc_id, mirror=outcome)


__all__ = ["DocumentWriteResult", "put_document"]
