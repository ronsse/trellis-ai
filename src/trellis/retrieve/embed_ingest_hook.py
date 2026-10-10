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
it does not fetch the document row. That excerpt is cut *here*, by
:func:`~trellis.retrieve.excerpts.truncate_excerpt`, rather than at
retrieval time: this is the only point on the semantic path that still
holds the full document, so it is the only point where the cut can be
boundary-aware and can say how much it dropped (#310). Document metadata
is passed through so importance/recency weighting sees the same tags the
document store holds — but that copy is a **snapshot taken here**, and a
metadata-only re-put to the document store does not re-embed. Post-embed
writers therefore have to mirror their change across explicitly, via
:func:`trellis.core.vector_metadata.sync_vector_metadata` (#338); rows that
diverged before those writers existed are repaired by ``trellis admin
resync-vector-metadata``, which needs no embedder.

``run_embed_on_ingest`` returns a small summary dict so callers that
want to surface embedding telemetry can, without re-deriving it. When
the flag is off the hook returns ``None`` and does nothing.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.write_config import EMBED_ON_INGEST_FLAG, WriteBehaviourConfig
from trellis.errors import ConfigError
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.classification import SHADOW_TAGS_KEY

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

__all__ = [
    "EMBED_INPUT_CHAR_CAP",
    "EMBED_ON_INGEST_FLAG",
    "VECTOR_METADATA_EXCERPT_CHARS",
    "build_vector_row",
    "embed_on_ingest_enabled",
    "run_embed_on_ingest",
]

# ``EMBED_ON_INGEST_FLAG`` is re-exported from
# :mod:`trellis.core.write_config`, which owns the name and the parsing for
# every write-behaviour knob. Off by default.

#: Cap on the characters sent to the embedder. Embedding models have
#: finite context windows (≈8k tokens for common models); 8000 chars
#: (≈2k tokens) keeps every provider comfortably inside its window while
#: covering far more content than a pack excerpt ever renders.
EMBED_INPUT_CHAR_CAP = 8000

#: Cap on the content excerpt stored in vector metadata. Matches
#: :data:`~trellis.retrieve.excerpts.EXCERPT_MAX_CHARS`, which is what
#: ``SemanticSearch`` renders into ``PackItem.excerpt``; storing more
#: duplicates the document store to no benefit. The stored string is the
#: *truncator's* output, so it is already ``<=`` this many characters —
#: a boundary-aware, size-marked excerpt rather than a raw slice.
VECTOR_METADATA_EXCERPT_CHARS = 500


def embed_on_ingest_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_EMBED_ON_INGEST`` is set truthy."""
    return WriteBehaviourConfig.from_env().embed_on_ingest


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
            at :data:`EMBED_INPUT_CHAR_CAP` chars; the stored ``content``
            excerpt is truncated to
            :data:`VECTOR_METADATA_EXCERPT_CHARS` on a clean boundary.
        metadata: Document metadata, passed through so retrieval-side
            importance/tag weighting sees it.
        embedding_fn: ``callable(str) -> list[float]``.
        created_at: Recency stamp for retrieval decay. The live hook
            omits it (embed time == ingest time); the backfill passes
            the document row's stored ``created_at`` so old documents
            don't masquerade as fresh. **A ``created_at`` (or
            ``updated_at``) already in *metadata* outranks this argument**
            — it is written into ``setdefault`` position deliberately,
            because a stamp in the bag is the *source's* clock and this
            argument is only ever the row's write clock. That precedence
            is now the explicit rule both document-backed retrieval axes
            follow; see
            :func:`~trellis.retrieve.strategies.resolve_recency_stamp`
            (#417).

    Returns:
        ``{"item_id": ..., "vector": ..., "metadata": ...}`` — the shape
        :meth:`VectorStore.upsert_bulk` accepts per row.
    """
    vector = embedding_fn(content[:EMBED_INPUT_CHAR_CAP])
    row_metadata: dict[str, Any] = {
        # Shadow tags are a measurement-only record on the document (#321).
        # Copying them here would duplicate them into a second store whose
        # write path has no shadow awareness and which the promotion analyzer
        # never reads — a copy that can only drift.
        **{k: v for k, v in (metadata or {}).items() if k != SHADOW_TAGS_KEY},
        "doc_id": doc_id,
        "content": truncate_excerpt(content, VECTOR_METADATA_EXCERPT_CHARS),
    }
    row_metadata.setdefault("created_at", created_at or datetime.now(UTC).isoformat())
    return {"item_id": doc_id, "vector": vector, "metadata": row_metadata}


@functools.cache
def _warn_embedder_resolve_failed_once(error_type: str, setting: str | None) -> None:
    """Emit ``embed_on_ingest_embedder_resolve_failed`` once per cause.

    A broken embedder config (missing provider extra, missing API key, a
    malformed ``TRELLIS_EMBEDDING_FN``/``embeddings.provider`` path) fails
    ``registry.embedding_fn`` identically on every ingest until an operator
    fixes the setting, so logging a full traceback per document — the
    prior behaviour — turned one misconfiguration into a traceback per
    write. ``functools.cache`` makes a repeat call with the same arguments
    a no-op: the body (and its ``logger.warning``) runs on the first call
    for a given ``(error_type, setting)``, and every later failure with
    the same cause returns the cached ``None`` without logging again.
    WARNING, not ``.exception`` — the type and setting are what an
    operator acts on; the traceback is noise after the first one.

    ``_warn_embedder_resolve_failed_once.cache_clear()`` is test-facing
    only, to isolate cases within one process — a production process
    does not reset it mid-flight.
    """
    logger.warning(
        "embed_on_ingest_embedder_resolve_failed",
        error_type=error_type,
        setting=setting,
    )


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
        failure is caught and logged — it never propagates. When
        ``registry.embedding_fn`` itself fails to resolve, ``reason``
        carries only the exception's type name and, when the exception
        names one (see :class:`~trellis.errors.ConfigError`'s
        ``setting``), the broken setting — e.g. ``"ConfigError:
        embeddings.provider"`` — never the exception's message text,
        which can echo a credential (this resolve path never sees document
        content — it fails before any document is read). The matching
        warning is logged once per distinct cause per process, not once
        per call; see :func:`_warn_embedder_resolve_failed_once`.
    """
    if not embed_on_ingest_enabled():
        return None

    if not content or not content.strip():
        return {"embedded": False, "reason": "empty_content"}

    try:
        embedding_fn = registry.embedding_fn
    except Exception as exc:
        # A misconfigured embedder (bad TRELLIS_EMBEDDING_FN path, missing
        # provider extra) raises at resolve time, and the same cause fails
        # every later ingest identically — same fail-soft contract as an
        # embed failure (never fail the ingest), but made loud once per
        # cause instead of once per document.
        #
        # Describe, don't quote: `setting` is read from the exception's
        # own `.setting` attribute only when the exception IS a
        # ConfigError, whose `setting` is typed `str | None` (errors.py).
        # A plain `getattr(exc, "setting", None)` would let an unrelated
        # exception's same-named, possibly-unhashable or untrusted
        # attribute reach the cache key and the returned `reason`.
        error_type = type(exc).__name__
        setting = exc.setting if isinstance(exc, ConfigError) else None
        _warn_embedder_resolve_failed_once(error_type, setting)
        reason = f"{error_type}: {setting}" if setting else error_type
        return {"embedded": False, "reason": reason}
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
