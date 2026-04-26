"""Tests for GraphStore ABC and SQLiteGraphStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.stores.graph import SQLiteGraphStore


@pytest.fixture
def graph_store(tmp_path: Path):
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


def test_upsert_and_get_node(graph_store):
    nid = graph_store.upsert_node(None, "service", {"name": "auth"})
    node = graph_store.get_node(nid)
    assert node is not None
    assert node["node_type"] == "service"
    assert node["properties"]["name"] == "auth"


def test_upsert_node_with_explicit_id(graph_store):
    graph_store.upsert_node("n1", "person", {"name": "Alice"})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["name"] == "Alice"


def test_update_node(graph_store):
    graph_store.upsert_node("n1", "service", {"v": 1})
    graph_store.upsert_node("n1", "service", {"v": 2})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["v"] == 2


def test_upsert_and_get_edge(graph_store):
    graph_store.upsert_node("a", "service", {})
    graph_store.upsert_node("b", "service", {})
    eid = graph_store.upsert_edge("a", "b", "depends_on", {"weight": 1.0})
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "depends_on"
    assert edges[0]["edge_id"] == eid


def test_get_edges_incoming(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    edges = graph_store.get_edges("b", direction="incoming")
    assert len(edges) == 1


def test_get_edges_both(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("c", "b", "links_to")
    edges = graph_store.get_edges("b", direction="both")
    assert len(edges) == 2


def test_get_subgraph(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("b", "c", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=2)
    assert len(sg["nodes"]) == 3
    assert len(sg["edges"]) == 2


def test_query_by_type(graph_store):
    graph_store.upsert_node(None, "service", {"name": "a"})
    graph_store.upsert_node(None, "person", {"name": "b"})
    results = graph_store.query(node_type="service")
    assert len(results) == 1


def test_query_by_properties(graph_store):
    graph_store.upsert_node(None, "service", {"team": "platform"})
    graph_store.upsert_node(None, "service", {"team": "data"})
    results = graph_store.query(properties={"team": "platform"})
    assert len(results) == 1


def test_delete_node_cascades(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_node("a") is True
    assert graph_store.get_node("a") is None
    assert graph_store.get_edges("b") == []


def test_delete_edge(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    eid = graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_edge(eid) is True
    assert graph_store.get_edges("a") == []


def test_get_nodes_bulk(graph_store):
    graph_store.upsert_node("a", "s", {"n": 1})
    graph_store.upsert_node("b", "s", {"n": 2})
    graph_store.upsert_node("c", "s", {"n": 3})
    nodes = graph_store.get_nodes_bulk(["a", "c"])
    assert len(nodes) == 2


def test_count(graph_store):
    assert graph_store.count_nodes() == 0
    graph_store.upsert_node(None, "s", {})
    assert graph_store.count_nodes() == 1
    assert graph_store.count_edges() == 0


def test_upsert_and_resolve_alias(graph_store):
    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})
    alias_id = graph_store.upsert_alias(
        "orders_entity",
        "unity_catalog",
        "main.analytics.orders",
        raw_name="orders",
        match_confidence=0.95,
        is_primary=True,
    )
    assert alias_id is not None

    alias = graph_store.resolve_alias("unity_catalog", "main.analytics.orders")
    assert alias is not None
    assert alias["entity_id"] == "orders_entity"
    assert alias["raw_name"] == "orders"
    assert alias["match_confidence"] == 0.95
    assert alias["is_primary"] is True


def test_get_aliases_for_entity(graph_store):
    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})
    graph_store.upsert_alias("orders_entity", "unity_catalog", "main.analytics.orders")
    graph_store.upsert_alias("orders_entity", "dbt", "model.project.orders")

    aliases = graph_store.get_aliases("orders_entity")
    assert len(aliases) == 2
    assert {(alias["source_system"], alias["raw_id"]) for alias in aliases} == {
        ("unity_catalog", "main.analytics.orders"),
        ("dbt", "model.project.orders"),
    }


def test_get_nonexistent(graph_store):
    assert graph_store.get_node("nope") is None


def test_delete_nonexistent(graph_store):
    assert graph_store.delete_node("nope") is False
    assert graph_store.delete_edge("nope") is False


# ---------------------------------------------------------------------------
# node_role / generation_spec (v3 additive schema)
# ---------------------------------------------------------------------------


class TestNodeRole:
    """node_role / generation_spec round-trip and validation."""

    def test_default_node_role_is_semantic(self, graph_store):
        graph_store.upsert_node("n1", "service", {"name": "auth"})
        node = graph_store.get_node("n1")
        assert node is not None
        assert node["node_role"] == "semantic"
        assert node["generation_spec"] is None

    def test_structural_node_round_trip(self, graph_store):
        graph_store.upsert_node(
            "col1",
            "uc_column",
            {"table": "orders", "dtype": "int"},
            node_role="structural",
        )
        node = graph_store.get_node("col1")
        assert node is not None
        assert node["node_role"] == "structural"
        assert node["generation_spec"] is None

    def test_curated_node_round_trip(self, graph_store):
        spec = {
            "generator_name": "community_detection_louvain",
            "generator_version": "1.0.0",
            "generated_at": "2026-04-11T00:00:00+00:00",
            "source_node_ids": ["a", "b"],
            "source_trace_ids": [],
            "parameters": {"resolution": 1.2},
        }
        graph_store.upsert_node(
            "cluster1",
            "domain",
            {"name": "payments"},
            node_role="curated",
            generation_spec=spec,
        )
        node = graph_store.get_node("cluster1")
        assert node is not None
        assert node["node_role"] == "curated"
        assert node["generation_spec"] is not None
        gen = node["generation_spec"]
        assert gen["generator_name"] == "community_detection_louvain"
        assert gen["parameters"] == {"resolution": 1.2}

    def test_curated_without_spec_is_rejected(self, graph_store):
        with pytest.raises(ValueError, match="generation_spec is required"):
            graph_store.upsert_node(
                "c1",
                "domain",
                {"name": "x"},
                node_role="curated",
            )

    def test_spec_on_non_curated_is_rejected(self, graph_store):
        spec = {"generator_name": "x", "generator_version": "1"}
        with pytest.raises(ValueError, match="generation_spec must be None"):
            graph_store.upsert_node(
                "n1",
                "service",
                {},
                generation_spec=spec,
            )

    def test_invalid_role_is_rejected(self, graph_store):
        with pytest.raises(ValueError, match="Invalid node_role"):
            graph_store.upsert_node(
                "n1",
                "service",
                {},
                node_role="bogus",
            )

    def test_node_role_is_immutable_across_versions(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        # Same role — OK
        graph_store.upsert_node("n1", "service", {"v": 2})
        # Changing role — rejected
        with pytest.raises(ValueError, match="Cannot change node_role"):
            graph_store.upsert_node(
                "n1",
                "service",
                {"v": 3},
                node_role="structural",
            )

    def test_structural_history_preserves_role(self, graph_store):
        graph_store.upsert_node(
            "s1",
            "uc_column",
            {"dtype": "int"},
            node_role="structural",
        )
        graph_store.upsert_node(
            "s1",
            "uc_column",
            {"dtype": "bigint"},
            node_role="structural",
        )
        history = graph_store.get_node_history("s1")
        assert len(history) == 2
        assert all(v["node_role"] == "structural" for v in history)

    def test_query_returns_node_role(self, graph_store):
        graph_store.upsert_node("s1", "uc_column", {"x": 1}, node_role="structural")
        graph_store.upsert_node("s2", "service", {"x": 1})
        results = graph_store.query(properties={"x": 1})
        roles = {r["node_id"]: r["node_role"] for r in results}
        assert roles["s1"] == "structural"
        assert roles["s2"] == "semantic"

    def test_subgraph_includes_node_role(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("col", "uc_column", {}, node_role="structural")
        graph_store.upsert_edge("a", "col", "has_column")
        sg = graph_store.get_subgraph(["a"], depth=1)
        roles = {n["node_id"]: n["node_role"] for n in sg["nodes"]}
        assert roles["a"] == "semantic"
        assert roles["col"] == "structural"


class TestCompactVersions:
    """Gap 4.2 — SCD2 retention for hot nodes/edges/aliases."""

    def _close_old_versions(self, store, table: str, valid_to_iso: str) -> None:
        """Backdate every closed row's ``valid_to`` in *table* for testing.

        Upserts alone don't let us pick a ``valid_to`` — the store stamps
        it with ``utc_now()``. Tests need deterministic values, so we
        rewrite them after the fact with raw SQL. This is the same
        pattern other SCD2-aware tests use when they need time control.
        """
        store._conn.execute(
            f"UPDATE {table} SET valid_to = ? WHERE valid_to IS NOT NULL",  # noqa: S608
            (valid_to_iso,),
        )
        store._conn.commit()

    def test_compacts_closed_rows_before_cutoff(self, graph_store):
        from datetime import UTC, datetime, timedelta

        # Two updates → one current row + one closed row.
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        # Backdate the closed version to 10 days ago.
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self._close_old_versions(graph_store, "nodes", ten_days_ago)

        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff)

        assert report.nodes_compacted == 1
        assert report.edges_compacted == 0
        assert report.aliases_compacted == 0
        assert report.total_compacted == 1
        assert report.dry_run is False
        # Current row still reachable.
        current = graph_store.get_node("n1")
        assert current is not None
        assert current["properties"]["v"] == 2
        # History no longer has the old version.
        history = graph_store.get_node_history("n1")
        assert len(history) == 1
        assert history[0]["valid_to"] is None

    def test_preserves_current_rows(self, graph_store):
        from datetime import UTC, datetime, timedelta

        # Single current row only; no closed rows exist.
        graph_store.upsert_node("n1", "service", {"v": 1})
        future = datetime.now(UTC) + timedelta(days=365)
        report = graph_store.compact_versions(future)
        assert report.nodes_compacted == 0
        assert graph_store.get_node("n1") is not None

    def test_dry_run_reports_without_deleting(self, graph_store):
        from datetime import UTC, datetime, timedelta

        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self._close_old_versions(graph_store, "nodes", ten_days_ago)

        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff, dry_run=True)
        assert report.dry_run is True
        assert report.nodes_compacted == 1
        # History still intact — dry-run is read-only.
        assert len(graph_store.get_node_history("n1")) == 2

    def test_skips_rows_at_or_after_cutoff(self, graph_store):
        from datetime import UTC, datetime, timedelta

        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        # Closed version is only 1 day old.
        one_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self._close_old_versions(graph_store, "nodes", one_day_ago)

        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff)
        assert report.nodes_compacted == 0
        assert len(graph_store.get_node_history("n1")) == 2

    def test_compacts_edges_and_aliases(self, graph_store):
        from datetime import UTC, datetime, timedelta

        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        # Create + replace an edge (replacement closes the prior version).
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 1})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 2})
        # Create + replace an alias.
        graph_store.upsert_alias("a", "systemX", "raw-1", raw_name="old")
        graph_store.upsert_alias("a", "systemX", "raw-1", raw_name="new")

        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self._close_old_versions(graph_store, "edges", ten_days_ago)
        self._close_old_versions(graph_store, "entity_aliases", ten_days_ago)

        report = graph_store.compact_versions(datetime.now(UTC) - timedelta(days=5))
        assert report.edges_compacted == 1
        assert report.aliases_compacted == 1
        assert report.total_compacted == 2

    def test_valid_to_range_reflects_compacted_rows(self, graph_store):
        from datetime import UTC, datetime, timedelta

        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        graph_store.upsert_node("n2", "service", {"v": 1})
        graph_store.upsert_node("n2", "service", {"v": 2})

        # Make the two closed rows land at distinct valid_to values.
        oldest = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        newest = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        graph_store._conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE node_id = ? AND valid_to IS NOT NULL",
            (oldest, "n1"),
        )
        graph_store._conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE node_id = ? AND valid_to IS NOT NULL",
            (newest, "n2"),
        )
        graph_store._conn.commit()

        report = graph_store.compact_versions(datetime.now(UTC) - timedelta(days=5))
        assert report.nodes_compacted == 2
        assert report.oldest_compacted_valid_to is not None
        assert report.newest_compacted_valid_to is not None
        assert report.oldest_compacted_valid_to < report.newest_compacted_valid_to

    def test_emits_event_when_event_log_provided(self, graph_store, tmp_path: Path):
        from datetime import UTC, datetime, timedelta

        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            graph_store.upsert_node("n1", "service", {"v": 1})
            graph_store.upsert_node("n1", "service", {"v": 2})
            ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
            self._close_old_versions(graph_store, "nodes", ten_days_ago)

            graph_store.compact_versions(
                datetime.now(UTC) - timedelta(days=5),
                event_log=event_log,
            )
            events = event_log.get_events(
                event_type=EventType.GRAPH_VERSIONS_COMPACTED, limit=10
            )
            assert len(events) == 1
            payload = events[0].payload
            assert payload["nodes_compacted"] == 1
            assert payload["dry_run"] is False
            assert "before" in payload
        finally:
            event_log.close()

    def test_dry_run_still_emits_event(self, graph_store, tmp_path: Path):
        from datetime import UTC, datetime, timedelta

        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            graph_store.upsert_node("n1", "service", {"v": 1})
            graph_store.upsert_node("n1", "service", {"v": 2})
            ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
            self._close_old_versions(graph_store, "nodes", ten_days_ago)

            graph_store.compact_versions(
                datetime.now(UTC) - timedelta(days=5),
                dry_run=True,
                event_log=event_log,
            )
            events = event_log.get_events(
                event_type=EventType.GRAPH_VERSIONS_COMPACTED, limit=10
            )
            assert len(events) == 1
            assert events[0].payload["dry_run"] is True
        finally:
            event_log.close()

    def test_base_class_raises_not_implemented(self):
        # Stand-in for backends that haven't opted into compaction.
        from datetime import UTC, datetime

        from trellis.stores.base.graph import GraphStore

        class _StubStore(GraphStore):
            # Fill out just enough of the ABC to instantiate.
            def upsert_node(self, *a, **k): ...  # type: ignore[override]
            def upsert_nodes_bulk(self, *a, **k): ...  # type: ignore[override]
            def get_node(self, *a, **k): ...  # type: ignore[override]
            def get_nodes_bulk(self, *a, **k): ...  # type: ignore[override]
            def upsert_alias(self, *a, **k): ...  # type: ignore[override]
            def resolve_alias(self, *a, **k): ...  # type: ignore[override]
            def get_aliases(self, *a, **k): ...  # type: ignore[override]
            def upsert_edge(self, *a, **k): ...  # type: ignore[override]
            def upsert_edges_bulk(self, *a, **k): ...  # type: ignore[override]
            def get_edges(self, *a, **k): ...  # type: ignore[override]
            def get_subgraph(self, *a, **k): ...  # type: ignore[override]
            def query(self, *a, **k): ...  # type: ignore[override]
            def get_node_history(self, *a, **k): ...  # type: ignore[override]
            def delete_node(self, *a, **k): ...  # type: ignore[override]
            def delete_edge(self, *a, **k): ...  # type: ignore[override]
            def count_nodes(self): ...  # type: ignore[override]
            def count_edges(self): ...  # type: ignore[override]
            def close(self): ...  # type: ignore[override]

        store = _StubStore()
        with pytest.raises(NotImplementedError, match="compact_versions"):
            store.compact_versions(datetime.now(UTC))
