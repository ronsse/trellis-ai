# ADR: ArcadeDB as the Blessed Substrate for Graph + Vector

**Status:** Accepted
**Date:** 2026-05-11
**Deciders:** Trellis core
**Related:**
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — defines plane/substrate terminology and the "blessed default per plane" rule this ADR completes for the Knowledge plane.
- [`./adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) — the per-backend translation layer this work doesn't disturb (callers still go through the `GraphStore` ABC + canonical DSL).
- [`./adr-plugin-contract.md`](./adr-plugin-contract.md) — entry-point loader; new backends register the same way.
- [`../../src/trellis/stores/bolt_opencypher/`](../../src/trellis/stores/bolt_opencypher/) — shared base class extracted from the Neo4j backend.
- [`../../src/trellis/stores/arcadedb/`](../../src/trellis/stores/arcadedb/) — the new substrate.

---

## Context

Trellis needs a production graph + vector substrate that:

1. **Runs on AWS without licensing fees.** Neo4j AuraDB charges per cluster-hour; for a self-hosted Trellis deployment that's an avoidable cost.
2. **Speaks openCypher** so the existing ~1100 LOC of Cypher payload in `Neo4jGraphStore` remains usable.
3. **Supports SCD-2 versioned writes from multiple concurrent agents** — the immutable-trace contract requires real transactional semantics, not single-writer embedded mode.
4. **Has native HNSW vector indexing** so the vector plane can land on the same substrate as the graph plane (operational simplification).
5. **Uses an OSI-approved license** — important for downstream redistribution and managed-service deployments.

Four candidates were evaluated against these criteria:

| Candidate | Verdict |
|---|---|
| **ArcadeDB** | **Chosen.** Apache 2.0; Bolt + openCypher 25 (97.8% TCK); native HNSW (jVector); self-host on AWS for predictable cost. |
| **Neptune Database** | Rejected. Managed-AWS Bolt-compatible openCypher, but $130/mo minimum (1 NCU) plus I/O-based billing volatility. Self-hosted ArcadeDB is more predictable. |
| **FalkorDB** | Deferred. SSPLv1 license is a problem for any managed-service deployment of Trellis. The openCypher dialect is a subset rather than a full TCK pass, which would require backend-specific test xfails. |
| **LadybugDB / Kuzu** | Rejected. Embedded-only, single-writer. Incompatible with multi-agent concurrent writes and the immutable trace audit story. Third-party multi-writer forks exist but lack production track record. |

## Decision

Adopt **ArcadeDB** (Apache 2.0, self-hosted) as the blessed substrate for the Knowledge plane's `GraphStore` and `VectorStore` backends. Land it as a thin adapter over a newly extracted `BoltOpenCypherGraphStore` base class that Neo4j now also subclasses, so the two openCypher backends share their ~1000 LOC of Cypher payload instead of carrying parallel copies.

## Validation

Phase 0 spike validated end-to-end against ArcadeDB 26.6.1-SNAPSHOT in Docker (2026-05-11):

- **76/76** `GraphStoreContractTests` pass against the new `ArcadeDBGraphStore` (which subclasses `BoltOpenCypherGraphStore`). No backend-specific Cypher overrides needed.
- **17/17** ArcadeDB vector tests pass against `ArcadeDBVectorStore` — LSM_VECTOR (jVector) index, k-NN via `vectorNeighbors`, metadata filtering, delete + count.
- Full unit suite stays green: 2841 passed, 12 skipped, 235 deselected.
- Ruff clean, mypy clean.

Two portable Cypher compatibility tweaks were needed in the shared base class to satisfy both Neo4j and ArcadeDB's parsers:

1. **Temporal compares use `datetime()` casts** on both sides. ArcadeDB silently re-renders ISO-8601 timestamp strings on read (e.g. `2026-05-11T02:00:00` → `2026-05-11T02:00Z`), which broke lexicographic string compare in `_temporal_where`. The cast forces a datetime-valued compare; Neo4j accepts the same syntax. The `coalesce(datetime(valid_to), MAX_DATETIME)` formulation is used inside `all(...)` list lambdas where ArcadeDB rejects parenthesized OR with mixed `IS NULL` / `datetime()` operands.
2. **Bulk-edge UNWIND uses a WHERE clause instead of a property-binding pattern** for `edge_type`. ArcadeDB's OPTIONAL MATCH does not resolve UNWIND row references inside the `{prop: row.field}` binder; the match silently produces no rows and prior versions never get closed. Moving the filter to `WHERE old.edge_type = row.edge_type` works on both backends.

Both tweaks are pure Cypher and remain correct on Neo4j — verifiable by running the Neo4j contract suite against a live AuraDB (not done in this PR; user-driven).

## Architecture

```
GraphStore (ABC)                       ← unchanged; the swap point for callers
  │
  ├─ BoltOpenCypherGraphStore (NEW)    ← Cypher + SCD-2 + JSON encoding + session/tx
  │    ├─ Neo4jGraphStore              ← basic auth, full DDL
  │    └─ ArcadeDBGraphStore           ← basic auth, optional HTTP database create
  │
  ├─ SQLiteGraphStore                  ← unchanged
  └─ PostgresGraphStore                ← unchanged

VectorStore (ABC)                      ← unchanged
  │
  ├─ SQLiteVectorStore                 ← unchanged
  ├─ PgVectorStore                     ← unchanged
  ├─ Neo4jVectorStore                  ← unchanged (shape #2 — embedding on :Node)
  └─ ArcadeDBVectorStore (NEW)         ← shape #2 — SQL/HTTP path, LSM_VECTOR index
```

**ArcadeDB has two protocols on the same engine.** The graph store talks Cypher via the Bolt port (7687); the vector store talks SQL via the HTTP REST endpoint (2480). Both see the same `(:Node)` rows because ArcadeDB serializes both protocols against the same storage. The vector store can't piggyback on the Bolt session because ArcadeDB's vector index DDL and `vectorNeighbors` function are SQL-only.

> **Self-hosting requirement — the Bolt plugin is not enabled by default.** A stock self-hosted ArcadeDB exposes only the HTTP endpoint (2480); the Bolt port (7687) the `ArcadeDBGraphStore` depends on is opened by a plugin that must be enabled explicitly. Start the server (or build the container image) with:
>
> ```
> -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin
> ```
>
> If multiple plugins are configured, comma-separate them in the same flag. **Troubleshooting:** a connection-refused on `7687` while `2480` answers normally almost always means the Bolt plugin flag is missing from the server JVM args — the graph store will fail to connect even though `admin verify` can reach HTTP. (Surfaced building the EKS ArcadeDB wrapper image — see [#197](https://github.com/ronsse/trellis-ai/issues/197).)

**Driver cache is shared** across Bolt backends. The `_neo4j_drivers` dict on `StoreRegistry` was renamed to `_bolt_drivers`; future Bolt-speaking backends (a Neptune adapter, for example) join the same cache. ArcadeDB graph + vector stores in the same registry share a single Bolt driver pool for the graph side and a stateless HTTP connection for the vector side.

## The seam contract

Concrete `BoltOpenCypherGraphStore` subclasses override at most three things:

| Hook | Purpose | Default | Neo4j | ArcadeDB |
|---|---|---|---|---|
| `__init__(driver, database, owns_driver)` | Build / inject the Bolt driver; the base class consumes it. | Required signature on the super call. | Basic auth, `bolt://` URI, mutually-exclusive injected-vs-owned driver paths. | Same auth flow; adds idempotent HTTP database creation via `ensure_database`. |
| `SCHEMA_STATEMENTS` (class attribute) | DDL run by `_init_schema`. | Full Neo4j list (constraints + node indexes + relationship indexes). | Inherited as-is. | Inherited as-is (ArcadeDB accepts the full Neo4j-style DDL surface). |
| `close()` | Lifecycle; defaults to closing the driver iff this store owns it. | Closes the driver if owned. | Same default + Neo4j-labeled log event. | Same default + ArcadeDB-labeled log event. |

`ArcadeDBVectorStore` is **not** a subclass of `BoltOpenCypherGraphStore` because vector ops are SQL-via-HTTP rather than Cypher-via-Bolt — there's no shared payload to reuse, and inheritance would be misleading.

## What this does not do

- **No FalkorDB backend.** Deferred entirely — captured above. SSPL + dialect-subset compounding makes it the wrong next backend.
- **No LadybugDB / Kuzu backend.** Rejected. Embedded single-writer architecture is incompatible with the project's concurrent-writer story.
- **No Neptune Database backend.** Captured but not pursued. The cost predictability of self-hosted ArcadeDB on ECS/EKS is the deciding factor; managed Neptune can revisit if a customer specifically needs it.
- **No migration tooling between graph backends.** Out of scope. The JSON property serialization makes a future dump-and-reload migration tractable (export each backend's nodes/edges as JSON; round-trip through the GraphStore ABC).

## Migration path for existing Neo4j users

Neo4j stays a fully supported backend. The refactor is non-breaking: `Neo4jGraphStore`'s public API is unchanged; callers don't notice the parent class. Existing AuraDB deployments continue to work without any config change.

When a Neo4j deployment wants to switch to ArcadeDB:

1. Stand up an ArcadeDB cluster (ECS Fargate + EFS is the suggested shape; persistent EFS volume for the journal).
2. Export Neo4j graph state via the GraphStore API (`get_node_history`, `get_edges`, `query`).
3. Re-ingest against an `ArcadeDBGraphStore` pointing at the new cluster — vectors via `ArcadeDBVectorStore.upsert_bulk`.
4. Swap `~/.config/trellis/config.yaml` to `graph: backend: arcadedb` (and `vector: backend: arcadedb` if consolidating from pgvector).

Detailed runbook is out of scope for this ADR.

## Operational shape on AWS

Recommended deployment (informational, not part of this code change):

- **Compute**: ECS Fargate task running the `arcadedata/arcadedb:latest` image. One task is enough for design-partner workloads; horizontal scale comes from the HA-Raft replication plugin (separate ADR if ever needed).
- **Storage**: EFS volume mounted at `/home/arcadedb/databases` for the journal and database files. Survives task replacement and supports concurrent reads from multiple tasks.
- **Networking**: place inside a private subnet; expose the Bolt port (7687) and HTTP port (2480) only to the Trellis services that need them.
- **Backups**: ArcadeDB's `AutoBackupSchedulerPlugin` writes timestamped backup files; pair with an S3 sync on the backup directory.

## Credential split — admin vs. runtime (issue #193)

Long-running application paths must not hold admin credentials after
init/migration. Both ArcadeDB stores therefore accept an optional
privileged pair alongside the runtime pair:

| Credential | Config keys | Env fallback | Used for |
|---|---|---|---|
| Runtime (least privilege) | `user` / `password` | `TRELLIS_ARCADEDB_USER` / `TRELLIS_ARCADEDB_PASSWORD` | The Bolt driver serving all graph reads/writes; all runtime vector SQL over HTTP |
| Admin (init/migration only) | `admin_user` / `admin_password` | `TRELLIS_ARCADEDB_ADMIN_USER` / `TRELLIS_ARCADEDB_ADMIN_PASSWORD` | `ensure_database` (HTTP database creation), the typed-property edge-provenance DDL, and the vector store's `CREATE VERTEX TYPE` / `CREATE PROPERTY` / `LSM_VECTOR` index DDL |

When the admin pair is unset, the runtime pair is used for every phase —
single-credential deployments (dev, docker-compose, the validation spike)
keep working unchanged. When it is set, the admin secret is consumed by
the registry during store construction and never reaches the Bolt driver,
runtime SQL calls, or the store constructor's forwarded params.

**Deployment order for a split-credential production rollout:**

1. Provision the ArcadeDB server; create the least-privilege runtime user
   with read/write access to the target database (ArcadeDB server-side
   user management — `security.json` / server API — is out of Trellis's
   scope).
2. First boot (or an explicit migration run) supplies both pairs:
   `admin_*` performs database creation + DDL; the runtime pair carries
   all traffic thereafter.
3. Steady state: deployments may omit `admin_*` entirely and set
   `ensure_database_exists: false` — the store then never needs a
   privileged credential, and DDL drift shows up as a loud boot error
   rather than a silent escalation.

## Open questions

- **Production load characterization.** The spike validated correctness, not throughput. A representative load test against ArcadeDB on ECS Fargate would be the next signal; until then we assume single-instance performance is comparable to or better than AuraDB Free (the prior reference).
- **HA + replication.** ArcadeDB ships an HA-Raft plugin. Trellis's current operational story is single-writer, so HA is not on the critical path; revisit when a deployment needs failover.
- **Multi-model consolidation.** ArcadeDB could in theory also host the `DocumentStore` (it has FTS) and the `EventLog`. Out of scope here — Postgres remains the cloud default for those planes — but worth revisiting if operational simplification outweighs the value of plane-specific tuning.
