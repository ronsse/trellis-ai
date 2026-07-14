"""Evidence-document creation — the doc-first half of pointer-not-prose writes.

Graph nodes carry **pointers, never prose**: an entity node points at an
embedded evidence *document* (via ``evidence_ref`` / ``document_ids``) that
holds the words, and the graph stays an index of identities and relations
(see ``docs/design/plan-memory-lifecycle.md`` §3). This module owns the
document-creation seam that the graph write then points at.

Two invariants live here:

* **Doc-first ordering.** The evidence document must be created *before* the
  graph mutation that carries its pointer. There is no cross-store
  transaction between the document store and the graph store, so on partial
  failure an orphaned document (findable, prunable) is acceptable while a
  graph node pointing at a nonexistent document is never acceptable. Callers
  MUST call :func:`ensure_evidence_document` and only then submit the graph
  mutation with the returned id.

* **Idempotency.** The document identity is derived from the content hash, so
  a retried save resolves to the existing document instead of double-creating.
  This is the seam issue #263 (reconcile-on-write) plugs into.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


def ensure_evidence_document(
    registry: StoreRegistry,
    content: str,
    *,
    metadata: dict[str, Any] | None = None,
    source: str = "mcp:save_knowledge",
) -> str:
    """Create (or dedup to) the evidence document for a graph node.

    Deduplicates by content hash: an identical document already in the store
    resolves to its existing id rather than being stored again, so a retried
    command finds the same document (idempotent). New documents are embedded
    via the standard feature-flagged ingest hook so semantic retrieval can
    reach the prose — embedding failures are fail-soft and never roll back the
    document write.

    This is intentionally a standalone, reusable function (not inline in a
    surface tool): it is the doc-creation seam described in the module
    docstring, and it must run and return before any graph write that will
    carry the returned id as a pointer.

    Args:
        registry: The active :class:`StoreRegistry`.
        content: The evidence prose to store. Must be non-empty.
        metadata: Optional document metadata (tags, source, domain, ...).
        source: Audit identifier for the embed hook / logs.

    Returns:
        The evidence document's id — new, or the existing id on a content-hash
        hit.

    Raises:
        ValueError: If ``content`` is empty or whitespace-only.
    """
    if not content or not content.strip():
        msg = "content must not be empty"
        raise ValueError(msg)

    # Lazy imports keep the mutate package free of a retrieve/core import at
    # module load time (matches the codebase's PLC0415 convention).
    from trellis.core.hashing import content_hash  # noqa: PLC0415
    from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest  # noqa: PLC0415

    meta = metadata or {}
    chash = content_hash(content)

    existing = registry.knowledge.document_store.get_by_hash(chash)
    if existing is not None:
        existing_id: str = existing["doc_id"]
        logger.debug(
            "evidence_document_dedup", doc_id=existing_id, content_hash=chash
        )
        return existing_id

    doc_id = registry.knowledge.document_store.put(None, content, metadata=meta)

    # Feature-flagged embed-on-ingest (TRELLIS_ENABLE_EMBED_ON_INGEST=1) so the
    # evidence prose becomes a similarity surface. Fail-soft inside the hook.
    run_embed_on_ingest(registry, doc_id, content, meta, source=source)

    logger.info("evidence_document_created", doc_id=doc_id, content_hash=chash)
    return doc_id
