"""Tests for Reciprocal Rank Fusion reranker."""

from trellis.retrieve.rerankers.rrf import RRFReranker
from trellis.schemas.pack import PackItem


def _item(item_id: str, score: float, strategy: str, excerpt: str = "") -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=excerpt or f"Content for {item_id}",
        relevance_score=score,
        strategy_source=strategy,
    )


class TestRRFReranker:
    def test_empty_candidates(self):
        reranker = RRFReranker()
        assert reranker.rerank("query", []) == []

    def test_name(self):
        assert RRFReranker().name == "rrf"

    def test_single_strategy_preserves_order(self):
        candidates = [
            _item("a", 0.9, "keyword"),
            _item("b", 0.7, "keyword"),
            _item("c", 0.5, "keyword"),
        ]
        result = RRFReranker().rerank("query", candidates)
        assert [r.item_id for r in result] == ["a", "b", "c"]

    def test_multi_strategy_fusion(self):
        """An item appearing in both keyword and semantic lists should score higher."""
        candidates = [
            _item("shared", 0.9, "keyword"),
            _item("keyword_only", 0.8, "keyword"),
            _item("shared", 0.6, "semantic"),  # duplicate, different strategy
            _item("semantic_only", 0.7, "semantic"),
        ]
        result = RRFReranker().rerank("query", candidates)

        # "shared" should rank first because it gets contributions from both lists
        assert result[0].item_id == "shared"
        # Score breakdown should have contributions from both strategies
        assert "rrf_keyword" in result[0].score_breakdown
        assert "rrf_semantic" in result[0].score_breakdown
        assert "rrf_total" in result[0].score_breakdown

    def test_no_duplicate_items_in_output(self):
        candidates = [
            _item("a", 0.9, "keyword"),
            _item("a", 0.8, "semantic"),
            _item("b", 0.7, "keyword"),
        ]
        result = RRFReranker().rerank("query", candidates)
        ids = [r.item_id for r in result]
        assert len(ids) == len(set(ids))

    def test_original_score_preserved_in_breakdown(self):
        candidates = [_item("a", 0.42, "keyword")]
        result = RRFReranker().rerank("query", candidates)
        assert result[0].score_breakdown["original_score"] == 0.42

    def test_custom_k_parameter(self):
        candidates = [
            _item("a", 0.9, "keyword"),
            _item("b", 0.8, "keyword"),
        ]
        # With very low k, rank differences matter more
        result_low_k = RRFReranker(k=1).rerank("query", candidates)
        result_high_k = RRFReranker(k=1000).rerank("query", candidates)
        # Both should preserve order
        assert result_low_k[0].item_id == "a"
        assert result_high_k[0].item_id == "a"
        # But score gap should be larger with low k
        gap_low = result_low_k[0].relevance_score - result_low_k[1].relevance_score
        gap_high = result_high_k[0].relevance_score - result_high_k[1].relevance_score
        assert gap_low > gap_high

    def test_items_without_strategy_source(self):
        """Items with no strategy_source go into the '_unknown' bucket."""
        candidates = [
            _item("a", 0.9, "keyword"),
            PackItem(
                item_id="b",
                item_type="document",
                excerpt="test",
                relevance_score=0.8,
                strategy_source=None,
            ),
        ]
        result = RRFReranker().rerank("query", candidates)
        assert len(result) == 2
