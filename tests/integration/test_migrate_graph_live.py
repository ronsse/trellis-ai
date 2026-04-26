"""Live cross-backend migration test (SQLite → Neo4j).

Exercises ``GraphMigrator`` end-to-end against a real Neo4j instance.
SQLite is the source so we can seed it deterministically without
network round-trips; Neo4j is the destination because it's the
blessed backend for the cloud path. Skipped unless
``TRELLIS_TEST_NEO4J_URI`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("neo4j")

from trellis.migrate import GraphMigrator
from trellis.stores.sqlite.graph import SQLiteGraphStore

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]


def _wipe_neo4j() -> None:
    """Drop all migration-test nodes/relationships before each test."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        with driver.session(database=DATABASE) as session:
            session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n").consume()
    finally:
        driver.close()


@pytest.fixture
def sqlite_source(tmp_path: Path) -> SQLiteGraphStore:
    return SQLiteGraphStore(db_path=tmp_path / "source.db")


@pytest.fixture
def neo4j_dest() -> Any:
    """Neo4jGraphStore against the integration AuraDB instance."""
    from trellis.stores.neo4j.graph import Neo4jGraphStore

    _wipe_neo4j()
    store = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
    yield store
    store.close()


def test_sqlite_to_neo4j_round_trip(
    sqlite_source: SQLiteGraphStore, neo4j_dest: Any
) -> None:
    # Seed SQLite with three nodes, two edges, one alias.
    a = sqlite_source.upsert_node(
        "agent-alice",
        node_type="Person",
        properties={"name": "Alice"},
    )
    b = sqlite_source.upsert_node(
        "proj-trellis",
        node_type="Project",
        properties={"name": "Trellis"},
    )
    c = sqlite_source.upsert_node(
        "concept-graph",
        node_type="Concept",
        properties={"label": "Graph"},
    )
    sqlite_source.upsert_edge(a, b, "memberOf", properties={})
    sqlite_source.upsert_edge(b, c, "uses", properties={})
    sqlite_source.upsert_alias(a, source_system="github", raw_id="alice42")

    migrator = GraphMigrator(sqlite_source, neo4j_dest)
    report = migrator.run()

    assert report.errors == []
    assert report.nodes_read == 3
    assert report.nodes_written == 3
    assert report.edges_read == 2
    assert report.edges_written == 2
    assert report.aliases_read == 1
    assert report.aliases_written == 1

    # Verify Neo4j reflects every row.
    node_a = neo4j_dest.get_node(a)
    assert node_a is not None
    assert node_a["node_type"] == "Person"
    assert node_a["properties"]["name"] == "Alice"

    edges_from_a = neo4j_dest.get_edges(a, direction="outgoing")
    assert len(edges_from_a) == 1
    assert edges_from_a[0]["target_id"] == b
    assert edges_from_a[0]["edge_type"] == "memberOf"

    edges_from_b = neo4j_dest.get_edges(b, direction="outgoing")
    assert len(edges_from_b) == 1
    assert edges_from_b[0]["target_id"] == c

    resolved = neo4j_dest.resolve_alias("github", "alice42")
    assert resolved is not None
    assert resolved["entity_id"] == a


def test_idempotent_on_retry_against_neo4j(
    sqlite_source: SQLiteGraphStore, neo4j_dest: Any
) -> None:
    sqlite_source.upsert_node("n", node_type="Person", properties={"name": "Bob"})
    first = GraphMigrator(sqlite_source, neo4j_dest).run()
    second = GraphMigrator(sqlite_source, neo4j_dest).run()

    assert first.nodes_written == 1
    assert second.nodes_written == 0
    assert second.nodes_skipped == 1


def test_dry_run_against_neo4j_writes_nothing(
    sqlite_source: SQLiteGraphStore, neo4j_dest: Any
) -> None:
    sqlite_source.upsert_node("n", node_type="Project", properties={})
    report = GraphMigrator(sqlite_source, neo4j_dest).run(dry_run=True)
    assert report.dry_run is True
    assert report.nodes_written == 1
    # Nothing actually landed in Neo4j.
    assert neo4j_dest.get_node("n") is None
