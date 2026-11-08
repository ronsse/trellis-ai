"""Raise a stored trace summary to the label its citations now give.

A summary that already has a vector is never rewritten: the pass skips the trace
(``skipped_existing``) before it renders anything, which is also why the label
:meth:`~trellis_workers.trace_embed.handler.TraceSummaryIngestHandler._label_for_write`
stamped is only as current as the pass that wrote it. Label a cited document
``confidential`` later and the summary of it stays ``internal``, and is served to a
caller cleared only to ``internal``.

The sweep closes that, and it is the smallest of the three places the fix could
live. A label-write hook would need the cited-by lookup the trace store does not
have (``evidence_used`` is inside the trace JSON), and it would miss every writer
that does not go through one door. Judging at serve time would put a trace read and
a read per citation on every retrieval. This reads the same inputs the pass reads,
once an interval, from the summaries outward, and so catches a relabel whoever made
it.

**Raise only.** A summary is raised when the citations give a stricter label than it
carries, and never lowered: a label a person set is never replaced, and a lowered
citation leaves a derived stamp where it is until a person relabels the summary
(over-withholding, the safe direction). A person's label below the new one is
raised in place, keeping its other fields, and ``trace-summary:cited`` is appended
to ``classified_by``.

**The vector stays.** A summary that was served before the relabel has a vector row,
and a row cannot be unsent. The write mirrors the new label onto it
(``put_document`` does, for ``data_classification``), which is what withholds it
from a caller below that label; deleting it is the redaction and retention paths'
job, not this sweep's.

**Unreadable is skipped and counted,** not raised: a store that cannot answer must
not turn into a ``restricted`` summary nobody lowers. The next sweep tries again.
That holds for a citation that becomes unreadable *after* the sweep read it, too,
except that the label the sweep judged is already justified, so it is written: the
write re-judges the citations, and in ``embed="none"`` mode an unreadable re-read
keeps the sweep's label instead of stamping ``restricted``
(:data:`~trellis_workers.trace_embed.handler.RESTAMP_UNVERIFIED_WARNING`). The summary
is counted ``raised`` and ``unreadable``, so the sweep is unclean and the next one
re-judges it; a citation raised in between is caught there, because a sweep only
raises. When the re-read is clean, the write's own fresher verdict wins, stricter
or not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.document_write import is_redaction_tombstone
from trellis.llm.egress import EgressGate
from trellis.mutate.commands import CommandStatus
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    DataClassification,
    sensitivity_of,
    sensitivity_rank,
)
from trellis_workers.trace_embed.handler import (
    CITED_LABEL_CLASSIFIER,
    RESTAMP_UNVERIFIED_WARNING,
    build_trace_summary_command,
    judgement_unreadable,
)
from trellis_workers.trace_embed.render import (
    DOC_ID_PREFIX,
    SOURCE_SYSTEM,
    trace_id_from_doc_id,
)

if TYPE_CHECKING:
    from trellis.mutate.executor import MutationExecutor
    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.trace import TraceStore
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

__all__ = ["RestampResult", "restamp_summaries"]

#: Summaries listed per ``list_documents`` round trip.
PAGE_SIZE = 200

#: Kept ahead of ``trace-summary:cited`` when a raised label named no classifier.
UNATTRIBUTED = "unattributed"


@dataclass
class RestampResult:
    """What one sweep did."""

    #: Summaries judged.
    scanned: int = 0
    #: Summaries raised (in a dry run, the ones that would be).
    raised: int = 0
    #: Summaries whose citations could not all be read: left as they were, or,
    #: when only the write's own re-read failed, written with the label this sweep
    #: judged (those are in ``raised`` too).
    unreadable: int = 0
    #: Raises the executor did not accept.
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Whether every summary was judged and every raise landed."""
        return not (self.unreadable or self.failed)


def _raised_metadata(
    metadata: Mapping[str, Any], target: Sensitivity
) -> dict[str, Any]:
    """``metadata`` with its label raised to ``target``, nothing else changed."""
    record = metadata.get(DATA_CLASSIFICATION_KEY)
    if isinstance(record, Mapping) and record.get("classified_by") != [
        CITED_LABEL_CLASSIFIER
    ]:
        by = record.get("classified_by")
        # A record naming no classifier is not this handler's stamp, and must not
        # become exactly one: that list is what lets a later pass lower it.
        classified_by = list(by) if isinstance(by, list) and by else [UNATTRIBUTED]
        if CITED_LABEL_CLASSIFIER not in classified_by:
            classified_by.append(CITED_LABEL_CLASSIFIER)
        raised: dict[str, Any] = {
            **record,
            "sensitivity": target,
            "classified_by": classified_by,
        }
    else:
        raised = DataClassification(
            sensitivity=target, classified_by=[CITED_LABEL_CLASSIFIER]
        ).model_dump(mode="json")
    return {**metadata, DATA_CLASSIFICATION_KEY: raised}


def _summary_ids(registry: StoreRegistry) -> list[str]:
    """Every stored trace summary's id, listed before any is rewritten."""
    store = registry.knowledge.document_store
    ids: list[str] = []
    offset = 0
    while True:
        page = store.list_documents(
            limit=PAGE_SIZE,
            offset=offset,
            include_chunks=False,
            filters={"source_system": SOURCE_SYSTEM},
        )
        ids.extend(
            str(doc["doc_id"])
            for doc in page
            if str(doc.get("doc_id", "")).startswith(DOC_ID_PREFIX)
        )
        if len(page) < PAGE_SIZE:
            return ids
        offset += len(page)


def restamp_summaries(
    registry: StoreRegistry,
    executor: MutationExecutor,
    *,
    dry_run: bool = False,
) -> RestampResult:
    """Raise every stored summary whose citations now label it above its own label.

    ``executor`` must route ``evidence.ingest`` to a
    :class:`~trellis_workers.trace_embed.handler.TraceSummaryIngestHandler` built
    with ``embed="none"``, so a raise writes the document and mirrors the label onto
    its vector row and calls no embedder.

    Cost per sweep: one listing of the summaries, then for each one a document read,
    a trace read and a read per citation, so N summaries citing k documents each
    cost N·k citation reads, nothing cached across summaries; a raised summary adds
    a second judging read inside the handler and one write. Nothing is written for
    a summary that is already labelled at or above what its citations give.
    """
    result = RestampResult()
    document_store = registry.knowledge.document_store
    egress = EgressGate.for_embedder(registry, document_store)

    def open_trace_store() -> TraceStore:
        return registry.operational.trace_store

    for doc_id in _summary_ids(registry):
        doc = document_store.get(doc_id)
        if doc is None or is_redaction_tombstone(doc):
            continue
        metadata = doc.get("metadata")
        content = doc.get("content")
        trace_id = trace_id_from_doc_id(doc_id)
        if (
            not isinstance(metadata, dict)
            or not isinstance(content, str)
            or not trace_id
        ):
            continue
        if sensitivity_of(metadata) == "restricted":
            # Already at the top: nothing the citations say can raise it, so it is
            # not judged, and an unreadable citation of it is not a fault here.
            continue
        result.scanned += 1
        # Judged without the summary's own label: its stamp reads as the row's own
        # label, which would hide an unreadable citation behind a withhold.
        unlabelled = {k: v for k, v in metadata.items() if k != DATA_CLASSIFICATION_KEY}
        judgement = egress.judge_summary(doc_id, unlabelled, open_trace_store)
        if judgement_unreadable(judgement):
            result.unreadable += 1
            continue
        inherited = judgement.inherited
        if inherited is None or sensitivity_rank(inherited) <= sensitivity_rank(
            sensitivity_of(metadata)
        ):
            continue
        result.raised += 1
        if dry_run:
            continue
        outcome = executor.execute(
            build_trace_summary_command(
                doc_id=doc_id,
                trace_id=trace_id,
                content=content,
                metadata=_raised_metadata(metadata, inherited),
                embed_mode="none",
            )
        )
        if outcome.status is not CommandStatus.SUCCESS:
            result.raised -= 1
            result.failed += 1
            result.failures.append(
                {
                    "trace_id": trace_id,
                    "status": outcome.status.value,
                    "error": outcome.message,
                }
            )
            logger.warning(
                "trace_embed_restamp_failed",
                doc_id=doc_id,
                status=outcome.status.value,
                message=outcome.message,
            )
        elif any(w.startswith(RESTAMP_UNVERIFIED_WARNING) for w in outcome.warnings):
            # The write could not read a citation the sweep had read, so it kept
            # the label the sweep judged. Written, but not verified: counted
            # unreadable so the sweep is unclean and the next one re-judges it.
            result.unreadable += 1
    return result
