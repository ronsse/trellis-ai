"""Tests for search strategies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.strategies import (
    RECENCY_FLOOR,
    GraphSearch,
    KeywordSearch,
    SemanticSearch,
    _apply_importance,
    _apply_recency_decay,
)


class TestApplyImportance:
    def test_no_importance(self) -> None:
        assert _apply_importance(1.0, {}) == 1.0

    def test_with_importance(self) -> None:
        assert _apply_importance(1.0, {"auto_importance": 0.5}) == 1.5

    def test_max_importance(self) -> None:
        assert _apply_importance(1.0, {"auto_importance": 1.0}) == 2.0

    def test_clamps_over_one(self) -> None:
        assert _apply_importance(1.0, {"auto_importance": 2.0}) == 2.0

    def test_clamps_negative(self) -> None:
        assert _apply_importance(1.0, {"auto_importance": -0.5}) == 1.0


class TestApplyRecencyDecay:
    _NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)

    def test_no_timestamp_is_noop(self) -> None:
        assert _apply_recency_decay(1.0, None, now=self._NOW) == 1.0
        assert _apply_recency_decay(1.0, "", now=self._NOW) == 1.0

    def test_unparseable_timestamp_is_noop(self) -> None:
        assert _apply_recency_decay(1.0, "not-a-date", now=self._NOW) == 1.0

    def test_fresh_item_no_decay(self) -> None:
        # Same instant — decay = 1.0, so score unchanged.
        assert _apply_recency_decay(1.0, self._NOW.isoformat(), now=self._NOW) == 1.0

    def test_half_life_halves_above_floor(self) -> None:
        ts = (self._NOW - timedelta(days=30)).isoformat()
        score = _apply_recency_decay(1.0, ts, now=self._NOW, half_life_days=30.0)
        # decay=0.5 → floor + (1-floor)*0.5
        expected = RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * 0.5
        assert score == pytest.approx(expected)

    def test_very_old_item_hits_floor(self) -> None:
        ts = (self._NOW - timedelta(days=3650)).isoformat()  # 10 years
        score = _apply_recency_decay(1.0, ts, now=self._NOW, half_life_days=30.0)
        assert score == pytest.approx(RECENCY_FLOOR, abs=1e-6)

    def test_future_timestamp_clamped_to_zero_age(self) -> None:
        ts = (self._NOW + timedelta(days=10)).isoformat()
        score = _apply_recency_decay(1.0, ts, now=self._NOW)
        assert score == 1.0

    def test_z_suffix_parsed(self) -> None:
        ts = "2026-04-15T12:00:00Z"
        assert _apply_recency_decay(1.0, ts, now=self._NOW) == 1.0

    def test_naive_timestamp_treated_as_utc(self) -> None:
        ts = "2026-04-15T12:00:00"
        assert _apply_recency_decay(1.0, ts, now=self._NOW) == pytest.approx(1.0)

    def test_scales_base_score(self) -> None:
        ts = (self._NOW - timedelta(days=30)).isoformat()
        score = _apply_recency_decay(2.0, ts, now=self._NOW, half_life_days=30.0)
        expected = 2.0 * (RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * 0.5)
        assert score == pytest.approx(expected)


class TestKeywordSearchRecency:
    _NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)

    def test_recent_doc_outranks_old_doc_at_same_base(self) -> None:
        store = MagicMock()
        old_ts = (self._NOW - timedelta(days=365)).isoformat()
        fresh_ts = self._NOW.isoformat()
        store.search.return_value = [
            {
                "doc_id": "old",
                "content": "old content",
                "metadata": {},
                "rank": -0.8,
                "updated_at": old_ts,
            },
            {
                "doc_id": "fresh",
                "content": "fresh content",
                "metadata": {},
                "rank": -0.8,
                "updated_at": fresh_ts,
            },
        ]
        # Patch "now" used in the decay helper by passing a custom half-life
        # and relying on isoformat-based aging relative to real now.
        # Instead, we verify ordering: fresh_ts is real-now, so its age
        # is ~0 regardless of when the test runs, while old_ts is 365 days
        # before the fixture anchor and will be older than real-now too.
        store.search.return_value[0]["updated_at"] = (
            datetime.now(UTC) - timedelta(days=365)
        ).isoformat()
        store.search.return_value[1]["updated_at"] = datetime.now(UTC).isoformat()
        strategy = KeywordSearch(store)
        items = strategy.search("content")
        assert items[0].item_id == "fresh"
        assert items[1].item_id == "old"
        assert items[0].relevance_score > items[1].relevance_score


class TestKeywordSearch:
    @pytest.fixture
    def doc_store(self) -> MagicMock:
        store = MagicMock()
        store.search.return_value = [
            {
                "doc_id": "d1",
                "content": "Python guide",
                "metadata": {"tag": "tutorial"},
                "rank": -0.8,
            },
            {
                "doc_id": "d2",
                "content": "Java guide",
                "metadata": {"tag": "tutorial", "auto_importance": 0.5},
                "rank": -0.6,
            },
        ]
        return store

    def test_returns_pack_items(self, doc_store: MagicMock) -> None:
        strategy = KeywordSearch(doc_store)
        items = strategy.search("guide")
        assert len(items) == 2
        assert all(item.item_type == "document" for item in items)

    def test_importance_weighting(self, doc_store: MagicMock) -> None:
        strategy = KeywordSearch(doc_store)
        items = strategy.search("guide")
        # d2 has importance=0.5, so 0.6 * 1.5 = 0.9 > d1's 0.8 * 1.0 = 0.8
        assert items[0].item_id == "d2"

    def test_sorted_by_relevance(self, doc_store: MagicMock) -> None:
        strategy = KeywordSearch(doc_store)
        items = strategy.search("guide")
        scores = [item.relevance_score for item in items]
        assert scores == sorted(scores, reverse=True)

    def test_strategy_name(self, doc_store: MagicMock) -> None:
        assert KeywordSearch(doc_store).name == "keyword"

    def test_passes_filters(self, doc_store: MagicMock) -> None:
        strategy = KeywordSearch(doc_store)
        strategy.search("guide", filters={"tag": "tutorial"})
        doc_store.search.assert_called_once_with(
            "guide",
            limit=20,
            filters={"tag": "tutorial"},
        )


class TestSemanticSearch:
    @pytest.fixture
    def vector_store(self) -> MagicMock:
        store = MagicMock()
        store.query.return_value = [
            {
                "item_id": "v1",
                "score": 0.95,
                "metadata": {"content": "ML concepts", "auto_importance": 0.2},
            },
            {
                "item_id": "v2",
                "score": 0.80,
                "metadata": {"content": "Data pipelines"},
            },
        ]
        return store

    @pytest.fixture
    def embedding_fn(self) -> MagicMock:
        return MagicMock(return_value=[0.1, 0.2, 0.3])

    def test_returns_pack_items(
        self,
        vector_store: MagicMock,
        embedding_fn: MagicMock,
    ) -> None:
        strategy = SemanticSearch(vector_store, embedding_fn)
        items = strategy.search("ML")
        assert len(items) == 2
        assert items[0].item_id == "v1"

    def test_no_embedding_fn_returns_empty(
        self,
        vector_store: MagicMock,
    ) -> None:
        strategy = SemanticSearch(vector_store, embedding_fn=None)
        items = strategy.search("ML")
        assert items == []

    def test_calls_embedding_fn(
        self,
        vector_store: MagicMock,
        embedding_fn: MagicMock,
    ) -> None:
        strategy = SemanticSearch(vector_store, embedding_fn)
        strategy.search("ML query")
        embedding_fn.assert_called_once_with("ML query")

    def test_strategy_name(
        self,
        vector_store: MagicMock,
        embedding_fn: MagicMock,
    ) -> None:
        assert SemanticSearch(vector_store, embedding_fn).name == "semantic"


class TestGraphSearch:
    @pytest.fixture
    def graph_store(self) -> MagicMock:
        store = MagicMock()
        store.get_subgraph.return_value = {
            "nodes": [
                {
                    "node_id": "n1",
                    "node_type": "service",
                    "properties": {"name": "auth"},
                },
                {
                    "node_id": "n2",
                    "node_type": "service",
                    "properties": {"name": "api"},
                },
            ],
            "edges": [],
        }
        store.query.return_value = [
            {
                "node_id": "n3",
                "node_type": "person",
                "properties": {"name": "Alice"},
            },
        ]
        return store

    def test_subgraph_search_with_seed_ids(
        self,
        graph_store: MagicMock,
    ) -> None:
        strategy = GraphSearch(graph_store)
        items = strategy.search("", filters={"seed_ids": ["n1"]})
        assert len(items) == 2
        graph_store.get_subgraph.assert_called_once()

    def test_query_search_without_seeds(
        self,
        graph_store: MagicMock,
    ) -> None:
        strategy = GraphSearch(graph_store)
        items = strategy.search("", filters={"node_type": "person"})
        assert len(items) == 1
        assert items[0].item_id == "n3"

    def test_decreasing_scores(self, graph_store: MagicMock) -> None:
        strategy = GraphSearch(graph_store)
        items = strategy.search("", filters={"seed_ids": ["n1"]})
        assert items[0].relevance_score > items[1].relevance_score

    def test_strategy_name(self, graph_store: MagicMock) -> None:
        assert GraphSearch(graph_store).name == "graph"


class TestGraphSearchNodeRole:
    """GraphSearch excludes structural nodes and boosts curated nodes."""

    @pytest.fixture
    def role_store(self) -> MagicMock:
        store = MagicMock()
        store.query.return_value = [
            {
                "node_id": "svc",
                "node_type": "service",
                "node_role": "semantic",
                "properties": {"name": "auth"},
            },
            {
                "node_id": "col",
                "node_type": "uc_column",
                "node_role": "structural",
                "properties": {"name": "customer_id"},
            },
            {
                "node_id": "cluster",
                "node_type": "domain",
                "node_role": "curated",
                "properties": {"name": "payments"},
            },
        ]
        return store

    def test_structural_excluded_by_default(self, role_store: MagicMock) -> None:
        strategy = GraphSearch(role_store)
        items = strategy.search("", filters={})
        ids = {i.item_id for i in items}
        assert "col" not in ids
        assert "svc" in ids
        assert "cluster" in ids

    def test_structural_included_on_opt_in(self, role_store: MagicMock) -> None:
        strategy = GraphSearch(role_store)
        items = strategy.search("", filters={"include_structural": True})
        ids = {i.item_id for i in items}
        assert "col" in ids

    def test_node_role_lands_in_metadata(self, role_store: MagicMock) -> None:
        strategy = GraphSearch(role_store)
        items = strategy.search("", filters={})
        for item in items:
            assert item.metadata.get("node_role") in {"semantic", "curated"}

    def test_curated_boost_applied(self, role_store: MagicMock) -> None:
        """A curated node should score higher than an equivalently-ranked
        semantic node thanks to the 1.3x boost."""
        # Reset the fixture so curated and semantic appear in the same slot
        role_store.query.return_value = [
            {
                "node_id": "svc",
                "node_type": "service",
                "node_role": "semantic",
                "properties": {"name": "auth"},
            },
            {
                "node_id": "cluster",
                "node_type": "domain",
                "node_role": "curated",
                "properties": {"name": "payments"},
            },
        ]
        strategy = GraphSearch(role_store, curated_boost=1.3)
        items = strategy.search("", filters={})
        by_id = {i.item_id: i for i in items}
        # Same base score (1.0 and 0.95), but curated at slot 1 gets * 1.3
        # which puts it above the semantic node at slot 0.
        assert by_id["cluster"].relevance_score > by_id["svc"].relevance_score
