"""Algorithmic correctness tests for :mod:`trellis.retrieve.rerankers.mmr`.

Hand-computed MMR expected values for small, deterministic inputs.

Reference formula (Carbonell & Goldstein, 1998):

    MMR(i) = lambda * Sim_query(i) - (1 - lambda) * max Sim_doc(i, sel)

This implementation uses the *normalised* relevance score in [0, 1]
as ``Sim_query`` (linear min-max over the candidate set) and Jaccard
similarity over word n-gram shingles for ``Sim_doc``.
"""

from __future__ import annotations

import pytest

from trellis.retrieve.rerankers.mmr import MMRReranker
from trellis.schemas.pack import PackItem


def _item(item_id: str, score: float, excerpt: str) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=excerpt,
        relevance_score=score,
        strategy_source="keyword",
    )


# --------------------------------------------------------------------------
# Boundary inputs
# --------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    assert MMRReranker().rerank("query", []) == []


def test_single_item_returns_single_item_with_full_score() -> None:
    [out] = MMRReranker().rerank("query", [_item("only", 0.42, "alpha beta gamma")])
    assert out.item_id == "only"
    # n=1 → fused_score = (1-0)/1 = 1.0
    assert out.relevance_score == pytest.approx(1.0)
    assert out.score_breakdown["original_score"] == pytest.approx(0.42)
    assert out.score_breakdown["mmr_rank"] == 1


# --------------------------------------------------------------------------
# Lambda boundary cases — pure relevance and pure diversity
# --------------------------------------------------------------------------


def test_lambda_one_pure_relevance_orders_by_score() -> None:
    """lambda=1 collapses to pure relevance: highest score first."""
    candidates = [
        _item("low", 0.1, "alpha beta gamma"),
        _item("high", 0.9, "alpha beta gamma"),  # identical text — irrelevant at λ=1
        _item("mid", 0.5, "alpha beta gamma"),
    ]
    result = MMRReranker(lambda_param=1.0, shingle_size=2).rerank("q", candidates)
    assert [r.item_id for r in result] == ["high", "mid", "low"]


def test_lambda_zero_pure_diversity_keeps_all_items() -> None:
    """lambda=0 ignores relevance entirely.

    The order between zero-similarity items at iteration 0 is *not*
    specified (set iteration), so we only assert containment + length.
    The crucial property is: every candidate appears once.
    """
    candidates = [
        _item("a", 0.9, "alpha one two"),
        _item("b", 0.1, "beta three four"),
        _item("c", 0.5, "gamma five six"),
    ]
    result = MMRReranker(lambda_param=0.0, shingle_size=2).rerank("q", candidates)
    assert {r.item_id for r in result} == {"a", "b", "c"}
    assert len(result) == 3


# --------------------------------------------------------------------------
# Mixed lambda — hand-computed re-ranking
# --------------------------------------------------------------------------


def test_mixed_lambda_demotes_near_duplicate_below_diverse_item() -> None:
    """Three-item case with hand-computed MMR.

    Setup (shingle_size=2):
      a: score=1.0, "the quick fox"        → shingles {the quick, quick fox}
      b: score=0.9, "the quick fox runs"   → shingles {the quick, quick fox, fox runs}
      c: score=0.5, "machine learning models"
                                           → shingles {machine learning,
                                              learning models}

    Min-max normalised relevance: norm(a)=1.0, norm(b)=0.8, norm(c)=0.0.

    lambda = 0.3:
      Step 1 (none selected, max_sim=0):
          MMR(a) = 0.3*1.0 = 0.30
          MMR(b) = 0.3*0.8 = 0.24
          MMR(c) = 0.3*0.0 = 0.00
        → pick a.

      Step 2 (a selected, shingles_a = {the quick, quick fox}):
          jaccard(a,b) = 2/3 (intersection=2, union=3)
          jaccard(a,c) = 0/4 = 0
          MMR(b) = 0.3*0.8 - 0.7*(2/3) = 0.24 - 0.4666... = -0.2267
          MMR(c) = 0.3*0.0 - 0.7*0     = 0.00
        → pick c (less negative — c wins).

      Step 3: pick b.

    Final ordering: [a, c, b].

    Compare to lambda=1.0 ordering [a, b, c] — diversity has flipped
    the bottom two.
    """
    candidates = [
        _item("a", 1.0, "the quick fox"),
        _item("b", 0.9, "the quick fox runs"),
        _item("c", 0.5, "machine learning models"),
    ]

    pure_rel = MMRReranker(lambda_param=1.0, shingle_size=2).rerank("q", candidates)
    assert [r.item_id for r in pure_rel] == ["a", "b", "c"]

    diverse = MMRReranker(lambda_param=0.3, shingle_size=2).rerank("q", candidates)
    assert [r.item_id for r in diverse] == ["a", "c", "b"]


def test_mixed_lambda_keeps_relevance_when_diversity_disagrees_marginally() -> None:
    """λ=0.7 (default-ish) on the same fixture should still rank a→b→c.

    Step 1: MMR(a)=0.7*1=0.7, MMR(b)=0.7*0.8=0.56, MMR(c)=0 → pick a.
    Step 2:
      jaccard(a,b)=2/3, jaccard(a,c)=0
      MMR(b) = 0.7*0.8 - 0.3*(2/3) = 0.56 - 0.20 = 0.36
      MMR(c) = 0.7*0.0 - 0.3*0     = 0.00
      → pick b (still positive, beats c).
    Step 3: pick c.

    Final: [a, b, c].
    """
    candidates = [
        _item("a", 1.0, "the quick fox"),
        _item("b", 0.9, "the quick fox runs"),
        _item("c", 0.5, "machine learning models"),
    ]
    result = MMRReranker(lambda_param=0.7, shingle_size=2).rerank("q", candidates)
    assert [r.item_id for r in result] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# Output invariants
# --------------------------------------------------------------------------


def test_relevance_scores_are_descending_after_rerank() -> None:
    """The MMR reranker assigns a monotone decreasing fused score."""
    candidates = [
        _item("a", 0.9, "first document content here"),
        _item("b", 0.7, "second different document content"),
        _item("c", 0.5, "third unrelated subject matter"),
    ]
    result = MMRReranker(lambda_param=0.7, shingle_size=2).rerank("q", candidates)
    scores = [r.relevance_score for r in result]
    assert scores == sorted(scores, reverse=True)


def test_score_breakdown_includes_mmr_rank_starting_at_one() -> None:
    candidates = [
        _item("a", 0.9, "alpha bravo charlie"),
        _item("b", 0.5, "delta echo foxtrot"),
    ]
    result = MMRReranker(lambda_param=1.0, shingle_size=2).rerank("q", candidates)
    assert result[0].score_breakdown["mmr_rank"] == 1
    assert result[1].score_breakdown["mmr_rank"] == 2
    # Original scores preserved in breakdown.
    assert result[0].score_breakdown["original_score"] == pytest.approx(0.9)
    assert result[1].score_breakdown["original_score"] == pytest.approx(0.5)


def test_dedup_by_item_id() -> None:
    """Duplicate item_ids in input collapse to one in output (first wins)."""
    candidates = [
        _item("a", 0.9, "first version of a"),
        _item("a", 0.5, "second version of a"),  # duplicate id
        _item("b", 0.7, "wholly different content"),
    ]
    result = MMRReranker(lambda_param=1.0, shingle_size=2).rerank("q", candidates)
    ids = [r.item_id for r in result]
    assert sorted(ids) == ["a", "b"]
    assert len(ids) == 2
