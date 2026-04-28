"""Unit tests for ``GraphMigrator`` — backend-agnostic graph migration.

SQLite → SQLite round-trip exercises the full path against real
storage without needing a live Neo4j or Postgres instance. Live
SQLite → Neo4j coverage lives in ``tests/integration/`` and gates on
the env vars.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.migrate import (
    GraphMigrator,
    MigrationCapacityExceededError,
    MigrationReport,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def source_store(tmp_path: Path) -> SQLiteGraphStore:
    return SQLiteGraphStore(db_path=tmp_path / "source.db")


@pytest.fixture
def dest_store(tmp_path: Path) -> SQLiteGraphStore:
    return SQLiteGraphStore(db_path=tmp_path / "dest.db")


def _seed_basic_graph(store: SQLiteGraphStore) -> tuple[str, str, str]:
    """Create two nodes joined by an edge, plus an alias on node A."""
    a = store.upsert_node(
        "node-a",
        node_type="Person",
        properties={"name": "Alice"},
    )
    b = store.upsert_node(
        "node-b",
        node_type="Project",
        properties={"name": "Trellis"},
    )
    edge = store.upsert_edge(a, b, "memberOf", properties={"since": "2026-04"})
    store.upsert_alias(a, source_system="github", raw_id="alice42", raw_name="Alice E.")
    return a, b, edge


class TestRoundTripBasicGraph:
    def test_migration_copies_nodes_edges_and_aliases(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        a, b, _ = _seed_basic_graph(source_store)

        migrator = GraphMigrator(source_store, dest_store)
        report = migrator.run()

        assert report.nodes_read == 2
        assert report.nodes_written == 2
        assert report.nodes_skipped == 0
        assert report.edges_read == 1
        assert report.edges_written == 1
        assert report.aliases_read == 1
        assert report.aliases_written == 1
        assert report.errors == []
        assert not report.dry_run

        # Verify destination state.
        node_a = dest_store.get_node(a)
        assert node_a is not None
        assert node_a["node_type"] == "Person"
        assert node_a["properties"]["name"] == "Alice"

        node_b = dest_store.get_node(b)
        assert node_b is not None
        assert node_b["node_type"] == "Project"

        edges = dest_store.get_edges(a, direction="outgoing")
        assert len(edges) == 1
        assert edges[0]["target_id"] == b
        assert edges[0]["edge_type"] == "memberOf"

        resolved = dest_store.resolve_alias("github", "alice42")
        assert resolved is not None
        assert resolved["entity_id"] == a

    def test_migration_is_idempotent_on_retry(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        _seed_basic_graph(source_store)
        first = GraphMigrator(source_store, dest_store).run()
        second = GraphMigrator(source_store, dest_store).run()

        # Second run reads everything but writes nothing — all are
        # already in the destination, all skipped.
        assert second.nodes_read == first.nodes_read
        assert second.nodes_written == 0
        assert second.nodes_skipped == first.nodes_written
        assert second.edges_written == 0
        assert second.edges_skipped == first.edges_written
        assert second.aliases_written == 0
        assert second.aliases_skipped == first.aliases_written

    def test_dry_run_walks_without_writing(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        a, _b, _ = _seed_basic_graph(source_store)

        report = GraphMigrator(source_store, dest_store).run(dry_run=True)
        assert report.dry_run is True
        # Counts reflect what WOULD have been written.
        assert report.nodes_written == 2
        assert report.edges_written == 1
        assert report.aliases_written == 1
        # But destination is empty.
        assert dest_store.get_node(a) is None
        assert dest_store.resolve_alias("github", "alice42") is None


class TestEdgeCases:
    def test_empty_source_returns_zeroed_report(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        report = GraphMigrator(source_store, dest_store).run()
        assert report.nodes_read == 0
        assert report.nodes_written == 0
        assert report.edges_read == 0
        assert report.aliases_read == 0
        assert report.errors == []

    def test_node_with_no_edges_or_aliases_migrates_cleanly(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        source_store.upsert_node(
            "lonely", node_type="Concept", properties={"label": "Σ"}
        )
        report = GraphMigrator(source_store, dest_store).run()
        assert report.nodes_written == 1
        assert report.edges_read == 0
        assert report.aliases_read == 0
        assert dest_store.get_node("lonely") is not None

    def test_self_referential_edge(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        n = source_store.upsert_node("self", node_type="Concept", properties={})
        source_store.upsert_edge(n, n, "relatedTo", properties={})
        report = GraphMigrator(source_store, dest_store).run()
        assert report.edges_written == 1

    def test_deduplicates_edge_seen_via_both_endpoints(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        # The migrator walks outgoing edges per-node. A bidirectional
        # query would visit each edge twice; the migrator's seen_edge_ids
        # set prevents that.
        a = source_store.upsert_node("a", node_type="X", properties={})
        b = source_store.upsert_node("b", node_type="X", properties={})
        source_store.upsert_edge(a, b, "links", properties={})
        report = GraphMigrator(source_store, dest_store).run()
        assert report.edges_read == 1
        assert report.edges_written == 1


class TestCapacity:
    def test_exceeds_max_nodes_raises(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        for i in range(5):
            source_store.upsert_node(f"n-{i}", node_type="X", properties={"i": i})
        with pytest.raises(MigrationCapacityExceededError) as excinfo:
            GraphMigrator(source_store, dest_store, max_nodes=3).run()
        # The query asks for max_nodes+1 to detect overflow without
        # reading everything, so observed will be max_nodes+1 (= 4) even
        # when the source actually contains more.
        assert excinfo.value.observed > 3
        assert excinfo.value.limit == 3

    def test_at_max_nodes_succeeds(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        for i in range(3):
            source_store.upsert_node(f"n-{i}", node_type="X", properties={"i": i})
        # max_nodes=3 with exactly 3 nodes should NOT raise (the
        # implementation queries for max_nodes+1 and raises only on
        # overflow).
        report = GraphMigrator(source_store, dest_store, max_nodes=3).run()
        assert report.nodes_written == 3


class TestReportSummary:
    def test_summary_includes_dry_run_marker(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        source_store.upsert_node("n", node_type="X", properties={})
        report = GraphMigrator(source_store, dest_store).run(dry_run=True)
        assert "DRY RUN" in report.summary()

    def test_summary_omits_skip_block_when_zero(self) -> None:
        report = MigrationReport(nodes_read=5, nodes_written=5, elapsed_ms=10)
        assert "skipped" not in report.summary()

    def test_summary_includes_skip_block_when_nonzero(self) -> None:
        report = MigrationReport(
            nodes_read=5,
            nodes_written=2,
            nodes_skipped=3,
            elapsed_ms=10,
        )
        assert "skipped" in report.summary()
