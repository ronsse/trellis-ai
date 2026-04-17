"""Tests for Maximal Marginal Relevance reranker."""

from trellis.retrieve.rerankers.mmr import MMRReranker, _jaccard, _word_shingles
from trellis.schemas.pack import PackItem


def _item(item_id: str, score: float, excerpt: str) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=excerpt,
        relevance_score=score,
        strategy_source="keyword",
    )


class TestWordShingles:
    def test_normal_text(self):
        shingles = _word_shingles("the quick brown fox", n=2)
        assert "the quick" in shingles
        assert "quick brown" in shingles
        assert "brown fox" in shingles

    def test_short_text(self):
        shingles = _word_shingles("hello", n=3)
        assert shingles == {"hello"}

    def test_empty_text(self):
        assert _word_shingles("", n=3) == set()


class TestJaccard:
    def test_identical(self):
        s = {"a", "b", "c"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint(self):
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self):
        assert _jaccard({"a", "b"}, {"b", "c"}) == 1.0 / 3.0

    def test_empty(self):
        assert _jaccard(set(), {"a"}) == 0.0


class TestMMRReranker:
    def test_empty_candidates(self):
        reranker = MMRReranker()
        assert reranker.rerank("query", []) == []

    def test_name(self):
        assert MMRReranker().name == "mmr"

    def test_preserves_all_items(self):
        candidates = [
            _item("a", 0.9, "the quick brown fox"),
            _item("b", 0.7, "a completely different topic"),
            _item("c", 0.5, "yet another unrelated subject"),
        ]
        result = MMRReranker().rerank("query", candidates)
        assert len(result) == 3
        assert {r.item_id for r in result} == {"a", "b", "c"}

    def test_diversity_demotion(self):
        """Nearly identical excerpts should be separated by diverse items."""
        candidates = [
            _item("a", 0.9, "the quick brown fox jumps over the lazy dog"),
            _item(
                "b", 0.89, "the quick brown fox jumps over the lazy cat"
            ),  # near-dup, close score
            _item(
                "c", 0.88, "machine learning algorithms for data processing"
            ),  # diverse, close score
        ]
        result = MMRReranker(lambda_param=0.3).rerank("query", candidates)
        # With lambda=0.3 (diversity-heavy), "c" should be promoted above "b"
        # because "b" is very similar to "a" which is already selected.
        ids = [r.item_id for r in result]
        assert ids[0] == "a"  # highest relevance, selected first
        # "c" should come before "b" due to diversity
        assert ids.index("c") < ids.index("b")

    def test_pure_relevance_mode(self):
        """lambda=1.0 should behave like pure relevance ordering."""
        candidates = [
            _item("a", 0.9, "text one"),
            _item("b", 0.7, "text two"),
            _item("c", 0.5, "text three"),
        ]
        result = MMRReranker(lambda_param=1.0).rerank("query", candidates)
        assert [r.item_id for r in result] == ["a", "b", "c"]

    def test_score_breakdown_present(self):
        candidates = [_item("a", 0.9, "some content here")]
        result = MMRReranker().rerank("query", candidates)
        assert "mmr_score" in result[0].score_breakdown
        assert "original_score" in result[0].score_breakdown
        assert "mmr_rank" in result[0].score_breakdown

    def test_scores_descend(self):
        candidates = [
            _item("a", 0.9, "first document content"),
            _item("b", 0.7, "second document content different"),
            _item("c", 0.5, "third document content also different"),
        ]
        result = MMRReranker().rerank("query", candidates)
        scores = [r.relevance_score for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_single_item(self):
        candidates = [_item("a", 0.9, "only item")]
        result = MMRReranker().rerank("query", candidates)
        assert len(result) == 1
        assert result[0].item_id == "a"
