# Plan: Neo4j Hardening (POC-stage)

**Status:** active 2026-04-25
**Owner:** rotating
**Scope:** What it takes to make Neo4j a credible "blessed default" graph backend for both local and cloud, given that Postgres remains a supported alternative. This is POC-stage scope discipline: prioritize what eliminates surprise on the recommended path, defer everything that requires a workload signal we don't have yet.

> **Read first:** [`implementation-roadmap.md`](./implementation-roadmap.md) §1 for the live state of every backend, and [`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) for why we landed on Neo4j shape #2 (vectors as properties on `:Node` rows). This doc assumes you've read both.

---

## 1. Premise — what changed and why this exists

We've decided Neo4j is the **blessed graph backend** for Trellis — both local (Docker) and cloud (AuraDB). Postgres remains supported as an alternative for users who want to consolidate on one engine; pgvector + a Postgres GraphStore is a real path, not a deprecated one.

The hardening bar is *not* "production-hardened for a known workload" — there is no design partner pushing a specific workload yet. The bar is:

> A stranger who follows the recommended onboarding path against Neo4j (local or cloud) hits no avoidable surprises, and switching backends Postgres ↔ Neo4j is real, not a one-way downgrade.

Anything beyond that bar is gated on a workload signal we don't have. This plan deliberately under-builds the speculative parts.

---

## 2. What's already done (do not redo)

* `Neo4jGraphStore` (932 LOC) + `Neo4jVectorStore` (256 LOC) — both implemented under the `[neo4j]` optional extra.
* Live-tested against AuraDB Free 2026-04-25: 110 unit tests + 6 e2e integration tests pass.
* Canonical query DSL Phase 2 compiler for Neo4j (Cypher + Python-side property filtering).
* Two real bugs caught by the live runs:
    * AuraDB-name-is-instance-id (database name = instance ID, not `"neo4j"`)
    * Vector index async provisioning race (mitigated in tests by sharing a persistent index)
* Schema documented in `stores/neo4j/graph.py` module docstring (SCD-2, shape #2 vector storage, alias resolution, Community-edition concurrent-write race).

---

## 3. Phase 1 — credibility (must-haves before "blessed default" is a true claim)

Each item is something that, if missing the first time a stranger runs against Neo4j, looks like negligence — not a missing feature.

### 1.1 Driver lifecycle done right

* **State today:** `build_driver(uri, user, password)` in [`stores/neo4j/base.py:35`](../../src/trellis/stores/neo4j/base.py) constructs a bare driver with zero kwargs. No `connection_timeout`, no `max_connection_pool_size`, no `keep_alive`, no `max_transaction_retry_time`. AuraDB Free is forgiving; production over flaky networks is not.
* **Also today:** `Neo4jGraphStore` and `Neo4jVectorStore` each call `build_driver` independently — same URI/user/password, two connection pools. The base.py comment justifies it ("driver pools internally") but you're paying 2× pool cost when both stores point at the same instance.
* **Done when:**
    * Constructor kwargs flow through to driver: `connection_timeout`, `max_connection_pool_size`, `keep_alive`, `max_transaction_retry_time` — with sane production defaults.
    * Driver caching by `(uri, user, database)` tuple in `base.py`, OR `StoreRegistry` owns and injects the driver. Same instance, one pool.
* **Files:** `src/trellis/stores/neo4j/base.py`, `graph.py`, `vector.py`. Possibly `stores/registry.py` if registry takes ownership.
* **Estimate:** ~150 lines.
* **Risk of leaving it:** orphaned pools, file-descriptor pressure on AuraDB Pro, opaque "connection refused" errors on transient network blips.

### 1.2 Close on shutdown

* **State today:** `close()` exists on both Neo4j stores (graph.py:930, vector.py:254). Nothing calls them — `StoreRegistry` has no shutdown path and `trellis_api/app.py` lifespan doesn't fan out a close.
* **Done when:**
    * `StoreRegistry.close()` exists, fans out to every cached store backend's `close()` (Neo4j drivers, psycopg connections, S3 boto sessions).
    * `trellis_api/app.py` lifespan calls it on shutdown.
* **Files:** `stores/registry.py`, `src/trellis_api/app.py`.
* **Estimate:** ~50 lines.
* **Risk of leaving it:** file-descriptor leaks across uvicorn restarts; AuraDB connection-quota exhaustion on Pro tier.

### 1.3 `validate()` pings Neo4j on startup

* **State today:** E.3's [`StoreRegistry.validate()`](../../src/trellis/stores/registry.py) deliberately punts on per-backend ping — the docstring at line 853 acknowledges it. So if Neo4j is unreachable, uvicorn starts cleanly and the *first request* fails with a Bolt error.
* **Done when:**
    * `validate()` optionally calls `driver.verify_connectivity()` per Neo4j store, gated on a `check_connectivity: bool = False` kwarg or `TRELLIS_VALIDATE_CONNECTIVITY=1` env var.
    * Off in dev (fast restarts), on in production via the `app.py` lifespan call.
    * `RegistryValidationError` aggregates connectivity failures alongside config failures so the operator sees everything at once.
* **Files:** `stores/registry.py`, `stores/neo4j/{graph,vector}.py` (small connectivity-helper method or shared via `base.py`).
* **Estimate:** ~80 lines.
* **Risk of leaving it:** "Neo4j down" surfaces as an opaque Bolt error on the first request rather than at startup. For a blessed default, that's amateur.

### 1.4 Vector index "wait for online"

* **State today:** A.1's e2e suite surfaced that AuraDB provisions vector indexes asynchronously — a fresh `CREATE VECTOR INDEX` followed by an immediate `db.index.vector.queryNodes` call races and fails with "no such vector schema index". Currently mitigated by reusing a persistent test index.
* **Done when:**
    * `Neo4jVectorStore._init_schema()` polls `SHOW VECTOR INDEXES YIELD name, state` after `CREATE` until `state="ONLINE"`, with a configurable timeout (default ~30s) and a clear error message on timeout.
    * Helper lives in `stores/neo4j/base.py` or a new `stores/neo4j/schema.py` so the graph store could reuse it for index/constraint provisioning.
* **Files:** `stores/neo4j/vector.py`, possibly a small Cypher helper module.
* **Estimate:** ~60 lines.
* **Risk of leaving it:** production index rebuild → race on first query → opaque "no such vector schema index" error. Same failure mode the e2e suite already hit.

### 1.5 Concurrent-write race documentation (Community edition only)

* **State today:** Documented in `stores/neo4j/graph.py:11-17` but unmitigated. Neo4j Community Edition does not support partial uniqueness constraints (`UNIQUE ... WHERE valid_to IS NULL`). The "at most one current version per node_id" invariant is enforced by the close-then-insert transaction rather than by the database. Under concurrent writers on Community, a second writer can observe a stale "no current" state and create a duplicate current row.
* **Done when** (pick one of two options based on what's cheap and clear):
    * **Option A (doc-only, smaller):** A new section in `docs/deployment/neo4j-self-hosted.md` (created in Phase 2.2) calls out the deployment matrix explicitly: "Single-writer Community = safe; multi-writer Community = use AuraDB / Neo4j Enterprise to layer a node-key constraint." Cross-link from the module docstring.
    * **Option B (code, larger):** Opt-in `enable_strict_uniqueness=True` constructor flag that detects Enterprise/AuraDB at startup and adds the node-key constraint when supported, no-ops otherwise. Logs a `warning` if the user passes the flag against Community.
* **Recommendation:** Option A first. Option B becomes interesting only if a self-hosted Community user reports a duplicate. POC discipline: don't pre-build.
* **Files:** Option A → docs only; Option B → `stores/neo4j/graph.py` + tests.
* **Estimate:** Option A ~30 lines; Option B ~80 lines + tests.

---

## 4. Phase 2 — Postgres as a real alternative ("we still offer Postgres" must be true, not theoretical)

### 2.1 Combined-plane config example

* **State today:** [`adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) names "Knowledge plane = Neo4j, Operational plane = Postgres" as the recommended cloud shape but no validated example config exists. Users reading the README cannot copy-paste a starting config.
* **Done when:**
    * `docs/deployment/recommended-config.yaml` (or equivalent under `docs/deployment/`) shows three blessed configurations side-by-side with comments:
        1. **Local-default:** Knowledge = Neo4j (local Docker), Operational = SQLite (file). Keeps the zero-cloud-dep posture; Neo4j hosts the knowledge graph as the blessed path.
        2. **Cloud-default:** Knowledge = Neo4j (AuraDB), Operational = Postgres (RDS or Neon). Both hosted, both managed.
        3. **Postgres-only alternative:** Knowledge = Postgres + pgvector, Operational = Postgres. One engine, supported but called out as "alternative, not blessed."
    * `tests/integration/test_combined_plane.py` (or similar) runs a small end-to-end mutation through `MutationExecutor` against the local-default config to prove the wiring works as documented.
* **Files:** new `docs/deployment/recommended-config.yaml` + commentary, `tests/integration/test_combined_plane.py`.
* **Estimate:** ~200 lines.
* **Risk of leaving it:** users guess at the config shape from ADR prose. Some get it wrong, give up, and try a different tool.

### 2.2 Local + cloud onboarding docs

* **State today:** `.env.example` has the Docker one-liner as a comment; `docs/deployment/aws-ecs.md` exists for the API runtime. Nothing dedicated for Neo4j onboarding. A user pip-installing the `[neo4j]` extra has no doc to follow.
* **Done when:**
    * `docs/deployment/neo4j-local.md` — Docker one-liner (promote from `.env.example` comment), how to verify the driver works (`python -c "from neo4j import ..."`), config snippet referencing the local-default from 2.1, the vector-index gotcha, smoke command. Ends with `trellis demo load` returning a green check.
    * `docs/deployment/neo4j-auradb.md` — Free tier provisioning steps (Aura console → "Create instance" → save credentials), the **AuraDB-name-is-instance-id callout** (already documented in implementation-roadmap.md but needs to live where users find it), Pro upgrade path one-liner, the env var loadout. Ends with same smoke.
* **Files:** two new doc pages parallel to `aws-ecs.md`.
* **Estimate:** ~300 lines combined.
* **Risk of leaving it:** every new user repeats the "wait, what's the database name on AuraDB" discovery we already paid for once.

### 2.3 Migration tool: `trellis admin migrate-graph`

* **State today:** Switching backends today means starting fresh — no data migration path between SQLite, Postgres, and Neo4j graph stores. That makes Postgres feel like a deliberate downgrade from Neo4j rather than a real alternative.
* **Done when:**
    * `trellis admin migrate-graph --from <src> --to <dst> [--batch-size N] [--dry-run]` walks the source backend's nodes, edges, and aliases and replays them through the canonical DSL into the destination.
    * Idempotent on retry (checkpoint on `node_id` / `edge_id`); logs a `MIGRATION_PROGRESS` event every N batches; emits a `MIGRATION_COMPLETED` summary event.
    * `--dry-run` reports counts without writing.
    * Tests: round-trip one direction (sqlite → neo4j), spot-check temporal versioning (SCD-2 closed rows), spot-check aliases.
* **Why this is load-bearing:** the canonical DSL we landed in B.1+B.2 (PR #16) makes this *possible* without per-backend special-casing. Without the migration tool, "we still offer Postgres" is theoretical because nobody can move between them.
* **Files:** new `src/trellis_cli/migrate.py` + tests.
* **Estimate:** ~250 lines + ~100 lines of tests.
* **Risk of leaving it:** backend choice feels one-way and irreversible. Operators won't experiment.

---

## 5. Phase 3 — deferred (gated on workload signal)

Do not pre-build any of these. Each lands cleanly when its signal arrives.

| Item | Gate |
|---|---|
| HNSW vector-index params (`M`, `efConstruction`) tuning | Recall or latency complaint on real workload |
| `upsert_node` UNWIND-based bulk path | N>1 ingest pattern observed in production traces |
| EXPLAIN-validated query plan baseline | Slow query reported, OR populated graph with >100K nodes |
| Self-hosted Neo4j HA / clustering runbook | Self-hosted Pro/Enterprise user asks |
| Concurrent-write race active mitigation (Community) | Self-hosted Community user reports a duplicate row (1.5 Option A is the holding answer) |
| Backup/restore docs (self-hosted) | Self-hosted user asks; AuraDB users have managed backups |
| Provenance fields as first-class edge columns ([roadmap B.3](./implementation-roadmap.md)) | Policy or retrieval consumer wants to gate on these |
| Vector DSL Phase 4 ([roadmap C.1](./implementation-roadmap.md)) | Vector contract drift surfaces, OR plugin author asks for strongly-typed vector filters |
| Graph compaction automation ([TODO.md exploratory item](../../TODO.md)) | `as_of` query latency degradation observed |
| Bookmark management for read-after-write consistency in Neo4j clusters | Multi-instance Neo4j deployment with consistency complaints |
| Tracing / metrics emission (Prometheus, OTel) on Neo4j queries | Operator asks for query observability beyond structlog |

---

## 6. What this plan deliberately rejects

* **Auto-default-to-Neo4j on `trellis admin init`.** Keep SQLite as the zero-config path. Neo4j is *recommended*, not *automatic*. Forcing a Docker dep for `pip install trellis-ai && trellis admin init` is hostile to evaluation.
* **Performance work without a workload.** Defaults are usually fine for ≤50K nodes. Tuning before measurement is folklore.
* **Self-hosted HA / clustering.** Single-instance Docker for local + AuraDB managed for cloud covers the blessed paths. HA is a "when someone asks" item.
* **Multi-tenant Neo4j (database-per-tenant).** Speculative; no tenant-isolation requirement on the table.
* **Reactive driver (async neo4j sessions).** The codebase is sync today; switching to async is a cross-cutting change with no signal that the sync path is bottlenecking.

---

## 7. Recommended execution order

| # | Item | Why this slot |
|---|---|---|
| 1 | **1.1** Driver lifecycle | Foundational; everything downstream assumes a properly-configured driver. |
| 2 | **1.2** Close on shutdown | Trivial follow-on to 1.1; together they fix the lifecycle story end-to-end. |
| 3 | **1.3** `validate()` pings | Builds on E.3 (already landed). Smallest delta that turns a vague Bolt error into a startup failure. |
| 4 | **1.4** Vector index wait | Independent of 1.1-3; protects production restarts. |
| 5 | **1.5 Option A** Doc the Community race | Fast; 30 lines; lets us defer Option B safely. |
| 6 | **2.1** Combined-plane config | Once Phase 1 hardening is in, document the blessed configs against it. |
| 7 | **2.2** Onboarding docs | Reads from 2.1. Two pages parallel to `aws-ecs.md`. |
| 8 | **2.3** Migration tool | Largest item; lands last because it's the highest-cost / lowest-immediacy. The "Postgres alternative" claim becomes true the day this ships. |

Total: ~3 days of focused work spread across ~5 PRs. Each item is independent enough to land separately.

---

## 8. Hand-off

When picking up this plan:

1. Read [`CLAUDE.md`](../../CLAUDE.md) for project conventions and hard rules.
2. Read this doc top-to-bottom.
3. Read the relevant ADR for the phase you're touching (linked above).
4. Run the existing Neo4j live test before starting: `set -a && source .env && set +a && pytest tests/unit/stores/test_neo4j_*.py tests/unit/stores/contracts/test_neo4j_graph_contract.py tests/integration/test_neo4j_e2e.py -q`. Should report ~116 passed.
5. Update this doc when a phase lands. The State / Done When / Files entries are the contract.

If the workload assumptions in §5 change (a partner appears, a real workload surfaces drift), promote the relevant deferred item into Phase 3 of this plan and rebalance §7's execution order.
