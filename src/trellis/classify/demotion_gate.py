"""Evidence gate for effectiveness-based noise demotion (#336).

**Demotion requires evidence of unhelpfulness, never absence of evidence
of helpfulness.** That inversion is the whole module.

The effectiveness pass proposes noise candidates from
``usage_rate = helpful_citations / appearances`` and demotes everything
below ``noise_rate_threshold`` (0.3). The rule reads as "served often,
rarely used" — but it is a claim about a *base rate*, and the base rate
was never measured. On the reference deployment, 30 days to 2026-08-28:

===========================================  =======
measurement                                    value
===========================================  =======
servings (item x graded pack)                    340
helpful citations                                 35
``P(item cited helpful | item served)``       0.1029
unhelpful citations                              140
``P(item cited unhelpful | item served)``     0.4118
servings inside a pack carrying any verdict   0.932
===========================================  =======

A *perfectly ordinary* item served twice is cited helpful zero times
with probability ``(1 - 0.1029)^2 = 0.805``. The proposal rule therefore
flags 80% of good items by construction, and measured against the live
corpus it flagged **64 of 79 scored items (81%)** — including durable
technical memories that #336 had already restored once. Raising
``min_appearances`` does not rescue it: at five appearances the
false-flag probability is still 0.58, and no production item has been
served more than five times.

The negative signal, meanwhile, is **four times denser** than the
positive one and goes completely unread: the proposal rule never looks
at ``unhelpful_item_ids``. This gate reads it.

Two independent conditions, both of which must hold before a proposed
candidate is admitted:

1. **Corpus coverage** — at least :data:`MIN_ATTRIBUTED_PACKS` packs in
   the window carried a per-item verdict. Below that there is no
   population to reason over and the whole batch is refused, in the
   shape :mod:`trellis.retrieve.pack_value` established: state the
   coverage, refuse the ratio, name the reason.
2. **Per-item evidence** — the item was cited *unhelpful* at least
   :data:`MIN_UNHELPFUL_CITATIONS` times, and cited unhelpful more often
   than helpful.

Condition 1 alone would have changed nothing: production sits at 17
attributed packs, comfortably over the floor, so a coverage gate passes
today and demotes the same 64 items. It is included because coverage can
collapse (a grading agent goes dark, #309) and silence must not read as
consensus — not because it is what makes the gate sound. Condition 2 is
what makes it sound, and the two are deliberately not collapsed into one
number: they fail for different reasons and a reader needs to know which.

Screening is a *decision* and is kept separate from the write —
:func:`~trellis.classify.feedback.apply_noise_tags` stays a dumb writer,
so a caller performing a deliberate manual demotion (which is not an
inference and needs no evidence) is unaffected.

Demotion remains a **penalty, not an exclusion**, and remains reversible:
an admitted item is tagged ``signal_quality="noise"`` and dropped from
packs by default, and ``retention.restore`` can undo the archival that
follows. The gate narrows *which* items reach that path; it does not
change what the path does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel

logger = structlog.get_logger(__name__)

#: Minimum packs carrying a per-item verdict before *any* demotion is
#: admitted. Shares the value and the reasoning of
#: :data:`trellis.retrieve.pack_value.MIN_ATTRIBUTED_PACKS` — below this
#: a rate is not a measurement — so the two coverage surfaces apply one
#: standard. Imported by value rather than by reference to keep
#: ``classify`` free of a dependency on ``retrieve``.
MIN_ATTRIBUTED_PACKS = 5

#: Minimum explicit ``unhelpful_item_ids`` citations before an item is
#: admitted for demotion. One citation is too cheap at the measured
#: unhelpful base rate (0.41): a single serving of an ordinary item
#: produces one roughly two times in five. Two independent citations put
#: that at ~0.17 while still admitting 24 of 79 scored items on the live
#: corpus — the gate has to be able to say *yes*, or it is the same
#: constant it replaces pointing the other way.
MIN_UNHELPFUL_CITATIONS = 2

#: Refusal slugs. Machine-readable, stable, and distinct per cause — a
#: reader has to be able to tell "nobody graded this" from "graders
#: disagreed" from "the corpus is too thin to ask".
REFUSED_THIN_CORPUS = "below_min_attributed_packs"
REFUSED_NO_EVIDENCE = "no_evidence_supplied"
REFUSED_NO_UNHELPFUL = "no_unhelpful_citation"
REFUSED_INSUFFICIENT = "insufficient_unhelpful_citations"
REFUSED_CONTESTED = "contested_by_helpful_citations"


class DemotionEvidence(TrellisModel):
    """Per-item citation counts backing (or failing to back) a demotion."""

    item_id: str
    #: Times the item was served in a pack that later received feedback.
    appearances: int = 0
    #: Times a feedback event named it in ``helpful_item_ids``.
    helpful_count: int = 0
    #: Times a feedback event named it in ``unhelpful_item_ids``.
    unhelpful_count: int = 0

    @classmethod
    def from_score_row(cls, row: Mapping[str, Any]) -> DemotionEvidence:
        """Build from an ``EffectivenessReport.item_scores`` row.

        Tolerates a row missing ``unhelpful_count`` — an older report, or
        one produced by a backend that never counted it — by treating the
        absence as zero unhelpful citations, which refuses rather than
        admits. Absent evidence must never be the permissive branch.
        """
        return cls(
            item_id=str(row.get("item_id", "")),
            appearances=int(row.get("appearances", 0) or 0),
            helpful_count=int(row.get("referenced_count", 0) or 0),
            unhelpful_count=int(row.get("unhelpful_count", 0) or 0),
        )


class DemotionDecision(TrellisModel):
    """One candidate's verdict, with the counts that produced it."""

    item_id: str
    admitted: bool
    #: Empty when admitted; one of the ``REFUSED_*`` slugs otherwise.
    reason: str = ""
    appearances: int = 0
    helpful_count: int = 0
    unhelpful_count: int = 0


class NoiseDemotionScreen(TrellisModel):
    """Outcome of screening one batch of proposed noise candidates.

    Reports the coverage it judged on alongside the verdict, so a reader
    never has to infer why a batch came back empty.
    """

    #: Candidates handed to the screen.
    candidates_considered: int = 0
    #: Item ids cleared for demotion — what a caller should actually write.
    admitted: list[str] = Field(default_factory=list)
    #: Every verdict, admitted and refused alike, in candidate order.
    decisions: list[DemotionDecision] = Field(default_factory=list)
    #: Refusal slug → count. Present even when empty so the shape is stable.
    refused_by_reason: dict[str, int] = Field(default_factory=dict)
    #: Packs in the window that carried any per-item verdict.
    attributed_packs: int = 0
    min_attributed_packs: int = MIN_ATTRIBUTED_PACKS
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS
    #: True when the whole batch was refused on corpus coverage.
    suppressed: bool = False
    #: :data:`REFUSED_THIN_CORPUS` when suppressed, else empty.
    suppressed_reason: str = ""

    @property
    def refused_count(self) -> int:
        """Candidates the screen declined to admit."""
        return self.candidates_considered - len(self.admitted)


def _judge(
    evidence: DemotionEvidence | None,
    item_id: str,
    *,
    min_unhelpful_citations: int,
) -> DemotionDecision:
    """Decide one candidate. Pure; the only place the rule is written."""
    if evidence is None:
        return DemotionDecision(
            item_id=item_id, admitted=False, reason=REFUSED_NO_EVIDENCE
        )

    unhelpful = evidence.unhelpful_count
    helpful = evidence.helpful_count

    def verdict(*, admitted: bool, reason: str) -> DemotionDecision:
        return DemotionDecision(
            item_id=item_id,
            admitted=admitted,
            reason=reason,
            appearances=evidence.appearances,
            helpful_count=helpful,
            unhelpful_count=unhelpful,
        )

    if unhelpful <= 0:
        return verdict(admitted=False, reason=REFUSED_NO_UNHELPFUL)
    if unhelpful < min_unhelpful_citations:
        return verdict(admitted=False, reason=REFUSED_INSUFFICIENT)
    if helpful >= unhelpful:
        # Graders disagreed about this item. Disagreement is a reason to
        # leave it alone, not a tiebreak in favour of removal.
        return verdict(admitted=False, reason=REFUSED_CONTESTED)
    return verdict(admitted=True, reason="")


def screen_noise_candidates(
    candidates: Iterable[str],
    evidence: (
        Mapping[str, DemotionEvidence]
        | Iterable[DemotionEvidence | Mapping[str, Any]]
        | None
    ),
    *,
    attributed_packs: int,
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS,
    min_attributed_packs: int = MIN_ATTRIBUTED_PACKS,
) -> NoiseDemotionScreen:
    """Screen proposed noise candidates against citation evidence.

    Args:
        candidates: Proposed item ids, e.g.
            ``EffectivenessReport.noise_candidates``.
        evidence: Either a mapping of ``item_id -> DemotionEvidence`` or
            an iterable of ``item_scores`` rows (which are converted via
            :meth:`DemotionEvidence.from_score_row`). ``None`` means no
            evidence was gathered at all — every candidate is refused.
        attributed_packs: Packs in the analysis window whose feedback
            carried at least one per-item verdict. This is the coverage
            the batch is judged on, and it is *not* the count of packs
            served: a served-but-ungraded pack contributes no evidence.
        min_unhelpful_citations: Override for
            :data:`MIN_UNHELPFUL_CITATIONS`.
        min_attributed_packs: Override for :data:`MIN_ATTRIBUTED_PACKS`.

    Returns:
        A :class:`NoiseDemotionScreen`. ``admitted`` is the list a caller
        should write; ``decisions`` explains every candidate either way.
    """
    ids = list(candidates)

    evidence_by_id: dict[str, DemotionEvidence] = {}
    if isinstance(evidence, Mapping):
        evidence_by_id = dict(evidence)
    elif evidence is not None:
        for row in evidence:
            record = (
                row
                if isinstance(row, DemotionEvidence)
                else DemotionEvidence.from_score_row(row)
            )
            if record.item_id:
                evidence_by_id[record.item_id] = record

    # Corpus coverage first: below the floor there is no population to
    # reason over, so per-item verdicts would be theatre.
    if attributed_packs < min_attributed_packs:
        decisions = [
            DemotionDecision(item_id=i, admitted=False, reason=REFUSED_THIN_CORPUS)
            for i in ids
        ]
        screen = NoiseDemotionScreen(
            candidates_considered=len(ids),
            admitted=[],
            decisions=decisions,
            refused_by_reason={REFUSED_THIN_CORPUS: len(ids)} if ids else {},
            attributed_packs=attributed_packs,
            min_attributed_packs=min_attributed_packs,
            min_unhelpful_citations=min_unhelpful_citations,
            suppressed=True,
            suppressed_reason=REFUSED_THIN_CORPUS,
        )
        logger.info(
            "noise_demotion_suppressed",
            candidates=len(ids),
            attributed_packs=attributed_packs,
            min_attributed_packs=min_attributed_packs,
            reason=REFUSED_THIN_CORPUS,
        )
        return screen

    decisions = [
        _judge(
            evidence_by_id.get(i),
            i,
            min_unhelpful_citations=min_unhelpful_citations,
        )
        for i in ids
    ]

    admitted = [d.item_id for d in decisions if d.admitted]
    refused_by_reason: dict[str, int] = Field(default_factory=dict)
    for d in decisions:
        if not d.admitted:
            refused_by_reason[d.reason] = refused_by_reason.get(d.reason, 0) + 1

    screen = NoiseDemotionScreen(
        candidates_considered=len(ids),
        admitted=admitted,
        decisions=decisions,
        refused_by_reason=refused_by_reason,
        attributed_packs=attributed_packs,
        min_attributed_packs=min_attributed_packs,
        min_unhelpful_citations=min_unhelpful_citations,
    )
    logger.info(
        "noise_demotion_screened",
        candidates=len(ids),
        admitted=len(admitted),
        refused=screen.refused_count,
        refused_by_reason=refused_by_reason,
        attributed_packs=attributed_packs,
    )
    return screen
