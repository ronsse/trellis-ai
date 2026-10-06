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
import threading
from typing import Any

import pytest

pytest.importorskip("neo4j")

from neo4j import ManagedTransaction

from tests.unit.stores import bolt_duplicate_current

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


def test_a_purge_whose_commit_lost_to_a_concurrent_purge_reports_no_removal(
    graph_store, monkeypatch
):
    # ArcadeDB never blocks a purge. The second purge's first attempt runs
    # its statements while the first purge's transaction is open, the first
    # commits, the second's commit conflicts, and the driver re-runs its
    # transaction function, which finds nothing left to remove.
    import neo4j

    store = graph_store
    store.upsert_node("n1", "person", {"phase": "1"})
    store.upsert_node("n1", "person", {"phase": "2"})
    store.upsert_node("n1-peer", "person", {})
    store.upsert_edge("n1", "n1-peer", "knows")
    store.upsert_alias("n1", "race-sys", "raw-n1", raw_name="One")
    first = threading.get_ident()
    original = neo4j.Session.execute_write
    second_ran, first_done = threading.Event(), threading.Event()
    state: dict[str, Any] = {"second_attempts": 0}

    def second() -> None:
        state["second"] = store.delete_node("n1")

    def gated(session: Any, transaction_function: Any, *args: Any, **kwargs: Any):
        if "delete_node" not in getattr(transaction_function, "__qualname__", ""):
            return original(session, transaction_function, *args, **kwargs)

        def held(tx: Any, *a: Any, **k: Any) -> Any:
            result = transaction_function(tx, *a, **k)
            if "purge" not in state:
                state["purge"] = threading.Thread(target=second)
                state["purge"].start()
                state["overlapped"] = second_ran.wait(15)
            return result

        def overlapping(tx: Any, *a: Any, **k: Any) -> Any:
            result = transaction_function(tx, *a, **k)
            state["second_attempts"] += 1
            if state["second_attempts"] == 1:
                second_ran.set()
                state["first_committed"] = first_done.wait(15)
            return result

        wrapper = held if threading.get_ident() == first else overlapping
        return original(session, wrapper, *args, **kwargs)

    monkeypatch.setattr(neo4j.Session, "execute_write", gated)
    state["first"] = store.delete_node("n1")
    first_done.set()
    assert "purge" in state, "the first purge never ran a delete_node transaction"
    state["purge"].join(timeout=60)

    assert state["overlapped"], "the second purge never ran inside the first's"
    assert state["first_committed"]
    assert state["second_attempts"] >= 2, "the second purge's commit never conflicted"
    assert not state["purge"].is_alive()
    assert (state["first"], state["second"]) == (True, False)
    assert _rows_left(store, "n1") == {"Node": 0, "Alias": 0, "AliasClaim": 0}


def test_a_purge_the_server_refuses_is_a_store_error(graph_store):
    """The server's refusal ends the purge as ``StoreError``, without its text.

    ArcadeDB refuses a transaction against a database it does not have, and
    its message names the database.
    """
    from neo4j.exceptions import Neo4jError

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
    assert isinstance(refusal, Neo4jError)
    assert absent in str(refusal)
    assert f"Purge of node n1 failed: {type(refusal).__name__}" in caught.value.message
    assert absent not in caught.value.message


def _lose_the_purge_commit(monkeypatch, *, committed: bool) -> list[Exception]:
    """Raise ``IncompleteCommit`` after each ``delete_node`` transaction.

    The driver raises it when the connection drops with the commit
    outstanding. With ``committed`` the purge commits first; otherwise its
    statements run and are rolled back. Returns the errors raised so far.
    """
    import neo4j
    from neo4j.exceptions import IncompleteCommit

    original = neo4j.Session.execute_write
    raised: list[Exception] = []

    def lost(session: Any, transaction_function: Any, *args: Any, **kwargs: Any):
        if "delete_node" not in getattr(transaction_function, "__qualname__", ""):
            return original(session, transaction_function, *args, **kwargs)
        if committed:
            original(session, transaction_function, *args, **kwargs)
        else:
            with session.begin_transaction() as tx:
                transaction_function(tx, *args, **kwargs)
                tx.rollback()
        msg = "synthetic lost commit"
        raised.append(IncompleteCommit(msg))
        raise raised[-1]

    monkeypatch.setattr(neo4j.Session, "execute_write", lost)
    return raised


def _seed_purge_target(store, node_id: str) -> None:
    """Two versions, an edge and an alias: rows of every kind a purge removes."""
    store.upsert_node(node_id, "person", {"phase": "1"})
    store.upsert_node(node_id, "person", {"phase": "2"})
    store.upsert_node(f"{node_id}-peer", "person", {})
    store.upsert_edge(node_id, f"{node_id}-peer", "knows")
    store.upsert_alias(node_id, "race-sys", f"raw-{node_id}", raw_name="One")


def test_a_lost_commit_the_purge_made_is_the_purge(graph_store, monkeypatch):
    store = graph_store
    _seed_purge_target(store, "n1")
    lost = _lose_the_purge_commit(monkeypatch, committed=True)

    assert store.delete_node("n1") is True
    assert len(lost) == 1
    assert _rows_left(store, "n1") == {"Node": 0, "Alias": 0, "AliasClaim": 0}


def test_a_lost_commit_the_purge_did_not_make_is_a_failed_purge(
    graph_store, monkeypatch
):
    from trellis.errors import StoreError

    store = graph_store
    _seed_purge_target(store, "n1")
    seeded = _rows_left(store, "n1")
    lost = _lose_the_purge_commit(monkeypatch, committed=False)

    with pytest.raises(StoreError) as caught:
        store.delete_node("n1")

    assert lost == [caught.value.__cause__]
    assert caught.value.message == "Purge of node n1 failed: IncompleteCommit"
    assert seeded["Node"] == 2
    assert _rows_left(store, "n1") == seeded


@pytest.mark.parametrize(
    ("source_id", "target_id"),
    [("ghost_a", "ghost_b"), ("ghost_a", "a"), ("a", "ghost_b")],
)
def test_upsert_edge_missing_endpoints_raises(graph_store, source_id, target_id):
    graph_store.upsert_node("a", "s", {})
    with pytest.raises(ValueError, match="no current version"):
        graph_store.upsert_edge(source_id, target_id, "links_to")
    assert graph_store.count_edges() == 0


def _purge_after_endpoint_check(store, monkeypatch, gone: str) -> None:
    """Purge ``gone`` once ``upsert_edges_bulk``'s endpoint check has found
    it current, so the write runs without it.

    ``_fetch_current_node_id_set`` also backs the post-rollback re-read that
    runs after the dropped-row write, so this fires on the first call only —
    a second call finds ``gone`` already purged and just passes through.
    """
    check = store._fetch_current_node_id_set
    purged = False

    def check_then_purge(session, node_ids):
        nonlocal purged
        found = check(session, node_ids)
        if not purged:
            assert store.delete_node(gone)
            purged = True
        return found

    monkeypatch.setattr(store, "_fetch_current_node_id_set", check_then_purge)


def _raw_counts(store) -> tuple[int, int]:
    """Count every vertex and relationship, whatever its label."""
    with store._driver.session(database=store._database) as session:
        nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    return nodes, rels


@pytest.mark.parametrize("vanished", ["source", "target"])
def test_upsert_edges_bulk_refuses_a_row_whose_endpoint_vanished(
    graph_store, monkeypatch, vanished
):
    """An endpoint purged between ``upsert_edges_bulk``'s endpoint check and
    its write fails the call with ValueError naming the row and the
    endpoint, and adds no edge or vertex."""
    graph_store.upsert_node("a", "s", {})
    graph_store.upsert_node("b", "s", {})
    gone = "a" if vanished == "source" else "b"
    _purge_after_endpoint_check(graph_store, monkeypatch, gone)
    nodes, rels = _raw_counts(graph_store)
    edge = {"source_id": "a", "target_id": "b", "edge_type": "links_to"}
    with pytest.raises(
        ValueError,
        match=rf"upsert_edges_bulk\[0\]: {vanished} '{gone}' has no current version",
    ):
        graph_store.upsert_edges_bulk([edge])
    assert _raw_counts(graph_store) == (nodes - 1, rels)


def test_upsert_edges_bulk_refuses_a_dropped_row_beside_a_duplicated_edge(
    graph_store, monkeypatch
):
    """Row 0's edge has two current versions, so the write returns a record
    for each: as many records as rows, though row 1, whose target was purged
    after the endpoint check, wrote nothing. The call names row 1, and its
    transaction rolls back, so row 0's edge gains no version."""
    for node_id in ("a", "b", "c", "d"):
        graph_store.upsert_node(node_id, "s", {})
    graph_store.upsert_edge("a", "b", "links_to")
    with graph_store._driver.session(database=graph_store._database) as session:
        session.run(
            "MATCH (s:Node {node_id: 'a'})-[r:EDGE]->(t:Node {node_id: 'b'}) "
            "CREATE (s)-[copy:EDGE]->(t) SET copy = properties(r)"
        ).consume()
    _purge_after_endpoint_check(graph_store, monkeypatch, "d")
    nodes, rels = _raw_counts(graph_store)
    assert rels == 2
    edges = [
        {"source_id": "a", "target_id": "b", "edge_type": "links_to"},
        {"source_id": "c", "target_id": "d", "edge_type": "links_to"},
    ]
    with pytest.raises(
        ValueError, match=r"upsert_edges_bulk\[1\]: target 'd' has no current version"
    ):
        graph_store.upsert_edges_bulk(edges)
    assert _raw_counts(graph_store) == (nodes - 1, rels)


def test_upsert_edges_bulk_raises_valueerror_when_endpoint_recreated_mid_write(
    graph_store, monkeypatch
):
    """Row 1's target is purged after the endpoint check, so the write
    drops row 1, and another writer re-creates it right after the UNWIND,
    while the transaction that wrote row 0 is still open. The call raises
    the documented ``ValueError``, not a raw driver error, and writes no
    edge."""
    for node_id in ("a", "b", "c", "d"):
        graph_store.upsert_node(node_id, "s", {})
    _purge_after_endpoint_check(graph_store, monkeypatch, "d")

    orig_run = ManagedTransaction.run

    def recreate_after_unwind(self, query, parameters=None, **kw):
        result = orig_run(self, query, parameters, **kw)
        text = str(query)
        if "UNWIND $rows" in text and "CREATE (s)-[new:EDGE]->(t)" in text:
            records = list(result)
            # Another writer recreates "d" while this transaction is
            # still open, beside row 0's edge, which this UNWIND wrote.
            graph_store.upsert_node("d", "s", {})
            return records
        return result

    monkeypatch.setattr(ManagedTransaction, "run", recreate_after_unwind)

    nodes, rels = _raw_counts(graph_store)
    edges = [
        {"source_id": "a", "target_id": "b", "edge_type": "links_to"},
        {"source_id": "c", "target_id": "d", "edge_type": "links_to"},
    ]
    with pytest.raises(
        ValueError,
        match=r"upsert_edges_bulk\[1\]: source 'c' or target 'd' was not current",
    ):
        graph_store.upsert_edges_bulk(edges)
    assert _raw_counts(graph_store) == (nodes, rels)
    assert rels == 0


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
        from trellis.errors import StoreError
        from trellis.stores.arcadedb.base import execute_sql

        graph_store.upsert_node("a", "service", {})
        graph_store.upsert_node("b", "service", {})
        # First, land a valid edge so the EDGE type has a row.
        graph_store.upsert_edge("a", "b", "depends_on", confidence=0.5)
        # Try to UPDATE the confidence to an out-of-range value via
        # raw SQL. ArcadeDB's MIN/MAX constraint should reject this.
        with pytest.raises(StoreError):
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
        from trellis.errors import StoreError
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
            with pytest.raises(StoreError):
                execute_sql(
                    HTTP_URL,
                    USER,
                    PASSWORD,
                    DATABASE,
                    "UPDATE EDGE SET confidence = 2.5 WHERE edge_type = 'depends_on'",
                )
        finally:
            registry.close()


class TestAliasContentionClassification:
    """Pin how real ArcadeDB faults reach the client, and how we read them.

    #556: the predecessor of this check matched ``Record #N:M not found``
    in the exception *message*, and that text never crosses Bolt — so the
    branch was dead while looking covered, because its only passing
    subject was a hand-built exception in a unit test. **Every error in
    this class is produced by the server**, over the real driver, against
    the real schema. Nothing here constructs a ``Neo4jError``.

    The negative cases are the load-bearing half: without them
    :meth:`ArcadeDBGraphStore._is_alias_write_contention` could be
    ``return True`` and the positive cases would still pass.
    """

    @staticmethod
    def _capture(store, cypher, *, in_transaction, **params):
        """Run ``cypher`` and hand back the error the server produced."""
        from neo4j.exceptions import Neo4jError

        with store._driver.session(database=store._database) as session:
            try:
                if in_transaction:
                    session.execute_write(lambda tx: tx.run(cypher, **params).consume())
                else:
                    session.run(cypher, **params).consume()
            except Neo4jError as exc:
                return exc
        pytest.fail(f"expected {cypher!r} to fail, it succeeded")

    def _seed_claim(self, store, claim_key):
        with store._driver.session(database=store._database) as session:
            session.run("MERGE (c:AliasClaim {claim_key: $k})", k=claim_key).consume()

    # -- the duplicate half: base identity match, live subject ----------

    def test_transactional_duplicate_carries_its_cause(self, graph_store):
        """A claim duplicate at commit still names the constraint.

        This is what the base class's identity match reads, so the
        ArcadeDB override is not responsible for this half.
        """
        self._seed_claim(graph_store, "dup-cause")
        exc = self._capture(
            graph_store,
            "CREATE (c:AliasClaim {claim_key: $k})",
            in_transaction=True,
            k="dup-cause",
        )
        assert "duplicated key" in str(exc).lower(), (
            "the constraint's identity is what the portable predicate "
            f"matches on; got {exc!s}"
        )
        assert graph_store._is_alias_write_contention(exc)

    # -- the erasure that killed the regex ------------------------------

    def test_autocommit_duplicate_loses_its_cause_to_the_generic_bucket(
        self, graph_store
    ):
        """The same fault, without its cause, on the generic code.

        ArcadeDB serializes only the outer exception's own text across
        Bolt. Provoking *one* fault two ways shows the drop directly:
        at commit the cause arrives, in autocommit it does not and the
        code degrades to the generic bucket. That is the path a
        stale-record replacement takes, which is why matching on the
        message could never work and this override matches on the code.

        If ArcadeDB ever forwards the cause chain, this test fails — and
        a message-shaped check becomes available again.
        """
        from trellis.stores.arcadedb.graph import _GENERIC_ENGINE_ERROR_CODE

        self._seed_claim(graph_store, "dup-erased")
        exc = self._capture(
            graph_store,
            "CREATE (c:AliasClaim {claim_key: $k})",
            in_transaction=False,
            k="dup-erased",
        )
        assert exc.code == _GENERIC_ENGINE_ERROR_CODE
        assert "duplicated key" not in str(exc).lower(), (
            f"cause chain unexpectedly survived the Bolt boundary — got {exc!s}"
        )
        assert "Record #" not in str(exc), (
            "the RID the retired regex matched is still absent; if this "
            "fires, revisit the message-shaped check #556 removed"
        )
        assert graph_store._is_alias_write_contention(exc)

    # -- the imprecision, stated rather than hidden ---------------------

    def test_generic_engine_fault_is_read_as_contention(self, graph_store):
        """An evaluation fault shares the bucket, and does retry 3x.

        Accepted deliberately: this predicate is reachable only from
        ``_execute_alias_write``, whose Cypher is a fixed literal with
        typed string parameters, so an evaluation fault there is a code
        bug that surfaces identically after three attempts. Asserted so
        the cost is recorded rather than discovered.
        """
        exc = self._capture(
            graph_store, "RETURN 'abc' + {a: 1} * 3", in_transaction=False
        )
        assert graph_store._is_alias_write_contention(exc)

    # -- negative controls ----------------------------------------------

    @pytest.mark.parametrize(
        ("label", "cypher"),
        [
            ("syntax", "THIS IS NOT CYPHER AT ALL"),
            ("near-miss syntax", "MATCH (n) RETRUN n"),
            ("unknown function", "RETURN nosuchfunction(1)"),
            ("arithmetic", "RETURN 1/0"),
        ],
    )
    def test_statement_faults_are_not_read_as_contention(
        self, graph_store, label, cypher
    ):
        """These carry their own codes, so retrying them would be wrong.

        Together with the positive cases these are what make the check
        a classification rather than an unconditional retry.
        """
        exc = self._capture(graph_store, cypher, in_transaction=False)
        assert not graph_store._is_alias_write_contention(exc), (
            f"{label} fault would now be retried 3x: code={exc.code} msg={exc!s}"
        )

    def test_missing_parameter_is_not_read_as_contention(self, graph_store):
        exc = self._capture(
            graph_store, "MATCH (n {x: $nope}) RETURN n", in_transaction=False
        )
        assert not graph_store._is_alias_write_contention(exc)


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

    @pytest.mark.parametrize("duplicated_end", ["source", "target"])
    def test_upsert_edge_writes_one_version(self, graph_store, duplicated_end):
        bolt_duplicate_current.check_upsert_edge_writes_one_version(
            graph_store, duplicated_end
        )

    @pytest.mark.parametrize("duplicated_end", ["source", "target"])
    def test_upsert_edges_bulk_writes_one_version(self, graph_store, duplicated_end):
        bolt_duplicate_current.check_upsert_edges_bulk_writes_one_version(
            graph_store, duplicated_end
        )

    def test_get_nodes_bulk_shows_one_version(self, graph_store):
        bolt_duplicate_current.check_get_nodes_bulk_shows_one_version(graph_store)

    def test_get_subgraph_shows_one_version(self, graph_store):
        bolt_duplicate_current.check_get_subgraph_shows_one_version(graph_store)

    def test_query_shows_one_version(self, graph_store):
        bolt_duplicate_current.check_query_shows_one_version(graph_store)

    def test_execute_node_query_shows_one_version(self, graph_store):
        bolt_duplicate_current.check_execute_node_query_shows_one_version(graph_store)

    def test_filtered_listing_as_of_shows_the_version_then(self, graph_store):
        bolt_duplicate_current.check_filtered_listing_as_of_shows_the_version_then(
            graph_store
        )
