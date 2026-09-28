"""Tests for `trellis demo` commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.ast_rules import assert_hand_read_floor
from trellis.schemas.enums import EdgeKind
from trellis_cli.main import app

runner = CliRunner()

# Counted by hand at b9b313b2, never computed by the tests that use them.
# 25 _build_entities + 3 _build_precedents (demo.py) + 20 cold-start
# fixture entities (the count `demo load` prints for the bundled fixture).
DEMO_NODE_FLOOR = 48
# 25 _build_edges (demo.py) + 20 cold-start fixture edges (as printed).
DEMO_EDGE_FLOOR = 45
# The 25 _build_edges tuples, each written by a direct single-row upsert_edge.
DIRECT_EDGE_CALL_FLOOR = 25


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


class TestDemoLoad:
    def test_load_succeeds(self) -> None:
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0
        assert "entities" in result.stdout
        assert "traces" in result.stdout

    def test_seeds_local_aliases_for_entities(self) -> None:
        # Demo loader is the only path that ships with seeded aliases — the
        # README quickstart's `retrieve entity user-api` depends on it.
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0

        from trellis_cli.stores import LOCAL_SOURCE_SYSTEM, get_graph_store

        graph = get_graph_store()
        match = graph.resolve_alias(LOCAL_SOURCE_SYSTEM, "user-api")
        assert match is not None
        node = graph.get_node(match["entity_id"])
        assert node is not None
        assert node["node_type"] == "service"
        assert node["properties"]["name"] == "user-api"

    def test_loads_cold_start_fixture_via_extractor_path(self) -> None:
        """The demo loads dbt + OpenLineage fixtures through the same
        ExtractionDispatcher + MutationExecutor path a real deployment uses,
        so drift between the demo and cold-start ingestion stays impossible."""
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0, result.stdout
        assert "cold-start" in result.stdout

        from trellis_cli.stores import get_graph_store

        graph = get_graph_store()
        # dbt manifest fixture should land fct_orders as a dbt_model with
        # the routing properties populated.
        fct = graph.get_node("model.jaffle_shop.fct_orders")
        assert fct is not None
        assert fct["node_type"] == "dbt_model"
        props = fct["properties"]
        assert props.get("source_system") == "snowflake"
        assert props.get("schema_name") == "marts"
        assert props.get("database_name") == "analytics"
        assert props.get("physical_uri") == "snowflake://analytics/marts/fct_orders"

        # OpenLineage fixture should land at least one job entity.
        job = graph.get_node("job:dbt:fct_orders_build")
        assert job is not None
        assert job["node_type"] == "job"


class TestDemoLoadEdgeEndpoints:
    """Every edge `demo load` writes joins two current graph nodes.

    ``BoltOpenCypherGraphStore.upsert_edge`` (Neo4j, ArcadeDB) raises when
    either endpoint is not a current node, so the edges the load used to
    write from evidence document ids and trace ids crashed it there. On
    SQLite and Postgres the same edges landed as dangling rows that
    ``get_subgraph`` never renders.
    """

    def test_every_demo_edge_has_current_endpoints(self) -> None:
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0, result.stdout

        from trellis.stores.base.graph_query import EdgeQuery, NodeQuery
        from trellis_cli import demo
        from trellis_cli.stores import get_graph_store

        graph = get_graph_store()
        nodes = graph.execute_node_query(NodeQuery(limit=100_000))
        edges = graph.execute_edge_query(EdgeQuery(limit=100_000))
        # A different method (COUNT) must agree with the listing, so a listing
        # that silently truncates cannot pass as a clean graph.
        assert len(nodes) == graph.count_nodes()
        assert len(edges) == graph.count_edges()
        assert_hand_read_floor(len(nodes), DEMO_NODE_FLOOR, subject="demo graph node")
        assert_hand_read_floor(len(edges), DEMO_EDGE_FLOOR, subject="demo graph edge")

        node_ids = {n["node_id"] for n in nodes}
        dangling = sorted(
            (e["edge_type"], e["source_id"] in node_ids, e["target_id"] in node_ids)
            for e in edges
            if e["source_id"] not in node_ids or e["target_id"] not in node_ids
        )
        assert dangling == [], dangling

        # Named anchors: each write path the scan must still be seeing.
        triples = {(e["source_id"], e["target_id"], e["edge_type"]) for e in edges}
        assert (
            demo._id("doc-runbook"),
            demo._id("svc-api"),
            EdgeKind.EVIDENCE_ATTACHED_TO.value,
        ) in triples
        assert any(
            "model.jaffle_shop.fct_orders" in (e["source_id"], e["target_id"])
            for e in edges
        )
        assert {n["node_id"] for n in nodes if n["node_type"] == "precedent"} == {
            demo._id("prec-conn-pool"),
            demo._id("prec-canary-deploy"),
            demo._id("prec-memory-profiling"),
        }

    def test_precedent_provenance_survives_in_the_event_payload(self) -> None:
        """The trace-to-precedent link lives in ``PRECEDENT_PROMOTED``.

        Passes before and after the edge removal by design. The removed
        ``trace_promoted_to_precedent`` edges started at trace ids that are
        not graph nodes, so ``get_subgraph`` never rendered them; this
        payload, which ``list_precedents`` returns whole, is where the link
        survives.
        """
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0, result.stdout

        from trellis.retrieve.precedents import list_precedents
        from trellis_cli import demo
        from trellis_cli.stores import get_event_log

        listed = {
            p["entity_id"]: p["payload"].get("source_traces")
            for p in list_precedents(get_event_log())
        }
        assert listed == {
            demo._id("prec-conn-pool"): [demo._id("trace-incident")],
            demo._id("prec-canary-deploy"): [
                demo._id("trace-deploy"),
                demo._id("trace-ml-fail"),
            ],
            demo._id("prec-memory-profiling"): [demo._id("trace-ml-fail")],
        }

    def test_load_succeeds_when_edges_require_current_endpoints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Bolt backends' endpoint precondition, reproduced on SQLite.

        Patches the concrete ``SQLiteGraphStore`` rather than the ABC, whose
        ``upsert_edge`` the SQLite store overrides; the call floor fails if
        the patch stops intercepting.
        """
        from trellis.stores.sqlite.graph import SQLiteGraphStore

        original = SQLiteGraphStore.upsert_edge
        calls: list[tuple[str, str]] = []

        def strict_upsert_edge(
            self: SQLiteGraphStore,
            source_id: str,
            target_id: str,
            *args: Any,
            **kwargs: Any,
        ) -> str:
            calls.append((source_id, target_id))
            if self.get_node(source_id) is None or self.get_node(target_id) is None:
                msg = (
                    f"Cannot upsert edge: source {source_id!r} or target "
                    f"{target_id!r} has no current version"
                )
                raise ValueError(msg)
            return original(self, source_id, target_id, *args, **kwargs)

        monkeypatch.setattr(SQLiteGraphStore, "upsert_edge", strict_upsert_edge)
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0, (result.stdout, repr(result.exception))
        assert "Cold-start fixture failed" not in result.stdout
        assert_hand_read_floor(
            len(calls), DIRECT_EDGE_CALL_FLOOR, subject="single-row upsert_edge call"
        )
