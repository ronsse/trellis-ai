"""The governed write: one command per trace, through ``MutationExecutor``.

Every write here goes through the five-stage pipeline (validate → policy →
idempotency → execute → emit event) under :data:`Operation.EVIDENCE_INGEST` —
an operation the :class:`~trellis.mutate.commands.OperationRegistry` has
always validated (required args ``{"evidence"}``) but that has never had a
handler registered. This is that handler.

It is registered on the worker's own executor rather than added to
``create_curate_handlers``. The handler set is a Protocol-based injection
point by design, and scoping it here keeps ``evidence.ingest`` from silently
acquiring semantics on every surface (MCP ``execute_mutation`` included) as a
side effect of adding a worker.

Two contracts differ from the ingest-time hooks, on purpose
--------------------------------------------------------

**The embed is not fail-soft.** ``run_embed_on_ingest`` swallows every
embedding failure because a document ingest's success contract is "the
document is durably stored" and a broken embedder must not fail a user's
write. This worker's success contract is the opposite — the *vector row* is
the entire point — so an embed failure raises, the command comes back
``FAILED``, and the watermark stays pinned behind that trace. Fail-soft here
would produce precisely the green-looking no-op this item exists to fix.

**No idempotency key is set,** and that is the load-bearing decision. The
executor's Stage-3 key check is satisfied by any ``MUTATION_EXECUTED`` event
carrying the key, so keying on the trace would make a second attempt a
permanent ``DUPLICATE`` — a trace whose document landed but whose vector row
did not (embedder outage, row later deleted, backend restored from an older
snapshot) could then never be repaired, and the summary would call it deduped
rather than missing. The worker's idempotency is *state-based* instead:
:func:`~trellis_workers.trace_embed.worker.trace_is_embedded` asks the vector
store, before the command is ever built, whether the row exists. That is
strictly stronger than a key — it cannot say "done" about a row that is not
there — and it matches the surrounding call sites (MCP ``save_experience``
submits ``trace.ingest`` with no key either). ``document_store.put`` with an
explicit id upserts, so a re-run over a half-written trace repairs it rather
than duplicating it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.errors import StoreError, ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.retrieve.embed_ingest_hook import build_vector_row

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

__all__ = ["TraceSummaryIngestHandler", "build_trace_summary_command"]

#: ``Command.requested_by`` for every write this worker makes — the
#: ``<surface>:<verb>`` convention from :class:`Command`.
REQUESTED_BY = "worker:embed-traces"


def build_trace_summary_command(
    *,
    doc_id: str,
    trace_id: str,
    content: str,
    metadata: dict[str, Any],
    created_at: str | None = None,
) -> Command:
    """The ``evidence.ingest`` command for one rendered trace summary."""
    return Command(
        operation=Operation.EVIDENCE_INGEST,
        target_id=doc_id,
        target_type="document",
        requested_by=REQUESTED_BY,
        args={
            "evidence": {
                "doc_id": doc_id,
                "trace_id": trace_id,
                "content": content,
                "metadata": metadata,
                "created_at": created_at,
            }
        },
    )


class TraceSummaryIngestHandler:
    """Store a rendered trace summary and embed it. Doc-first, then vector."""

    def __init__(
        self,
        registry: StoreRegistry,
        embedding_fn: Callable[[str], list[float]],
    ) -> None:
        self._registry = registry
        self._embedding_fn = embedding_fn

    def handle(self, command: Command) -> tuple[str | None, str]:
        evidence = command.args.get("evidence")
        if not isinstance(evidence, dict):
            msg = "evidence must be a mapping"
            raise ValidationError(msg, code="trace_summary_shape")

        doc_id = evidence.get("doc_id")
        content = evidence.get("content")
        if not isinstance(doc_id, str) or not doc_id:
            msg = "evidence.doc_id must be a non-empty string"
            raise ValidationError(msg, code="trace_summary_shape")
        if not isinstance(content, str) or not content.strip():
            msg = f"rendered trace summary is empty for {doc_id}"
            raise ValidationError(msg, code="trace_summary_empty")

        metadata: dict[str, Any] = dict(evidence.get("metadata") or {})
        raw_created_at = evidence.get("created_at")
        created_at = raw_created_at if isinstance(raw_created_at, str) else None

        # Classify-on-write, same seam and same flag as every other document
        # write path (``ensure_evidence_document`` / ``sync_records``). These
        # rows are a real retrieval surface, so they want the same tags —
        # including the ``signal_quality`` facet the noise boundary reads.
        # Fail-soft inside the helper; it returns the caller's mapping on any
        # failure and never raises into a write path.
        from trellis.classify.ingest import classify_metadata_on_write  # noqa: PLC0415

        metadata = classify_metadata_on_write(
            metadata,
            content,
            source_system=str(metadata.get("source_system") or ""),
            doc_id=doc_id,
        )

        # Doc-first, exactly as ``mutate/evidence.py`` requires: there is no
        # cross-store transaction, and an orphaned document (findable,
        # prunable, and overwritten by the next pass) is the acceptable half of
        # a partial failure. A vector row with no document behind it is not —
        # ``reindex-vectors`` and ``resync-vector-metadata`` both page the
        # document store, so such a row would be invisible to every repair
        # tool the project has.
        # ``preserve_updated_at``: the body is derived from an immutable trace,
        # so a re-put is always a repair and never an edit. Bumping the stamp
        # would silently re-rank the row on the keyword axis, whose recency
        # decay reads ``updated_at`` first.
        #
        # KNOWN LIMIT, and it only cuts one way: ``DocumentStore.put`` has no
        # way to backdate an *insert*, so a backfilled document row carries
        # today's ``created_at`` and looks fresh to ``KeywordSearch``. The
        # semantic axis — the one this worker exists for — is unaffected,
        # because ``build_vector_row`` takes the trace's own stamp below.
        self._registry.knowledge.document_store.put(
            doc_id, content, metadata=metadata, preserve_updated_at=True
        )

        vector_store = getattr(self._registry.knowledge, "vector_store", None)
        if vector_store is None:  # pragma: no cover - guarded by the driver
            msg = "no vector store configured"
            raise StoreError(msg, store="vector")

        row = build_vector_row(
            doc_id,
            content,
            metadata,
            self._embedding_fn,
            created_at=created_at,
        )
        vector_store.upsert(
            item_id=row["item_id"],
            vector=row["vector"],
            metadata=row["metadata"],
        )

        logger.info(
            "trace_summary_embedded",
            doc_id=doc_id,
            trace_id=evidence.get("trace_id"),
            dimensions=len(row["vector"]),
        )
        return doc_id, (
            f"Embedded trace summary {doc_id} ({len(row['vector'])} dimensions)"
        )
