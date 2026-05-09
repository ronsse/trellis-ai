"""Algorithmic correctness tests for :mod:`trellis.retrieve.rerankers.rrf`.

Hand-computed RRF expected values for small, deterministic inputs.

Reference formula (Cormack, Clarke & Butt, 2009):

    RRF_score(item) = sum over all lists L containing item of:
                          1.0 / (k + rank_in_L)

where ``rank_in_L`` is 1-indexed and ``k`` is the smoothing constant.
The implementation uses the standard ``DEFAULT_RRF_K = 60`` from the
original paper.
"""

from __future__ import annotations

import pytest

from trellis.retrieve.rerankers.rrf import DEFAULT_RRF_K, RRFReranker
from trellis.schemas.pack import PackItem


def _item(item_id: str, score: float, strategy: str) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=f"Excerpt for {item_id}",
        relevance_score=score,
        strategy_source=strategy,
    )


# --------------------------------------------------------------------------
# Constants and boundary inputs
# --------------------------------------------------------------------------


def test_default_k_matches_canonical_rrf_paper() -> None:
    assert DEFAULT_RRF_K == 60


def test_empty_input_returns_empty() -> None:
    assert RRFReranker().rerank("query", []) == []


def test_single_item_single_strategy() -> None:
    result = RRFReranker().rerank("query", [_item("a", 0.9, "keyword")])
    assert len(result) == 1
    # rank=1 in the only list → 1 / (60 + 1) = 1/61
    assert result[0].relevance_score == pytest.approx(1 / 61)
    assert result[0].score_breakdown["rrf_keyword"] == pytest.approx(1 / 61)
    assert result[0].score_breakdown["rrf_total"] == pytest.approx(1 / 61)


# --------------------------------------------------------------------------
# Two ranked lists with one overlapping item — hand-computed
# --------------------------------------------------------------------------


def test_two_lists_with_overlap() -> None:
    """Hand-computed RRF (k=60).

    keyword list  (sorted desc by relevance_score):
      rank 1: A  → contribution 1/(60+1) = 1/61
      rank 2: B  → contribution 1/(60+2) = 1/62

    semantic list:
      rank 1: A  → contribution 1/(60+1) = 1/61
      rank 2: C  → contribution 1/(60+2) = 1/62

    Fused scores:
      A = 1/61 + 1/61 = 2/61  ≈ 0.032787
      B = 1/62           ≈ 0.016129
      C = 1/62           ≈ 0.016129

    Expected order: A first; B and C tied — output preserves the
    de-duped first-seen iteration order from the input list when
    Python's sort is stable.
    """
    candidates = [
        _item("A", 0.9, "keyword"),
        _item("B", 0.7, "keyword"),
        _item("A", 0.6, "semantic"),  # duplicate id, different strategy
        _item("C", 0.5, "semantic"),
    ]
    result = RRFReranker().rerank("query", candidates)

    # No duplicates in output.
    ids = [r.item_id for r in result]
    assert ids[0] == "A"
    assert set(ids) == {"A", "B", "C"}
    assert len(ids) == 3

    by_id = {r.item_id: r for r in result}
    assert by_id["A"].relevance_score == pytest.approx(2 / 61)
    assert by_id["B"].relevance_score == pytest.approx(1 / 62)
    assert by_id["C"].relevance_score == pytest.approx(1 / 62)

    # Per-strategy contributions in the breakdown.
    assert by_id["A"].score_breakdown["rrf_keyword"] == pytest.approx(1 / 61)
    assert by_id["A"].score_breakdown["rrf_semantic"] == pytest.approx(1 / 61)
    assert by_id["A"].score_breakdown["rrf_total"] == pytest.approx(2 / 61)
    # original_score is whichever PackItem was emitted first for A.
    assert by_id["A"].score_breakdown["original_score"] == pytest.approx(0.9)


# --------------------------------------------------------------------------
# Three ranked lists — hand-computed
# --------------------------------------------------------------------------


def test_three_lists_fusion() -> None:
    """Hand-computed three-list RRF.

    keyword:  A (0.9, rank 1), B (0.5, rank 2)
    semantic: A (0.8, rank 1), C (0.7, rank 2)
    graph:    B (0.9, rank 1), C (0.5, rank 2)

    Per-item RRF (k=60):
      A = 1/61 + 1/61            = 2/61    ≈ 0.0327869
      B = 1/62 + 1/61            = 123/3782 ≈ 0.0325224
      C = 1/62 + 1/62            = 2/62    ≈ 0.0322581

    Order: A > B > C.
    """
    candidates = [
        _item("A", 0.9, "keyword"),
        _item("B", 0.5, "keyword"),
        _item("A", 0.8, "semantic"),
        _item("C", 0.7, "semantic"),
        _item("B", 0.9, "graph"),
        _item("C", 0.5, "graph"),
    ]
    result = RRFReranker().rerank("query", candidates)
    assert [r.item_id for r in result] == ["A", "B", "C"]

    by_id = {r.item_id: r for r in result}
    assert by_id["A"].relevance_score == pytest.approx(2 / 61)
    assert by_id["B"].relevance_score == pytest.approx(1 / 62 + 1 / 61)
    assert by_id["C"].relevance_score == pytest.approx(2 / 62)

    # All three strategies appear in A's breakdown only where it was found.
    a_breakdown = by_id["A"].score_breakdown
    assert "rrf_keyword" in a_breakdown
    assert "rrf_semantic" in a_breakdown
    assert "rrf_graph" not in a_breakdown  # A isn't in the graph list

    b_breakdown = by_id["B"].score_breakdown
    assert "rrf_keyword" in b_breakdown
    assert "rrf_graph" in b_breakdown
    assert "rrf_semantic" not in b_breakdown


# --------------------------------------------------------------------------
# Single-list and unknown-strategy edge cases
# --------------------------------------------------------------------------


def test_item_in_only_one_list() -> None:
    """Item appearing in exactly one list scores 1/(k+rank) for that list."""
    candidates = [_item("only_kw", 0.9, "keyword")]
    result = RRFReranker().rerank("query", candidates)
    assert result[0].relevance_score == pytest.approx(1 / 61)


def test_explicit_k_value_changes_score_predictably() -> None:
    """With explicit k=10, top item scores 1/11 (vs 1/61 with default)."""
    candidates = [_item("a", 0.9, "keyword")]

    default_result = RRFReranker().rerank("q", candidates)
    custom_result = RRFReranker(k=10).rerank("q", candidates)

    assert default_result[0].relevance_score == pytest.approx(1 / 61)
    assert custom_result[0].relevance_score == pytest.approx(1 / 11)


def test_items_without_strategy_source_share_unknown_bucket() -> None:
    """``strategy_source=None`` items go into a single ``_unknown`` list.

    With two None items at ranks 1 and 2 in that bucket:
      a → 1/61
      b → 1/62
    """
    candidates = [
        PackItem(
            item_id="a",
            item_type="document",
            excerpt="text a",
            relevance_score=0.9,
            strategy_source=None,
        ),
        PackItem(
            item_id="b",
            item_type="document",
            excerpt="text b",
            relevance_score=0.7,
            strategy_source=None,
        ),
    ]
    result = RRFReranker().rerank("query", candidates)
    assert [r.item_id for r in result] == ["a", "b"]
    assert result[0].relevance_score == pytest.approx(1 / 61)
    assert result[1].relevance_score == pytest.approx(1 / 62)
    # Unknown bucket key.
    assert "rrf__unknown" in result[0].score_breakdown


# --------------------------------------------------------------------------
# Output invariants
# --------------------------------------------------------------------------


def test_relevance_scores_are_descending_after_rerank() -> None:
    candidates = [
        _item("a", 0.9, "keyword"),
        _item("b", 0.7, "keyword"),
        _item("c", 0.5, "keyword"),
    ]
    result = RRFReranker().rerank("query", candidates)
    scores = [r.relevance_score for r in result]
    assert scores == sorted(scores, reverse=True)


def test_breakdown_total_equals_sum_of_strategy_contributions() -> None:
    candidates = [
        _item("X", 0.9, "keyword"),
        _item("X", 0.8, "semantic"),
        _item("X", 0.7, "graph"),
    ]
    result = RRFReranker().rerank("query", candidates)
    [item] = result
    bd = item.score_breakdown
    contributions = [
        v for k, v in bd.items() if k.startswith("rrf_") and k != "rrf_total"
    ]
    assert bd["rrf_total"] == pytest.approx(sum(contributions))
