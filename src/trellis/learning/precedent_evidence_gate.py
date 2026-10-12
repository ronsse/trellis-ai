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

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from trellis.core.base import TrellisModel
from trellis.learning.pack_observations import join_pack_events_with_coverage
from trellis.mutate.commands import Operation
from trellis.mutate.retention import ARCHIVED_STATE, CURRENT_STATE
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import EventLog, EventType
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

    Returns ``None`` — not a zero-valued evidence record — when the
    precedent never appears in any joined pack's ``injected_items`` in the
    window: *no attributed packs* and *attributed packs with zero
    citations* are different facts, and only the latter is evidence at
    all. :func:`judge_precedent_demotion` treats the two identically
    today (both refuse), but a future caller reading the evidence
    directly must be able to tell them apart.
    """
    feedback_events, pack_events, _, _ = join_pack_events_with_coverage(
        event_log, since=since, limit=limit
    )

    appearances = 0
    helpful_count = 0
    unhelpful_count = 0
    seen_any = False

    for feedback_event in feedback_events:
        payload = feedback_event.payload or {}
        pack_id = str(payload.get("pack_id") or "").strip()
        if not pack_id:
            continue
        pack_event = pack_events.get(pack_id)
        if pack_event is None:
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
    return promoted_at


def precedent_skip_reason(graph_store: GraphStore, entity_id: str) -> str | None:
    """Why ``entity_id`` should not be judged at all, or ``None`` to judge it.

    By-value-inlined lifecycle check rather than an import of
    :func:`trellis.retrieve.precedents.is_withheld_precedent` — this
    module stays free of a dependency on ``trellis.retrieve``, the same
    boundary :mod:`trellis.learning.evidence_gate` states for
    ``classify``. Fails open to "judge it" on a missing node or a
    missing/malformed lifecycle record, matching
    :mod:`trellis.retrieve.lifecycle`'s own convention: absence is never
    by itself a reason to withhold.
    """
    node = graph_store.get_node(entity_id)
    if node is None:
        return None
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
    source: str,
) -> str | None:
    """Emit the single ``LEARNING_AUTO_DEMOTE_EVALUATED`` event for a run.

    Mirrors :func:`trellis.learning.candidate_auto_promote._emit_evaluated_event`'s
    payload shape. ``outcome`` is always ``"ok"``: unlike the promote
    policy's candidate-id-less rows, every current precedent this module
    discovers gets exactly one outcome, so there is no silent-drop case
    to report as ``"degraded"`` here.
    """
    payload: dict[str, Any] = {
        "outcome": "ok",
        "mode": "report_only",
        "min_unhelpful_citations": min_unhelpful_citations,
        "attribution_caveat": ATTRIBUTION_CAVEAT,
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

    Writes nothing: no demotion, no node change. Discovers every
    precedent with a successful ``precedent.promote`` event
    (:func:`discover_current_precedents`), skips one that is already
    demoted, archived, or otherwise not the current lifecycle state
    (:func:`precedent_skip_reason`) without reading its citations at all,
    and otherwise gathers its citation evidence since its own
    ``promoted_at`` (:func:`gather_precedent_citation_evidence`) and
    judges it (:func:`judge_precedent_demotion`).

    Exactly one :attr:`EventType.LEARNING_AUTO_DEMOTE_EVALUATED` event is
    emitted per call, covering every discovered precedent — mirrors
    :func:`trellis.learning.candidate_auto_promote.run_candidate_auto_promotion`'s
    "always emit, even for zero precedents" shape.
    """
    promoted_at_by_entity = discover_current_precedents(
        event_log, limit=promotion_scan_limit
    )

    outcomes: list[PrecedentDemotionOutcome] = []
    for entity_id, promoted_at in promoted_at_by_entity.items():
        skip_reason = precedent_skip_reason(graph_store, entity_id)
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

        evidence = gather_precedent_citation_evidence(
            event_log, entity_id, since=promoted_at, limit=join_limit
        )
        decision = judge_precedent_demotion(
            evidence, entity_id, min_unhelpful_citations=min_unhelpful_citations
        )
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

    event_id = _emit_evaluated_event(
        event_log,
        outcomes=outcomes,
        min_unhelpful_citations=min_unhelpful_citations,
        source=source,
    )
    return PrecedentDemotionReport(
        outcomes=outcomes,
        event_id=event_id,
        min_unhelpful_citations=min_unhelpful_citations,
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
    """Serialise a :class:`PrecedentDemotionReport` for ``--format json``."""
    return {
        "would_demote_count": report.would_demote_count,
        "refused_count": report.refused_count,
        "skipped_count": report.skipped_count,
        "min_unhelpful_citations": report.min_unhelpful_citations,
        "attribution_caveat": report.attribution_caveat,
        "event_id": report.event_id,
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
    "PrecedentCitationEvidence",
    "PrecedentDemotionDecision",
    "PrecedentDemotionOutcome",
    "PrecedentDemotionReport",
    "discover_current_precedents",
    "emit_precedent_demotion_failure_event",
    "evaluate_precedent_demotions",
    "gather_precedent_citation_evidence",
    "judge_precedent_demotion",
    "precedent_skip_reason",
    "report_to_dict",
]
