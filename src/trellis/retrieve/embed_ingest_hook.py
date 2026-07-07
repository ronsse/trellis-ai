"""Shared post-ingest document→vector embedding hook.

The REST API (``POST /api/v1/documents``, ``POST /api/v1/evidence``) and
MCP (``save_memory``) document-ingest paths all want the *same* opt-in
behaviour: once a document is durably stored, embed its content and
upsert the vector so :class:`~trellis.retrieve.strategies.SemanticSearch`
can retrieve it. Factoring it here keeps the call sites from
triplicating the flag check, availability check, and fail-soft handling
— the same way ``run_trace_extraction`` is shared.

Contract (mirrors the trace-extraction hook):

* Gated by ``TRELLIS_ENABLE_EMBED_ON_INGEST`` — off by default, so an
  existing deployment sees byte-identical behaviour.
* Requires both ``registry.embedding_fn`` and a configured vector store;
  when either is missing the hook logs a warning and no-ops rather than
  failing the ingest.
* Runs **after** the document is durably stored. It only ever *reads*
  the document content; the vector row is keyed by the document's
  ``doc_id`` so the two stores stay 1:1.
* Fully best-effort: any failure is logged and swallowed. A broken or
  unreachable embedder must NEVER fail the ingest.

The vector row's metadata carries a ``content`` excerpt because
``SemanticSearch`` renders ``PackItem.excerpt`` from vector metadata —
it does not fetch the document row. Document metadata is passed through
so importance/recency weighting sees the same tags the document store
holds. Metadata-only re-puts (enrichment tag writes) do NOT re-embed;
the vector's metadata copy refreshes on the next content write or
``trellis admin reindex-vectors --force`` run.

``run_embed_on_ingest`` returns a small summary dict so callers that
want to surface embedding telemetry can, without re-deriving it. When
the flag is off the hook returns ``None`` and does nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Truthy spellings that turn the post-ingest embedding stage on.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Feature flag — off by default.
EMBED_ON_INGEST_FLAG = "TRELLIS_ENABLE_EMBED_ON_INGEST"

#: Cap on the characters sent to the embedder. Embedding models have
#: finite context windows (≈8k tokens for common models); 8000 chars
#: (≈2k tokens) keeps every provider comfortably inside its window while
#: covering far more content than a pack excerpt ever renders.
EMBED_INPUT_CHAR_CAP = 8000

#: Content excerpt stored in vector metadata. ``SemanticSearch`` renders
#: at most 500 chars into ``PackItem.excerpt``; storing more duplicates
#: the document store to no benefit.
VECTOR_METADATA_EXCERPT_CHARS = 500


def embed_on_ingest_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_EMBED_ON_INGEST`` is set truthy."""
    import os  # noqa: PLC0415

    return os.environ.get(EMBED_ON_INGEST_FLAG, "").strip().lower() in _TRUTHY


def build_vector_row(
    doc_id: str,
    content: str,
    metadata: dict[str, Any] | None,
    embedding_fn: Callable[[str], list[float]],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Embed one document into a vector-store row.

    The single shared core of document→vector embedding — the live
    ingest hook and the ``trellis admin reindex-vectors`` backfill both
    call this, so the metadata shape (``content`` excerpt, ``doc_id``
    key, recency stamp) cannot drift between the two paths.

    Args:
        doc_id: Document ID; becomes the vector ``item_id`` (1:1).
        content: Full document content. Input to the embedder is capped
            at :data:`EMBED_INPUT_CHAR_CAP` chars.
        metadata: Document metadata, passed through so retrieval-side
            importance/tag weighting sees it.
        embedding_fn: ``callable(str) -> list[float]``.
        created_at: Recency stamp for retrieval decay. The live hook
            omits it (embed time == ingest time); the backfill passes
            the document row's stored ``created_at`` so old documents
            don't masquerade as fresh.

    Returns:
        ``{"item_id": ..., "vector": ..., "metadata": ...}`` — the shape
        :meth:`VectorStore.upsert_bulk` accepts per row.
    """
    vector = embedding_fn(content[:EMBED_INPUT_CHAR_CAP])
    row_metadata: dict[str, Any] = {
        **(metadata or {}),
        "doc_id": doc_id,
        "content": content[:VECTOR_METADATA_EXCERPT_CHARS],
    }
    row_metadata.setdefault("created_at", created_at or datetime.now(UTC).isoformat())
    return {"item_id": doc_id, "vector": vector, "metadata": row_metadata}


def run_embed_on_ingest(
    registry: StoreRegistry,
    doc_id: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    *,
    source: str,
) -> dict[str, Any] | None:
    """Post-ingest hook: embed a stored document into the vector store.

    Args:
        registry: The active :class:`StoreRegistry`.
        doc_id: ID of the document that was **already** durably stored.
        content: The stored content. Read-only.
        metadata: The stored metadata. Read-only.
        source: Audit identifier for logging
            (e.g. ``"api:create-document"``, ``"mcp:save_memory"``).

    Returns:
        ``None`` when the feature flag is off. Otherwise a summary dict:
        ``{"embedded": True, "dimensions": int}`` on success, or
        ``{"embedded": False, "reason": "..."}`` when skipped (empty
        content, embedder/vector store unconfigured) or failed. Any
        failure is caught and logged — it never propagates.
    """
    if not embed_on_ingest_enabled():
        return None

    if not content or not content.strip():
        return {"embedded": False, "reason": "empty_content"}

    try:
        embedding_fn = registry.embedding_fn
    except Exception as exc:
        # A misconfigured embedder (bad TRELLIS_EMBEDDING_FN path, missing
        # provider extra) raises at resolve time — same fail-soft contract
        # as an embed failure: log it, never fail the ingest.
        logger.exception("embed_on_ingest_embedder_resolve_failed", doc_id=doc_id)
        return {"embedded": False, "reason": str(exc)}
    vector_store = getattr(registry.knowledge, "vector_store", None)
    if embedding_fn is None or vector_store is None:
        logger.warning(
            "embed_on_ingest_unavailable",
            doc_id=doc_id,
            source=source,
            has_embedding_fn=embedding_fn is not None,
            has_vector_store=vector_store is not None,
        )
        return {"embedded": False, "reason": "embedder_or_vector_store_unconfigured"}

    try:
        row = build_vector_row(doc_id, content, metadata, embedding_fn)
        vector_store.upsert(
            item_id=row["item_id"],
            vector=row["vector"],
            metadata=row["metadata"],
        )
    except Exception as exc:
        # GRACEFUL-DEGRADATION: document ingest's success contract is
        # "the document is durably stored". Embedding is a feature-
        # flagged bonus pass; its failure (embedder down, dimension
        # mismatch) must never roll back a successful document write.
        # Logged at exception level so persistent breakage is visible.
        logger.exception("embed_on_ingest_failed", doc_id=doc_id, source=source)
        return {"embedded": False, "reason": str(exc)}

    logger.info(
        "embed_on_ingest_completed",
        doc_id=doc_id,
        source=source,
        dimensions=len(row["vector"]),
    )
    return {"embedded": True, "dimensions": len(row["vector"])}
