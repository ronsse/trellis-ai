"""Unit tests for trellis.learning.evidence_gate.

The gate's whole job is to admit *some* candidates and refuse others for
distinguishable reasons, so every branch is asserted by its own slug. Two
of them — ``contested_by_helpful_citations`` and ``below_min_attributed_packs``
— are never exercised by the live corpus (measured: 0 contested, 75
attributed observations against a floor of 5), so if they are not covered
here they are covered nowhere.
"""

from __future__ import annotations

from trellis.learning.evidence_gate import (
    MIN_ATTRIBUTED_OBSERVATIONS,
    MIN_UNHELPFUL_CITATIONS,
    REFUSED_CONTESTED,
    REFUSED_INSUFFICIENT,
    REFUSED_NO_EVIDENCE,
    REFUSED_NO_UNHELPFUL,
    REFUSED_THIN_CORPUS,
    CitationEvidence,
    screen_noise_candidates,
)

_AMPLE = MIN_ATTRIBUTED_OBSERVATIONS + 10


def _ev(cid: str, *, helpful: int = 0, unhelpful: int = 0, served: int = 5):
    return CitationEvidence(
        candidate_id=cid,
        intent_family="fam",
        item_id=f"item-{cid}",
        appearances=served,
        helpful_count=helpful,
        unhelpful_count=unhelpful,
    )


class TestPerCandidateRule:
    def test_admits_repeated_unhelpful_with_no_helpful(self) -> None:
        screen = screen_noise_candidates(
            ["c1"], [_ev("c1", unhelpful=2)], attributed_observations=_AMPLE
        )
        assert screen.admitted == ["c1"]
        assert screen.decisions[0].reason == ""
        assert screen.refused_by_reason == {}
        assert screen.refused_count == 0

    def test_refuses_absence_of_unhelpful_citation(self) -> None:
        """The rule #336 exists to enforce: absence of evidence is not evidence."""
        screen = screen_noise_candidates(
            ["c1"], [_ev("c1", helpful=0, unhelpful=0)], attributed_observations=_AMPLE
        )
        assert screen.admitted == []
        assert screen.decisions[0].reason == REFUSED_NO_UNHELPFUL

    def test_refuses_a_single_unhelpful_citation(self) -> None:
        screen = screen_noise_candidates(
            ["c1"], [_ev("c1", unhelpful=1)], attributed_observations=_AMPLE
        )
        assert screen.decisions[0].reason == REFUSED_INSUFFICIENT

    def test_refuses_when_helpful_ties_unhelpful(self) -> None:
        """Equality refuses. A tie is disagreement, not a tiebreak for removal."""
        screen = screen_noise_candidates(
            ["c1"], [_ev("c1", helpful=2, unhelpful=2)], attributed_observations=_AMPLE
        )
        assert screen.admitted == []
        assert screen.decisions[0].reason == REFUSED_CONTESTED

    def test_refuses_when_helpful_outweighs_unhelpful(self) -> None:
        screen = screen_noise_candidates(
            ["c1"], [_ev("c1", helpful=5, unhelpful=2)], attributed_observations=_AMPLE
        )
        assert screen.decisions[0].reason == REFUSED_CONTESTED

    def test_missing_evidence_refuses_rather_than_admits(self) -> None:
        screen = screen_noise_candidates(["c1"], [], attributed_observations=_AMPLE)
        assert screen.admitted == []
        assert screen.decisions[0].reason == REFUSED_NO_EVIDENCE
        assert screen.decisions[0].unhelpful_count == 0

    def test_threshold_is_honoured_when_raised(self) -> None:
        """The floor is a parameter, not a constant the rule ignores."""
        screen = screen_noise_candidates(
            ["c1"],
            [_ev("c1", unhelpful=2)],
            attributed_observations=_AMPLE,
            min_unhelpful_citations=3,
        )
        assert screen.decisions[0].reason == REFUSED_INSUFFICIENT
        assert screen.min_unhelpful_citations == 3


class TestCorpusCoverage:
    def test_thin_corpus_suppresses_every_candidate(self) -> None:
        ids = ["c1", "c2"]
        evidence = [_ev("c1", unhelpful=9), _ev("c2", unhelpful=9)]
        screen = screen_noise_candidates(
            ids, evidence, attributed_observations=MIN_ATTRIBUTED_OBSERVATIONS - 1
        )
        assert screen.suppressed is True
        assert screen.suppressed_reason == REFUSED_THIN_CORPUS
        assert screen.admitted == []
        assert screen.refused_by_reason == {REFUSED_THIN_CORPUS: 2}
        # Counts still reported: the coverage floor is a statement about
        # the window, not about the candidate.
        assert [d.unhelpful_count for d in screen.decisions] == [9, 9]

    def test_exactly_at_the_floor_is_not_suppressed(self) -> None:
        screen = screen_noise_candidates(
            ["c1"],
            [_ev("c1", unhelpful=2)],
            attributed_observations=MIN_ATTRIBUTED_OBSERVATIONS,
        )
        assert screen.suppressed is False
        assert screen.admitted == ["c1"]

    def test_empty_batch_reports_a_stable_shape(self) -> None:
        screen = screen_noise_candidates([], [], attributed_observations=0)
        assert screen.suppressed is True
        assert screen.candidates_considered == 0
        assert screen.refused_by_reason == {}
        assert screen.refused_count == 0


class TestScreenReporting:
    def test_every_candidate_gets_a_decision_in_order(self) -> None:
        ids = ["c1", "c2", "c3"]
        evidence = [
            _ev("c1", unhelpful=3),
            _ev("c2", unhelpful=1),
            _ev("c3", helpful=4, unhelpful=4),
        ]
        screen = screen_noise_candidates(ids, evidence, attributed_observations=_AMPLE)
        assert [d.candidate_id for d in screen.decisions] == ids
        assert screen.candidates_considered == 3
        assert screen.admitted == ["c1"]
        assert screen.refused_count == 2
        assert screen.refused_by_reason == {
            REFUSED_INSUFFICIENT: 1,
            REFUSED_CONTESTED: 1,
        }

    def test_evidence_may_be_supplied_as_a_mapping(self) -> None:
        screen = screen_noise_candidates(
            ["c1"],
            {"c1": _ev("c1", unhelpful=4)},
            attributed_observations=_AMPLE,
        )
        assert screen.admitted == ["c1"]

    def test_decision_carries_the_counts_that_produced_it(self) -> None:
        screen = screen_noise_candidates(
            ["c1"],
            [_ev("c1", helpful=1, unhelpful=3, served=7)],
            attributed_observations=_AMPLE,
        )
        decision = screen.decisions[0]
        assert (decision.helpful_count, decision.unhelpful_count) == (1, 3)
        assert decision.appearances == 7
        assert decision.item_id == "item-c1"
        assert decision.intent_family == "fam"


class TestCitationEvidenceFromCandidate:
    def test_reads_the_candidate_block(self) -> None:
        evidence = CitationEvidence.from_candidate(
            {
                "candidate_id": "c1",
                "intent_family": "fam",
                "item_id": "i1",
                "citation_evidence": {
                    "appearances": 4,
                    "helpful_count": 1,
                    "unhelpful_count": 3,
                },
            }
        )
        assert (evidence.helpful_count, evidence.unhelpful_count) == (1, 3)
        assert evidence.appearances == 4

    def test_a_candidate_with_no_evidence_block_refuses(self) -> None:
        """An artifact written before this module existed must not admit."""
        evidence = CitationEvidence.from_candidate(
            {"candidate_id": "c1", "item_id": "i1"}
        )
        assert evidence.unhelpful_count == 0
        screen = screen_noise_candidates(
            ["c1"], [evidence], attributed_observations=_AMPLE
        )
        assert screen.admitted == []
        assert screen.decisions[0].reason == REFUSED_NO_UNHELPFUL


class TestPortedConstants:
    def test_thresholds_match_the_classify_gate_they_were_copied_from(self) -> None:
        """Copied by value, so drift is only catchable by asserting it.

        ``trellis.learning`` must not import ``trellis.classify`` (see the
        module docstring), so this is the only thing standing between the
        two gates and a silent divergence.
        """
        from trellis.classify import demotion_gate

        assert MIN_UNHELPFUL_CITATIONS == demotion_gate.MIN_UNHELPFUL_CITATIONS
        assert MIN_ATTRIBUTED_OBSERVATIONS == demotion_gate.MIN_ATTRIBUTED_PACKS
        assert REFUSED_THIN_CORPUS == demotion_gate.REFUSED_THIN_CORPUS
        assert REFUSED_NO_EVIDENCE == demotion_gate.REFUSED_NO_EVIDENCE
        assert REFUSED_NO_UNHELPFUL == demotion_gate.REFUSED_NO_UNHELPFUL
        assert REFUSED_INSUFFICIENT == demotion_gate.REFUSED_INSUFFICIENT
        assert REFUSED_CONTESTED == demotion_gate.REFUSED_CONTESTED

    def test_learning_does_not_import_classify_at_module_scope(self) -> None:
        """The copy exists to keep this edge out of the import graph."""
        import ast
        import pathlib

        source = pathlib.Path(
            trellis_learning_path := __import__(
                "trellis.learning.evidence_gate", fromlist=["__file__"]
            ).__file__
        ).read_text()
        assert trellis_learning_path
        imported = {
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(
            module.startswith(("trellis.classify", "trellis.retrieve"))
            for module in imported
        )
