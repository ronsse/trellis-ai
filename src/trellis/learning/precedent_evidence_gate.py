"""Evidence gate for precedent demotion — report-only.

A precedent promoted by :func:`trellis.learning.scoring.submit_learning_promotion`
can go on to be served in flat ``get_context`` packs and cited helpful or
unhelpful just like any other item. This module asks, per precedent, whether
the citations collected *since that precedent was promoted* support
demoting it — and answers only for the record. Nothing here writes a
demotion; :class:`~trellis.mutate.handlers.PrecedentDemoteHandler` is the
only path that changes a node's lifecycle state, and it is reached from
here not at all.

It is the third by-value instance of the evidence-gate shape introduced by
:mod:`trellis.classify.demotion_gate` (#336) and already copied once by
:mod:`trellis.learning.evidence_gate`:

    **Demotion requires evidence of unhelpfulness, never absence of
    evidence of helpfulness.**

Thresholds and refusal slugs are copied **by value**, not imported —
:mod:`trellis.learning.evidence_gate`'s own docstring states the same rule
for the same reason: ``trellis.learning`` does not import from ``classify``
or ``retrieve``. Keep the three gates' decision shape in sync by hand.

**This gate is precedent-scoped, not corpus-scoped.** ``demotion_gate`` and
``evidence_gate`` both screen a *batch* of proposed candidates and refuse
the whole batch below a corpus-coverage floor
(``MIN_ATTRIBUTED_PACKS`` / ``MIN_ATTRIBUTED_OBSERVATIONS``) before judging
any one candidate — that floor guards against a grading surface going dark
across the whole window. This module judges one precedent against its own
citation history, so there is no batch to suppress; a precedent with no
citations since promotion is simply the ``evidence is None`` case the
per-item rule already refuses (:data:`REFUSED_NO_EVIDENCE`). Only
:data:`MIN_UNHELPFUL_CITATIONS` is borrowed here.

**The borrowed floor is provisional.** ``MIN_UNHELPFUL_CITATIONS = 2`` was
measured against noise-candidate citations (see ``evidence_gate.py``'s
within-pack permutation study), not against precedent citations
specifically — a promoted precedent's citation base rate has never been
measured. Every report this module produces says so; re-measure before any
future commit-mode policy enables on this number.

**Attribution coverage gap.** Only a precedent served through a *flat*
``get_context`` pack can accumulate evidence here at all. ``get_lessons``
and ``list_precedents`` never assemble a ``PACK_ASSEMBLED`` event (they
have no ``pack_id``), and a *sectioned* pack
(``PackBuilder.build_sectioned``) emits no ``injected_items`` — both are
structurally invisible to the join below, not merely unmeasured. A
precedent reachable only through those surfaces will always read
``REFUSED_NO_EVIDENCE`` here, regardless of how often agents actually find
it useful. :data:`ATTRIBUTION_CAVEAT` carries this statement so every
caller surface — CLI text, CLI JSON, the emitted event — says it plainly
rather than once in a docstring nobody serving a report has read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from trellis.core.base import TrellisModel
from trellis.learning.pack_observations import join_pack_events_with_coverage
from trellis.mutate.commands import Operation
from trellis.mutate.retention import ARCHIVED_STATE, CURRENT_STATE
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import (
    Event,
    EventLog,
    EventType,
    ScanCoverage,
    merge_coverage,
)
from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: Minimum explicit ``unhelpful_item_ids`` citations, since promotion,
#: before a precedent is admitted for demotion. Copied by value from
#: :data:`trellis.classify.demotion_gate.MIN_UNHELPFUL_CITATIONS` — see
#: the module docstring: this floor is provisional for a precedent
#: population and has not been separately re-measured.
MIN_UNHELPFUL_CITATIONS = 2

#: Refusal slugs, byte-identical in spelling to ``demotion_gate``'s and
#: ``evidence_gate``'s equivalents (no ``REFUSED_THIN_CORPUS`` here — see
#: the module docstring for why a corpus-coverage floor does not apply to
#: a single-precedent judgment).
REFUSED_NO_EVIDENCE = "no_evidence_supplied"
REFUSED_NO_UNHELPFUL = "no_unhelpful_citation"
REFUSED_INSUFFICIENT = "insufficient_unhelpful_citations"
REFUSED_CONTESTED = "contested_by_helpful_citations"

#: Dispositions a :class:`PrecedentDemotionOutcome` can carry.
DISPOSITION_WOULD_DEMOTE = "would_demote"
DISPOSITION_REFUSED = "refused"
DISPOSITION_SKIPPED = "skipped"

#: Skip reasons for a precedent this module declines to judge at all —
#: distinct from a ``REFUSED_*`` slug, which means "judged, and did not
#: clear the gate". A precedent already demoted or archived needs no
#: evidence read to answer "would you demote this" because the answer is
#: "it already is one of those" or "it is gone".
SKIPPED_ALREADY_DEMOTED = "already_demoted"
SKIPPED_ARCHIVED = "archived"
SKIPPED_NOT_CURRENT = "not_current"
#: A trace- or miner-sourced precedent never gets a graph entity
#: (``PrecedentPromoteHandler``'s docstring), so
#: :class:`~trellis.mutate.handlers.PrecedentDemoteHandler` refuses it
#: outright (``target_not_graph_backed``) rather than reading a
#: lifecycle record it does not have. "would_demote" here would
#: describe a write that handler can never make, so this id is skipped,
#: not judged.
SKIPPED_NOT_GRAPH_BACKED = "not_graph_backed"

#: Stated on every report this module produces, in every output surface
#: (CLI text, CLI JSON, the emitted event's payload) — see the module
#: docstring's "Attribution coverage gap".
ATTRIBUTION_CAVEAT = (
    "Only precedents served through flat get_context packs can "
    "accumulate citation evidence here. get_lessons and sectioned packs "
    "emit no per-item attribution, so a precedent reachable only through "
    "those surfaces always reads no_evidence_supplied regardless of how "
    "useful it actually is."
)

#: Scan limit for the promotion-discovery scan — mirrors
#: :data:`trellis.learning.candidate_auto_promote._PROMOTION_SCAN_LIMIT`.
_PROMOTION_SCAN_LIMIT = 500

#: Scan limit for each side of the PACK_ASSEMBLED / FEEDBACK_RECORDED join
#: — mirrors :mod:`trellis.learning.pack_observations`'s own default.
_JOIN_SCAN_LIMIT = 1000


class PrecedentCitationEvidence(TrellisModel):
    """Per-precedent citation counts backing (or failing to back) a demotion.

    By-value copy of :class:`trellis.classify.demotion_gate.DemotionEvidence`'s
    shape, ``item_id`` renamed to ``entity_id``.
    """

    entity_id: str
    #: Packs, since promotion, in which this precedent was served
    #: (appeared in ``injected_items``) and later received feedback.
    appearances: int = 0
    #: Of those, times a feedback event named it in ``helpful_item_ids``.
    helpful_count: int = 0
    #: Of those, times a feedback event named it in ``unhelpful_item_ids``.
    unhelpful_count: int = 0


class PrecedentDemotionDecision(TrellisModel):
    """One precedent's verdict, with the counts that produced it.

    By-value copy of
    :class:`trellis.classify.demotion_gate.DemotionDecision`'s shape.
    """

    entity_id: str
    admitted: bool
    #: Empty when admitted; one of the ``REFUSED_*`` slugs otherwise.
    reason: str = ""
    appearances: int = 0
    helpful_count: int = 0
    unhelpful_count: int = 0


def _counts(evidence: PrecedentCitationEvidence | None) -> dict[str, int]:
    """Citation counts to stamp on a decision record.

    By-value copy of :func:`trellis.classify.demotion_gate._counts`:
    ``{}`` when there is no evidence, so the decision keeps its zero
    defaults and a zero then means what :data:`REFUSED_NO_EVIDENCE` says.
    """
    if evidence is None:
        return {}
    return {
        "appearances": evidence.appearances,
        "helpful_count": evidence.helpful_count,
        "unhelpful_count": evidence.unhelpful_count,
    }


def judge_precedent_demotion(
    evidence: PrecedentCitationEvidence | None,
    entity_id: str,
    *,
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS,
) -> PrecedentDemotionDecision:
    """Decide one precedent. Pure; by-value copy of ``demotion_gate._judge``'s
    decision shape — the only place this rule is written for precedents.

    Contested (``helpful >= unhelpful``) is refused, and no amount of
    ``helpful_count`` can substitute for an absent or insufficient
    ``unhelpful_count``: this function never admits on the absence of
    helpful citations, only on the presence of enough unhelpful ones.
    """
    if evidence is None:
        return PrecedentDemotionDecision(
            entity_id=entity_id, admitted=False, reason=REFUSED_NO_EVIDENCE
        )

    unhelpful = evidence.unhelpful_count
    helpful = evidence.helpful_count

    def verdict(*, admitted: bool, reason: str) -> PrecedentDemotionDecision:
        return PrecedentDemotionDecision(
            entity_id=entity_id,
            admitted=admitted,
            reason=reason,
            **_counts(evidence),
        )

    if unhelpful <= 0:
        return verdict(admitted=False, reason=REFUSED_NO_UNHELPFUL)
    if unhelpful < min_unhelpful_citations:
        return verdict(admitted=False, reason=REFUSED_INSUFFICIENT)
    if helpful >= unhelpful:
        # Graders disagreed about this precedent. Disagreement is a
        # reason to leave it alone, not a tiebreak in favour of removal.
        return verdict(admitted=False, reason=REFUSED_CONTESTED)
    return verdict(admitted=True, reason="")


def _citation_ids(raw: Any) -> frozenset[str]:
    """Normalize a ``*_item_ids`` field into a set of non-empty ids.

    By-value copy of
    :func:`trellis.learning.pack_observations._citation_ids` (private
    there, so duplicated here rather than imported) — tolerant of the
    field being absent, ``None``, or a bare string.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return frozenset()
    return frozenset(cleaned for entry in raw if (cleaned := str(entry or "").strip()))


def _attribute_precedent_citation_evidence(
    feedback_events: Sequence[Event],
    pack_events: Mapping[str, Event],
    entity_id: str,
    *,
    since: datetime,
) -> PrecedentCitationEvidence | None:
    """Attribute one precedent's evidence out of an already-joined result.

    ``feedback_events``/``pack_events`` may have been joined with a
    ``since`` earlier than this precedent's own ``promoted_at`` —
    :func:`evaluate_precedent_demotions` runs the PACK_ASSEMBLED +
    FEEDBACK_RECORDED join once, at the *earliest* ``promoted_at`` among
    the precedents it judges this run, rather than once per precedent.
    The ``occurred_at >= since`` filters below are what still scope each
    precedent's own window to its own promotion time on top of that
    shared, wider read — dropping them would let an earlier precedent's
    citations bleed into a later one's count, which is exactly what the
    per-precedent-window mutant proof in the test suite catches.

    Returns ``None`` — not a zero-valued evidence record — when the
    precedent never appears in any joined pack's ``injected_items`` in
    its own window: *no attributed packs* and *attributed packs with
    zero citations* are different facts, and only the latter is evidence
    at all. :func:`judge_precedent_demotion` treats the two identically
    today (both refuse), but a future caller reading the evidence
    directly must be able to tell them apart.
    """
    appearances = 0
    helpful_count = 0
    unhelpful_count = 0
    seen_any = False

    for feedback_event in feedback_events:
        if feedback_event.occurred_at < since:
            continue
        payload = feedback_event.payload or {}
        pack_id = str(payload.get("pack_id") or "").strip()
        if not pack_id:
            continue
        pack_event = pack_events.get(pack_id)
        if pack_event is None or pack_event.occurred_at < since:
            continue
        pack_payload = pack_event.payload or {}
        injected = pack_payload.get("injected_items")
        if not isinstance(injected, Sequence) or isinstance(injected, str | bytes):
            continue
        served_ids = {
            str(raw.get("item_id") or "").strip()
            for raw in injected
            if isinstance(raw, dict)
        }
        if entity_id not in served_ids:
            continue

        seen_any = True
        appearances += 1
        if entity_id in _citation_ids(payload.get("helpful_item_ids")):
            helpful_count += 1
        if entity_id in _citation_ids(payload.get("unhelpful_item_ids")):
            unhelpful_count += 1

    if not seen_any:
        return None
    return PrecedentCitationEvidence(
        entity_id=entity_id,
        appearances=appearances,
        helpful_count=helpful_count,
        unhelpful_count=unhelpful_count,
    )


def join_precedent_citation_events(
    event_log: EventLog, *, since: datetime, limit: int = _JOIN_SCAN_LIMIT
) -> tuple[list[Event], dict[str, Event], ScanCoverage]:
    """Run the PACK_ASSEMBLED + FEEDBACK_RECORDED join once for a whole run.

    :func:`evaluate_precedent_demotions` used to call
    :func:`gather_precedent_citation_evidence` once per discovered
    precedent, re-running this join from scratch for every one. It now
    calls this once instead, with ``since`` set to the *earliest*
    ``promoted_at`` among the precedents being judged this run, then
    attributes each precedent's own evidence with
    :func:`_attribute_precedent_citation_evidence` using that
    precedent's *own* ``promoted_at`` as the per-row floor — the window
    semantics (``occurred_at >= promoted_at``) are unchanged; only the
    repeated join is removed. Also returns the read's
    :class:`~trellis.stores.base.event_log.ScanCoverage`, discarded by
    :func:`gather_precedent_citation_evidence`, so a truncated join can
    be surfaced rather than silently under-counting unhelpful citations.
    """
    feedback_events, pack_events, _served_count, coverage = (
        join_pack_events_with_coverage(event_log, since=since, limit=limit)
    )
    return feedback_events, pack_events, coverage


def gather_precedent_citation_evidence(
    event_log: EventLog,
    entity_id: str,
    *,
    since: datetime,
    limit: int = _JOIN_SCAN_LIMIT,
) -> PrecedentCitationEvidence | None:
    """Join PACK_ASSEMBLED + FEEDBACK_RECORDED into one precedent's evidence.

    Reuses :func:`trellis.learning.pack_observations.join_pack_events_with_coverage`
    directly (a sanctioned sibling import within ``trellis.learning``, not
    a gate-decision-shape copy) rather than re-deriving the join. Passing
    ``since=`` (the precedent's own ``promoted_at``) into that join is
    what scopes both the PACK_ASSEMBLED and FEEDBACK_RECORDED reads to
    ``occurred_at >= promoted_at`` — a citation recorded before the
    precedent existed contributes nothing, by construction of the window
    rather than by a second filter applied after the fact.

    A single-entity convenience wrapper kept for direct callers (tests,
    and anything that only ever asks about one precedent at a time) —
    :func:`evaluate_precedent_demotions` itself no longer calls this: it
    runs :func:`join_precedent_citation_events` once per run instead.
    This wrapper discards that join's own coverage, since a single ad
    hoc lookup has no run-level report to carry it on; a caller that
    needs the coverage should call :func:`join_precedent_citation_events`
    and :func:`_attribute_precedent_citation_evidence` directly.
    """
    feedback_events, pack_events, _served_count, _coverage = (
        join_pack_events_with_coverage(event_log, since=since, limit=limit)
    )
    return _attribute_precedent_citation_evidence(
        feedback_events, pack_events, entity_id, since=since
    )


def discover_current_precedents(
    event_log: EventLog, *, limit: int = _PROMOTION_SCAN_LIMIT
) -> dict[str, datetime]:
    """Map every promoted entity id to its own ``precedent.promote`` time.

    No ``requested_by`` filter — mirrors
    :func:`trellis.learning.candidate_auto_promote._scan_already_promoted_entity_ids`:
    a precedent a human promoted and one an (unbuilt) automatic policy
    promoted are judged identically here, since nothing in this module
    submits a command and :class:`~trellis.mutate.handlers.PrecedentDemoteHandler`'s
    own provenance narrowing is conditional on the *caller's* identity,
    not on how the target was promoted.

    Read newest-first so, when a precedent was demoted and later
    re-promoted, the *latest* ``precedent.promote`` event wins for that
    entity id — the citation window should start at the most recent
    promotion, not the first one ever.

    Thin wrapper over :func:`discover_current_precedents_with_coverage`
    for callers that don't need the truncation signal.
    """
    promoted_at, _coverage = discover_current_precedents_with_coverage(
        event_log, limit=limit
    )
    return promoted_at


def discover_current_precedents_with_coverage(
    event_log: EventLog, *, limit: int = _PROMOTION_SCAN_LIMIT
) -> tuple[dict[str, datetime], ScanCoverage]:
    """:func:`discover_current_precedents` plus whether its cap was hit.

    This reads with ``payload_filters``, which
    :func:`~trellis.stores.base.event_log.scan_events` does not accept —
    the same documented constraint as
    :func:`trellis.learning.candidate_auto_promote._scan_already_promoted_entity_ids`,
    so truncation is hand-detected the way ``scan_events`` detects it: a
    full-looking read (``len(events) >= limit``) means older
    ``precedent.promote`` events may have been dropped. That is a
    sharper loss than under-counting a citation — a precedent promoted
    before the cut is invisible to this whole evaluation run, not merely
    one row short.
    """
    events = event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED,
        order="desc",
        payload_filters={
            "operation": Operation.PRECEDENT_PROMOTE.value,
            "status": "success",
        },
        limit=limit,
    )
    promoted_at: dict[str, datetime] = {}
    for event in events:
        if not event.entity_id or event.entity_id in promoted_at:
            continue
        promoted_at[event.entity_id] = event.occurred_at

    type_label = "mutation_executed:precedent.promote"
    truncated = limit > 0 and len(events) >= limit
    covered_since = ""
    note = ""
    if truncated:
        oldest = min((event.occurred_at for event in events), default=None)
        if oldest is not None:
            covered_since = oldest.isoformat()
        note = (
            f"TRUNCATED: the {limit:,}-event cap was reached scanning "
            f"{type_label} ({len(events):,} events read). The newest "
            "promotions were kept; a precedent promoted earlier than "
            "this report's evidence start may be missing from this "
            "report entirely, not merely under-counted."
        )
    coverage = ScanCoverage(
        limit=limit,
        scanned=len(events),
        matched=None,
        dropped=None,
        truncated=truncated,
        truncated_event_types=[type_label] if truncated else [],
        covered_since=covered_since,
        note=note,
    )
    return promoted_at, coverage


def precedent_skip_reason(graph_store: GraphStore, entity_id: str) -> str | None:
    """Why ``entity_id`` should not be judged at all, or ``None`` to judge it.

    By-value-inlined lifecycle check rather than an import of
    :func:`trellis.retrieve.precedents.is_withheld_precedent` — this
    module stays free of a dependency on ``trellis.retrieve``, the same
    boundary :mod:`trellis.learning.evidence_gate` states for
    ``classify``.

    **A missing graph node is skipped, never judged.** Every
    ``entity_id`` reaching this function came from
    :func:`discover_current_precedents`, so it already has a successful
    ``precedent.promote`` event; a missing node then means the
    trace-/miner-sourced promotion path, which never mints one
    (``PrecedentPromoteHandler``'s docstring) —
    :class:`~trellis.mutate.handlers.PrecedentDemoteHandler` refuses that
    id outright (``target_not_graph_backed``), so a "would_demote"
    verdict here would describe a write that handler can never make.
    A missing or malformed lifecycle record on a node that *does* exist
    still fails open to "judge it", matching
    :mod:`trellis.retrieve.lifecycle`'s own convention: absence of a
    lifecycle record, as opposed to absence of the node itself, is never
    by itself a reason to withhold.
    """
    node = graph_store.get_node(entity_id)
    if node is None:
        return SKIPPED_NOT_GRAPH_BACKED
    record = node.get("properties", {}).get(LIFECYCLE_KEY)
    if not isinstance(record, dict):
        return None
    state = record.get("state")
    if not isinstance(state, str) or state == CURRENT_STATE:
        return None
    if state == "demoted":
        return SKIPPED_ALREADY_DEMOTED
    if state == ARCHIVED_STATE:
        return SKIPPED_ARCHIVED
    return SKIPPED_NOT_CURRENT


@dataclass(frozen=True, slots=True)
class PrecedentDemotionOutcome:
    """What one current precedent's evidence says about demoting it.

    Mirrors
    :class:`trellis.learning.candidate_auto_promote.CandidateAutoPromoteOutcome`'s
    shape (plain dataclass, not a :class:`TrellisModel`) — this is a
    CLI/event-reporting record, not a copied gate-decision schema.
    """

    entity_id: str
    #: ``"would_demote"`` — cleared the gate, this run is report-only so
    #: nothing is written; ``"refused"`` — judged and did not clear the
    #: gate (see ``reason`` for the ``REFUSED_*`` slug); ``"skipped"`` —
    #: not judged at all (already demoted, archived, or otherwise not the
    #: current lifecycle state).
    disposition: str
    reason: str = ""
    promoted_at: str | None = None
    appearances: int = 0
    helpful_count: int = 0
    unhelpful_count: int = 0


@dataclass(frozen=True, slots=True)
class PrecedentDemotionReport:
    """Aggregate result of one :func:`evaluate_precedent_demotions` pass."""

    outcomes: list[PrecedentDemotionOutcome] = field(default_factory=list)
    event_id: str | None = None
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS
    attribution_caveat: str = ATTRIBUTION_CAVEAT
    #: Merged coverage of the promotion-discovery scan and the single
    #: PACK_ASSEMBLED/FEEDBACK_RECORDED join this run made. A truncated
    #: scan can under-count unhelpful citations or miss a precedent
    #: entirely — see :attr:`notes`.
    scan: ScanCoverage = field(default_factory=ScanCoverage)
    #: Human-readable callouts a caller surface should show above the
    #: outcomes — currently just :attr:`scan`'s own note, when truncated.
    notes: list[str] = field(default_factory=list)

    @property
    def would_demote_count(self) -> int:
        return sum(
            1 for o in self.outcomes if o.disposition == DISPOSITION_WOULD_DEMOTE
        )

    @property
    def refused_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == DISPOSITION_REFUSED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for o in self.outcomes if o.disposition == DISPOSITION_SKIPPED)


def _emit_evaluated_event(
    event_log: EventLog,
    *,
    outcomes: Sequence[PrecedentDemotionOutcome],
    min_unhelpful_citations: int,
    scan: ScanCoverage,
    source: str,
) -> str | None:
    """Emit the single ``LEARNING_AUTO_DEMOTE_EVALUATED`` event for a run.

    Mirrors :func:`trellis.learning.candidate_auto_promote._emit_evaluated_event`'s
    payload shape. ``outcome`` is always ``"ok"``: unlike the promote
    policy's candidate-id-less rows, every current precedent this module
    discovers gets exactly one outcome, so there is no silent-drop case
    to report as ``"degraded"`` here.

    ``scan`` is included so a reader of the raw event stream — not only
    the CLI/report surface — can see that a scan was capped without
    re-deriving it: a truncated discovery or join scan can under-count
    unhelpful citations and read as "no evidence" rather than "didn't
    look far enough".
    """
    payload: dict[str, Any] = {
        "outcome": "ok",
        "mode": "report_only",
        "min_unhelpful_citations": min_unhelpful_citations,
        "attribution_caveat": ATTRIBUTION_CAVEAT,
        "scan": scan.model_dump(),
        "dispositions": [
            {
                "entity_id": o.entity_id,
                "disposition": o.disposition,
                "reason": o.reason,
                "promoted_at": o.promoted_at,
            }
            for o in outcomes
        ],
        "counts": {
            "total": len(outcomes),
            "would_demote": sum(
                1 for o in outcomes if o.disposition == DISPOSITION_WOULD_DEMOTE
            ),
            "refused": sum(1 for o in outcomes if o.disposition == DISPOSITION_REFUSED),
            "skipped": sum(1 for o in outcomes if o.disposition == DISPOSITION_SKIPPED),
        },
    }
    event = event_log.emit(
        EventType.LEARNING_AUTO_DEMOTE_EVALUATED,
        source,
        payload=payload,
    )
    return event.event_id


def emit_precedent_demotion_failure_event(
    event_log: EventLog,
    *,
    error: BaseException,
    source: str = "precedent_evidence_gate",
) -> str | None:
    """Record a run that never produced outcomes at all.

    Mirrors
    :func:`trellis.learning.candidate_auto_promote.emit_candidate_auto_promote_failure_event`:
    reuses :attr:`EventType.LEARNING_AUTO_DEMOTE_EVALUATED` rather than a
    new event type, never includes ``str(error)`` (only ``error.type``),
    and swallows its own emission failure rather than masking the
    original exception.
    """
    payload: dict[str, Any] = {
        "outcome": "failed",
        "error": {
            "type": type(error).__name__,
            "message": "withheld — see error.type",
        },
    }
    try:
        event = event_log.emit(
            EventType.LEARNING_AUTO_DEMOTE_EVALUATED,
            source,
            payload=payload,
        )
    except Exception:
        logger.warning(
            "precedent_demotion_failure_event_emit_failed",
            exc_info=True,
            original_error_type=type(error).__name__,
        )
        return None
    return event.event_id


def judge_current_precedents(
    event_log: EventLog,
    graph_store: GraphStore,
    *,
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS,
    promotion_scan_limit: int = _PROMOTION_SCAN_LIMIT,
    join_limit: int = _JOIN_SCAN_LIMIT,
) -> tuple[list[PrecedentDemotionOutcome], ScanCoverage]:
    """Discover and judge every current precedent — the reusable core.

    Writes nothing and emits no event: this is the discovery-and-judging
    body :func:`evaluate_precedent_demotions` has always run, pulled out
    so a commit-capable caller
    (:func:`trellis.learning.precedent_auto_demote.run_precedent_auto_demotion`)
    can reuse it without duplicating the join/skip/judge logic or its own
    event-emission shape. Discovers every precedent with a successful
    ``precedent.promote`` event
    (:func:`discover_current_precedents_with_coverage`), skips one that
    is already demoted, archived, or otherwise not the current lifecycle
    state (:func:`precedent_skip_reason`) without reading its citations
    at all, and otherwise judges it (:func:`judge_precedent_demotion`)
    against citation evidence attributed since its own ``promoted_at``.

    The PACK_ASSEMBLED/FEEDBACK_RECORDED join
    (:func:`join_precedent_citation_events`) runs **once per call**, not
    once per precedent: at the earliest ``promoted_at`` among the
    precedents actually being judged (skipped ones never reach the
    join). Each precedent's own evidence is then attributed out of that
    one shared result (:func:`_attribute_precedent_citation_evidence`),
    filtered to its own ``occurred_at >= promoted_at``.

    Returns the per-precedent outcomes (unordered relative to nothing in
    particular — insertion order of
    :func:`discover_current_precedents_with_coverage`'s result) and the
    merged discovery+join scan coverage: a truncated scan can under-count
    unhelpful citations and must not read as "no evidence".
    """
    promoted_at_by_entity, discovery_coverage = (
        discover_current_precedents_with_coverage(event_log, limit=promotion_scan_limit)
    )

    skip_reason_by_entity: dict[str, str] = {}
    to_judge: dict[str, datetime] = {}
    for entity_id, promoted_at in promoted_at_by_entity.items():
        skip_reason = precedent_skip_reason(graph_store, entity_id)
        if skip_reason is not None:
            skip_reason_by_entity[entity_id] = skip_reason
        else:
            to_judge[entity_id] = promoted_at

    if to_judge:
        join_since = min(to_judge.values())
        feedback_events, pack_events, join_coverage = join_precedent_citation_events(
            event_log, since=join_since, limit=join_limit
        )
    else:
        feedback_events, pack_events, join_coverage = [], {}, ScanCoverage()

    decisions: dict[str, PrecedentDemotionDecision] = {}
    for entity_id, promoted_at in to_judge.items():
        evidence = _attribute_precedent_citation_evidence(
            feedback_events, pack_events, entity_id, since=promoted_at
        )
        decisions[entity_id] = judge_precedent_demotion(
            evidence, entity_id, min_unhelpful_citations=min_unhelpful_citations
        )

    outcomes: list[PrecedentDemotionOutcome] = []
    for entity_id, promoted_at in promoted_at_by_entity.items():
        skip_reason = skip_reason_by_entity.get(entity_id)
        if skip_reason is not None:
            outcomes.append(
                PrecedentDemotionOutcome(
                    entity_id=entity_id,
                    disposition=DISPOSITION_SKIPPED,
                    reason=skip_reason,
                    promoted_at=promoted_at.isoformat(),
                )
            )
            continue

        decision = decisions[entity_id]
        outcomes.append(
            PrecedentDemotionOutcome(
                entity_id=entity_id,
                disposition=(
                    DISPOSITION_WOULD_DEMOTE
                    if decision.admitted
                    else DISPOSITION_REFUSED
                ),
                reason=decision.reason,
                promoted_at=promoted_at.isoformat(),
                appearances=decision.appearances,
                helpful_count=decision.helpful_count,
                unhelpful_count=decision.unhelpful_count,
            )
        )

    scan = merge_coverage(discovery_coverage, join_coverage)
    return outcomes, scan


def evaluate_precedent_demotions(
    event_log: EventLog,
    graph_store: GraphStore,
    *,
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS,
    promotion_scan_limit: int = _PROMOTION_SCAN_LIMIT,
    join_limit: int = _JOIN_SCAN_LIMIT,
    source: str = "precedent_evidence_gate",
) -> PrecedentDemotionReport:
    """Evaluate every current precedent and report what would be demoted.

    Writes nothing: no demotion, no node change. See
    :func:`judge_current_precedents` for the discovery/judging logic this
    wraps. Exactly one :attr:`EventType.LEARNING_AUTO_DEMOTE_EVALUATED`
    event is emitted per call, covering every discovered precedent, with
    ``mode`` always ``"report_only"`` — mirrors
    :func:`trellis.learning.candidate_auto_promote.run_candidate_auto_promotion`'s
    "always emit, even for zero precedents" shape. A caller that wants a
    real, possibly-commit-mode run should use
    :func:`trellis.learning.precedent_auto_demote.run_precedent_auto_demotion`
    instead — this function's own event payload never varies by mode.
    """
    outcomes, scan = judge_current_precedents(
        event_log,
        graph_store,
        min_unhelpful_citations=min_unhelpful_citations,
        promotion_scan_limit=promotion_scan_limit,
        join_limit=join_limit,
    )

    notes: list[str] = []
    if scan.truncated and scan.note:
        notes.append(scan.note)

    event_id = _emit_evaluated_event(
        event_log,
        outcomes=outcomes,
        min_unhelpful_citations=min_unhelpful_citations,
        scan=scan,
        source=source,
    )
    return PrecedentDemotionReport(
        outcomes=outcomes,
        event_id=event_id,
        min_unhelpful_citations=min_unhelpful_citations,
        scan=scan,
        notes=notes,
    )


def _outcome_to_dict(outcome: PrecedentDemotionOutcome) -> dict[str, Any]:
    """Serialise a :class:`PrecedentDemotionOutcome` for ``--format json``."""
    return {
        "entity_id": outcome.entity_id,
        "disposition": outcome.disposition,
        "reason": outcome.reason,
        "promoted_at": outcome.promoted_at,
        "appearances": outcome.appearances,
        "helpful_count": outcome.helpful_count,
        "unhelpful_count": outcome.unhelpful_count,
    }


def report_to_dict(report: PrecedentDemotionReport) -> dict[str, Any]:
    """Serialise a :class:`PrecedentDemotionReport` for ``--format json``.

    ``scan`` is hand-assembled from :attr:`PrecedentDemotionReport.scan`
    rather than the whole dataclass dumped wholesale, so a new field
    reaches this surface only when named here — the same discipline
    :mod:`trellis_cli.analyze`'s own truncated reports follow.
    """
    return {
        "would_demote_count": report.would_demote_count,
        "refused_count": report.refused_count,
        "skipped_count": report.skipped_count,
        "min_unhelpful_citations": report.min_unhelpful_citations,
        "attribution_caveat": report.attribution_caveat,
        "event_id": report.event_id,
        "scan": report.scan.model_dump(),
        "notes": report.notes,
        "outcomes": [_outcome_to_dict(o) for o in report.outcomes],
    }


__all__ = [
    "ATTRIBUTION_CAVEAT",
    "DISPOSITION_REFUSED",
    "DISPOSITION_SKIPPED",
    "DISPOSITION_WOULD_DEMOTE",
    "MIN_UNHELPFUL_CITATIONS",
    "REFUSED_CONTESTED",
    "REFUSED_INSUFFICIENT",
    "REFUSED_NO_EVIDENCE",
    "REFUSED_NO_UNHELPFUL",
    "SKIPPED_ALREADY_DEMOTED",
    "SKIPPED_ARCHIVED",
    "SKIPPED_NOT_CURRENT",
    "SKIPPED_NOT_GRAPH_BACKED",
    "PrecedentCitationEvidence",
    "PrecedentDemotionDecision",
    "PrecedentDemotionOutcome",
    "PrecedentDemotionReport",
    "discover_current_precedents",
    "discover_current_precedents_with_coverage",
    "emit_precedent_demotion_failure_event",
    "evaluate_precedent_demotions",
    "gather_precedent_citation_evidence",
    "join_precedent_citation_events",
    "judge_current_precedents",
    "judge_precedent_demotion",
    "precedent_skip_reason",
    "report_to_dict",
]
