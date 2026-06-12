"""Tests for ArcadeDBGraphStore — env-gated against a real ArcadeDB instance.

Skipped unless ``TRELLIS_TEST_ARCADEDB_URI`` is set (the same env-var
pattern :file:`test_arcadedb_vector.py` already uses). Run locally with:

    docker run --rm -d --name arcadedb -p 2480:2480 -p 7687:7687 \\
        -e arcadedb.server.rootPassword=playwithdata \\
        -e arcadedb.server.plugins=BoltProtocolPlugin \\
        arcadedata/arcadedb:latest
    export TRELLIS_TEST_ARCADEDB_URI=bolt://localhost:7687
    export TRELLIS_TEST_ARCADEDB_HTTP_URL=http://localhost:2480
    export TRELLIS_TEST_ARCADEDB_PASSWORD=playwithdata

ArcadeDB is the blessed graph + vector substrate per
:file:`docs/design/adr-arcadedb-blessed-substrate.md`. The five
provenance properties (Phase 3 of ``adr-graph-ontology.md`` §6.4) live
as schema-typed relationship properties on ``EDGE`` — STRING for the
four free-form fields, FLOAT (32-bit) with MIN 0.0 / MAX 1.0 for
``confidence``. The ``extractor_tier`` allowlist is enforced at the
Python boundary via :func:`validate_edge_provenance`.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("neo4j")

URI = os.environ.get("TRELLIS_TEST_ARCADEDB_URI", "")
USER = os.environ.get("TRELLIS_TEST_ARCADEDB_USER", "root")
PASSWORD = os.environ.get("TRELLIS_TEST_ARCADEDB_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_ARCADEDB_DATABASE", "trellis_graph_test")
HTTP_URL = os.environ.get("TRELLIS_TEST_ARCADEDB_HTTP_URL", "http://localhost:2480")

pytestmark = [
    pytest.mark.arcadedb,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_ARCADEDB_URI not set"),
]


@pytest.fixture
def graph_store():
    """Fresh ArcadeDBGraphStore with a cleaned database per test.

    Mirrors the Neo4j fixture pattern — wipe :Node / :Alias rows
    between tests so each test sees a deterministic state. The typed-
    property schema is created once per database and survives the
    wipe (DELETE doesn't touch the schema), so we don't need to
    re-run migrations between tests.
    """
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

    store = ArcadeDBGraphStore(
        URI,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        http_url=HTTP_URL,
        ensure_database_exists=True,
    )
    with store._driver.session(database=store._database) as session:
        session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
    yield store
    store.close()


class TestArcadeDBEdgeProvenance:
    """Round-trip the five provenance fields through a real ArcadeDB.

    These mirror :class:`TestEdgeProvenance` in
    :file:`test_neo4j_graph.py` — the shared ``BoltOpenCypherGraphStore``
    base does the property writes, so the behaviour must be identical
    across the two backends.
    """

    def test_round_trip_all_fields(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        graph_store.upsert_edge(
            "a",
            "b",
            "depends_on",
            source_trace_id="tr_42",
            agent_id="agent-7",
            confidence=0.83,
            evidence_ref="doc-9",
            extractor_tier="HYBRID",
        )
        edges = graph_store.get_edges("a", direction="outgoing")
        assert len(edges) == 1
        edge = edges[0]
        assert edge["source_trace_id"] == "tr_42"
        assert edge["agent_id"] == "agent-7"
        # ArcadeDB FLOAT is 32-bit single-precision; ``pytest.approx``
        # absorbs the rounding (0.83 → ~0.8299999...).
        assert edge["confidence"] == pytest.approx(0.83, rel=1e-5)
        assert edge["evidence_ref"] == "doc-9"
        assert edge["extractor_tier"] == "HYBRID"

    def test_missing_provenance_reads_back_none(self, graph_store):
        from trellis.stores.base.edge_provenance import EDGE_PROVENANCE_FIELDS

        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 1.0})
        edge = graph_store.get_edges("a", direction="outgoing")[0]
        for field in EDGE_PROVENANCE_FIELDS:
            assert edge[field] is None, (
                f"{field}: expected None on edge without provenance, got "
                f"{edge[field]!r}"
            )

    def test_bad_confidence_raises_before_network(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="confidence must be in"):
            graph_store.upsert_edge("a", "b", "depends_on", confidence=1.5)
        # Validator runs before the Bolt round trip — no edge was
        # written.
        assert graph_store.get_edges("a", direction="outgoing") == []

    def test_bad_extractor_tier_raises_before_network(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            graph_store.upsert_edge("a", "b", "depends_on", extractor_tier="MAGIC")
        assert graph_store.get_edges("a", direction="outgoing") == []

    def test_bulk_provenance_round_trip(self, graph_store):
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        graph_store.upsert_node("c", "s", {})
        graph_store.upsert_edges_bulk(
            [
                {
                    "source_id": "a",
                    "target_id": "b",
                    "edge_type": "links_to",
                    "confidence": 0.5,
                    "extractor_tier": "DETERMINISTIC",
                    "agent_id": "agent-1",
                },
                {
                    "source_id": "a",
                    "target_id": "c",
                    "edge_type": "links_to",
                },
            ]
        )
        edges = sorted(
            graph_store.get_edges("a", direction="outgoing"),
            key=lambda e: e["target_id"],
        )
        assert edges[0]["confidence"] == pytest.approx(0.5, rel=1e-5)
        assert edges[0]["extractor_tier"] == "DETERMINISTIC"
        assert edges[0]["agent_id"] == "agent-1"
        assert edges[1]["confidence"] is None
        assert edges[1]["agent_id"] is None

    def test_schema_migration_is_idempotent(self, graph_store):
        """Re-running the typed-property migration is a no-op.

        ``CREATE PROPERTY ... IF NOT EXISTS`` is documented as
        idempotent — re-invoking the migration against an already-
        migrated database must succeed without raising.
        """
        # The fixture already ran the migration once. Run it again
        # explicitly to confirm idempotency.
        from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

        ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema(
            http_url=HTTP_URL,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
        )

    def test_arcadedb_min_max_constraint_enforced_server_side(self, graph_store):
        """ArcadeDB's FLOAT (MIN 0.0, MAX 1.0) is a defense-in-depth
        backstop behind :func:`validate_edge_provenance`.

        The Python validator should catch every out-of-range
        ``confidence`` before the network call. If a caller managed to
        bypass it (e.g. by writing raw SQL), the schema constraint
        would still reject the value. We exercise this by issuing the
        write via ArcadeDB SQL, bypassing the Cypher path.
        """
        from trellis.stores.arcadedb.base import execute_sql

        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        # First, land a valid edge so the EDGE type has a row.
        graph_store.upsert_edge("a", "b", "depends_on", confidence=0.5)
        # Try to UPDATE the confidence to an out-of-range value via
        # raw SQL. ArcadeDB's MIN/MAX constraint should reject this.
        with pytest.raises(RuntimeError):
            execute_sql(
                HTTP_URL,
                USER,
                PASSWORD,
                DATABASE,
                "UPDATE EDGE SET confidence = 2.5 WHERE edge_type = 'depends_on'",
            )

    def test_registry_built_arcadedb_installs_provenance_schema(self):
        """Schema-typed property installation must run via the registry
        path, not just direct construction.

        Reproduces the gap PR #126 + #127 reviewers identified: building
        ArcadeDBGraphStore via ``StoreRegistry`` used to strip
        ``http_url`` + ``password`` before calling the constructor with
        ``driver=...``, leaving the constructor's injected-driver
        branch unable to run the typed-property migration. The registry
        now runs the migration itself before injecting the driver — so
        a registry-built store should reject an out-of-range
        ``confidence`` write at the server boundary even when the
        Python validator is bypassed.
        """
        from trellis.stores.arcadedb.base import execute_sql
        from trellis.stores.registry import StoreRegistry

        config = {
            "graph": {
                "backend": "arcadedb",
                "uri": URI,
                "user": USER,
                "password": PASSWORD,
                "database": DATABASE,
                "http_url": HTTP_URL,
            },
        }
        registry = StoreRegistry(config=config)
        try:
            graph_store = registry.knowledge.graph_store
            # Clean rows from prior tests in this class.
            with graph_store._driver.session(database=graph_store._database) as session:
                session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
            graph_store.upsert_node("a", "service", {})
            graph_store.upsert_node("b", "service", {})
            graph_store.upsert_edge("a", "b", "depends_on", confidence=0.5)
            # Server-side FLOAT MIN/MAX must reject an out-of-range
            # raw-SQL update — proof the typed-property migration
            # installed via the registry path. Pre-fix, this UPDATE
            # would succeed because the property was auto-created
            # untyped on first write.
            with pytest.raises(RuntimeError):
                execute_sql(
                    HTTP_URL,
                    USER,
                    PASSWORD,
                    DATABASE,
                    "UPDATE EDGE SET confidence = 2.5 WHERE edge_type = 'depends_on'",
                )
        finally:
            registry.close()
