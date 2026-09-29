"""Tests for `trellis demo` commands."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.ast_rules import assert_hand_read_floor
from tests.cli_output import plain
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
        from trellis_cli.stores import get_graph_store

        assert_hand_read_floor(
            get_graph_store().count_edges(), DEMO_EDGE_FLOOR, subject="demo graph edge"
        )


class TestDemoLoadColdStartFailures:
    """A cold-start command the executor does not apply is reported.

    The executor folds a handler exception into a FAILED result and a policy
    denial into a REJECTED one, and raises neither. ``demo load`` counted
    only SUCCESS results, so on both paths it printed a smaller edge count,
    exited 0 and showed no failure line.
    """

    def test_a_failed_cold_start_command_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.stores.base.graph_query import NodeQuery
        from trellis.stores.sqlite.graph import SQLiteGraphStore
        from trellis_cli import demo
        from trellis_cli.stores import get_graph_store

        original = SQLiteGraphStore.upsert_edge
        refused: list[tuple[str, str]] = []

        def refuse_cold_start_edges(
            self: SQLiteGraphStore,
            source_id: str,
            target_id: str,
            *args: Any,
            **kwargs: Any,
        ) -> str:
            # Every id the demo builds comes from demo._id; the fixture's don't.
            demo_ids = set(demo._IDS.values())
            if source_id in demo_ids and target_id in demo_ids:
                return original(self, source_id, target_id, *args, **kwargs)
            refused.append((source_id, target_id))
            # A markup-shaped tail, as store text quoting an id can carry.
            msg = f"Cannot upsert edge: {source_id!r} -> {target_id!r} [document]"
            raise ValueError(msg)

        monkeypatch.setattr(SQLiteGraphStore, "upsert_edge", refuse_cold_start_edges)
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0, (result.stdout, repr(result.exception))

        # Rich wraps at the console width, which is 80 columns in CI.
        out = " ".join(plain(result.stdout).split())
        assert out.count("Cold-start fixture failed") == 1, out
        counted = re.search(
            r"Cold-start fixture failed: dbt-manifest: (\d+) of (\d+) commands", out
        )
        assert counted is not None, out
        # The fixture's entities land before its edges, so the batch is the
        # nodes it wrote plus the edges the store refused.
        cold_start_nodes = [
            n
            for n in get_graph_store().execute_node_query(NodeQuery(limit=100_000))
            if n["node_id"] not in set(demo._IDS.values())
        ]
        assert refused, "the patched store refused no cold-start edge"
        assert (int(counted[1]), int(counted[2])) == (
            len(refused),
            len(refused) + len(cold_start_nodes),
        ), out
        source_id, target_id = refused[0]
        assert "link.create failed: Execution failed: Cannot upsert edge: " in out
        assert f"{source_id!r} -> {target_id!r} [document]" in out
        assert "cold-start entities" not in out

    def test_a_rejected_cold_start_command_is_reported(self, tmp_path: Path) -> None:
        from trellis.schemas.enums import PolicyType
        from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
        from trellis.stores.policy_store import PolicyStore

        # Only the cold-start path writes through the executor, so denying
        # link.create reaches no other demo write.
        PolicyStore(tmp_path / "data" / "stores" / "policies.json").add(
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="global"),
                rules=[PolicyRule(operation="link.create", action="deny")],
            )
        )
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0, (result.stdout, repr(result.exception))

        out = " ".join(plain(result.stdout).split())
        assert out.count("Cold-start fixture failed") == 1, out
        assert "link.create rejected" in out
        assert "cold-start entities" not in out
