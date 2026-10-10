"""Tests for search strategies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from trellis.errors import BackendNotInstalledError, ConfigError
from trellis.retrieve import strategies as strategies_module
from trellis.retrieve.strategies import (
    DEFAULT_IMPORTANCE_DECAY_FLOOR,
    DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
    DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS,
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    GRAPH_RECENCY_CLOCK_FIELD,
    RECENCY_FLOOR,
    EmbedderResolveFailure,
    GraphSearch,
    KeywordSearch,
    SemanticSearch,
    _apply_importance,
    _apply_recency_decay,
    _warn_embedder_resolve_failed_once,
    _warn_semantic_search_init_failed_once,
    build_strategies,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore


def _fresh_meta(importance: float) -> dict[str, Any]:
    """Build a metadata dict with a fresh ``importance_scored_at`` stamp.

    Useful for tests that exercise above-threshold importance values without
    tripping the greenfield writer-contract guard.
    """
    return {
        "auto_importance": importance,
        "content_tags": {
            "importance_scored_at": datetime.now(UTC).isoformat(),
        },
    }


class TestApplyImportance:
    def test_no_importance(self) -> None:
        assert _apply_importance(1.0, {}) == 1.0

    def test_zero_importance_no_stamp_required(self) -> None:
        # importance == 0.0 short-circuits before the freshness check.
        assert _apply_importance(1.0, {"auto_importance": 0.0}) == 1.0

    def test_sub_threshold_importance_no_stamp_required(self) -> None:
        # Below decay_threshold → legacy multiplier, no stamp lookup.
        assert _apply_importance(1.0, {"auto_importance": 0.2}) == 1.2

    def test_with_importance_fresh_stamp(self) -> None:
        assert _apply_importance(1.0, _fresh_meta(0.5)) == pytest.approx(1.5)

    def test_max_importance_fresh_stamp(self) -> None:
        assert _apply_importance(1.0, _fresh_meta(1.0)) == pytest.approx(2.0)

    def test_clamps_over_one_fresh_stamp(self) -> None:
        assert _apply_importance(1.0, _fresh_meta(2.0)) == pytest.approx(2.0)

    def test_clamps_negative(self) -> None:
        # Negative clamps to 0.0 → short-circuit before freshness check.
        assert _apply_importance(1.0, {"auto_importance": -0.5}) == 1.0


class TestApplyImportanceFreshness:
    """Read-path guardrail (adr-importance-score-freshness §3.4)."""

    _NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)

    def test_fresh_stamp_no_decay(self) -> None:
        """Inside the horizon: legacy multiplier applied as-is."""
        meta = {
            "auto_importance": 0.9,
            "content_tags": {
                "importance_scored_at": (self._NOW - timedelta(days=10)).isoformat(),
            },
        }
        assert _apply_importance(1.0, meta, now=self._NOW) == pytest.approx(1.9)

    def test_stale_high_score_decays(self) -> None:
        """Past horizon: score decays with the same half-life math."""
        # 180d horizon + 30d half-life. At horizon + 30d (210d ago), the
        # excess of 30d => decay = 0.5; floor=0.3; so importance is
        # 0.9 * (0.3 + 0.7 * 0.5) = 0.9 * 0.65 = 0.585.
        # Final: 1.0 * (1.0 + 0.585) = 1.585.
        meta = {
            "auto_importance": 0.9,
            "content_tags": {
                "importance_scored_at": (self._NOW - timedelta(days=210)).isoformat(),
            },
        }
        result = _apply_importance(1.0, meta, now=self._NOW)
        expected_importance = 0.9 * (
            DEFAULT_IMPORTANCE_DECAY_FLOOR
            + (1.0 - DEFAULT_IMPORTANCE_DECAY_FLOOR) * 0.5
        )
        assert result == pytest.approx(1.0 + expected_importance)

    def test_very_old_stale_hits_floor(self) -> None:
        """Decades past the horizon: score asymptotes to floor."""
        meta = {
            "auto_importance": 1.0,
            "content_tags": {
                "importance_scored_at": (self._NOW - timedelta(days=10000)).isoformat(),
            },
        }
        result = _apply_importance(1.0, meta, now=self._NOW)
        # Importance dampens to ~floor.
        expected = 1.0 + (1.0 * DEFAULT_IMPORTANCE_DECAY_FLOOR)
        assert result == pytest.approx(expected, abs=1e-3)

    def test_stale_below_threshold_skips_decay(self) -> None:
        """Stale but sub-threshold scores are not decayed (no stamp lookup)."""
        # No stamp at all — sub-threshold path skips the freshness check.
        meta = {"auto_importance": 0.4}
        assert _apply_importance(1.0, meta, now=self._NOW) == pytest.approx(1.4)

    def test_missing_stamp_above_threshold_raises(self) -> None:
        """Greenfield contract: above-threshold score with no stamp = bug."""
        meta = {"auto_importance": 0.7}  # No content_tags at all.
        with pytest.raises(ValueError, match="importance_scored_at is missing"):
            _apply_importance(1.0, meta, now=self._NOW)

    def test_missing_stamp_in_content_tags_above_threshold_raises(self) -> None:
        """Stamp missing inside content_tags also raises."""
        meta = {
            "auto_importance": 0.7,
            "content_tags": {"domain": ["api"]},  # No importance_scored_at.
        }
        with pytest.raises(ValueError, match="importance_scored_at is missing"):
            _apply_importance(1.0, meta, now=self._NOW)

    def test_no_fallback_to_classified_at(self) -> None:
        """The guardrail must NOT fall back to ``classified_at`` (greenfield)."""
        meta = {
            "auto_importance": 0.7,
            "content_tags": {
                # classified_at present, but importance_scored_at missing.
                "classified_at": self._NOW.isoformat(),
            },
        }
        with pytest.raises(ValueError, match="importance_scored_at is missing"):
            _apply_importance(1.0, meta, now=self._NOW)

    def test_flat_alias_stamp_accepted(self) -> None:
        """Stamps stored at top-level metadata (flat alias) are read."""
        meta = {
            "auto_importance": 0.7,
            "importance_scored_at": self._NOW.isoformat(),
        }
        result = _apply_importance(1.0, meta, now=self._NOW)
        # Inside horizon → legacy multiplier.
        assert result == pytest.approx(1.7)

    def test_unparseable_stamp_returns_unchanged(self) -> None:
        """Unparseable stamps are treated as fresh (the higher-level
        guardrail enforces non-None; format is best-effort)."""
        meta = {
            "auto_importance": 0.7,
            "content_tags": {"importance_scored_at": "not-a-date"},
        }
        result = _apply_importance(1.0, meta, now=self._NOW)
        assert result == pytest.approx(1.7)

    def test_monotonic_in_importance_for_fresh_items(self) -> None:
        """Property: for fresh items, higher importance => higher score."""
        prev = 0.0
        for imp in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
            score = _apply_importance(1.0, _fresh_meta(imp), now=self._NOW)
            assert score >= prev
            prev = score

    def test_constants_have_documented_defaults(self) -> None:
        """Pin the constants so any change requires updating the ADR."""
        assert DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS == 180.0
        assert DEFAULT_IMPORTANCE_DECAY_FLOOR == 0.3
        assert DEFAULT_IMPORTANCE_DECAY_THRESHOLD == 0.5
        assert DEFAULT_RECENCY_HALF_LIFE_DAYS == 30.0


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
                "metadata": {
                    "tag": "tutorial",
                    "auto_importance": 0.5,
                    # Greenfield writer contract: above-threshold importance
                    # requires a freshness witness
                    # (adr-importance-score-freshness.md §3.5).
                    "content_tags": {
                        "importance_scored_at": datetime.now(UTC).isoformat(),
                    },
                },
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
        person_rows = [
            {
                "node_id": "n3",
                "node_type": "person",
                "properties": {"name": "Alice"},
            },
        ]
        # GraphSearch routes alias-expanding queries through the canonical
        # DSL (execute_node_query) and direct queries through query().
        # Mirror the row set on both so the test doesn't care which path
        # the strategy picked.
        store.query.return_value = person_rows
        store.execute_node_query.return_value = person_rows
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

    def test_edge_types_filter_forwarded_to_subgraph(
        self,
        graph_store: MagicMock,
    ) -> None:
        """``filters["edge_types"]`` reaches ``get_subgraph(edge_types=...)``.

        Pins the contract the github_corpus_convergence scenario relies on
        for ``author_attribution`` queries: passing
        ``edge_types=["wasAttributedTo"]`` constrains BFS to that edge kind
        instead of doing the bidirectional all-edges traversal.
        """
        strategy = GraphSearch(graph_store)
        strategy.search(
            "",
            filters={
                "seed_ids": ["user.alice"],
                "edge_types": ["wasAttributedTo"],
                "depth": 1,
            },
        )
        graph_store.get_subgraph.assert_called_once()
        _, kwargs = graph_store.get_subgraph.call_args
        assert kwargs["edge_types"] == ["wasAttributedTo"]
        assert kwargs["depth"] == 1

    def test_edge_types_default_none_when_not_supplied(
        self,
        graph_store: MagicMock,
    ) -> None:
        """Backwards-compat: omitting ``edge_types`` forwards ``None``.

        Pins the no-regression contract for callers that predate the
        ``edge_types`` filter — they keep getting bidirectional all-edges
        traversal exactly as before.
        """
        strategy = GraphSearch(graph_store)
        strategy.search("", filters={"seed_ids": ["n1"]})
        _, kwargs = graph_store.get_subgraph.call_args
        assert kwargs["edge_types"] is None


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

    def test_unconfirmed_mints_excluded_by_default(self) -> None:
        """#300: unconfirmed extraction mints never enter packs unasked."""
        store = MagicMock()
        store.query.return_value = [
            {
                "node_id": "dev",
                "node_type": "Device",
                "node_role": "semantic",
                "properties": {"name": "Oura ring", "extraction_status": "unconfirmed"},
            },
            {
                "node_id": "svc",
                "node_type": "service",
                "node_role": "semantic",
                "properties": {"name": "auth"},
            },
            {
                "node_id": "ok",
                "node_type": "Device",
                "node_role": "semantic",
                "properties": {"name": "Fenix", "extraction_status": "confirmed"},
            },
        ]
        strategy = GraphSearch(store)
        ids = {i.item_id for i in strategy.search("", filters={})}
        assert ids == {"svc", "ok"}

    def test_unconfirmed_mints_included_on_opt_in(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            {
                "node_id": "dev",
                "node_type": "Device",
                "node_role": "semantic",
                "properties": {"name": "Oura ring", "extraction_status": "unconfirmed"},
            },
        ]
        strategy = GraphSearch(store)
        ids = {
            i.item_id
            for i in strategy.search("", filters={"include_unconfirmed": True})
        }
        assert ids == {"dev"}

    def test_unconfirmed_filter_covers_subgraph_branch(self) -> None:
        """Seeded traversal is gated too — both branches share the filter."""
        store = MagicMock()
        store.get_subgraph.return_value = {
            "nodes": [
                {
                    "node_id": "dev",
                    "node_type": "Device",
                    "node_role": "semantic",
                    "properties": {
                        "name": "Oura ring",
                        "extraction_status": "unconfirmed",
                    },
                },
                {
                    "node_id": "svc",
                    "node_type": "service",
                    "node_role": "semantic",
                    "properties": {"name": "auth"},
                },
            ],
            "edges": [],
        }
        strategy = GraphSearch(store)
        ids = {i.item_id for i in strategy.search("", filters={"seed_ids": ["svc"]})}
        assert ids == {"svc"}

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


# ---------------------------------------------------------------------------
# ADR Phase 2 — canonical / legacy bucketing on retrieval
# ---------------------------------------------------------------------------


class TestGraphSearchCanonicalBucketing:
    """A query for ``"Person"`` must match both ``Person`` and ``person`` rows."""

    def _stub_store(self, *, dsl_rows: list[dict[str, Any]]) -> MagicMock:
        store = MagicMock()
        # Direct .query() must NOT be reached when alias-expansion fans
        # out — assert by failing loudly if it is.
        store.query.side_effect = AssertionError(
            "GraphSearch should route alias-expanding queries through "
            "execute_node_query, not query()"
        )
        store.execute_node_query.return_value = dsl_rows
        return store

    def test_canonical_query_routes_through_dsl_with_aliases(self) -> None:
        from trellis.stores.base.graph_query import FilterClause, NodeQuery

        rows = [
            {
                "node_id": "alice",
                "node_type": "Person",
                "properties": {"name": "Alice"},
            },
            {
                "node_id": "bob",
                "node_type": "person",
                "properties": {"name": "Bob"},
            },
        ]
        store = self._stub_store(dsl_rows=rows)
        items = GraphSearch(store).search("", filters={"node_type": "Person"})

        # Both rows surface; the canonical bucket key on the metadata
        # collapses them so downstream group-by is unambiguous.
        assert {i.item_id for i in items} == {"alice", "bob"}
        assert all(i.metadata["node_type_canonical"] == "Person" for i in items)
        # Raw stored type preserved for debugging / display.
        by_id = {i.item_id: i for i in items}
        assert by_id["alice"].metadata["node_type"] == "Person"
        assert by_id["bob"].metadata["node_type"] == "person"

        # Verify the strategy compiled an ``in`` clause with the
        # expanded alias set — not a plain eq filter.
        store.execute_node_query.assert_called_once()
        ((node_query,), _) = store.execute_node_query.call_args
        assert isinstance(node_query, NodeQuery)
        node_type_clauses = [c for c in node_query.filters if c.field == "node_type"]
        assert len(node_type_clauses) == 1
        clause = node_type_clauses[0]
        assert clause == FilterClause(
            field="node_type", op="in", value=("Person", "person")
        )

    def test_legacy_alias_query_buckets_with_canonical(self) -> None:
        # Symmetric case: a caller still using ``"person"`` should also
        # see the canonical ``"Person"`` rows under the same bucket.
        rows = [
            {
                "node_id": "alice",
                "node_type": "Person",
                "properties": {"name": "Alice"},
            },
        ]
        store = self._stub_store(dsl_rows=rows)
        items = GraphSearch(store).search("", filters={"node_type": "person"})
        assert items[0].metadata["node_type_canonical"] == "Person"

    def test_open_string_type_skips_dsl(self) -> None:
        # Open-string types have no aliases to expand. Stay on the
        # legacy ``query`` path so backends that haven't shipped the
        # DSL compiler still work.
        store = MagicMock()
        store.query.return_value = [
            {
                "node_id": "m1",
                "node_type": "dbt_model",
                "properties": {"name": "users"},
            },
        ]
        store.execute_node_query.side_effect = AssertionError(
            "open-string node_type must not trigger the DSL hop"
        )
        items = GraphSearch(store).search("", filters={"node_type": "dbt_model"})
        assert items[0].item_id == "m1"
        # Open-string canonical is the value itself.
        assert items[0].metadata["node_type_canonical"] == "dbt_model"
        store.query.assert_called_once()
        kwargs = store.query.call_args.kwargs
        assert kwargs["node_type"] == "dbt_model"

    def test_canonical_only_no_aliases_skips_dsl(self) -> None:
        # ``Organization`` is canonical with no legacy alias mapping
        # to it — a single-element expansion. Stay on the simple path.
        store = MagicMock()
        store.query.return_value = [
            {
                "node_id": "acme",
                "node_type": "Organization",
                "properties": {"name": "Acme"},
            },
        ]
        store.execute_node_query.side_effect = AssertionError(
            "single-bucket canonical must not trigger the DSL hop"
        )
        items = GraphSearch(store).search("", filters={"node_type": "Organization"})
        assert items[0].item_id == "acme"
        assert items[0].metadata["node_type_canonical"] == "Organization"
        kwargs = store.query.call_args.kwargs
        assert kwargs["node_type"] == "Organization"

    def test_no_node_type_filter_uses_query_path(self) -> None:
        store = MagicMock()
        store.query.return_value = []
        store.execute_node_query.side_effect = AssertionError(
            "calls without node_type must not trigger the DSL hop"
        )
        GraphSearch(store).search("", filters={})
        store.query.assert_called_once()


class TestGraphRecencyClock:
    """#420 — the graph axis selects and ranks on **one** clock.

    ``GraphStore.query`` is ``ORDER BY created_at DESC LIMIT n`` on every
    shipped backend, and that query *is* the unseeded branch's entire
    candidate window. Ranking those same rows by ``updated_at`` meant the
    axis selected on one column and ordered on another, so an SCD-2
    re-version — a ``retention.prune`` / ``retention.restore`` lifecycle
    stamp, an ``entity.update`` property merge, an extraction upsert —
    read as freshness on an axis whose window it cannot widen.

    Note what #420's own framing got wrong, since the tests are shaped by
    it: the issue claimed a re-versioned old entity could float back into
    retrieval. It cannot — SCD-2 carries ``created_at`` forward, so the
    node never re-enters the window at all. The defect is entirely
    *in-window rank*, which is why the fix is reader-side and one line and
    why no ``preserve_updated_at`` equivalent was added to ``upsert_node``.
    """

    @staticmethod
    def _node(node_id: str, *, age_days: float, updated_age_days: float) -> Any:
        now = datetime.now(UTC)
        return {
            "node_id": node_id,
            "node_type": "concept",
            "properties": {"name": node_id},
            "created_at": (now - timedelta(days=age_days)).isoformat(),
            "updated_at": (now - timedelta(days=updated_age_days)).isoformat(),
        }

    def test_a_lifecycle_reversion_changes_no_score(self) -> None:
        """Same rows, one re-versioned: every score identical.

        Asserts the **scores**, elementwise, not merely that the item is
        still present — recency decay is a multiplier, so a re-version that
        changed nothing about selection could still silently reprice the
        whole axis.
        """
        untouched = [
            self._node("fresh", age_days=1, updated_age_days=1),
            self._node("stale", age_days=300, updated_age_days=300),
        ]
        # ``retention.restore`` re-opens an SCD-2 version: ``updated_at``
        # becomes now, ``created_at`` is carried forward untouched.
        reversioned = [
            self._node("fresh", age_days=1, updated_age_days=1),
            self._node("stale", age_days=300, updated_age_days=0),
        ]

        def _scores(rows: list[dict[str, Any]]) -> dict[str, float]:
            store = MagicMock()
            store.query.return_value = rows
            return {
                item.item_id: item.relevance_score
                for item in GraphSearch(store).search("")
            }

        before = _scores(untouched)
        after = _scores(reversioned)

        assert set(before) == {"fresh", "stale"}
        # Not vacuous in either direction: the stale row must actually be
        # carrying decay, or "unchanged" would be trivially true.
        assert before["stale"] < 0.5 * before["fresh"], before
        for node_id, score in before.items():
            assert after[node_id] == pytest.approx(score), (node_id, before, after)

    def test_ranking_never_contradicts_the_selection_order(self) -> None:
        """The axis may not reorder its own recency window by a second clock.

        The store hands back rows in ``created_at DESC`` order — that is the
        selection. If ranking reads ``updated_at``, a re-versioned old node
        outranks rows the store placed above it, and the axis reports an
        order its own window contradicts. Ranking off the selection column
        makes the two agree by construction.
        """
        rows = [
            self._node("fresh", age_days=0, updated_age_days=0),
            self._node("mid", age_days=200, updated_age_days=200),
            # Oldest by the selection clock, newest by the other one.
            self._node("stale", age_days=400, updated_age_days=0),
        ]
        store = MagicMock()
        store.query.return_value = rows

        items = GraphSearch(store).search("")

        assert [i.item_id for i in items] == ["fresh", "mid", "stale"]
        # Spell out the failure this excludes: under ``updated_at`` ranking
        # the re-versioned row takes no decay at all and lands second.
        assert items[2].item_id == "stale"
        assert items[2].relevance_score < items[1].relevance_score

    def test_decay_reads_the_column_the_store_ordered_by(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Against a real backend: one column, both jobs.

        The mock-store tests above pin the consequence; this pins the
        premise on ``SQLiteGraphStore`` itself — that ``query`` orders by
        ``created_at``, that SCD-2 carries ``created_at`` forward across a
        re-version while moving ``updated_at``, and that the timestamp
        ``GraphSearch`` hands to the decay is that same carried-forward
        value rather than the bumped one.
        """
        store = SQLiteGraphStore(tmp_path / "graph.db")
        older = store.upsert_node(
            node_id="older", node_type="concept", properties={"name": "older"}
        )
        newer = store.upsert_node(
            node_id="newer", node_type="concept", properties={"name": "newer"}
        )
        before = store.get_node(older)
        # Re-version the *older* node — the retention.restore shape.
        store.upsert_node(
            node_id=older,
            node_type="concept",
            properties={"name": "older", "lifecycle": {"state": "current"}},
        )
        after = store.get_node(older)
        assert before is not None
        assert after is not None
        assert after["created_at"] == before["created_at"]
        assert after["updated_at"] > after["created_at"]

        rows = store.query(limit=10)
        # Selection: newest ``created_at`` first, and the re-version did not
        # move the older node up.
        assert [r["node_id"] for r in rows] == [newer, older]

        seen: list[str | None] = []
        real_decay = strategies_module._apply_recency_decay

        def _record(base: float, timestamp: Any, **kwargs: Any) -> float:
            seen.append(timestamp)
            return real_decay(base, timestamp, **kwargs)

        monkeypatch.setattr(strategies_module, "_apply_recency_decay", _record)
        items = GraphSearch(store).search("")

        assert [i.item_id for i in items] == [newer, older]
        # Ranking consumed exactly the column selection ordered on.
        assert seen == [r[GRAPH_RECENCY_CLOCK_FIELD] for r in rows]
        assert after["updated_at"] not in seen

    @pytest.mark.parametrize(
        ("clock", "label"),
        [(None, "absent"), ("", "empty"), ("not-a-date", "unparseable")],
    )
    def test_an_unusable_clock_is_served_but_not_served_silently(
        self, clock: str | None, label: str
    ) -> None:
        """Fail-open on an unusable clock, and say so once per search.

        ``_apply_recency_decay`` leaves the score alone when the timestamp
        does not parse, which is right per item and wrong to leave invisible
        in bulk: the bolt adapter reads ``created_at`` off the property bag
        (``props.get("created_at")``), so a backend holding nodes written
        outside Trellis could rank an entire axis undecayed with nothing in
        the result saying the clock was gone. Dropping the ``updated_at``
        fallback is what makes that reachable, so the warning ships with it.

        Parametrized across all three unusable shapes because the warning's
        predicate has to be the decay's own (``_parse_stamp(...) is None``),
        not a truthiness check: ``"not-a-date"`` is truthy, decays nothing,
        and under a truthiness check would have been counted as fine.
        """
        row: dict[str, Any] = {
            "node_id": "clockless",
            "node_type": "concept",
            "properties": {"name": "x"},
        }
        if clock is not None:
            row[GRAPH_RECENCY_CLOCK_FIELD] = clock
        store = MagicMock()
        store.query.return_value = [
            row,
            self._node("dated", age_days=10, updated_age_days=10),
        ]

        with capture_logs() as logs:
            items = GraphSearch(store).search("")

        # Served, not dropped — an undated node is not a broken one.
        assert {i.item_id for i in items} == {"clockless", "dated"}, label
        warned = [
            entry
            for entry in logs
            if entry["event"] == "graph_search_recency_clock_unusable"
        ]
        assert len(warned) == 1, label
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["rows_without_usable_clock"] == 1
        assert warned[0]["rows_scored"] == 2
        assert warned[0]["field"] == GRAPH_RECENCY_CLOCK_FIELD

    def test_a_fully_dated_window_logs_nothing(self) -> None:
        """The warning must not fire on the ordinary case."""
        store = MagicMock()
        store.query.return_value = [self._node("dated", age_days=1, updated_age_days=1)]

        with capture_logs() as logs:
            GraphSearch(store).search("")

        assert not [
            e for e in logs if e["event"] == "graph_search_recency_clock_unusable"
        ]


class _KnowledgeStoresOnly:
    """Minimal ``registry.knowledge`` stand-in -- construction only, never searched."""

    def __init__(self) -> None:
        self.document_store = object()
        self.graph_store = object()
        self.vector_store = object()


class _EmbeddingFnRaises:
    """Stand-in for StoreRegistry whose ``embedding_fn`` property raises.

    Deliberately not a MagicMock: a mock's attribute access never raises on
    its own, so it cannot stand in for the case under test (Q5-A) --
    ``getattr(registry, "embedding_fn", None)`` suppresses only an
    ``AttributeError`` from the attribute not existing, never a raise from
    *inside* the property.
    """

    def __init__(self, exc: Exception) -> None:
        self.knowledge = _KnowledgeStoresOnly()
        self._exc = exc

    @property
    def embedding_fn(self) -> Any:
        raise self._exc


class TestBuildStrategiesEmbedderResolveFailure:
    """Q5-A: ``registry.embedding_fn`` raising must degrade, not propagate.

    Before this fix, ``build_strategies`` read
    ``getattr(registry, "embedding_fn", None)`` directly -- and ``getattr``
    with a default suppresses only ``AttributeError``, not a raise from
    inside the property itself. A misconfigured embedder therefore crashed
    every pack build identically to a corrupt policy file, instead of
    degrading to keyword + graph search the way a failed vector-backend
    init already did.
    """

    @pytest.fixture(autouse=True)
    def _clear_warn_cache(self) -> Any:
        _warn_embedder_resolve_failed_once.cache_clear()
        yield
        _warn_embedder_resolve_failed_once.cache_clear()

    def test_a_config_error_is_caught_not_propagated(self) -> None:
        registry = _EmbeddingFnRaises(
            ConfigError("embeddings.provider is not set", setting="embeddings.provider")
        )

        result = build_strategies(registry)  # type: ignore[arg-type]

        assert not any(isinstance(s, SemanticSearch) for s in result.strategies)
        assert {type(s).__name__ for s in result.strategies} == {
            "KeywordSearch",
            "GraphSearch",
        }
        assert result.embedder_configured is False
        assert result.embedder_resolve_failure == EmbedderResolveFailure(
            error_type="ConfigError", setting="embeddings.provider"
        )

    def test_a_backend_not_installed_error_is_caught_too(self) -> None:
        """Not just the base ConfigError -- a subclass raised the same way."""
        registry = _EmbeddingFnRaises(
            BackendNotInstalledError(backend_name="openai", extra="llm-openai")
        )

        result = build_strategies(registry)  # type: ignore[arg-type]

        assert result.embedder_configured is False
        failure = result.embedder_resolve_failure
        assert failure is not None
        assert failure.error_type == "BackendNotInstalledError"
        assert failure.setting == "backend.openai"

    def test_a_non_config_error_is_caught_with_no_setting(self) -> None:
        """Only a ConfigError carries a ``.setting`` hint; anything else
        still degrades, just without naming a setting key."""
        registry = _EmbeddingFnRaises(RuntimeError("boom"))

        result = build_strategies(registry)  # type: ignore[arg-type]

        assert result.embedder_configured is False
        failure = result.embedder_resolve_failure
        assert failure is not None
        assert failure.error_type == "RuntimeError"
        assert failure.setting is None

    def test_the_message_text_never_appears_in_the_result(self) -> None:
        """Describe, don't quote: the raw exception message must not leak
        into the reported failure, even though it is right there on ``exc``."""
        registry = _EmbeddingFnRaises(
            ConfigError(
                "postgresql://user:supersecret@host/db is unreachable",
                setting="database.dsn",
            )
        )

        result = build_strategies(registry)  # type: ignore[arg-type]

        failure = result.embedder_resolve_failure
        assert failure is not None
        assert "supersecret" not in failure.error_type
        assert "supersecret" not in (failure.setting or "")
        strategy_failure = failure.to_strategy_failure()
        assert "supersecret" not in strategy_failure.message
        # The *safe* parts still must reach the message -- a message that
        # never carries the setting would also pass the line above.
        assert strategy_failure.message == "ConfigError: database.dsn"
        assert strategy_failure.error_class == "ConfigError"
        assert strategy_failure.strategy == "semantic"

    def test_to_strategy_failure_omits_the_colon_when_there_is_no_setting(
        self,
    ) -> None:
        """The no-setting branch of the same join, pinned the other way."""
        registry = _EmbeddingFnRaises(RuntimeError("boom"))

        result = build_strategies(registry)  # type: ignore[arg-type]

        failure = result.embedder_resolve_failure
        assert failure is not None
        assert failure.to_strategy_failure().message == "RuntimeError"

    def test_it_warns_once_per_distinct_cause(self) -> None:
        registry = _EmbeddingFnRaises(ConfigError("x", setting="embeddings.provider"))

        with capture_logs() as logs:
            build_strategies(registry)  # type: ignore[arg-type]
            build_strategies(registry)  # type: ignore[arg-type]

        warned = [
            e for e in logs if e["event"] == "semantic_search_embedder_resolve_failed"
        ]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["error_type"] == "ConfigError"
        assert warned[0]["setting"] == "embeddings.provider"

    def test_an_explicit_embedding_fn_argument_bypasses_the_registry_entirely(
        self,
    ) -> None:
        """Passing ``embedding_fn`` directly must never even touch the
        raising property -- a caller supplying its own callable (e.g. a
        test) is unaffected by a broken registry config."""
        registry = _EmbeddingFnRaises(ConfigError("x", setting="embeddings.provider"))

        result = build_strategies(
            registry,  # type: ignore[arg-type]
            embedding_fn=lambda _text: [0.1, 0.2],
        )

        assert result.embedder_configured is True
        assert result.embedder_resolve_failure is None
        assert any(isinstance(s, SemanticSearch) for s in result.strategies)

    def test_an_unhashable_setting_still_degrades(self) -> None:
        """O1 (#843 gate): a ``ConfigError.setting`` that is not a string
        (a list, say -- unhashable) must not reach the
        ``functools.cache``-keyed dedup call unguarded. This branch
        predates this PR (#838) and had the identical gap R1 fixes on the
        sibling semantic-init branch below."""
        registry = _EmbeddingFnRaises(
            ConfigError("bad", setting=["embeddings", "provider"])
        )

        result = build_strategies(registry)  # type: ignore[arg-type]

        assert result.embedder_configured is False
        failure = result.embedder_resolve_failure
        assert failure is not None
        assert failure.error_type == "ConfigError"
        assert failure.setting is None


class _KnowledgeVectorStoreRaises:
    """``registry.knowledge`` stand-in whose ``vector_store`` property raises.

    Mirrors ``_EmbeddingFnRaises`` above: a mock's attribute access never
    raises on its own, so a real property is needed to reproduce #830's
    construction-time failure. ``registry.knowledge.vector_store`` is
    evaluated as a ``SemanticSearch(...)`` call argument, inside the
    ``try`` that the embedder-resolve failure's ``try`` (above) does not
    cover -- this is the *other* branch of ``build_strategies``.
    """

    def __init__(self, exc: Exception) -> None:
        self.document_store = object()
        self.graph_store = object()
        self._exc = exc

    @property
    def vector_store(self) -> Any:
        raise self._exc


class _VectorStoreRaises:
    """Stand-in for StoreRegistry whose ``knowledge.vector_store`` raises."""

    def __init__(self, exc: Exception) -> None:
        self.knowledge = _KnowledgeVectorStoreRaises(exc)


class TestBuildStrategiesSemanticSearchInitFailure:
    """#830 fu4: ``semantic_search_init_failed`` must warn once per cause,
    never per build, and never carry the exception's message or a
    traceback.

    ``StoreRegistry._get`` caches only success, so a broken vector
    backend is re-instantiated -- and re-raises identically -- on every
    pack build. Before this fix, ``build_strategies`` logged
    ``semantic_search_init_failed`` with ``exc_info=True`` (a full
    traceback, which carries the message) unconditionally on every call.
    """

    @pytest.fixture(autouse=True)
    def _clear_warn_cache(self) -> Any:
        _warn_semantic_search_init_failed_once.cache_clear()
        yield
        _warn_semantic_search_init_failed_once.cache_clear()

    def test_it_degrades_to_keyword_and_graph_only(self) -> None:
        registry = _VectorStoreRaises(RuntimeError("boom"))

        result = build_strategies(
            registry,  # type: ignore[arg-type]
            embedding_fn=lambda _text: [0.1, 0.2],
        )

        assert not any(isinstance(s, SemanticSearch) for s in result.strategies)
        assert {type(s).__name__ for s in result.strategies} == {
            "KeywordSearch",
            "GraphSearch",
        }

    def test_it_warns_once_per_distinct_cause(self) -> None:
        registry = _VectorStoreRaises(ConfigError("x", setting="vector_store.provider"))

        with capture_logs() as logs:
            build_strategies(registry, embedding_fn=lambda _text: [0.1])  # type: ignore[arg-type]
            build_strategies(registry, embedding_fn=lambda _text: [0.1])  # type: ignore[arg-type]

        warned = [e for e in logs if e["event"] == "semantic_search_init_failed"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["error_type"] == "ConfigError"
        assert warned[0]["setting"] == "vector_store.provider"

    def test_log_never_carries_the_message_text_or_a_traceback(self) -> None:
        """Describe, don't quote: the prior ``exc_info=True`` behaviour put
        a full traceback -- including the message -- on every build."""
        sentinel = "SENTINEL-leak-marker-semantic-init-7c2a"
        registry = _VectorStoreRaises(RuntimeError(sentinel))

        with capture_logs() as logs:
            build_strategies(registry, embedding_fn=lambda _text: [0.1])  # type: ignore[arg-type]

        warned = [e for e in logs if e["event"] == "semantic_search_init_failed"]
        assert len(warned) == 1
        assert warned[0]["error_type"] == "RuntimeError"
        assert warned[0]["setting"] is None
        # No exc_info -> no "exception" key carrying a rendered traceback.
        assert "exc_info" not in warned[0]
        for value in warned[0].values():
            assert sentinel not in str(value)

    def test_logs_again_for_a_different_cause(self) -> None:
        """Dedup is per-cause, not a global silence switch."""
        with capture_logs() as logs:
            build_strategies(  # type: ignore[arg-type]
                _VectorStoreRaises(RuntimeError("a")),
                embedding_fn=lambda _text: [0.1],
            )
            build_strategies(  # type: ignore[arg-type]
                _VectorStoreRaises(ConfigError("b", setting="vector_store.dsn")),
                embedding_fn=lambda _text: [0.1],
            )

        warned = [e for e in logs if e["event"] == "semantic_search_init_failed"]
        assert len(warned) == 2
        assert {w["error_type"] for w in warned} == {"RuntimeError", "ConfigError"}

    def test_an_unhashable_setting_still_degrades(self) -> None:
        """R1 (#843 gate, probe scratchpad/g843_unhashable.py): a
        ``ConfigError.setting`` that is not a string (a list, say --
        unhashable) must not reach the ``functools.cache``-keyed dedup
        call unguarded. Before this fix it raised
        ``TypeError: unhashable type: 'list'`` out of build_strategies,
        failing every pack build instead of degrading to keyword + graph
        the way #839 already guarantees for the sibling
        embed_ingest_hook/vector_metadata helpers."""
        registry = _VectorStoreRaises(
            ConfigError("bad", setting=["vector_store", "provider"])
        )

        with capture_logs() as logs:
            result = build_strategies(
                registry,  # type: ignore[arg-type]
                embedding_fn=lambda _text: [0.1],
            )

        assert {type(s).__name__ for s in result.strategies} == {
            "KeywordSearch",
            "GraphSearch",
        }
        warned = [e for e in logs if e["event"] == "semantic_search_init_failed"]
        assert len(warned) == 1
        assert warned[0]["setting"] is None

    def test_distinct_settings_of_the_same_error_type_both_warn(self) -> None:
        """O2 (kills mutant G9): dedup keys on (error_type, setting), not
        error_type alone. Two ``ConfigError``s that share a type but name
        different settings are distinct causes and must both warn --
        the CHANGELOG's claim is "once per distinct (error_type,
        setting)"."""
        with capture_logs() as logs:
            build_strategies(  # type: ignore[arg-type]
                _VectorStoreRaises(ConfigError("a", setting="vector_store.provider")),
                embedding_fn=lambda _text: [0.1],
            )
            build_strategies(  # type: ignore[arg-type]
                _VectorStoreRaises(ConfigError("b", setting="vector_store.dsn")),
                embedding_fn=lambda _text: [0.1],
            )

        warned = [e for e in logs if e["event"] == "semantic_search_init_failed"]
        assert len(warned) == 2
        assert {w["setting"] for w in warned} == {
            "vector_store.provider",
            "vector_store.dsn",
        }
        assert {w["error_type"] for w in warned} == {"ConfigError"}

    def test_an_unrelated_exceptions_own_setting_attribute_is_ignored(
        self,
    ) -> None:
        """O3 (kills mutant G10): only a ``ConfigError``'s ``.setting`` is
        trusted. An unrelated exception shape that happens to carry a
        same-named ``.setting`` attribute (untrusted, possibly
        unhashable) must log ``setting=None``, not that attribute's
        value."""

        class _HasSettingError(RuntimeError):
            setting = "not-a-config-error-setting"

        with capture_logs() as logs:
            result = build_strategies(  # type: ignore[arg-type]
                _VectorStoreRaises(_HasSettingError("boom")),
                embedding_fn=lambda _text: [0.1],
            )

        assert {type(s).__name__ for s in result.strategies} == {
            "KeywordSearch",
            "GraphSearch",
        }
        warned = [e for e in logs if e["event"] == "semantic_search_init_failed"]
        assert len(warned) == 1
        assert warned[0]["error_type"] == "_HasSettingError"
        assert warned[0]["setting"] is None
