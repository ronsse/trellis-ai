"""Strict evidence ingestion for rendered trace summaries.

The storage body is shared with core ``EvidenceIngestHandler``. This worker
selects strict embedding and deliberately remains keyless so a document whose
vector row is missing can be repaired.

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

**A summary the egress gate declines is stored, without a vector.** Egress
governs what is sent to the embedder, not what is stored. A summary is judged,
after the document is written, by its own label and by the documents its trace
cites (:meth:`~trellis.llm.egress.EgressGate.summary_verdict`); when that says no
the embedder is never called, ``embed.withheld`` is recorded, and the command
succeeds with the document in place, so keyword and graph search can find it and
only the semantic axis cannot. ``last_embed_withheld`` carries the verdict for
the worker to count: ``WITHHELD`` (a label) advances its cursor, ``UNREADABLE``
(a store could not be asked) holds it, and the next pass writes the same
document again and tries the embed again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.llm.egress import EgressGate, EgressVerdict
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import EvidenceIngestHandler
from trellis.ops.embed_egress_health import UNREADABLE_REEMIT_AFTER
from trellis.retrieve.embed_ingest_hook import record_embed_withheld

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.base.trace import TraceStore
    from trellis.stores.registry import StoreRegistry

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
                "preserve_updated_at": True,
                "embed_mode": "strict",
            }
        },
    )


class TraceSummaryIngestHandler(EvidenceIngestHandler):
    """Core evidence ingestion specialized to strict trace-summary embedding."""

    #: The one writer the ``trace-summary:`` refusal exempts (#1054). Structural
    #: on purpose: ``requested_by`` is a string a caller can write into a command.
    reserved_doc_id_prefixes: tuple[str, ...] = ()

    def __init__(
        self,
        registry: StoreRegistry,
        embedding_fn: Callable[[str], list[float]],
    ) -> None:
        super().__init__(
            registry,
            embed="strict",
            embedding_fn=embedding_fn,
            empty_error_code="trace_summary_empty",
        )

    def _embed_strict(
        self,
        doc_id: str,
        content: str,
        metadata: dict[str, Any],
        evidence: dict[str, Any],
        requested_by: str,
    ) -> bool:
        """Embed the stored summary unless the egress gate declines it.

        Runs after the summary document is written, so a declined summary is
        stored and findable by keyword and graph search; only its vector is
        missing. ``True`` for a decline, with the verdict on
        :attr:`last_embed_withheld`; anything else is the core strict embed.
        """
        egress = EgressGate.for_embedder(
            self._registry, self._registry.knowledge.document_store
        )

        def open_trace_store() -> TraceStore:
            return self._registry.operational.trace_store

        verdict = egress.summary_verdict(doc_id, metadata, open_trace_store)
        if verdict is EgressVerdict.PERMITTED:
            return super()._embed_strict(
                doc_id, content, metadata, evidence, requested_by
            )
        self.last_embed_withheld = verdict
        # An unreadable summary is met again on every pass while the cursor is
        # held, and counted once a day rather than once per pass. A withheld one
        # advances the cursor, so it is met once.
        record_embed_withheld(
            self._registry,
            doc_id,
            verdict,
            source=requested_by,
            once_within=(
                UNREADABLE_REEMIT_AFTER if verdict is EgressVerdict.UNREADABLE else None
            ),
        )
        return True
