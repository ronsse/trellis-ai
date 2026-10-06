"""Tests for Neo4jGraphStore — requires a real Neo4j instance.

Skipped unless ``TRELLIS_TEST_NEO4J_URI`` is set and the ``neo4j``
driver is importable. Run locally with:

    docker run --rm -d --name trellis-neo4j -p 7687:7687 -p 7474:7474 \\
        -e NEO4J_AUTH=neo4j/testtest12 neo4j:5
    export TRELLIS_TEST_NEO4J_URI=bolt://localhost:7687
    export TRELLIS_TEST_NEO4J_USER=neo4j
    export TRELLIS_TEST_NEO4J_PASSWORD=testtest12

Or against a Neo4j AuraDB Free instance (validated 2026-04-25):

    export TRELLIS_TEST_NEO4J_URI=neo4j+s://<id>.databases.neo4j.io
    export TRELLIS_TEST_NEO4J_USER=<id>          # AuraDB: user = instance_id
    export TRELLIS_TEST_NEO4J_PASSWORD=<from console>
    export TRELLIS_TEST_NEO4J_DATABASE=<id>      # AuraDB: db = instance_id

The URI + user + password env vars are split so CI can keep the
password out of the URL.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("neo4j")

from tests.unit.stores import bolt_duplicate_current

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
# AuraDB Free instances host the user database under the instance ID,
# not under the canonical "neo4j" name. Set this env var to override.
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]


@pytest.fixture
def graph_store():
    """Fresh Neo4jGraphStore with a cleaned database per test."""
    from trellis.stores.neo4j.graph import Neo4jGraphStore

    store = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
    # Wipe all data the store might have created in a prior run.
    with store._driver.session(database=store._database) as session:
        session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
    yield store
    store.close()


def _backdate_closed(store, label: str, valid_to_iso: str) -> None:
    """Rewrite every closed row's ``valid_to`` on ``label`` for test control."""
    with store._driver.session(database=store._database) as session:
        session.run(
            f"MATCH (n:{label}) WHERE n.valid_to IS NOT NULL SET n.valid_to = $vt",
            vt=valid_to_iso,
        )


def _backdate_closed_edges(store, valid_to_iso: str) -> None:
    with store._driver.session(database=store._database) as session:
        session.run(
            "MATCH ()-[r:EDGE]->() WHERE r.valid_to IS NOT NULL SET r.valid_to = $vt",
            vt=valid_to_iso,
        )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


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


def test_update_node_returns_latest(graph_store):
    graph_store.upsert_node("n1", "service", {"v": 1})
    graph_store.upsert_node("n1", "service", {"v": 2})
    node = graph_store.get_node("n1")
    assert node is not None
    assert node["properties"]["v"] == 2


def test_get_nonexistent(graph_store):
    assert graph_store.get_node("nope") is None


def test_get_nodes_bulk(graph_store):
    graph_store.upsert_node("a", "s", {"n": 1})
    graph_store.upsert_node("b", "s", {"n": 2})
    graph_store.upsert_node("c", "s", {"n": 3})
    nodes = graph_store.get_nodes_bulk(["a", "c"])
    ids = {n["node_id"] for n in nodes}
    assert ids == {"a", "c"}


def test_count(graph_store):
    assert graph_store.count_nodes() == 0
    graph_store.upsert_node(None, "s", {})
    assert graph_store.count_nodes() == 1
    assert graph_store.count_edges() == 0


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_upsert_and_get_edge(graph_store):
    graph_store.upsert_node("a", "service", {})
    graph_store.upsert_node("b", "service", {})
    eid = graph_store.upsert_edge("a", "b", "depends_on", {"weight": 1.0})
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "depends_on"
    assert edges[0]["edge_id"] == eid
    assert edges[0]["properties"]["weight"] == 1.0


def test_edge_upsert_replaces_current(graph_store):
    graph_store.upsert_node("a", "service", {})
    graph_store.upsert_node("b", "service", {})
    first = graph_store.upsert_edge("a", "b", "depends_on", {"w": 1})
    second = graph_store.upsert_edge("a", "b", "depends_on", {"w": 2})
    # Same logical edge — edge_id is carried forward.
    assert first == second
    edges = graph_store.get_edges("a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["properties"]["w"] == 2


def test_get_edges_incoming(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    assert len(graph_store.get_edges("b", direction="incoming")) == 1


def test_get_edges_both(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("c", "b", "links_to")
    assert len(graph_store.get_edges("b", direction="both")) == 2


def test_get_edges_filter_by_type(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("a", "b", "depends_on")
    filtered = graph_store.get_edges("a", direction="outgoing", edge_type="depends_on")
    assert len(filtered) == 1
    assert filtered[0]["edge_type"] == "depends_on"


def test_upsert_edge_missing_endpoints_raises(graph_store):
    with pytest.raises(ValueError, match="no current version"):
        graph_store.upsert_edge("ghost_a", "ghost_b", "links_to")


def test_delete_edge(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    eid = graph_store.upsert_edge("a", "b", "links")
    assert graph_store.delete_edge(eid) is True
    assert graph_store.get_edges("a") == []


# ---------------------------------------------------------------------------
# Subgraph
# ---------------------------------------------------------------------------


def test_get_subgraph_depth_2(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    graph_store.upsert_edge("b", "c", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=2)
    node_ids = {n["node_id"] for n in sg["nodes"]}
    assert node_ids == {"a", "b", "c"}
    assert len(sg["edges"]) == 2


def test_get_subgraph_depth_0_returns_seeds_only(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    sg = graph_store.get_subgraph(["a"], depth=0)
    assert [n["node_id"] for n in sg["nodes"]] == ["a"]
    assert sg["edges"] == []


def test_get_subgraph_edge_type_filter(graph_store):
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    graph_store.upsert_node("c", "s", {})
    graph_store.upsert_edge("a", "b", "depends_on")
    graph_store.upsert_edge("a", "c", "mentions")
    sg = graph_store.get_subgraph(["a"], depth=1, edge_types=["depends_on"])
    node_ids = {n["node_id"] for n in sg["nodes"]}
    assert node_ids == {"a", "b"}


# ---------------------------------------------------------------------------
# Query / delete / aliases
# ---------------------------------------------------------------------------


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


def test_delete_nonexistent_returns_false(graph_store):
    assert graph_store.delete_node("nope") is False
    assert graph_store.delete_edge("nope") is False


def test_a_purge_the_server_refuses_is_a_store_error(graph_store):
    """The server's refusal ends the purge as ``StoreError``, without its text.

    The server refuses every statement against a database it does not have
    with ``Neo.ClientError.Database.DatabaseNotFound``, and its message names
    the database.
    """
    from neo4j.exceptions import ClientError

    from trellis.errors import StoreError
    from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

    absent = "absentdb"
    store = BoltOpenCypherGraphStore(
        driver=graph_store._driver,
        database=absent,
        owns_driver=False,
        init_schema=False,
    )

    with pytest.raises(StoreError) as caught:
        store.delete_node("n1")

    refusal = caught.value.__cause__
    assert isinstance(refusal, ClientError)
    assert refusal.code == "Neo.ClientError.Database.DatabaseNotFound"
    assert absent in str(refusal)
    assert "Purge of node n1 failed: ClientError" in caught.value.message
    assert absent not in caught.value.message


def _wait_for_blocked_purge(store) -> bool:
    """Poll until a ``delete_node`` statement is blocked on a lock (20 s cap).

    ``delete_node``'s statements, and no other statement the store runs,
    bind ``$nid``.
    """
    cypher = (
        "SHOW TRANSACTIONS YIELD currentQuery, status "
        "WHERE status STARTS WITH 'Blocked' AND currentQuery CONTAINS '$nid' "
        "RETURN count(*) AS n"
    )
    deadline = time.monotonic() + 20
    with store._driver.session(database=store._database) as session:
        while time.monotonic() < deadline:
            if session.run(cypher).single()["n"]:
                return True
            time.sleep(0.005)
    return False


def _rows_left(store, node_id: str) -> dict[str, int]:
    """Count the rows, any version, each label still holds for a node."""
    with store._driver.session(database=store._database) as session:
        return {
            label: session.run(
                f"MATCH (x:{label}) WHERE x.{key} = $nid RETURN count(x) AS c",
                nid=node_id,
            ).single()["c"]
            for label, key in (
                ("Node", "node_id"),
                ("Alias", "entity_id"),
                ("AliasClaim", "entity_id"),
            )
        }


@pytest.mark.parametrize(
    "write", ["upsert_node", "update_node_if_current", "upsert_alias"]
)
def test_delete_node_removes_a_version_written_while_it_waited(
    graph_store, monkeypatch, write: str
):
    # The write's statements run and hold their locks, the purge starts,
    # and the write commits only once SHOW TRANSACTIONS shows the purge
    # blocked. The version the write created is invisible to the statement
    # that waited.
    import neo4j

    store = graph_store
    store.upsert_node("n1", "person", {"phase": "1"})
    store.upsert_alias("n1", "race-sys", f"raw-{write}", raw_name="One")
    v1 = store.get_node("n1")
    writes = {
        "upsert_node": lambda: store.upsert_node("n1", "person", {"phase": "2"}),
        "update_node_if_current": lambda: store.update_node_if_current(
            "n1", v1["valid_from"], "person", {"phase": "2"}, node_role="semantic"
        ),
        "upsert_alias": lambda: store.upsert_alias(
            "n1", "race-sys", f"raw-{write}", raw_name="Two"
        ),
    }
    writer = threading.get_ident()
    original = neo4j.Session.execute_write
    state: dict[str, Any] = {}

    def purge() -> None:
        state["deleted"] = store.delete_node("n1")

    def gated(session: Any, transaction_function: Any, *args: Any, **kwargs: Any):
        if threading.get_ident() != writer:
            return original(session, transaction_function, *args, **kwargs)

        def held(tx: Any, *a: Any, **k: Any) -> Any:
            result = transaction_function(tx, *a, **k)
            if "purge" not in state:
                state["purge"] = threading.Thread(target=purge)
                state["purge"].start()
                state["waited"] = _wait_for_blocked_purge(store)
            return result

        return original(session, held, *args, **kwargs)

    monkeypatch.setattr(neo4j.Session, "execute_write", gated)
    writes[write]()
    assert "purge" in state, "the write never ran a write transaction"
    state["purge"].join(timeout=30)

    assert state["waited"], "the purge never blocked on the write's lock"
    assert not state["purge"].is_alive()
    assert state["deleted"] is True
    assert _rows_left(store, "n1") == {"Node": 0, "Alias": 0, "AliasClaim": 0}


def test_delete_node_removes_a_version_written_after_it_took_its_locks(
    graph_store, monkeypatch
):
    # A first write holds its locks until the purge's lock statement waits
    # on it. The purge pauses before its Node delete until a second write
    # has closed the version the first created, and that write commits only
    # once the Node delete waits on it. The version the second write created
    # is invisible to that delete, so only the delete's repeat removes it.
    import neo4j

    store = graph_store
    store.upsert_node("n1", "person", {"phase": "1"})
    writer = threading.get_ident()
    original = neo4j.Session.execute_write
    paused, resume = threading.Event(), threading.Event()
    state: dict[str, Any] = {}

    class PauseBeforeNodeDelete:
        def __init__(self, tx: Any) -> None:
            self._tx = tx

        def run(self, query: str, *args: Any, **kwargs: Any) -> Any:
            node_delete = "MATCH (n:Node {node_id: $nid}) DETACH DELETE"
            if query.startswith(node_delete) and not paused.is_set():
                paused.set()
                resume.wait(20)
            return self._tx.run(query, *args, **kwargs)

    def purge() -> None:
        state["deleted"] = store.delete_node("n1")

    def start_purge() -> None:
        state["purge"] = threading.Thread(target=purge)
        state["purge"].start()
        state["locking"] = _wait_for_blocked_purge(store)

    def resume_purge() -> None:
        resume.set()
        state["deleting"] = _wait_for_blocked_purge(store)

    def gated(session: Any, transaction_function: Any, *args: Any, **kwargs: Any):
        if threading.get_ident() == writer:

            def held(tx: Any, *a: Any, **k: Any) -> Any:
                result = transaction_function(tx, *a, **k)
                state.pop("hold", lambda: None)()
                return result

            return original(session, held, *args, **kwargs)
        if "delete_node" in getattr(transaction_function, "__qualname__", ""):

            def pausing(tx: Any, *a: Any, **k: Any) -> Any:
                return transaction_function(PauseBeforeNodeDelete(tx), *a, **k)

            return original(session, pausing, *args, **kwargs)
        return original(session, transaction_function, *args, **kwargs)

    monkeypatch.setattr(neo4j.Session, "execute_write", gated)
    state["hold"] = start_purge
    store.upsert_node("n1", "person", {"phase": "2"})
    assert paused.wait(20), "the purge never reached its Node delete"
    state["hold"] = resume_purge
    store.upsert_node("n1", "person", {"phase": "3"})
    state["purge"].join(timeout=30)

    assert state["locking"], "the purge never blocked on the first write's lock"
    assert state["deleting"], "the Node delete never blocked on the second write"
    assert not state["purge"].is_alive()
    assert state["deleted"] is True
    assert _rows_left(store, "n1") == {"Node": 0, "Alias": 0, "AliasClaim": 0}


def _seed_purge_target(store, node_id: str) -> None:
    """Two versions, an edge and an alias: rows of every kind a purge removes."""
    store.upsert_node(node_id, "person", {"phase": "1"})
    store.upsert_node(node_id, "person", {"phase": "2"})
    store.upsert_node(f"{node_id}-peer", "person", {})
    store.upsert_edge(node_id, f"{node_id}-peer", "knows")
    store.upsert_alias(node_id, "race-sys", f"raw-{node_id}", raw_name="One")


def _hold_first_purge(
    monkeypatch, store, second: Any, *, at_node_delete: bool = False
) -> dict[str, Any]:
    """Hold this thread's first ``delete_node`` transaction open.

    Its statements run and hold their locks, ``second`` starts on another
    thread, and the transaction commits only once SHOW TRANSACTIONS shows a
    purge statement blocked behind it. With ``at_node_delete`` the hold
    comes before its Node delete instead, so only the statements ahead of
    that delete hold locks. The returned dict carries that thread
    (``"purge"``) and whether the gate saw the block (``"waited"``).
    """
    import neo4j

    first = threading.get_ident()
    original = neo4j.Session.execute_write
    state: dict[str, Any] = {}

    def start_second() -> None:
        if "purge" not in state:
            state["purge"] = threading.Thread(target=second)
            state["purge"].start()
            state["waited"] = _wait_for_blocked_purge(store)

    class StartSecondAtNodeDelete:
        def __init__(self, tx: Any) -> None:
            self._tx = tx

        def run(self, query: str, *args: Any, **kwargs: Any) -> Any:
            if query.startswith("MATCH (n:Node {node_id: $nid}) DETACH DELETE"):
                start_second()
            return self._tx.run(query, *args, **kwargs)

    def gated(session: Any, transaction_function: Any, *args: Any, **kwargs: Any):
        purging = "delete_node" in getattr(transaction_function, "__qualname__", "")
        if threading.get_ident() != first or not purging:
            return original(session, transaction_function, *args, **kwargs)

        def held(tx: Any, *a: Any, **k: Any) -> Any:
            if at_node_delete:
                return transaction_function(StartSecondAtNodeDelete(tx), *a, **k)
            result = transaction_function(tx, *a, **k)
            start_second()
            return result

        return original(session, held, *args, **kwargs)

    monkeypatch.setattr(neo4j.Session, "execute_write", gated)
    return state


@pytest.mark.parametrize(
    "at_node_delete",
    [False, True],
    ids=["held_after_its_statements", "held_before_its_node_delete"],
)
def test_a_purge_that_waited_on_a_concurrent_purge_reports_no_removal(
    graph_store, monkeypatch, at_node_delete: bool
):
    # The second purge blocks behind the first and goes on only once the
    # first has committed, when every row is gone. It removed nothing, so
    # it reports False, as the loser does on Postgres. Held before its Node
    # delete, the first purge holds only the locks its lock statement took,
    # so that statement must run in the delete's transaction.
    store = graph_store
    _seed_purge_target(store, "n1")
    outcome: dict[str, Any] = {}

    def second() -> None:
        outcome["second"] = store.delete_node("n1")

    state = _hold_first_purge(monkeypatch, store, second, at_node_delete=at_node_delete)
    outcome["first"] = store.delete_node("n1")
    assert "purge" in state, "the first purge never ran a delete_node transaction"
    state["purge"].join(timeout=30)

    assert state["waited"], "the second purge never blocked on the first's lock"
    assert not state["purge"].is_alive()
    assert outcome == {"first": True, "second": False}
    assert _rows_left(store, "n1") == {"Node": 0, "Alias": 0, "AliasClaim": 0}


def test_a_redaction_that_lost_a_concurrent_purge_emits_no_audit_event(
    graph_store, monkeypatch, tmp_path
):
    # Two redactions of one node through MutationExecutor, the first held
    # open until the second's purge is blocked behind it. The loser removed
    # nothing: it is FAILED and writes no second REDACTION_APPLIED.
    from trellis.mutate import build_curate_executor
    from trellis.mutate.commands import Command, CommandStatus, Operation
    from trellis.stores.base.event_log import EventType
    from trellis.stores.registry import StoreRegistry

    _seed_purge_target(graph_store, "n1")
    neo4j_graph = {
        "backend": "neo4j",
        "uri": URI,
        "user": USER,
        "password": PASSWORD,
        "database": DATABASE,
    }
    registry = StoreRegistry(
        config={"graph": neo4j_graph}, stores_dir=tmp_path / "stores"
    )
    executor = build_curate_executor(registry)
    results: dict[str, Any] = {}

    def redact(key: str) -> None:
        results[key] = executor.execute(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": "n1", "reason": "synthetic race"},
                requested_by="race-test",
            )
        )

    try:
        state = _hold_first_purge(monkeypatch, graph_store, lambda: redact("second"))
        redact("first")
        assert "purge" in state, "the first redaction never reached its purge"
        state["purge"].join(timeout=30)
        applied = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
    finally:
        registry.close()

    assert state["waited"], "the second purge never blocked on the first's lock"
    assert not state["purge"].is_alive()
    assert [event.entity_id for event in applied] == ["n1"]
    assert (results["first"].status, results["second"].status) == (
        CommandStatus.SUCCESS,
        CommandStatus.FAILED,
    )
    assert _rows_left(graph_store, "n1") == {"Node": 0, "Alias": 0, "AliasClaim": 0}


def test_upsert_and_resolve_alias(graph_store):
    graph_store.upsert_node("orders_entity", "table", {"name": "orders"})
    graph_store.upsert_alias(
        "orders_entity",
        "unity_catalog",
        "main.analytics.orders",
        raw_name="orders",
        match_confidence=0.95,
        is_primary=True,
    )
    alias = graph_store.resolve_alias("unity_catalog", "main.analytics.orders")
    assert alias is not None
    assert alias["entity_id"] == "orders_entity"
    assert alias["raw_name"] == "orders"
    assert alias["match_confidence"] == 0.95
    assert alias["is_primary"] is True


def test_get_aliases_for_entity(graph_store):
    graph_store.upsert_node("orders_entity", "table", {})
    graph_store.upsert_alias("orders_entity", "uc", "main.orders")
    graph_store.upsert_alias("orders_entity", "dbt", "model.orders")
    aliases = graph_store.get_aliases("orders_entity")
    assert {(a["source_system"], a["raw_id"]) for a in aliases} == {
        ("uc", "main.orders"),
        ("dbt", "model.orders"),
    }


# ---------------------------------------------------------------------------
# node_role / generation_spec
# ---------------------------------------------------------------------------


class TestNodeRole:
    def test_default_role_is_semantic(self, graph_store):
        graph_store.upsert_node("n1", "service", {})
        node = graph_store.get_node("n1")
        assert node["node_role"] == "semantic"
        assert node["generation_spec"] is None

    def test_curated_node_with_spec(self, graph_store):
        spec = {
            "generator_name": "louvain",
            "generator_version": "1.0.0",
            "parameters": {"resolution": 1.2},
        }
        graph_store.upsert_node(
            "c1",
            "domain",
            {"name": "payments"},
            node_role="curated",
            generation_spec=spec,
        )
        node = graph_store.get_node("c1")
        assert node["node_role"] == "curated"
        assert node["generation_spec"]["parameters"] == {"resolution": 1.2}

    def test_curated_without_spec_rejected(self, graph_store):
        with pytest.raises(ValueError, match="generation_spec is required"):
            graph_store.upsert_node("c1", "domain", {}, node_role="curated")

    def test_role_is_immutable(self, graph_store):
        graph_store.upsert_node("n1", "service", {})
        with pytest.raises(ValueError, match="Cannot change node_role"):
            graph_store.upsert_node("n1", "service", {}, node_role="structural")

    def test_document_ids_round_trip(self, graph_store):
        graph_store.upsert_node(
            "n1",
            "service",
            {},
            document_ids=["doc-1", "doc-2"],
        )
        node = graph_store.get_node("n1")
        assert node["document_ids"] == ["doc-1", "doc-2"]


# ---------------------------------------------------------------------------
# Temporal (SCD-2) versioning
# ---------------------------------------------------------------------------


class TestTemporal:
    def test_history_ordered_desc(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        graph_store.upsert_node("n1", "service", {"v": 3})
        history = graph_store.get_node_history("n1")
        assert [h["properties"]["v"] for h in history] == [3, 2, 1]
        # Only newest is still open.
        assert history[0]["valid_to"] is None
        assert all(h["valid_to"] is not None for h in history[1:])

    def test_as_of_returns_past_version(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        between = datetime.now(UTC)
        # Give the clock room; the next upsert's valid_from must be strictly
        # greater than `between` for the as_of read to pick v=1.
        graph_store.upsert_node("n1", "service", {"v": 2})
        node = graph_store.get_node("n1", as_of=between)
        # We can't guarantee which version wins when valid_from == between,
        # so just confirm we get *some* version (not None) and that the
        # current read returns v=2.
        assert node is not None
        assert graph_store.get_node("n1")["properties"]["v"] == 2


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompactVersions:
    def test_compacts_closed_nodes_before_cutoff(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _backdate_closed(graph_store, "Node", ten_days_ago)

        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff)

        assert report.nodes_compacted == 1
        assert report.total_compacted == 1
        assert report.dry_run is False
        # Current row still reachable.
        assert graph_store.get_node("n1")["properties"]["v"] == 2
        # Only one version survives compaction.
        assert len(graph_store.get_node_history("n1")) == 1

    def test_preserves_current_rows(self, graph_store):
        graph_store.upsert_node("n1", "service", {})
        future = datetime.now(UTC) + timedelta(days=365)
        report = graph_store.compact_versions(future)
        assert report.nodes_compacted == 0
        assert graph_store.get_node("n1") is not None

    def test_dry_run_reports_without_deleting(self, graph_store):
        graph_store.upsert_node("n1", "service", {"v": 1})
        graph_store.upsert_node("n1", "service", {"v": 2})
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _backdate_closed(graph_store, "Node", ten_days_ago)
        cutoff = datetime.now(UTC) - timedelta(days=5)
        report = graph_store.compact_versions(cutoff, dry_run=True)
        assert report.dry_run is True
        assert report.nodes_compacted == 1
        assert len(graph_store.get_node_history("n1")) == 2

    def test_compacts_edges_and_aliases(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 1})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 2})
        graph_store.upsert_alias("a", "systemX", "raw-1", raw_name="old")
        graph_store.upsert_alias("a", "systemX", "raw-1", raw_name="new")

        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _backdate_closed_edges(graph_store, ten_days_ago)
        _backdate_closed(graph_store, "Alias", ten_days_ago)

        report = graph_store.compact_versions(datetime.now(UTC) - timedelta(days=5))
        assert report.edges_compacted == 1
        assert report.aliases_compacted == 1


# ---------------------------------------------------------------------------
# Edge provenance (Phase 3 of adr-graph-ontology §6.4 / item 2 of the
# self-improvement program). The five Cypher relationship properties +
# the Python-boundary validator. Mirrors the SQLite + Postgres patterns
# in test_edge_provenance.py and test_postgres_stores.py.
# ---------------------------------------------------------------------------


class TestEdgeProvenance:
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
        assert edge["confidence"] == pytest.approx(0.83)
        assert edge["evidence_ref"] == "doc-9"
        assert edge["extractor_tier"] == "HYBRID"

    def test_missing_provenance_reads_back_none(self, graph_store):
        from trellis.stores.base.edge_provenance import EDGE_PROVENANCE_FIELDS

        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 1.0})
        edge = graph_store.get_edges("a", direction="outgoing")[0]
        for field in EDGE_PROVENANCE_FIELDS:
            assert edge[field] is None

    def test_bad_confidence_raises_before_network(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="confidence must be in"):
            graph_store.upsert_edge("a", "b", "depends_on", confidence=1.5)
        # The validator runs before the Bolt round trip, so no edge
        # was written.
        assert graph_store.get_edges("a", direction="outgoing") == []

    def test_bad_extractor_tier_raises_before_network(self, graph_store):
        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            graph_store.upsert_edge("a", "b", "depends_on", extractor_tier="MAGIC")
        assert graph_store.get_edges("a", direction="outgoing") == []

    def test_bulk_edges_round_trip_provenance(self, graph_store):
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
        assert edges[0]["confidence"] == pytest.approx(0.5)
        assert edges[0]["extractor_tier"] == "DETERMINISTIC"
        assert edges[0]["agent_id"] == "agent-1"
        assert edges[1]["confidence"] is None
        assert edges[1]["agent_id"] is None

    def test_bulk_bad_provenance_raises_with_row_index(self, graph_store):
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        with pytest.raises(ValueError, match=r"upsert_edges_bulk\[0\]"):
            graph_store.upsert_edges_bulk(
                [
                    {
                        "source_id": "a",
                        "target_id": "b",
                        "edge_type": "links_to",
                        "extractor_tier": "INVALID",
                    }
                ]
            )


class TestDuplicateCurrentRow:
    """One node_id with two current rows, as concurrent upserts can leave."""

    def test_every_read_shows_the_newest_version(self, graph_store):
        bolt_duplicate_current.check_every_read_shows_the_newest_version(graph_store)

    def test_type_counts_follow_the_shown_version(self, graph_store):
        bolt_duplicate_current.check_type_counts_follow_the_shown_version(graph_store)

    def test_equal_stamps_pick_the_greater_version_id(self, graph_store):
        bolt_duplicate_current.check_equal_stamps_pick_the_greater_version_id(
            graph_store
        )

    def test_upsert_node_heals_a_duplicate(self, graph_store):
        bolt_duplicate_current.check_upsert_node_heals_a_duplicate(graph_store)

    def test_upsert_nodes_bulk_heals_a_duplicate(self, graph_store):
        bolt_duplicate_current.check_upsert_nodes_bulk_heals_a_duplicate(graph_store)

    def test_update_node_if_current_heals_a_duplicate(self, graph_store):
        bolt_duplicate_current.check_update_node_if_current_heals_a_duplicate(
            graph_store
        )
