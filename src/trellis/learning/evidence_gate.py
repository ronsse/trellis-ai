"""Per-item citation evidence for the learning layer's review queue.

``analyze_learning_observations`` proposes two kinds of candidate from one
rule over ``success_rate``: ``investigate_noise`` and a promote arm. Until
now neither arm read the per-item citations the grader actually supplied —
``helpful_item_ids`` and ``unhelpful_item_ids`` reached the join and were
dropped before scoring. This module is where they are read.

It is the learning-layer instance of the demotion evidence gate
:mod:`trellis.classify.demotion_gate` introduced for the effectiveness
pass (#336), and it inherits that module's governing rule verbatim:

    **Demotion requires evidence of unhelpfulness, never absence of
    evidence of helpfulness.**

Thresholds and refusal slugs are copied **by value**, not imported.
``trellis.learning`` imports ``schemas`` / ``mutate`` / ``ops`` / ``stores``
/ ``meta`` and nothing from ``classify`` or ``retrieve``; ``demotion_gate``
itself copies ``MIN_ATTRIBUTED_PACKS`` from ``retrieve.pack_value`` for the
same reason one layer down. Keep the three in sync by hand.

Measured before shipping (live window, 81 joined observations, 1370
distinct ``(intent_family, item_id)`` cells, 291 proposed candidates)
against a **within-pack permutation null**: each pack's helpful/unhelpful
citation *counts* are held fixed and redrawn uniformly from that pack's own
served items, 2000 draws, repeated at three seeds. That null preserves the
one constraint that matters — a citation can only name an item the pack
actually served — so it asks whether a verdict is attached to the *item* or
merely to *being present in a graded pack*.

===========================================  ======  =========  ========  =======
rule                                          real   null mean         P     lift
===========================================  ======  =========  ========  =======
current proposal (absence of helpful)           265      259.8     0.373    1.02x
this gate (u >= 2 and u > h)                     54       44.5     0.021    1.22x
u >= 3 and u > h                                  4        3.6     0.505    1.11x
===========================================  ======  =========  ========  =======

Two conclusions, and the second is the one worth carrying:

**The gate ships.** It is above chance where the rule it screens is
*literally at* chance, and it cuts the review queue from 265 to 54. The
lift is modest and is stated as modest: with a median 23-item pack naming
32% of its items unhelpful (18 of 81 packs named more than half), random
assignment already reproduces most admissions. A reviewer reading
``admitted`` is reading a better-than-chance shortlist, not a verdict.

**The mirror-image promote gate does not ship, because it was built,
measured and refused.** The obvious symmetry — block a promote candidate
whose citations contradict it, or which nothing ever cited helpful — was
planned, corroborated by a raw count (24 of 26 promote candidates were
never cited helpful; 18 of 26 carry no per-item citation at all), and then
measured: the contradicted block admits 6 against a null mean of 6.2
(95% CI [4, 9], P = 0.698 / 0.698 / 0.702 across three seeds, lift 0.97x).
That is chance, exactly. And a block on *absence* of helpful citation would
commit the error #336 exists to prevent, mirrored: with
``P(cited helpful | served)`` at roughly 0.10, "never cited helpful" is the
expected state of a perfectly good item, so such a rule flags most of the
arm by construction — a constant pointing the other way. The promote arm
therefore carries its citation counts as **evidence a human reads** and is
not gated on them. Do not re-derive this; re-measure it if the numbers
move, and note that the honest null is the within-pack one (a null that
shuffles whole verdict lists between packs destroys the pack↔citation
constraint and will report any rule as wildly significant).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel

logger = structlog.get_logger(__name__)

#: Minimum observations in the window carrying any per-item verdict before
#: *any* candidate is admitted. Copied by value from
#: :data:`trellis.classify.demotion_gate.MIN_ATTRIBUTED_PACKS`.
#:
#: As there, this is a guard against a grading surface going dark (#309),
#: not what makes the gate sound — the live window sits well above it, so
#: it changes nothing today. The per-item rule is the load-bearing half.
MIN_ATTRIBUTED_OBSERVATIONS = 5

#: Minimum explicit ``unhelpful_item_ids`` citations before a proposed
#: noise candidate is admitted. Copied by value from
#: :data:`trellis.classify.demotion_gate.MIN_UNHELPFUL_CITATIONS`.
MIN_UNHELPFUL_CITATIONS = 2

#: Refusal slugs, byte-identical to ``demotion_gate``'s. The two screens
#: answer the same question over different populations; giving them one
#: vocabulary keeps a reader from having to learn which module wrote a
#: refusal before they can read it.
REFUSED_THIN_CORPUS = "below_min_attributed_packs"
REFUSED_NO_EVIDENCE = "no_evidence_supplied"
REFUSED_NO_UNHELPFUL = "no_unhelpful_citation"
REFUSED_INSUFFICIENT = "insufficient_unhelpful_citations"
REFUSED_CONTESTED = "contested_by_helpful_citations"

#: Verdict stamped on an arm this module deliberately does not judge.
#: Distinct from every ``REFUSED_*`` slug: those say "this candidate did
#: not clear the gate", this says "no gate ran". See the module docstring
#: for the measurement behind the promote arm's exemption.
NOT_SCREENED = "not_screened"


class CitationEvidence(TrellisModel):
    """Per-candidate citation counts backing (or failing to back) a verdict."""

    candidate_id: str
    intent_family: str = ""
    item_id: str = ""
    #: Times the item was served in a pack that later received feedback.
    appearances: int = 0
    #: Times a feedback event named it in ``helpful_item_ids``.
    helpful_count: int = 0
    #: Times a feedback event named it in ``unhelpful_item_ids``.
    unhelpful_count: int = 0

    @classmethod
    def from_candidate(cls, candidate: Mapping[str, Any]) -> CitationEvidence:
        """Build from one ``analyze_learning_observations`` candidate.

        Tolerates a candidate whose ``citation_evidence`` block is missing
        — an artifact written before this module existed — by treating the
        absence as zero citations, which **refuses** rather than admits.
        Absent evidence must never be the permissive branch.
        """
        block = candidate.get("citation_evidence")
        if not isinstance(block, Mapping):
            block = {}
        return cls(
            candidate_id=str(candidate.get("candidate_id", "")),
            intent_family=str(candidate.get("intent_family", "") or ""),
            item_id=str(candidate.get("item_id", "") or ""),
            appearances=int(block.get("appearances", 0) or 0),
            helpful_count=int(block.get("helpful_count", 0) or 0),
            unhelpful_count=int(block.get("unhelpful_count", 0) or 0),
        )


class CandidateDecision(TrellisModel):
    """One candidate's verdict, with the counts that produced it."""

    candidate_id: str
    admitted: bool
    #: Empty when admitted; one of the ``REFUSED_*`` slugs otherwise.
    reason: str = ""
    intent_family: str = ""
    item_id: str = ""
    appearances: int = 0
    helpful_count: int = 0
    unhelpful_count: int = 0


class NoiseEvidenceScreen(TrellisModel):
    """Outcome of screening one batch of proposed noise candidates.

    Reports the coverage it judged on alongside the verdict, so a reader
    never has to infer why a batch came back empty.
    """

    #: Candidates handed to the screen.
    candidates_considered: int = 0
    #: Candidate ids cleared for review as noise.
    admitted: list[str] = Field(default_factory=list)
    #: Every verdict, admitted and refused alike, in candidate order.
    decisions: list[CandidateDecision] = Field(default_factory=list)
    #: Refusal slug → count. Present even when empty so the shape is stable.
    refused_by_reason: dict[str, int] = Field(default_factory=dict)
    #: Observations in the window that carried any per-item verdict.
    attributed_observations: int = 0
    min_attributed_observations: int = MIN_ATTRIBUTED_OBSERVATIONS
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS
    #: True when the whole batch was refused on corpus coverage.
    suppressed: bool = False
    #: :data:`REFUSED_THIN_CORPUS` when suppressed, else empty.
    suppressed_reason: str = ""

    @property
    def refused_count(self) -> int:
        """Candidates the screen declined to admit."""
        return self.candidates_considered - len(self.admitted)


def _decision(
    evidence: CitationEvidence | None,
    candidate_id: str,
    *,
    admitted: bool,
    reason: str,
) -> CandidateDecision:
    """Build a decision, stamping whatever counts the evidence carried.

    A decision with no evidence reports zeros, which is what
    :data:`REFUSED_NO_EVIDENCE` says it means. Every decision that *has*
    evidence reports it, including one refused on corpus coverage: the
    counts describe the candidate, and the coverage floor is a statement
    about the window.
    """
    if evidence is None:
        return CandidateDecision(
            candidate_id=candidate_id, admitted=admitted, reason=reason
        )
    return CandidateDecision(
        candidate_id=candidate_id,
        admitted=admitted,
        reason=reason,
        intent_family=evidence.intent_family,
        item_id=evidence.item_id,
        appearances=evidence.appearances,
        helpful_count=evidence.helpful_count,
        unhelpful_count=evidence.unhelpful_count,
    )


def _judge(
    evidence: CitationEvidence | None,
    candidate_id: str,
    *,
    min_unhelpful_citations: int,
) -> CandidateDecision:
    """The pure per-candidate rule. No corpus-level condition here."""
    if evidence is None:
        return CandidateDecision(
            candidate_id=candidate_id, admitted=False, reason=REFUSED_NO_EVIDENCE
        )

    helpful = evidence.helpful_count
    unhelpful = evidence.unhelpful_count

    if unhelpful <= 0:
        return _decision(
            evidence, candidate_id, admitted=False, reason=REFUSED_NO_UNHELPFUL
        )
    if unhelpful < min_unhelpful_citations:
        return _decision(
            evidence, candidate_id, admitted=False, reason=REFUSED_INSUFFICIENT
        )
    if helpful >= unhelpful:
        # Graders disagreed about this candidate. Disagreement is a reason
        # to leave it alone, not a tiebreak in favour of removal.
        return _decision(
            evidence, candidate_id, admitted=False, reason=REFUSED_CONTESTED
        )
    return _decision(evidence, candidate_id, admitted=True, reason="")


def screen_noise_candidates(
    candidates: Iterable[str],
    evidence: Iterable[CitationEvidence] | Mapping[str, CitationEvidence],
    *,
    attributed_observations: int,
    min_unhelpful_citations: int = MIN_UNHELPFUL_CITATIONS,
    min_attributed_observations: int = MIN_ATTRIBUTED_OBSERVATIONS,
) -> NoiseEvidenceScreen:
    """Screen proposed noise candidates against per-item citation evidence.

    Two independent conditions, kept separate because they fail for
    different reasons and a reader needs to know which:

    1. **Corpus coverage.** Fewer than ``min_attributed_observations``
       observations carrying any per-item verdict suppresses the whole
       batch. Guards against a grading surface going dark; does not by
       itself make a verdict sound.
    2. **Per-candidate evidence.** At least ``min_unhelpful_citations``
       unhelpful citations, and strictly more unhelpful than helpful.

    The screen is a *decision* and is deliberately separate from any
    write: it narrows which candidates a reviewer sees flagged, and
    changes nothing about what happens to one that is.

    Args:
        candidates: Proposed candidate ids, in the order to report them.
        evidence: ``CitationEvidence`` records, as an iterable or as a
            mapping already keyed by candidate id.
        attributed_observations: Observations in the window that carried
            any per-item verdict.
        min_unhelpful_citations: Per-candidate evidence floor.
        min_attributed_observations: Corpus coverage floor.

    Returns:
        A :class:`NoiseEvidenceScreen` carrying every verdict, the
        admitted subset, and the coverage it judged on.
    """
    ids = [str(cid).strip() for cid in candidates if str(cid).strip()]
    if isinstance(evidence, Mapping):
        evidence_by_id = dict(evidence)
    else:
        evidence_by_id = {record.candidate_id: record for record in evidence}

    if attributed_observations < min_attributed_observations:
        decisions = [
            _decision(
                evidence_by_id.get(cid),
                cid,
                admitted=False,
                reason=REFUSED_THIN_CORPUS,
            )
            for cid in ids
        ]
        logger.info(
            "learning_noise_screen_suppressed",
            candidates=len(ids),
            attributed_observations=attributed_observations,
            min_attributed_observations=min_attributed_observations,
        )
        return NoiseEvidenceScreen(
            candidates_considered=len(ids),
            admitted=[],
            decisions=decisions,
            refused_by_reason={REFUSED_THIN_CORPUS: len(ids)} if ids else {},
            attributed_observations=attributed_observations,
            min_attributed_observations=min_attributed_observations,
            min_unhelpful_citations=min_unhelpful_citations,
            suppressed=True,
            suppressed_reason=REFUSED_THIN_CORPUS,
        )

    decisions = [
        _judge(
            evidence_by_id.get(cid),
            cid,
            min_unhelpful_citations=min_unhelpful_citations,
        )
        for cid in ids
    ]
    admitted = [decision.candidate_id for decision in decisions if decision.admitted]
    refused_by_reason: dict[str, int] = {}
    for decision in decisions:
        if not decision.admitted:
            refused_by_reason[decision.reason] = (
                refused_by_reason.get(decision.reason, 0) + 1
            )

    logger.info(
        "learning_noise_screen_completed",
        candidates=len(ids),
        admitted=len(admitted),
        attributed_observations=attributed_observations,
    )
    return NoiseEvidenceScreen(
        candidates_considered=len(ids),
        admitted=admitted,
        decisions=decisions,
        refused_by_reason=refused_by_reason,
        attributed_observations=attributed_observations,
        min_attributed_observations=min_attributed_observations,
        min_unhelpful_citations=min_unhelpful_citations,
    )


__all__ = [
    "MIN_ATTRIBUTED_OBSERVATIONS",
    "MIN_UNHELPFUL_CITATIONS",
    "NOT_SCREENED",
    "REFUSED_CONTESTED",
    "REFUSED_INSUFFICIENT",
    "REFUSED_NO_EVIDENCE",
    "REFUSED_NO_UNHELPFUL",
    "REFUSED_THIN_CORPUS",
    "CandidateDecision",
    "CitationEvidence",
    "NoiseEvidenceScreen",
    "screen_noise_candidates",
]
