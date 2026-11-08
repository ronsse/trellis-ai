"""Re-judge reconcile pairs that were withheld from a route that was not local (#1134).

With ``TRELLIS_ENABLE_RECONCILE_ON_WRITE`` on and the ``reconcile`` route not
declared local, ``save_memory`` does not send a pair to the judge when either
side is labelled above ``internal`` (:mod:`trellis.llm.egress`). The new memory
is stored as a plain ADD under the ``skipped`` marker and a
``RECONCILE_DEGRADED`` event with ``reason="egress_withheld"`` is emitted. Both
rows stay current from then on, so a pack can serve the old fact and a pair the
judge would have called NOOP keeps a second row. Nothing revisits them.

:func:`rejudge_withheld` is that revisit. It adds **no verdict logic**: the
judge is :func:`~trellis.mcp.reconcile.judge_reconcile`, a SUPERSEDE stamps the
older row through :func:`~trellis.mcp.reconcile.mark_document_superseded`, and
the stored markers and the ``MEMORY_OP_JUDGED`` emission are the write-time
path's own (:func:`~trellis.mcp.reconcile.emit_reconcile_verdict`).

How a withheld memory is found. The stored marker is ``skipped`` for every
fallback reason, so it cannot say *why* the memory was skipped, and neither the
marker nor the event names the near-duplicate the judge would have been shown
(the event's subject is the new memory). So the sweep takes the memory ids from
the ``egress_withheld`` events, keeps those whose row still reads ``skipped``
and ``current`` (that is the idempotency: a judged row carries its verdict's
marker and is not found again), and re-finds the near-duplicate with the same
MinHash threshold the write path uses, restricted to older rows that are still
current (the judge is told which memory is new) and to rows the caller's
clearance may read, by the write path's own test
(:func:`~trellis.mcp.reconcile.candidate_is_cleared`). The threshold matches;
the seeded population can be wider than the write path's index held, and the
sweep walks past a refused or younger match to the next, so it may pick an older
candidate the write path never saw.

**The egress gate is asked again, per pair, as it stands now** (
:func:`_pair_permitted`): a pair whose route is still not local and whose side
is still labelled stays pending, is counted, and is never sent. A re-label or a
route newly declared local is what releases a pair.

Nothing is deleted. Report-only is the default and stops before the judge: it
finds each memory, re-finds its candidate, screens the pair through the egress
gate (local and free) and counts what ``apply`` would judge as ``would_judge``.
It sends nothing and writes no row, lifecycle or event, because a verdict that
is thrown away would spend a model call and, on a permitted non-local route,
send text off-host for nothing. NOOP is recorded as a verdict and does not
stale-mark the newer row: the judge said the memory adds nothing, not that it is
replaced, and ``superseded_by`` is a claim the judge never made. The row stays
current; the report counts it under ``judged_by_verdict["noop"]``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.base import TrellisModel
from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.core.hashing import content_hash
from trellis.core.vector_metadata import resolve_vector_store
from trellis.llm.egress import EgressGate
from trellis.llm.routing import LLMConsumer
from trellis.mcp.reconcile import (
    MARKER_SKIPPED,
    RECONCILIATION_KEY,
    SUPERSEDES_DOC_KEY,
    UPDATES_DOC_KEY,
    ReconcileCandidate,
    ReconcileDecision,
    ReconcileFallbackReason,
    ReconcileOutcome,
    candidate_is_cleared,
    configured_model_id,
    emit_reconcile_degraded,
    emit_reconcile_verdict,
    judge_reconcile,
    mark_document_superseded,
    marker_for_fallback,
    reconcile_timeout_seconds,
)
from trellis.mcp.wiring import _build_llm_client, _seed_minhash_index
from trellis.schemas.classification import lifecycle_state_of
from trellis.schemas.memory_op import REF_TYPE_DOCUMENT
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.vector import VectorStore
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Events read per run. ``egress_withheld`` needs a labelled document and a
#: route that is not local, so the population is small; a read that hits the
#: cap says so (``events_truncated``) rather than reporting a short list as whole.
EVENT_READ_LIMIT = 50_000

#: Bound on the documents indexed to find near-duplicates. A ceiling on cost,
#: far above any corpus the MinHash seed is run over today.
_INDEX_MAX_DOCS = 1_000_000

_VERDICT_KEYS = tuple(d.value for d in ReconcileDecision)


class RejudgeRow(TrellisModel):
    """What happened to one withheld memory."""

    doc_id: str
    outcome: str
    candidate_id: str | None = None
    decision: str | None = None


class RejudgeReport(TrellisModel):
    """Counts for one run. Every count is of memories, never of pairs sent."""

    apply: bool
    #: Distinct memories the ``egress_withheld`` events name.
    found: int = 0
    #: Of those, rows gone, redacted, no longer ``current``, or already judged.
    no_longer_pending: int = 0
    #: Pending, but the gate still refuses the pair. Nothing was sent.
    still_withheld: int = 0
    #: Pending, but no older current near-duplicate exists any more.
    no_candidate: int = 0
    #: Report-only: pairs the gate permits and ``apply`` would send to the judge.
    #: The judge was not called. Always ``0`` under ``apply``.
    would_judge: int = 0
    #: The judge answered (fail-open fallbacks are ``judge_failures``). Always
    #: ``0`` in report-only mode, which does not call it.
    judged: int = 0
    judged_by_verdict: dict[str, int] = Field(
        default_factory=lambda: dict.fromkeys(_VERDICT_KEYS, 0)
    )
    #: Verdicts written to the row (always ``0`` in report-only mode).
    applied: int = 0
    #: The judge was unavailable or gave no usable answer; the row stays pending.
    judge_failures: int = 0
    #: A SUPERSEDE named a protected row (a promoted precedent or a tombstone).
    target_protected: int = 0
    #: The candidate or the row changed while judging; the row stays pending.
    candidate_changed: int = 0
    #: A write was refused or raised; the row stays pending.
    apply_failures: int = 0
    #: Pending rows beyond ``max_items``, not examined this run.
    deferred: int = 0
    #: The event read hit :data:`EVENT_READ_LIMIT`.
    events_truncated: bool = False
    rows: list[RejudgeRow] = Field(default_factory=list)


def _withheld_subjects(registry: StoreRegistry) -> tuple[list[str], bool]:
    """Memory ids the ``egress_withheld`` events name, oldest first, de-duplicated."""
    events = registry.operational.event_log.get_events(
        event_type=EventType.RECONCILE_DEGRADED,
        payload_filters={"reason": ReconcileFallbackReason.EGRESS_WITHHELD.value},
        limit=EVENT_READ_LIMIT,
        order="asc",
    )
    ids: dict[str, None] = {}
    for event in events:
        payload = event.payload
        if payload.get("subject_ref_type") != REF_TYPE_DOCUMENT:
            continue
        subject = payload.get("subject_ref_id")
        if isinstance(subject, str) and subject:
            ids.setdefault(subject, None)
    return list(ids), len(events) >= EVENT_READ_LIMIT


def _is_pending(doc: dict[str, Any] | None) -> bool:
    """A row the withheld write left behind and nothing has judged since."""
    if doc is None or is_redaction_tombstone(doc):
        return False
    metadata = doc.get("metadata") or {}
    return (
        metadata.get(RECONCILIATION_KEY) == MARKER_SKIPPED
        and lifecycle_state_of(metadata) == "current"
    )


def _created(doc: dict[str, Any]) -> datetime | None:
    raw = doc.get("created_at")
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    return raw if raw.tzinfo else raw.replace(tzinfo=UTC)


def _older_current_candidate(
    index: MinHashIndex,
    document_store: DocumentStore,
    doc: dict[str, Any],
    clearance: Sensitivity,
) -> ReconcileCandidate | None:
    """The nearest older, current, readable near-duplicate of *doc*, or ``None``.

    The judge is told which memory is new, so a row created after *doc* is not
    a candidate for it: it would be superseded by an older fact. Order is
    ``(created_at, doc_id)``, so two rows stamped alike are never each other's
    older. A row whose age cannot be read is not a candidate: the direction of
    a supersession is not one to guess. A row above *clearance* is not a
    candidate either, by the write path's own test (:func:`candidate_is_cleared`).
    """
    doc_id = doc["doc_id"]
    own = _created(doc)
    if own is None:
        return None
    for match_id, similarity in index.query(doc["content"], exclude_ids={doc_id}):
        other = document_store.get(match_id)
        if other is None or is_redaction_tombstone(other):
            continue
        stamp = _created(other)
        if stamp is None or (stamp, match_id) >= (own, doc_id):
            continue
        if lifecycle_state_of(other.get("metadata")) != "current":
            continue
        if not candidate_is_cleared(document_store, match_id, other, clearance):
            continue
        return ReconcileCandidate(
            doc_id=match_id, content=other["content"], similarity=float(similarity)
        )
    return None


def _pair_permitted(
    gate: EgressGate,
    document_store: DocumentStore,
    doc: dict[str, Any],
    candidate: ReconcileCandidate,
) -> bool:
    """Whether the judge may be shown both texts, by the gate as it is now.

    The same question ``save_memory`` asks (``_reconcile_egress_permits`` in
    :mod:`trellis.mcp.tools.memory`): both labels count, a chunk takes its
    parent's, and a candidate that cannot be read back is withheld. The memory
    has a stored row now, so its label is read as a stored document's.
    """
    if gate.local:
        return True
    if not gate.permits(doc["doc_id"], doc.get("metadata")):
        return False
    try:
        stored = document_store.get(candidate.doc_id)
    except Exception:
        # GRACEFUL-DEGRADATION: fail closed, as the write-time gate does.
        return False
    if stored is None:
        return False
    return bool(gate.permits(candidate.doc_id, stored.get("metadata")))


class _LazyJudgeClient:
    """The reconcile route's client, built on first use and at most once.

    A run whose every pair is still withheld builds none, so it needs no model
    and cannot reach one.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry
        self._built = False
        self._client: Any = None

    def get(self) -> Any:
        if not self._built:
            self._built = True
            try:
                self._client = _build_llm_client(self._registry, LLMConsumer.RECONCILE)
            except Exception:
                # GRACEFUL-DEGRADATION: no judge is not a failed run; the
                # memories stay pending and the report counts them.
                logger.warning("rejudge_client_build_failed")
        return self._client


def _judge_pair(
    gate: EgressGate,
    document_store: DocumentStore,
    judge: _LazyJudgeClient,
    doc: dict[str, Any],
    candidate: ReconcileCandidate,
    *,
    timeout: float,
    model_id: str,
) -> ReconcileOutcome | None:
    """Ask the judge about one pair, or return ``None`` having sent nothing.

    ``None`` means the gate refuses the pair. The gate is asked here, directly
    above the only call that sends and before any client is built, so no path
    reaches the judge around it. A pair the gate permits but that has no client
    to go to comes back as a ``model_unavailable`` fallback.
    """
    if not _pair_permitted(gate, document_store, doc, candidate):
        return None
    client = judge.get()
    if client is None:
        return _no_judge(model_id)
    return judge_reconcile(
        client,
        new_content=doc["content"],
        candidate=candidate,
        timeout=timeout,
        model_id=model_id,
    )


def _screen_or_judge(
    gate: EgressGate,
    document_store: DocumentStore,
    judge: _LazyJudgeClient,
    doc: dict[str, Any],
    candidate: ReconcileCandidate,
    *,
    apply: bool,
    timeout: float,
    model_id: str,
) -> tuple[bool, ReconcileOutcome | None]:
    """``(permitted, outcome)`` for one pair.

    Under *apply* the pair goes to :func:`_judge_pair`. Report-only screens the
    pair through the same predicate and stops, so the outcome is ``None``: the
    judge is not called, nothing is spent and nothing leaves the host.
    """
    if apply:
        outcome = _judge_pair(
            gate,
            document_store,
            judge,
            doc,
            candidate,
            timeout=timeout,
            model_id=model_id,
        )
        return outcome is not None, outcome
    return _pair_permitted(gate, document_store, doc, candidate), None


def _candidate_changed(
    document_store: DocumentStore, candidate: ReconcileCandidate
) -> bool:
    """The candidate is gone, edited, or no longer ``current`` since it was judged."""
    current = document_store.get(candidate.doc_id)
    if current is None:
        return True
    if content_hash(current["content"]) != content_hash(candidate.content):
        return True
    return lifecycle_state_of(current.get("metadata")) != "current"


def _write_marker(
    document_store: DocumentStore,
    vector_store: VectorStore | None,
    doc: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    """Replace the row's metadata only; its content and recency clock stay."""
    put_document(
        document_store,
        vector_store,
        doc["doc_id"],
        doc["content"],
        metadata,
        preserve_updated_at=True,
    )


def _verdict_subject(
    decision: ReconcileDecision, candidate: ReconcileCandidate, doc_id: str
) -> str:
    """The row the verdict is about: ADD is the new memory, the rest the candidate.

    The write path's choice (``trellis.mcp.tools.memory._reconcile_subject``).
    """
    return doc_id if decision is ReconcileDecision.ADD else candidate.doc_id


def _apply_verdict(
    registry: StoreRegistry,
    doc: dict[str, Any],
    candidate: ReconcileCandidate,
    outcome: ReconcileOutcome,
    report: RejudgeReport,
) -> str:
    """Write one verdict onto the withheld row; return the row's outcome slug.

    Re-reads the row and the candidate first: the sweep is a separate process
    from the writer and holds no lock, so either may have moved while the judge
    thought. A moved row stays pending for the next run.
    """
    document_store = registry.knowledge.document_store
    event_log = registry.operational.event_log
    vector_store = resolve_vector_store(registry)
    doc_id = doc["doc_id"]

    fresh = document_store.get(doc_id)
    if (
        not _is_pending(fresh)
        or fresh is None
        or content_hash(fresh["content"]) != content_hash(doc["content"])
        or _candidate_changed(document_store, candidate)
    ):
        report.candidate_changed += 1
        return "candidate_changed"

    base = dict(fresh.get("metadata") or {})
    decision = outcome.decision
    marked = {**base, RECONCILIATION_KEY: decision.value}
    if decision is ReconcileDecision.UPDATE:
        marked[UPDATES_DOC_KEY] = candidate.doc_id
    elif decision is ReconcileDecision.SUPERSEDE:
        marked[SUPERSEDES_DOC_KEY] = candidate.doc_id
    _write_marker(document_store, vector_store, fresh, marked)

    if decision is ReconcileDecision.SUPERSEDE:
        try:
            refusal = mark_document_superseded(
                document_store,
                old_doc_id=candidate.doc_id,
                new_doc_id=doc_id,
                vector_store=vector_store,
            )
        except Exception:
            # A raise (say, "database is locked") is not a refusal, but it leaves
            # the same claim with no stamp behind it. Without this the row no
            # longer reads ``skipped`` and the next run never judges it again.
            try:
                _release_claim(document_store, vector_store, fresh, base)
            except Exception:
                # The row keeps its claim (the crash window the module notes);
                # the error that brought us here is the one to report.
                logger.exception("rejudge_claim_rollback_failed", doc_id=doc_id)
            raise
        if refusal is not None:
            return _withdraw_claim(
                registry, fresh, base, outcome, refusal, report, vector_store
            )

    report.applied += 1
    try:
        emit_reconcile_verdict(
            event_log,
            outcome=outcome,
            new_content=doc["content"],
            candidate=candidate,
            subject_ref_type=REF_TYPE_DOCUMENT,
            subject_ref_id=_verdict_subject(decision, candidate, doc_id),
        )
    except Exception:
        # GRACEFUL-DEGRADATION: the verdict is written; only its audit event is
        # missing. Counting the row a failure would send the next run to judge a
        # pair it has already applied.
        logger.exception(
            "rejudge_verdict_event_failed", doc_id=doc_id, decision=decision.value
        )
    return decision.value


def _release_claim(
    document_store: DocumentStore,
    vector_store: VectorStore | None,
    fresh: dict[str, Any],
    base: dict[str, Any],
    marker: str = MARKER_SKIPPED,
) -> None:
    """Put the row back as it was before the SUPERSEDE claim, under *marker*."""
    restored = {k: v for k, v in base.items() if k != SUPERSEDES_DOC_KEY}
    restored[RECONCILIATION_KEY] = marker
    _write_marker(document_store, vector_store, fresh, restored)


def _withdraw_claim(
    registry: StoreRegistry,
    fresh: dict[str, Any],
    base: dict[str, Any],
    outcome: ReconcileOutcome,
    refusal: ReconcileFallbackReason,
    report: RejudgeReport,
    vector_store: VectorStore | None,
) -> str:
    """Take back a SUPERSEDE claim the seam refused, as the write path does.

    A protected target is a settled answer, so the row takes the marker the
    write path gives it (:func:`marker_for_fallback`) and is not judged again,
    and the refusal is counted on ``RECONCILE_DEGRADED`` with
    ``judge_failure=false``. A target that vanished is a race, not an answer:
    the row goes back to ``skipped`` and the next run looks for another
    candidate.
    """
    protected = refusal is ReconcileFallbackReason.TARGET_PROTECTED
    _release_claim(
        registry.knowledge.document_store,
        vector_store,
        fresh,
        base,
        marker_for_fallback(refusal) if protected else MARKER_SKIPPED,
    )
    if not protected:
        report.candidate_changed += 1
        return "candidate_changed"
    report.target_protected += 1
    emit_reconcile_degraded(
        registry.operational.event_log,
        outcome=ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=outcome.confidence,
            model_id=outcome.model_id,
            fallback=True,
            fallback_reason=refusal,
        ),
        subject_ref_type=REF_TYPE_DOCUMENT,
        subject_ref_id=fresh["doc_id"],
    )
    return "target_protected"


def rejudge_withheld(
    registry: StoreRegistry,
    *,
    clearance: Sensitivity,
    apply: bool = False,
    max_items: int = 500,
) -> RejudgeReport:
    """Re-judge every memory stored under ``egress_withheld`` that still can be.

    Report-only unless *apply*, and report-only never calls the judge. See the
    module docstring for what is read, what is asked of the egress gate and what
    is written. Never raises for a judge problem (it is counted and the row stays
    pending) or a refused write of one row (counted as ``apply_failures``); a
    store that cannot be read raises.

    *clearance* is the document clearance of whoever runs the sweep, with no
    default so a caller cannot forget to name one: a candidate above it is never
    chosen, as on the write path. The CLI is trusted-local and passes
    ``"restricted"``. ``current_clearance()`` is the MCP caller's and reads
    ``"internal"`` outside a request, which would hide every labelled candidate,
    so the sweep does not read it itself.
    """
    report = RejudgeReport(apply=apply)
    document_store = registry.knowledge.document_store
    subjects, report.events_truncated = _withheld_subjects(registry)
    report.found = len(subjects)

    gate = EgressGate.for_route(registry, LLMConsumer.RECONCILE, document_store)
    model_id = configured_model_id(registry)
    timeout = reconcile_timeout_seconds(registry)
    index: MinHashIndex | None = None
    judge = _LazyJudgeClient(registry)
    examined = 0

    for doc_id in subjects:
        doc = document_store.get(doc_id)
        if not _is_pending(doc) or doc is None:
            report.no_longer_pending += 1
            continue
        if examined >= max_items:
            report.deferred += 1
            continue
        examined += 1

        if index is None:
            index = MinHashIndex()
            _seed_minhash_index(index, document_store, max_docs=_INDEX_MAX_DOCS)
        candidate = _older_current_candidate(index, document_store, doc, clearance)
        if candidate is None:
            report.no_candidate += 1
            report.rows.append(RejudgeRow(doc_id=doc_id, outcome="no_candidate"))
            continue

        permitted, outcome = _screen_or_judge(
            gate,
            document_store,
            judge,
            doc,
            candidate,
            apply=apply,
            timeout=timeout,
            model_id=model_id,
        )
        if not permitted:
            report.still_withheld += 1
            report.rows.append(
                RejudgeRow(
                    doc_id=doc_id,
                    outcome="still_withheld",
                    candidate_id=candidate.doc_id,
                )
            )
            continue
        if outcome is None:
            report.would_judge += 1
            report.rows.append(
                RejudgeRow(
                    doc_id=doc_id,
                    outcome="would_judge",
                    candidate_id=candidate.doc_id,
                )
            )
            continue
        if outcome.fallback:
            report.judge_failures += 1
            report.rows.append(
                RejudgeRow(
                    doc_id=doc_id,
                    outcome="judge_failure",
                    candidate_id=candidate.doc_id,
                )
            )
            continue

        report.judged += 1
        report.judged_by_verdict[outcome.decision.value] += 1
        try:
            row_outcome = _apply_verdict(registry, doc, candidate, outcome, report)
        except Exception:
            # GRACEFUL-DEGRADATION: one refused write must not stop the sweep;
            # the row is back to pending and is retried next run.
            logger.exception("rejudge_apply_failed", doc_id=doc_id)
            report.apply_failures += 1
            row_outcome = "apply_failed"
        report.rows.append(
            RejudgeRow(
                doc_id=doc_id,
                outcome=row_outcome,
                candidate_id=candidate.doc_id,
                decision=outcome.decision.value,
            )
        )
    return report


def _no_judge(model_id: str) -> ReconcileOutcome:
    """The outcome of a run with no client: a judge failure, nothing sent."""
    return ReconcileOutcome(
        decision=ReconcileDecision.ADD,
        confidence=0.0,
        model_id=model_id,
        fallback=True,
        fallback_reason=ReconcileFallbackReason.MODEL_UNAVAILABLE,
    )


__all__ = [
    "EVENT_READ_LIMIT",
    "RejudgeReport",
    "RejudgeRow",
    "rejudge_withheld",
]
