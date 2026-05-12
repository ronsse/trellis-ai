"""ArcadeDB Phase-0 spike — archived reference for the ArcadeDB substrate decision.

Run via: PYTHONPATH=src python eval/arcadedb_spike.py

This script validated the Cypher patterns that
:class:`trellis.stores.bolt_opencypher.graph.BoltOpenCypherGraphStore`
depends on against ArcadeDB before the production
:class:`trellis.stores.arcadedb.graph.ArcadeDBGraphStore` adapter
landed. It remains in the tree as a self-contained re-validation tool
for future ArcadeDB upgrades: a single ``python eval/arcadedb_spike.py``
run produces a fresh punch list that can be compared against the
previously-verified baseline.

What it covers:

- Bolt driver connectivity at ``bolt://localhost:7687``.
- Schema DDL (constraints + node indexes + relationship indexes).
- SCD-2 close-then-insert via ``OPTIONAL MATCH`` + ``SET`` +
  ``coalesce`` (both hot and cold UNWIND paths).
- ``*1..N`` variable-length paths + ``DETACH DELETE``.
- Parameter binding.

What it does NOT cover (handled separately):

- The vector store — :class:`ArcadeDBVectorStore` uses SQL-over-HTTP,
  not Cypher-over-Bolt. The "Vector type + similarity" section below
  intentionally probes Neo4j-style Cypher vector syntax and is
  expected to FAIL on ArcadeDB; that's why
  :class:`ArcadeDBVectorStore` uses the documented
  ``LSM_VECTOR`` SQL index instead. See
  :mod:`trellis.stores.arcadedb.vector` for the production path.
- Full contract coverage — that's
  ``tests/unit/stores/contracts/test_arcadedb_graph_contract.py``
  (76/76) and ``tests/unit/stores/test_arcadedb_vector.py`` (17/17).

Pre-existing schema in ``trellis_spike`` (left over from earlier runs)
may cause some "insert node with embedding" steps to fail when the
property has already been declared with a stricter type than the
inline ``[0.1, 0.2, 0.3]`` literal can satisfy. That's a state
artifact, not a regression — drop and recreate the database in
ArcadeDB Studio (or pick a fresh DATABASE name in this file) to
re-baseline.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import ClientError, DatabaseError, Neo4jError

URI = "bolt://localhost:7687"
USER = "root"
PASSWORD = "playwithdata"
# ArcadeDB Bolt protocol uses a per-database session; the "database" in
# the driver is the ArcadeDB database name. Default is "graph" for
# the GremlinServerPlugin's default; we'll create our own.
DATABASE = "trellis_spike"


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}OK{RESET}    {label}{(' — ' + detail) if detail else ''}")


def fail(label: str, exc: BaseException) -> None:
    print(f"  {RED}FAIL{RESET}  {label} — {type(exc).__name__}: {exc}")


def skip(label: str, detail: str) -> None:
    print(f"  {YELLOW}SKIP{RESET}  {label} — {detail}")


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def main() -> int:
    print(f"Connecting to {URI} as {USER!r}...")
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        driver.verify_connectivity()
        ok("driver connected", URI)
    except Exception as exc:
        fail("driver connect", exc)
        return 1

    # ------------------------------------------------------------------
    # Create a clean database for the spike
    # ------------------------------------------------------------------
    section("Database create")
    # ArcadeDB exposes database create via system commands; the Bolt
    # session-level `database` selection assumes the database already
    # exists. We use the system-database "system" workaround OR call
    # the HTTP API. Try HTTP first since it's the documented path.
    import urllib.error
    import urllib.request
    import base64

    auth_header = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        f"http://localhost:2480/api/v1/server",
        data=(f'{{"command": "create database {DATABASE}"}}').encode(),
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok("create database via HTTP", resp.read().decode()[:120])
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        if "already exists" in body.lower():
            ok("database already exists", DATABASE)
        else:
            fail("create database", RuntimeError(f"HTTP {exc.code}: {body[:200]}"))
            return 1
    except Exception as exc:
        fail("create database", exc)
        return 1

    # ------------------------------------------------------------------
    # Basic connection-level Cypher
    # ------------------------------------------------------------------
    section("Basic Cypher")

    def run(label: str, cypher: str, **params: Any) -> tuple[bool, Any]:
        try:
            with driver.session(database=DATABASE) as session:
                rec = session.run(cypher, **params).consume()
                ok(label)
                return True, rec
        except Neo4jError as exc:
            fail(label, exc)
            return False, exc
        except Exception as exc:
            fail(label, exc)
            return False, exc

    def query(label: str, cypher: str, **params: Any) -> tuple[bool, Any]:
        try:
            with driver.session(database=DATABASE) as session:
                records = list(session.run(cypher, **params))
                ok(label, f"{len(records)} rows")
                return True, records
        except Exception as exc:
            fail(label, exc)
            return False, exc

    run("RETURN 1", "RETURN 1 AS x")
    run("parameter binding", "RETURN $x AS y", x=42)
    run("string parameter", "RETURN $s AS s", s="hello")
    run("list parameter", "RETURN $xs AS xs", xs=[1, 2, 3])
    run("dict parameter (flat)", "RETURN $m AS m", m={"a": 1, "b": "two"})

    # ------------------------------------------------------------------
    # Schema DDL — the SCHEMA_STATEMENTS surface
    # ------------------------------------------------------------------
    section("Schema DDL (SCHEMA_STATEMENTS surface)")

    ddls = [
        (
            "CREATE CONSTRAINT node_version_unique",
            "CREATE CONSTRAINT node_version_unique IF NOT EXISTS "
            "FOR (n:Node) REQUIRE n.version_id IS UNIQUE",
        ),
        (
            "CREATE CONSTRAINT alias_version_unique",
            "CREATE CONSTRAINT alias_version_unique IF NOT EXISTS "
            "FOR (a:Alias) REQUIRE a.version_id IS UNIQUE",
        ),
        (
            "CREATE INDEX node_id_idx",
            "CREATE INDEX node_id_idx IF NOT EXISTS FOR (n:Node) ON (n.node_id)",
        ),
        (
            "CREATE INDEX node_type_idx",
            "CREATE INDEX node_type_idx IF NOT EXISTS FOR (n:Node) ON (n.node_type)",
        ),
        (
            "CREATE INDEX node_role_idx",
            "CREATE INDEX node_role_idx IF NOT EXISTS FOR (n:Node) ON (n.node_role)",
        ),
        (
            "CREATE INDEX node_valid_idx (composite)",
            "CREATE INDEX node_valid_idx IF NOT EXISTS "
            "FOR (n:Node) ON (n.valid_from, n.valid_to)",
        ),
        (
            "CREATE INDEX alias_entity_idx",
            "CREATE INDEX alias_entity_idx IF NOT EXISTS FOR (a:Alias) ON (a.entity_id)",
        ),
        (
            "CREATE INDEX alias_lookup_idx (composite)",
            "CREATE INDEX alias_lookup_idx IF NOT EXISTS "
            "FOR (a:Alias) ON (a.source_system, a.raw_id)",
        ),
        (
            "CREATE INDEX edge_id_idx (relationship)",
            "CREATE INDEX edge_id_idx IF NOT EXISTS "
            "FOR ()-[r:EDGE]-() ON (r.edge_id)",
        ),
        (
            "CREATE INDEX edge_type_idx (relationship)",
            "CREATE INDEX edge_type_idx IF NOT EXISTS "
            "FOR ()-[r:EDGE]-() ON (r.edge_type)",
        ),
        (
            "CREATE INDEX edge_valid_idx (relationship composite)",
            "CREATE INDEX edge_valid_idx IF NOT EXISTS "
            "FOR ()-[r:EDGE]-() ON (r.valid_from, r.valid_to)",
        ),
    ]
    schema_results: dict[str, tuple[bool, Any]] = {}
    for label, cypher in ddls:
        schema_results[label] = run(label, cypher)

    # ------------------------------------------------------------------
    # SCD-2 close-then-insert pattern (the upsert_node payload)
    # ------------------------------------------------------------------
    section("SCD-2 close-then-insert (upsert_node)")

    # Wipe first
    run("wipe (DETACH DELETE)", "MATCH (n) DETACH DELETE n")

    # First insert — no prior version
    upsert_cypher = """
    OPTIONAL MATCH (old:Node {node_id: $node_id}) WHERE old.valid_to IS NULL
    WITH old, old.node_role AS prior_role
    WHERE prior_role IS NULL OR prior_role = $node_role
    SET old.valid_to = $now
    WITH old, coalesce(old.created_at, $now) AS created_at_carry
    CREATE (n:Node)
    SET n = $new_props
    SET n.created_at = created_at_carry
    RETURN n.node_id AS node_id
    """
    props_v1 = {
        "node_id": "node-1",
        "version_id": "v1",
        "node_type": "service",
        "node_role": "semantic",
        "properties_json": '{"k": "v1"}',
        "updated_at": "2026-05-11T00:00:00",
        "valid_from": "2026-05-11T00:00:00",
        "valid_to": None,
    }
    run(
        "upsert v1 (no prior)",
        upsert_cypher,
        node_id="node-1",
        now="2026-05-11T00:00:00",
        node_role="semantic",
        new_props=props_v1,
    )

    # Second insert — should close v1 and create v2
    props_v2 = {**props_v1, "version_id": "v2", "properties_json": '{"k": "v2"}',
                "valid_from": "2026-05-11T01:00:00", "updated_at": "2026-05-11T01:00:00"}
    run(
        "upsert v2 (close v1, create v2)",
        upsert_cypher,
        node_id="node-1",
        now="2026-05-11T01:00:00",
        node_role="semantic",
        new_props=props_v2,
    )

    # Assert: 2 :Node rows total for node-1; v1 has valid_to set; v2 has valid_to NULL.
    success, rows = query(
        "verify SCD-2 history",
        "MATCH (n:Node {node_id: $nid}) RETURN n.version_id AS v, n.valid_to AS to "
        "ORDER BY n.valid_from",
        nid="node-1",
    )
    if success and rows:
        history = [(r["v"], r["to"]) for r in rows]
        print(f"        history: {history}")
        if len(history) == 2 and history[0][1] is not None and history[1][1] is None:
            ok("SCD-2 versions correctly closed + open")
        else:
            fail("SCD-2 invariant", AssertionError(f"unexpected: {history}"))

    # ------------------------------------------------------------------
    # UNWIND bulk paths
    # ------------------------------------------------------------------
    section("UNWIND bulk paths")

    run("wipe again", "MATCH (n) DETACH DELETE n")

    # Hot path — CREATE-only UNWIND
    rows = [
        {"node_id": f"bulk-{i}", "props": {
            "node_id": f"bulk-{i}",
            "version_id": f"vbulk-{i}",
            "node_type": "doc",
            "node_role": "semantic",
            "properties_json": '{}',
            "created_at": "2026-05-11T00:00:00",
            "updated_at": "2026-05-11T00:00:00",
            "valid_from": "2026-05-11T00:00:00",
            "valid_to": None,
        }} for i in range(5)
    ]
    run(
        "UNWIND $rows AS row CREATE (n:Node) SET n = row.props (hot path)",
        "UNWIND $rows AS row CREATE (n:Node) SET n = row.props",
        rows=rows,
    )
    success, recs = query("count after bulk insert",
                          "MATCH (n:Node) WHERE n.valid_to IS NULL RETURN count(n) AS cnt")
    if success:
        cnt = recs[0]["cnt"]
        print(f"        count = {cnt}")

    # Cold path — OPTIONAL MATCH + close + create
    cold_cypher = """
    UNWIND $rows AS row
    OPTIONAL MATCH (old:Node {node_id: row.node_id})
      WHERE old.valid_to IS NULL
    WITH row, old, coalesce(old.created_at, row.props.valid_from)
         AS created_at_carry
    SET old.valid_to = row.props.valid_from
    WITH row, created_at_carry
    CREATE (n:Node)
    SET n = row.props
    SET n.created_at = created_at_carry
    """
    rows_v2 = [{"node_id": f"bulk-{i}", "props": {**rows[i]["props"], "version_id": f"vbulk-{i}-r2",
                "valid_from": "2026-05-11T02:00:00"}} for i in range(5)]
    run("UNWIND cold path (close + create)", cold_cypher, rows=rows_v2)

    # ------------------------------------------------------------------
    # Edges + variable-length paths + DETACH DELETE
    # ------------------------------------------------------------------
    section("Edges, variable-length paths, DETACH DELETE")

    # Re-wipe and create a small line graph: a -> b -> c -> d
    run("wipe for edge tests", "MATCH (n) DETACH DELETE n")
    for nid in ("a", "b", "c", "d"):
        run(
            f"create node {nid}",
            "CREATE (n:Node {node_id: $nid, node_type: 't', node_role: 'semantic', "
            "properties_json: '{}', created_at: $now, updated_at: $now, "
            "valid_from: $now, valid_to: null, version_id: $vid})",
            nid=nid, now="2026-05-11T00:00:00", vid=f"v-{nid}",
        )
    edge_cypher = """
    MATCH (s:Node {node_id: $src}) WHERE s.valid_to IS NULL
    MATCH (t:Node {node_id: $dst}) WHERE t.valid_to IS NULL
    CREATE (s)-[r:EDGE {edge_id: $eid, edge_type: 'depends', source_id: $src,
                        target_id: $dst, properties_json: '{}',
                        created_at: $now, valid_from: $now, valid_to: null,
                        version_id: $vid}]->(t)
    """
    for s, d in (("a", "b"), ("b", "c"), ("c", "d")):
        run(
            f"create edge {s}->{d}",
            edge_cypher,
            src=s, dst=d, eid=f"e-{s}{d}", now="2026-05-11T00:00:00", vid=f"v-{s}{d}",
        )

    # Variable-length path — depth 2
    success, rows = query(
        "variable-length path *1..2 from a",
        "MATCH (seed:Node {node_id: 'a'}) WHERE seed.valid_to IS NULL "
        "OPTIONAL MATCH (seed)-[rels:EDGE*1..2]-(other:Node) "
        "WHERE all(e IN rels WHERE e.valid_to IS NULL) "
        "RETURN collect(DISTINCT other.node_id) AS reachable",
    )
    if success and rows:
        print(f"        reachable from a (depth 2): {rows[0]['reachable']}")

    success, rows = query(
        "variable-length path *1..3 from a (whole chain)",
        "MATCH (seed:Node {node_id: 'a'}) WHERE seed.valid_to IS NULL "
        "OPTIONAL MATCH (seed)-[rels:EDGE*1..3]-(other:Node) "
        "WHERE all(e IN rels WHERE e.valid_to IS NULL) "
        "RETURN collect(DISTINCT other.node_id) AS reachable",
    )
    if success and rows:
        print(f"        reachable from a (depth 3): {rows[0]['reachable']}")

    run("DETACH DELETE node a", "MATCH (n:Node {node_id: 'a'}) DETACH DELETE n")
    success, rows = query("count remaining nodes", "MATCH (n:Node) RETURN count(n) AS cnt")
    if success and rows:
        print(f"        remaining = {rows[0]['cnt']}")

    # ------------------------------------------------------------------
    # Vector type + similarity (ArcadeDB native)
    # ------------------------------------------------------------------
    section("Vector type + similarity")

    # ArcadeDB doesn't necessarily support Neo4j-style `CREATE VECTOR INDEX`.
    # Try the Neo4j syntax first, then fall back to ArcadeDB's documented
    # vector type via SQL.
    run("wipe for vector tests", "MATCH (n) DETACH DELETE n")
    run("insert node with embedding",
        "CREATE (n:Node {node_id: 'v1', embedding: [0.1, 0.2, 0.3], "
        "node_type: 't', valid_to: null, properties_json: '{}'})")
    run("insert another node with embedding",
        "CREATE (n:Node {node_id: 'v2', embedding: [0.9, 0.8, 0.7], "
        "node_type: 't', valid_to: null, properties_json: '{}'})")

    # Try Neo4j vector index syntax
    print("\n  -- Try Neo4j vector index syntax --")
    try_neo4j_vector = (
        "CREATE VECTOR INDEX node_embedding IF NOT EXISTS "
        "FOR (n:Node) ON n.embedding "
        "OPTIONS {indexConfig: {`vector.dimensions`: 3, "
        "`vector.similarity_function`: 'cosine'}}"
    )
    run("CREATE VECTOR INDEX (Neo4j syntax)", try_neo4j_vector)

    # Try SEARCH ... IN VECTOR INDEX
    run("SEARCH ... IN VECTOR INDEX (Cypher 25 syntax)",
        "MATCH (n:Node) SEARCH n IN VECTOR INDEX node_embedding "
        "FOR $vec LIMIT 5 SCORE AS score RETURN n.node_id, score",
        vec=[0.1, 0.2, 0.3])

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    section("Summary")
    schema_failures = [k for k, (ok_flag, _) in schema_results.items() if not ok_flag]
    if schema_failures:
        print(f"{YELLOW}Schema DDL gaps ({len(schema_failures)}):{RESET}")
        for k in schema_failures:
            print(f"  - {k}")
    else:
        print(f"{GREEN}All SCHEMA_STATEMENTS DDL applied cleanly.{RESET}")

    driver.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
