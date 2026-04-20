# ADR: Storage Planes and Blessed Substrates

**Status:** Proposed
**Date:** 2026-04-18
**Deciders:** Trellis core
**Related:**
- [`./adr-terminology.md`](./adr-terminology.md) — canonical meanings of "plane", "substrate" vs "backend", and related terms
- [`./adr-llm-client-abstraction.md`](./adr-llm-client-abstraction.md) — Precedent for "Protocol in core, implementations optional"
- [`./adr-deferred-cognition.md`](./adr-deferred-cognition.md) — One-way bridge between write path and enrichment
- [`./adr-plugin-contract.md`](./adr-plugin-contract.md) — Entry-point plugin loader that merges with `_BUILTIN_BACKENDS` per plane
- [`../../src/trellis/stores/registry.py`](../../src/trellis/stores/registry.py) — `StoreRegistry`, plane-keyed backend table
- [`../../src/trellis/stores/base/`](../../src/trellis/stores/base/) — Store ABCs
- [`../../src/trellis/mutate/`](../../src/trellis/mutate/) — Governed mutation pipeline (the sanctioned bridge)
- [`../../TODO.md`](../../TODO.md) — Graphiti comparison section citing Kuzu as a missing backend

---

## Amendment — 2026-04-19 — blessed-local-substrate choice deferred

This ADR was drafted on 2026-04-18 and named **Kuzu** as the blessed local unified graph+vector substrate for the Knowledge Plane (§2.2, §2.3). Between drafting and implementation, the upstream Kuzu project was [archived on 2025-10-10 following Apple's acquisition of Kuzu Inc.](https://github.com/kuzudb/kuzu) and is no longer accepting new development or releases.

**The plane-partitioning decision (§2.1) and the blessed-substrate *principle* (§2.2 general, §2.5 sanctioned bridges, §2.6 config schema, §2.7 registry namespacing) remain in force.** Only the *specific pick* of a blessed local graph+vector substrate is deferred.

**Interim position (active guidance):**

- Local dev / `trellis admin init` default: `SQLiteGraphStore` (graph) + `SQLiteVectorStore` (vector). Two stores, separate instances. Pack assembly already runs search strategies independently and dedupes at the end (see [`src/trellis/retrieve/pack_builder.py`](../../src/trellis/retrieve/pack_builder.py)), so no "fusion step" is owed by the interim pick — the unified-substrate goal is about *future* simplification, not undoing current code.
- Production: Postgres (`PostgresGraphStore`) + `pgvector` (`PgVectorStore`). Same two-store shape; pgvector is mature and supported.
- Unified graph+vector backends remain **opt-in alternatives** that a deployment can select via config; the plane-nested `_BUILTIN_BACKENDS` table and the dual-inheritance pattern (a single backend class satisfying both `GraphStore` and `VectorStore`) are ready for one to be blessed later.

**Follow-up:** a separate ADR will evaluate candidates (SurrealDB, DuckDB + PGQ/VSS, FalkorDB, Dgraph, Neo4j Community) against the blessed-substrate criteria and pick the successor. Until then, §2.2's "Local (blessed)" and "Production (blessed)" cells for `GraphStore` and `VectorStore` should be read as **TBD**, and §2.3 as historical rationale for why a unified substrate was desirable — not as a current recommendation to use Kuzu.

The remaining Kuzu mentions in §§2.3, 2.6, Consequences, and Migration Plan are preserved as the original drafting context and should be re-read with this amendment in mind.

---

## 1. Context

### What exists today

Trellis ships six store ABCs (`TraceStore`, `DocumentStore`, `GraphStore`, `VectorStore`, `EventLog`, `BlobStore`) wired through `StoreRegistry` with a flat `_BUILTIN_BACKENDS` table ([registry.py:33-59](../../src/trellis/stores/registry.py)). All six live under a single `stores:` block in `~/.trellis/config.yaml`. SQLite is the default for five of them; `local` is the default for `BlobStore`. Postgres, pgvector, LanceDB, and S3 are registered as alternative backends.

Three implicit boundaries already exist in the code but are **not named**:

1. **Governed mutation pipeline** (`src/trellis/mutate/`) is the only sanctioned way to write to Graph, Vector, or Document stores; it emits to `EventLog` as a side effect. Workers (`trellis_workers`) write through it, not around it.
2. **Effectiveness feedback loop** (`src/trellis/classify/feedback.py`, consumed by `AdvisoryGenerator`) reads from `EventLog` and writes `content_tags` back onto Document records. This is a second, narrower bridge.
3. **Agent-facing surfaces** (MCP server, SDK, `/api/v1/search`, `/api/v1/evidence`) touch Graph, Vector, Document, and Blob. They do not read Trace or EventLog. Admin/debug surfaces are the inverse.

These boundaries are consistent but undocumented, and the config layer does not reflect them — a single YAML block treats `trace` and `graph` as peers, even though they serve categorically different purposes.

### What the backlog needs

Several in-flight decisions hinge on boundaries that don't have names yet:

| Need | Current obstacle |
|---|---|
| Client systems populating Trellis from their own data sources | No config-level distinction between "stores client data may reach" and "stores that are internal to Trellis" |
| Unified graph + vector backend (Kuzu, ArangoDB, Neo4j with native vector index) | `GraphStore` and `VectorStore` are peers in `_BUILTIN_BACKENDS`; no mechanism for a single backend to satisfy both with a shared instance |
| Deployment topologies where the knowledge layer and operational layer live on different databases, networks, or retention policies | `StoreRegistry` has one DSN envelope; `TRELLIS_PG_DSN` is shared across Trace/Document/Graph/EventLog |
| Graph entities referencing their source documents | Graph and Document stores have independent identity systems; no `document_ids` property on entity nodes |
| Open-source, local-first graph+vector substrate | No Kuzu backend exists today (grep confirms only documentation mentions in [TODO.md](../../TODO.md) and `docs/research/memory-systems-landscape.md`). SQLite's `VectorStore` is a separate table with no transactional coupling to `GraphStore` |

### The decision to make

Do we:

- **(A)** Keep the flat store layout; add Kuzu as one more `graph` and `vector` backend alongside SQLite and pgvector; let deployments figure out the rest.
- **(B)** Formalize two **storage planes** (Knowledge and Operational), declare **blessed substrates** per plane with ABCs for extension, and land Kuzu as the blessed graph+vector substrate for the Knowledge Plane. Document sanctioned bridges between planes explicitly.
- **(C)** Collapse the six-store model into a single unified backend (Graphiti-style), losing multi-store backend diversity but gaining transactional coherence.

---

## 2. Decision

**Option B: two named planes, blessed substrates per plane, Kuzu as the blessed local Knowledge graph+vector substrate.**

### 2.1 The two planes

| Plane | Stores | Who reads it | Who writes to it |
|---|---|---|---|
| **Knowledge Plane** | `GraphStore`, `VectorStore`, `DocumentStore`, `BlobStore` | Agents (via MCP, SDK, REST retrieval endpoints) | Client systems via `MutationExecutor` (workers, ingestion pipelines, `/api/v1/evidence`) |
| **Operational Plane** | `TraceStore`, `EventLog` | Trellis internals (MutationExecutor emits to EventLog; effectiveness analysis consumes it); admin/debug surfaces | Trellis internals only — never populated by client systems |

The planes are distinguished by **who the data is for**, not by what kind of data it is. Knowledge is the shared resource agents query. Operational is Trellis talking to itself about its own execution.

### 2.2 The blessed-substrate principle

Every store has:

1. **A plane** — fixed at the ABC level.
2. **A blessed local substrate** — the default for `trellis admin init` with zero additional configuration.
3. **A blessed production substrate** — the default for cloud deployments, usually requiring one DSN.
4. **Opt-in alternative substrates** — registered in `_BUILTIN_BACKENDS`, selected via config, and documented as "when you need it" rather than as peers of the default.

This mirrors the existing classifier tier precedent (`DETERMINISTIC > HYBRID > LLM` with `allow_llm_fallback=False` as default): one blessed path, explicit opt-in to alternatives.

**Blessed-substrate table:**

| Plane | Store | Local (blessed) | Production (blessed) | Opt-in alternatives |
|---|---|---|---|---|
| Knowledge | `GraphStore` | **TBD** (see Amendment) — interim: SQLite | **TBD** (see Amendment) — interim: Postgres | Unified-substrate candidates pending evaluation |
| Knowledge | `VectorStore` | **TBD** (see Amendment) — interim: SQLite | **TBD** (see Amendment) — interim: pgvector | LanceDB, unified-substrate candidates pending evaluation |
| Knowledge | `DocumentStore` | SQLite (FTS5) | Postgres (tsvector) | — |
| Knowledge | `BlobStore` | Local filesystem | S3 (incl. Supabase Storage) | — |
| Operational | `TraceStore` | SQLite | Postgres (separate instance) | — |
| Operational | `EventLog` | SQLite | Postgres (same instance as Trace) | — |

Trace and EventLog live on their own database — not because they can't share with Document, but because sharing makes the plane boundary ambiguous and couples retention/backup policies that should be independent.

### 2.3 Blessed Knowledge graph+vector substrate — deferred (see Amendment)

> **Superseded by the 2026-04-19 Amendment.** The original text of this section argued for Kuzu as the blessed local substrate. The upstream project was archived in October 2025; the specific substrate pick is deferred to a follow-up ADR. The *criteria* used below still apply to the evaluation of a successor.

A blessed local graph+vector substrate should be:

- **Fully OSS** with a permissive license, aligned with the project's open-source posture.
- **Embedded** — installable via `pip`, no daemon, no server, single-directory storage. Same operational profile as SQLite.
- **Natively graph+vector** — graph traversal and vector similarity (HNSW or equivalent) in a single backend, so `GraphStore` and `VectorStore` ABCs can be satisfied by one instance through dual inheritance.
- **Actively maintained** with a healthy contributor base.

The registry's plane-nested `_BUILTIN_BACKENDS` table already supports registering one class under both `knowledge.graph` and `knowledge.vector`; the `_find_shared_instance` / `_init_params_fingerprint` logic in [`src/trellis/stores/registry.py`](../../src/trellis/stores/registry.py) caches a single instance across both keys when they resolve to the same backend + params. Landing a blessed substrate later is a backend-class addition, not an architectural change.

### 2.4 Graph ↔ Document link

With Kuzu landing, entity nodes gain an optional `document_ids: list[str]` property pointing at the `DocumentStore` rows that sourced them. Closes the orphan-content gap where vector search, FTS, and graph traversal run in parallel without cross-linking. This is a schema addition, not a new store.

### 2.5 Sanctioned bridges (named explicitly)

Planes are not sealed. There are **two** legitimate cross-plane bridges and we document them rather than pretending planes never touch:

1. **MutationExecutor** — reads Knowledge state for idempotency checks, writes Knowledge, emits to Operational `EventLog`. One-way: Operational writes are a side effect of Knowledge writes.
2. **Effectiveness feedback loop** — reads Operational `EventLog` (pack usage, feedback events), writes `content_tags` back onto Knowledge `DocumentStore` records to demote noise. One-way: Operational informs Knowledge metadata; no inverse.

Any **third** cross-plane bridge requires a new ADR. This is the discipline that makes the split enforceable.

### 2.6 Config schema change

Current:

```yaml
stores:
  graph: { backend: sqlite }
  vector: { backend: sqlite }
  document: { backend: sqlite }
  trace: { backend: sqlite }
  event_log: { backend: sqlite }
  blob: { backend: local }
```

New:

```yaml
knowledge:
  graph: { backend: kuzu, path: ${TRELLIS_DATA_DIR}/kuzu }
  vector: { backend: kuzu }           # shares Kuzu instance with graph
  document: { backend: sqlite }       # or postgres
  blob: { backend: local }

operational:
  trace: { backend: sqlite }
  event_log: { backend: sqlite }
```

Production example with split DSNs:

```yaml
knowledge:
  graph: { backend: kuzu, path: /var/lib/trellis/kuzu }
  vector: { backend: kuzu }
  document: { backend: postgres, dsn_env: TRELLIS_KNOWLEDGE_PG_DSN }
  blob: { backend: s3, bucket: ${TRELLIS_S3_BUCKET} }

operational:
  trace: { backend: postgres, dsn_env: TRELLIS_OPERATIONAL_PG_DSN }
  event_log: { backend: postgres, dsn_env: TRELLIS_OPERATIONAL_PG_DSN }
```

`TRELLIS_PG_DSN` is **deprecated** (still read for one release as a fallback for both planes, then removed). `TRELLIS_KNOWLEDGE_PG_DSN` and `TRELLIS_OPERATIONAL_PG_DSN` replace it.

### 2.7 StoreRegistry change

`StoreRegistry` gains two namespaces:

```python
registry.knowledge.graph_store       # was: registry.graph_store
registry.knowledge.vector_store      # was: registry.vector_store
registry.knowledge.document_store    # was: registry.document_store
registry.knowledge.blob_store        # was: registry.blob_store

registry.operational.trace_store     # was: registry.trace_store
registry.operational.event_log       # was: registry.event_log
```

The old flat properties are kept as **deprecated aliases** for one release with a `DeprecationWarning`, then removed. Internal code moves immediately to the namespaced form.

Registry shares instances when a single backend satisfies multiple ABCs: if both `knowledge.graph` and `knowledge.vector` resolve to `backend: kuzu` with the same `path`, `_instantiate` caches a single `KuzuStore` instance and returns it under both keys.

### 2.8 What is NOT in scope

- **Collapsing into a single-store model (Option C).** Trellis keeps six ABCs; the abstraction is what enables backend diversity (Kuzu locally, ArangoDB at scale, Postgres-only deployments for teams without a Kuzu operator story).
- **Removing SQLite.** SQLite remains the blessed local substrate for Document, Trace, and EventLog. Only its positioning changes: it is no longer the "universal default" — it is the blessed substrate for specific workloads. SQLite-backed `GraphStore` and `VectorStore` are demoted to "dev-only escape hatch" and may be removed in a later ADR once Kuzu is proven.
- **Neo4j Enterprise or other commercial backends.** Open-source alignment is a first-class project value. Commercial backends may be added only if a community member maintains the adapter.
- **Cross-plane transactions.** The two sanctioned bridges are eventually-consistent (MutationExecutor emits to EventLog after Knowledge write; effectiveness loop runs asynchronously). No two-phase commit.
- **Quickstart profile UX.** The `trellis admin init --profile=quickstart` command that hides the two-plane config from new users is a docs/UX follow-up, not part of this ADR.

---

## 3. Options considered

### Option A: Flat layout, add Kuzu as one backend among many

**How it works:** Add `"kuzu"` entries to `_BUILTIN_BACKENDS["graph"]` and `_BUILTIN_BACKENDS["vector"]`. No config schema change. No plane naming. Document Kuzu as an alternative backend alongside SQLite and Postgres.

**Pros:**
- Smallest diff. No breaking config changes. No registry namespace split.
- Zero migration burden for existing users.

**Cons:**
- Leaves the plane boundary implicit. Client systems and internal operational data continue to share DSN envelopes, shared deployment, shared retention.
- Kuzu-as-one-of-many understates its role. Without "blessed substrate" positioning, users will not know which backend to pick, and Postgres+pgvector will stay the de facto default because it's what the docs show.
- Does not address the graph↔document link gap, the `TRELLIS_PG_DSN`-shared-across-everything problem, or the cross-plane bridge documentation gap.
- Does not establish the architectural principle that makes future decisions easier.

**Verdict:** Solves the narrowest slice. Leaves every other latent issue to be rediscovered later, and misses the window (pre-1.0, just public) where breaking config changes are cheap.

### Option C: Collapse into a single-store model (Graphiti-style)

**How it works:** Drop the six-store abstraction. Everything lives in the graph database as nodes and edges — Documents become node types, Traces become node types, embeddings become node properties. Single backend, single DSN, single transaction envelope.

**Pros:**
- Maximum transactional coherence. No cross-store reference model needed.
- Simplifies the mental model to a single resource.
- Matches Graphiti's production track record.

**Cons:**
- Locks Trellis to graph databases only. Loses Postgres-only deployments, SQLite-local dev, LanceDB analytical workloads.
- Throws away the plane distinction entirely — Operational audit data is architecturally fused with Knowledge.
- Large breaking change across every store consumer. Disproportionate to the problem being solved.
- Conflicts with the existing design point ([TODO.md §Graphiti comparison](../../TODO.md)) that Trellis is deliberately multi-store with local-first SQLite support. The ADR would be reversing a load-bearing decision, not refining it.

**Verdict:** Wrong direction for this project. Graphiti made this call; we made the other one on purpose.

---

## 4. Consequences

### Positive

- **Plane boundary is config-enforced.** Splitting `knowledge:` and `operational:` in YAML makes it structurally harder to accidentally cross the line. Client ingestion pipelines cannot reach `operational.*` stores through normal resolution.
- **Kuzu is a first-class local story.** `pip install trellis-ai[kuzu]` gives new users an embedded graph+vector store with HNSW, Cypher, and transactional coupling between entity upserts and embedding writes. Matches Graphiti's feature parity on the local-first axis while preserving Trellis's multi-store diversity elsewhere.
- **Blessed-substrate principle becomes a project-wide decision pattern.** Future additions (e.g., Confluence ingest, Drive BlobStore, new vector backends) slot in as "opt-in alternatives" without the authors having to re-litigate whether the project even supports that level of optionality.
- **Graph↔document link closes the orphan-content gap.** Entity nodes point at source documents; retrieval layers can fuse graph traversal with FTS without separate ID maps.
- **Two sanctioned bridges are documented.** MutationExecutor + effectiveness loop are named as the only legitimate cross-plane paths. Any third bridge requires a new ADR — a checkpoint against architectural drift.
- **Deployment topologies become declarative.** Teams can put Knowledge in Supabase, Operational in a separate Postgres, and the graph on a self-hosted Kuzu instance, by editing YAML.

### Negative

- **Breaking config change.** Existing `~/.trellis/config.yaml` files need migration. Mitigation: one-release deprecation window; `trellis admin migrate-config` command that reads a flat `stores:` block and rewrites it as split planes.
- **Kuzu adds a new dependency and a new failure mode.** Kuzu is v0.x and still churning. Mitigation: pin version tightly, keep `SQLiteGraphStore` and `SQLiteVectorStore` in the repo as the "dev-only escape hatch," and keep the ABCs stable so a future replacement is a backend swap, not an architectural migration.
- **Two registry namespaces add minor API surface.** Deprecated aliases keep old code working for one release; internal call sites move immediately.
- **The effectiveness loop being a second bridge makes the "one bridge" story more nuanced.** Naming it explicitly is the mitigation — pretending there's only one bridge would break the first time a reader traced the code.

### Neutral

- SQLite remains the blessed local substrate for Document, Trace, EventLog. No deprecation. Its positioning shifts from "universal default" to "blessed substrate for specific workloads."
- `TRELLIS_PG_DSN` is deprecated but not removed immediately. Users who set it continue to work for one release, with a warning.
- Kuzu as blessed default is a **local** decision. Production deployments remain free to choose Postgres+pgvector if that matches their ops story — it's an opt-in alternative, not a demotion.

---

## 5. Implementation plan

Ordered for incremental delivery. Each phase is independently shippable and leaves the tree in a working state.

### Phase 1: ADR accepted + naming locked

1. This ADR lands at `Status: Accepted`.
2. `ARCHITECTURE.md` (or a new top-level section in `README.md`) introduces "Knowledge Plane" and "Operational Plane" as canonical terms.
3. `CLAUDE.md` updated: the "Five Packages, One Core" table adds a plane column where applicable; "Two feedback paths" section cross-references the sanctioned-bridges subsection.

### Phase 2: Config schema + registry namespaces

1. `StoreRegistry.from_config_dir()` learns to read `knowledge:` and `operational:` blocks. Flat `stores:` remains supported with a `DeprecationWarning`.
2. `StoreRegistry.knowledge` and `StoreRegistry.operational` namespace objects introduced; old flat properties (`registry.graph_store` etc.) become deprecated aliases that delegate.
3. `_BUILTIN_BACKENDS` restructured with plane as outer key:
   ```python
   _BUILTIN_BACKENDS: dict[str, dict[str, dict[str, tuple[str, str]]]] = {
       "knowledge": {"graph": {...}, "vector": {...}, "document": {...}, "blob": {...}},
       "operational": {"trace": {...}, "event_log": {...}},
   }
   ```
4. `TRELLIS_KNOWLEDGE_PG_DSN` and `TRELLIS_OPERATIONAL_PG_DSN` env vars supported; `TRELLIS_PG_DSN` becomes a fallback for both with a deprecation log line.
5. `trellis admin migrate-config` CLI command: reads old flat config, writes new split config, leaves a backup.

### Phase 3: Kuzu backend

1. New package `trellis.stores.kuzu` with a `KuzuStore` class that implements both `GraphStore` and `VectorStore`.
2. Registered under `knowledge.graph: {"kuzu": (...)}` and `knowledge.vector: {"kuzu": (...)}` — both entries resolving to the same module + class.
3. Registry `_instantiate()` learns **shared-instance resolution**: when `knowledge.graph` and `knowledge.vector` both resolve to the same backend with the same init params, return a cached single instance satisfying both ABCs.
4. `[kuzu]` optional extra added to `pyproject.toml` (`trellis-ai[kuzu]`).
5. SCD Type 2 temporal versioning (existing in SQLite/Postgres `GraphStore`) implemented in Kuzu via node properties + history edges. `get_node_history()` must work.
6. Unit tests in `tests/unit/stores/test_kuzu.py` covering both ABCs, mirroring the existing `test_graph_store.py` and `test_vector_store.py` structure.
7. Integration test confirming a single Kuzu instance serves both `registry.knowledge.graph_store` and `registry.knowledge.vector_store` as the same object.

### Phase 4: Graph ↔ document link

1. Entity node schema gains optional `document_ids: list[str]` property, validated at the store boundary (like the existing `node_role` / `generation_spec` invariants in [`stores/base/graph.py`](../../src/trellis/stores/base/graph.py)).
2. Ingestion path (`/api/v1/evidence`, worker extractors) populates `document_ids` when a graph entity is derived from a `DocumentStore` row.
3. Retrieval layer (`PackBuilder`) learns to follow `document_ids` when building packs: graph traversal can now materialize document content without a separate FTS lookup.
4. Backfill migration for existing deployments: best-effort — leave `document_ids` empty for pre-existing entities, document that retrieval gracefully degrades when absent.

### Phase 5: Blessed quickstart profile

1. `trellis admin init --profile=quickstart` writes a default `config.yaml` with Kuzu as `knowledge.graph/vector` and SQLite as `knowledge.document` + `operational.trace/event_log`. Single `stores_dir`, zero env vars required.
2. `README.md` "Quickstart" section updated to match. The old SQLite-everywhere snippet removed.
3. `docs/agent-guide/operations.md` updated: production DSN patterns, plane split deployment topologies.

### Phase 6: Deprecation cleanup (next release)

1. Remove flat `stores:` config support.
2. Remove flat `registry.graph_store` etc. aliases.
3. Remove `TRELLIS_PG_DSN` fallback.
4. Decide whether to remove SQLite-backed `GraphStore` / `VectorStore` entirely or keep as dev-only (separate ADR).
