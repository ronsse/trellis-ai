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

**A summary inherits the strictest label of the documents it cites.** An
unlabelled document reads ``internal``, so a summary stored with no label would
serve the digest of a ``confidential`` document to a caller cleared only to
``internal``. The handler stamps the label in before the write
(:meth:`TraceSummaryIngestHandler._label_for_write`), from the same read of the
citations that decides the embed.

**Only the sensitivity is inherited, not regulatory tags.** ``regulatory_tags``
(``phi``, ``pii`` ...) are written by ``classification.tag_regulatory`` and read
by the handlers and the label proposer; no retrieval, egress or auth gate reads
them, so inheriting them would change nothing a caller can observe. If a gate ever
reads them, inherit them here as a union (never lower, never drop a tag a person
set), next to the sensitivity.

**A relabelled citation is picked up by the restamp sweep**
(:mod:`trellis_workers.trace_embed.restamp`), because a summary that already has a
vector is never rewritten by the pass above. It uses this handler in
``embed="none"`` mode.

**A restamp write never fails closed to ``restricted``; it aborts.** The pass may
stamp ``restricted`` for a citation it cannot read, because its next attempt
re-judges and replaces that derived stamp. A restamp only raises and never lowers,
so a ``restricted`` written because a citation became unreadable between the
sweep's read and this write would stay for good, although the sweep justified only
the label it read. In ``embed="none"`` mode the write is refused instead
(:data:`CITATIONS_UNREADABLE_CODE`): the summary keeps its label, the sweep counts
it unreadable, and the next sweep retries. A citation that is *raised* in between is
not refused: the later read is the newer one, and a stricter label it justifies
stands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from trellis.errors import ValidationError
from trellis.llm.egress import EgressGate, EgressVerdict, SummaryJudgement
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import EvidenceIngestHandler
from trellis.ops.embed_egress_health import UNREADABLE_REEMIT_AFTER
from trellis.retrieve.embed_ingest_hook import record_embed_withheld
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    DataClassification,
    sensitivity_of,
    sensitivity_rank,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.mutate.evidence_ingest import EvidenceEmbedMode
    from trellis.stores.base.trace import TraceStore
    from trellis.stores.registry import StoreRegistry

__all__ = [
    "CITATIONS_UNREADABLE_CODE",
    "TraceSummaryIngestHandler",
    "build_trace_summary_command",
    "judgement_unreadable",
]

#: ``Command.requested_by`` for every write this worker makes — the
#: ``<surface>:<verb>`` convention from :class:`Command`.
REQUESTED_BY = "worker:embed-traces"

#: The ``classified_by`` of the label a summary inherits from the documents it
#: cites. It marks the label as derived, so a rewrite may replace it
#: (:meth:`TraceSummaryIngestHandler._enforce_clearance`); a label anyone else set
#: carries another name and is never replaced.
CITED_LABEL_CLASSIFIER = "trace-summary:cited"

#: The ``ValidationError.code`` of a restamp write refused because the trace or a
#: citation could not be read. It is the ``MUTATION_REJECTED`` reason and the
#: result's ``rejection_reason``, which is how the sweep tells this refusal from a
#: failed write.
CITATIONS_UNREADABLE_CODE = "trace_summary_citations_unreadable"


def judgement_unreadable(judgement: SummaryJudgement) -> bool:
    """Whether the trace or a citation of a summary could not be read.

    Not the verdict alone: a citation that withholds the summary hides an
    unreadable one there, and the unreadable one's ``restricted`` is then in
    ``inherited``. One predicate, so the sweep and the write cannot differ.
    """
    return judgement.unreadable or judgement.verdict is EgressVerdict.UNREADABLE


def _is_cited_stamp(metadata: object) -> bool:
    """Whether ``metadata`` carries the label this handler derived from citations."""
    if not isinstance(metadata, Mapping):
        return False
    record = metadata.get(DATA_CLASSIFICATION_KEY)
    if not isinstance(record, Mapping):
        return False
    classified_by = record.get("classified_by")
    return classified_by == [CITED_LABEL_CLASSIFIER]


def build_trace_summary_command(
    *,
    doc_id: str,
    trace_id: str,
    content: str,
    metadata: dict[str, Any],
    created_at: str | None = None,
    embed_mode: EvidenceEmbedMode = "strict",
) -> Command:
    """The ``evidence.ingest`` command for one rendered trace summary.

    ``embed_mode`` must equal the executing handler's mode (the core handler
    refuses a mismatch): ``"strict"`` for the pass, ``"none"`` for a restamp.
    """
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
                "embed_mode": embed_mode,
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
        embedding_fn: Callable[[str], list[float]] | None = None,
        *,
        embed: EvidenceEmbedMode = "strict",
    ) -> None:
        super().__init__(
            registry,
            embed=embed,
            embedding_fn=embedding_fn,
            empty_error_code="trace_summary_empty",
        )
        #: What :meth:`_label_for_write` found out about the command in hand,
        #: for :meth:`_embed_strict`; set at the start of each command's write.
        #: Fail closed until then: ``handle`` runs ``_label_for_write`` before
        #: it can reach ``_embed_strict`` (proved by a mutant that raised in the
        #: old fallback branch while 172 trace-embed tests passed), so this value
        #: is never read, and were it ever read it withholds rather than embeds.
        self._judgement: SummaryJudgement = SummaryJudgement(
            verdict=EgressVerdict.UNREADABLE, inherited="restricted"
        )

    def _judge(self, doc_id: str, metadata: dict[str, Any]) -> SummaryJudgement:
        """The summary's verdict and inherited label, from one read of its citations."""
        egress = EgressGate.for_embedder(
            self._registry, self._registry.knowledge.document_store
        )

        def open_trace_store() -> TraceStore:
            return self._registry.operational.trace_store

        return egress.judge_summary(doc_id, metadata, open_trace_store)

    def _label_for_write(self, doc_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Stamp the summary with the strictest label among the documents it cites.

        A summary is rendered from a trace that cites documents, and an
        unlabelled document reads ``internal``, so without this a caller cleared
        only to ``internal`` is served the digest of a ``confidential`` one. The
        label is derived from this write's own read of the citations
        (:meth:`_judge`, which :meth:`_embed_strict` reuses), never from the
        stored row, so a rewrite replaces it: a relabel up or down, or a citation
        that stopped being unreadable, leaves the label the citations now give.
        Nothing is added when no citation is labelled above ``internal``.

        A restamp write (``embed="none"``) refuses instead of stamping the
        ``restricted`` an unreadable citation gives: see the module docstring.
        The first write and the pass keep failing closed.
        """
        self._judgement = self._judge(doc_id, metadata)
        if self._embed == "none" and judgement_unreadable(self._judgement):
            msg = (
                "a cited document of this trace summary could not be read; "
                "its label is left as it is and the next restamp sweep retries"
            )
            raise ValidationError(msg, code=CITATIONS_UNREADABLE_CODE)
        inherited = self._judgement.inherited
        if inherited is None or sensitivity_rank(inherited) <= sensitivity_rank(
            sensitivity_of(metadata)
        ):
            return metadata
        record = DataClassification(
            sensitivity=inherited, classified_by=[CITED_LABEL_CLASSIFIER]
        )
        return {**metadata, DATA_CLASSIFICATION_KEY: record.model_dump(mode="json")}

    def _enforce_clearance(self, doc_id: str, metadata: dict[str, Any]) -> None:
        """The core write rules, except for replacing this handler's own stamp.

        The overwrite rule refuses a write that lowers the stored label. The
        label :meth:`_label_for_write` stamped last time is derived, and this
        write derived its own from the citations as they are now, so a stamp
        that no longer applies must be able to go. A label anyone else set is
        still refused as before; this handler's clearance is ``restricted``, so
        no label it writes is above it.
        """
        stored = self._registry.knowledge.document_store.get(doc_id)
        if stored is not None and _is_cited_stamp(stored.get("metadata")):
            return
        super()._enforce_clearance(doc_id, metadata)

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

        The verdict is the one :meth:`_label_for_write` read before the write,
        not a fresh one over the stamped row: a stamp reads as the summary's own
        label, which would withhold it, and so would turn a citation that could
        not be read into a decline that advances the cursor and is never retried.
        """
        verdict = self._judgement.verdict
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
