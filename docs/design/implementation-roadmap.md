# Implementation Roadmap

**Last updated:** 2026-04-25 (evening — A.1, B.1, B.2 landed)
**Purpose:** Single-page hand-off for any agent (fresh or returning) picking up Trellis implementation work. Self-contained. Read this top-to-bottom before touching code.

---

## 1. State of the project — 2026-04-25

### What's live and tested

* **All five storage planes wired:** trace / document / graph / vector / event_log / blob. `StoreRegistry` (`src/trellis/stores/registry.py`) constructs each from config or env vars.
* **Three graph backends:** SQLite (default), Postgres, Neo4j. All three pass the `GraphStoreContractTests` suite.
* **Four vector backends:** SQLite (default), pgvector, LanceDB, Neo4j (shape #2 — embeddings as properties on `:Node` rows). The first three pass `VectorStoreContractTests`; Neo4j shape #2 has its own per-backend test file by design (its `upsert` requires an existing graph node).
* **Canonical query DSL** lives at `src/trellis/stores/base/graph_query.py`. `FilterClause` / `NodeQuery` / `SubgraphQuery` / `SubgraphResult` with `eq` / `in` / `exists` operators. Compiled to native dialect by each backend.
* **Well-known type registry** at `src/trellis/schemas/well_known.py`. Schema.org-aligned entity types, PROV-O-aligned edge kinds, alias maps from legacy lowercase to canonical, `canonicalize_*` helpers.

### Test counts (state at 2026-04-25)

| Run mode | Pass | Skip | Notes |
|---|---|---|---|
| `pytest tests/unit/` | 653 | 118 | Default. SQLite + LanceDB live; Postgres / Neo4j / pgvector skip cleanly. |
| `+ TRELLIS_TEST_NEO4J_*` set | +110 | -54 | 33 graph + 19 vector + 58 contract. Validated against AuraDB Free 2026-04-25. |
| `+ TRELLIS_TEST_PG_DSN` set | +unknown | — | Untested in this thread. Should run cleanly per the contract. |
| `pytest tests/integration/test_neo4j_e2e.py` (env loaded) | 6 | 0 | A.1 e2e: ENTITY_CREATE / LINK_CREATE / audit-events / JSON extractor → Neo4j / PackBuilder→graph / SemanticSearch→shape #2 vector. Skips cleanly otherwise. |
| `pytest tests/unit/schemas/test_well_known.py` | 85 | 0 | Phase 0 (41) + Phase 1/2 helpers (44 — alignment URIs, alias inverse, query expansion). |
| `pytest tests/unit/extract/` | 147 | 0 | All 139 prior + 8 new Phase 1 canonicalisation tests in `TestCanonicalNameEmission`. |
| `pytest tests/unit/retrieve/test_strategies.py` | 37 | 0 | All 32 prior + 5 new Phase 2 cross-bucket tests in `TestGraphSearchCanonicalBucketing`. |

### Live test credentials — `.env` is the source of truth

Local credentials (Neo4j AuraDB, Postgres DSN, etc.) live in `.env` at the repo root. **Gitignored** — see `.gitignore` lines 21-26 (`.env`, `.env.*`, with `!.env.example` exception). The committed `.env.example` documents every variable the test suite + production deployment recognises.

**Loading the file before running tests:**

```bash
# Bash / zsh
set -a && source .env && set +a

# PowerShell
Get-Content .env | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') {
        Set-Item -Path "env:$($matches[1])" -Value $matches[2]
    }
}
```

After loading, `pytest tests/unit/stores/test_neo4j_*.py` runs the live suites against whatever Neo4j / Postgres instance the file points at. Without `.env` loaded, the env-gated suites skip cleanly.

**Provisioned for the project:** a free-tier Neo4j AuraDB instance at `cfc3411f.databases.neo4j.io`. **Leave it running.** The password is in `.env`, *not* in this doc — agents working on the codebase load it via `source .env` rather than handling the credential manually.

**AuraDB-specific gotcha already caught:** the database name is the instance ID, not the canonical `"neo4j"`. Production users on AuraDB must pass `database=<instance_id>` to `Neo4jGraphStore` / `Neo4jVectorStore`. Test fixtures honour `TRELLIS_TEST_NEO4J_DATABASE` (default `"neo4j"`).

---

## 2. Recently completed (this thread, 2026-04-23 → 2026-04-25)

| Work | Files |
|---|---|
| `Neo4jGraphStore` + `Neo4jVectorStore` (shape #2) + tests | `src/trellis/stores/neo4j/`, `tests/unit/stores/test_neo4j_*.py` |
| Graph ontology ADR Phase 0 — `well_known.py` + alias maps + 41 tests + docs | `src/trellis/schemas/well_known.py`, `tests/unit/schemas/test_well_known.py`, ontology section in `docs/agent-guide/schemas.md`, terminology row in `docs/design/adr-terminology.md` |
| Canonical translation layer ADR Phases 0-3 — contract suites, DSL, per-backend compilers, plugin contract requirement | `tests/unit/stores/contracts/`, `src/trellis/stores/base/graph_query.py`, compilers in `src/trellis/stores/{sqlite,postgres,neo4j}/graph.py`, plugin-contract section in `docs/design/adr-plugin-contract.md` |
| Live-tested 110 Neo4j tests against AuraDB Free | one fixture fix (DB env var); no production code changes needed |
| **A.1** — End-to-end Neo4j integration suite (6 tests) | `tests/integration/conftest.py`, `tests/integration/test_neo4j_e2e.py`. Covers ENTITY_CREATE / LINK_CREATE through MutationExecutor, audit-event emission, JSONRulesExtractor → drafts → batch → graph rows, PackBuilder against Neo4j-backed graph, and SemanticSearch through the shape #2 vector store. Test-only changes; no production code touched. |
| **B.1 + B.2** — Graph ontology Phase 1 + 2 (canonical names in extractors + alias-bucketing in retrieval) | `src/trellis/schemas/well_known.py`, `src/trellis/extract/json_rules.py`, `src/trellis/extract/llm.py`, `src/trellis/retrieve/strategies.py` + ~150 lines of new tests. Canonicalises every emitted draft, stamps `schema_alignment` URIs on canonical types, and routes alias-expanding `GraphSearch` queries through the canonical DSL `in` filter so a query for `"Person"` buckets alongside legacy `"person"` rows. |

ADRs to read for full context:

* [`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) — DSL + contract suite design, Phases 0-3 marked landed
* [`adr-graph-ontology.md`](./adr-graph-ontology.md) — schema.org + PROV-O alignment, Phase 0 marked landed
* [`adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — `ContentTags` / `DataClassification` / `Lifecycle` split
* [`adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge / Operational plane separation
* [`adr-plugin-contract.md`](./adr-plugin-contract.md) — entry-point plugin discovery + DSL requirement
* [`adr-terminology.md`](./adr-terminology.md) — canonical term map (read first if any vocabulary feels ambiguous)

---

## 3. Open phases — execution-ordered

Each entry below is **fully scoped**: scope, files to touch, contract for "done", estimated size, gating signal. A fresh agent can pick any one and execute without further clarification.

### A — Validation & integration testing (no ADR; greenfield)

**Goal:** prove the integration around Neo4j, not just the GraphStore ABC.

#### A.1 — End-to-end Neo4j integration test ✅ landed 2026-04-25

**What landed:** `tests/integration/conftest.py` + `tests/integration/test_neo4j_e2e.py` (6 tests, 0 production-code changes). 6 pass against AuraDB with env loaded; 6 skip cleanly otherwise. Fixture is env-gated and shares the unit suite's vector index name + dimensions to side-step a Neo4j single-vector-index-per-(label,property) constraint that silently swallows competing `CREATE VECTOR INDEX IF NOT EXISTS` calls.

**Coverage delivered:**

* `ENTITY_CREATE` / `LINK_CREATE` through `MutationExecutor` land rows + edges in Neo4j (read back via `GraphStore.get_node` / `get_edges`).
* `MutationExecutor` emits both `MUTATION_EXECUTED` and `ENTITY_CREATED` to the operational `EventLog`.
* `JSONRulesExtractor` → `result_to_batch` → `MutationExecutor.execute_batch` → Neo4j rows (verified via direct graph reads). Note: `Operation.TRACE_INGEST` exists in the registry but has no handler — traces still go straight to `TraceStore.append`. The test path uses the real `ENTITY_CREATE` / `LINK_CREATE` handlers instead, which is the actual production data flow.
* `PackBuilder.build()` with `GraphSearch` over a Neo4j-backed graph store assembles a pack and emits `PACK_ASSEMBLED`.
* `vector.upsert(node_id, embedding)` on the shape #2 store, then `SemanticSearch` through `PackBuilder`, returns the node — exercising the shared `:Node` row across both stores.

**Gotcha logged for future agents:** `_wipe_neo4j` between tests must NOT drop the vector index. AuraDB provisions vector indexes asynchronously, so a fresh `CREATE` followed by an immediate `db.index.vector.queryNodes` call races and fails with "no such vector schema index". Sharing the persistent unit-suite index avoids the wait entirely.

**Files added (final):**
* `tests/integration/conftest.py` (~160 lines) — `registry` + `executor` fixtures, Neo4j wipe helper, vector-index constants.
* `tests/integration/test_neo4j_e2e.py` (~330 lines) — 6 tests covering the four bullet points above plus an event-log audit assertion.

#### A.2 — pgvector + Postgres live test

**Scope:** spin up Postgres locally (or against an existing instance), run the pgvector + Postgres contract subclasses + the Postgres graph tests. Same shape as the AuraDB run.

**Files to touch:** none — the env-gated subclasses already exist.

**Done when:** `pytest tests/unit/stores/contracts/ -v` reports 0 skips with `TRELLIS_TEST_PG_DSN` set.

**Gating:** access to a Postgres instance with pgvector extension installed. **Not yet attempted in this codebase.**

---

### B — Graph ontology ADR ([`adr-graph-ontology.md`](./adr-graph-ontology.md))

#### B.1 — Phase 1: extractors emit canonical names ✅ landed 2026-04-25

**What landed:** Built-in extractors (`JSONRulesExtractor`, `LLMExtractor`) canonicalise every emitted `EntityDraft.entity_type` and `EdgeDraft.edge_kind` via `trellis.schemas.well_known.canonicalize_*`, and auto-populate `properties["schema_alignment"]` with the schema.org / PROV-O URI for canonical types. Open-string types (e.g., `dbt_model`, `emits_metric`) pass through unchanged with no fabricated alignment URI — the open-string contract stays intact.

**New helpers in `well_known.py`:**

* `_ENTITY_SCHEMA_ALIGNMENT` / `_EDGE_SCHEMA_ALIGNMENT` — the URI-mapping single source of truth (`Person → schema.org/Person`, `used → prov:used`, `partOf → schema.org/isPartOf`, etc.). Trellis-specific canonicals (`Project`, `Concept`, `dependsOn`, `attachedTo`, `supports`, `appliesTo`) deliberately have no URI.
* `schema_alignment_for_entity_type(value)` / `schema_alignment_for_edge_kind(value)` — public helpers that canonicalise *value* first, then look up the URI. Return `None` for unknown types so a downstream JSON-LD exporter doesn't fabricate URIs.
* `ENTITY_TYPE_ALIAS_INVERSE` / `EDGE_KIND_ALIAS_INVERSE` — reverse maps (canonical → frozenset of aliases) used by Phase 2 query expansion.
* `expand_entity_type_query(value)` / `expand_edge_kind_query(value)` — return canonical + every legacy alias as a tuple, for retrieval-side bucketing.

**Behavioural notes:**

* User-supplied `property_fields={"schema_alignment": "..."}` mappings win — the auto-populator uses `setdefault` to avoid silently clobbering the rule author's choice. Covered by `test_user_property_named_schema_alignment_wins`.
* The `JSONRulesExtractor` change is purely a draft-time transform, not a rule-validation change — `EntityRule(entity_type="person")` still validates and now produces `EntityDraft(entity_type="Person", properties={"schema_alignment": "schema.org/Person"})`.

**Files touched:**
* `src/trellis/schemas/well_known.py` — +75 lines (alignment dicts + 4 helpers)
* `src/trellis/extract/json_rules.py` — +25 lines (canonicalise inside `_apply_entity_rule` / `_apply_field_edge_rule` / `_apply_ancestor_edge_rule` + a small `_edge_alignment_properties` helper)
* `src/trellis/extract/llm.py` — +15 lines (canonicalise inside `_entity_draft_from_raw` / `_edge_draft_from_raw`)
* `tests/unit/schemas/test_well_known.py` — +44 new tests covering alignment URIs and query expansion
* `tests/unit/extract/test_json_rules.py` — +8 new tests in `TestCanonicalNameEmission`
* `tests/unit/extract/test_llm.py` — updated `test_parses_clean_json` to assert canonicalised output

#### B.2 — Phase 2: retrieval canonicalizes for bucketing ✅ landed 2026-04-25

**What landed:** `GraphSearch.search()` now expands the requested `node_type` filter via `expand_entity_type_query` so a query for `"Person"` includes legacy `"person"` rows in the same bucket — and vice versa. When the expansion fans out (multi-value), the strategy compiles a single `FilterClause("node_type", "in", (...,))` and routes through the canonical DSL (`execute_node_query`) instead of issuing N round-trips. Single-bucket queries (open-string types or canonicals with no aliases) keep using the legacy `query()` path so backends without a Phase 2 DSL compiler still work.

**Plus:** every `PackItem` emitted by `GraphSearch` carries a new `metadata["node_type_canonical"]` field alongside the raw `metadata["node_type"]`. Downstream group-by analytics use the canonical key; display surfaces keep the raw stored type.

**Why analytics CLI was untouched:** `src/trellis_cli/analyze.py` covers context-effectiveness / advisories / pack telemetry — none of which group by entity type. Adding canonicalisation there has no consumer signal yet (Phase 3 land — defer).

**Files touched:**
* `src/trellis/retrieve/strategies.py` — +60 lines (`_query_nodes` helper, DSL routing, canonical-bucket metadata)
* `tests/unit/retrieve/test_strategies.py` — +5 new tests in `TestGraphSearchCanonicalBucketing` + fixture update so existing test wires both `query` and `execute_node_query` mocks

#### B.3 — Phase 3: provenance fields as columns (not properties)

**Scope:** promote `source_trace_id` / `agent_id` / `confidence` / `evidence_ref` / `extractor_tier` from edge `properties` JSON to dedicated columns on the `edges` table.

**Files to touch:**
* `src/trellis/stores/sqlite/graph.py`, `postgres/graph.py`, `neo4j/graph.py` — schema migrations + read/write paths
* `tests/unit/stores/contracts/graph_store_contract.py` — add provenance round-trip tests

**Done when:** edge rows carry first-class provenance, queryable without JSON unpacking.

**Estimated size:** ~400 lines (3 backends × schema migration + read/write).

**Gating:** a policy or retrieval consumer wants to gate on these fields. **Genuinely speculative without that signal — defer.**

#### B.4 — Phase 4: JSON-LD / RDF export

**Scope:** export tooling using populated `schema_alignment` URIs. `trellis admin export-rdf --format jsonld`.

**Gating:** a design partner wants RDF interop. **Speculative — defer.**

---

### C — Canonical translation layer ADR ([`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md))

#### C.1 — Phase 4: vector DSL

**Scope:** mirror the graph DSL on the vector side. `VectorQuery` with `FilterClause` against metadata paths, operator-spec'd (`eq` / `in` / `exists`). Per-backend compilers.

**Why deferred:** all four vector backends agreed on every contract test in the first run. **No drift signal yet.**

**Estimated size when it lands:** ~600 lines (DSL + 4 backend compilers + contract test extension).

**Gating:** vector contract suite shows recurring drift, OR a plugin author requests strongly-typed vector filters.

---

### D — Tag vocabulary split ADR ([`adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md))

Phase 0 (reserved-namespace validator + schema definitions) was landed in earlier work. Phases 1-5 all gated on **a design partner asking** — they're real work but premature without a partner.

| Phase | Scope | Gating |
|---|---|---|
| 1 | Storage migrations + classifier pipeline extension | Partner wants to store classification data |
| 2 | `RegexSensitivityClassifier` / `LifecycleKeywordClassifier` + backfill | Partner wants automatic classification |
| 3 | `PackBuilder` excludes deprecated items by default | Partner reports deprecated-content incidents |
| 4 | `SensitivityGate` / `LifecycleGate` / `PolicyContext` / `ClassificationResolver` | Partner wants enforced access control |
| 5 | `CUSTOM_TAG_USED` telemetry + admin reporting CLI + promotion process | Multiple partners; time to graduate flex tags |

**Recommendation:** do not pre-build. Each phase is independently shippable when its signal fires.

---

### E — Operations / infrastructure (from `TODO.md`)

#### E.1 — Docker + compose smoke test

**Scope:** run `docker compose up --build` against the existing compose file, verify `/healthz`, `/readyz`, `/api/version`, `/ui/`, `trellis demo load` against the containerized API. Proves the Postgres+pgvector path works under the same Dockerfile ECS will use.

**Gating:** Docker available on the dev host. **Currently not installed.**

#### E.2 — Uvicorn log unification

**Scope:** wire `uvicorn` / `uvicorn.access` loggers into `trellis_api.logging.configure_logging()`'s JSON renderer so CloudWatch sees one log shape per container. Non-blocker for POC.

**Estimated size:** ~30 lines + tests.

**Gating:** none — ready to start.

#### E.3 — Fail-fast config validation on startup

**Scope:** extend `StoreRegistry.from_config_dir` (or add a pre-flight in `trellis serve`) to surface missing DSNs / unreachable S3 buckets / embedding-dim mismatches before uvicorn accepts a listener. Today a malformed config only fails on first store access.

**Estimated size:** ~80 lines + tests.

**Gating:** none — ready to start.

#### E.4 — End-to-end AWS deployment dry-run

**Scope:** provision RDS + S3 + ECS in a sandbox account per `docs/deployment/aws-ecs.md`, push the image, boot the task, confirm green. Catches IAM / VPC endpoint / Secrets Manager wiring gaps the compose stack can't see.

**Gating:** AWS sandbox account access.

#### E.5 — Native API-key auth (Phase 1.5)

**Scope:** `TRELLIS_AUTH_MODE=off|optional|required` env toggle, Bearer tokens validated against a new `trellis_api_keys` table, scopes `read`/`write`/`admin`, wired via FastAPI `Depends`.

**Gating:** VPN-only assumption breaks. **Deferred until then.**

---

## 4. Recommended execution order for a fresh swarm

If picking up cold, work the list top-down. Each item's gating is satisfied by the time you reach it.

| # | Item | Status | Why this slot |
|---|---|---|---|
| 1 | **A.1** End-to-end Neo4j integration test | ✅ Landed 2026-04-25 | Validates the integration the ADRs assume works. Closes the loop on the AuraDB live tests. |
| 2 | **B.1 + B.2** Ontology Phase 1 + 2 (extractor canonical names + retrieval bucketing) | ✅ Landed 2026-04-25 | Small, well-scoped, no gating delays. Makes agent-facing graph queries less fragile. |
| 3 | **E.2 + E.3** Uvicorn log unification + fail-fast config validation | Ready | Operational hygiene before the AWS dry-run. ~110 lines combined. |
| 4 | **A.2** pgvector + Postgres live tests | Ready when Postgres available | Drift surface validated for the second cloud backend. |
| 5 | **E.1 + E.4** Docker compose smoke test + AWS dry-run | Need infra access | Ships the deployment story end-to-end. |
| 6 | **B.3** Provenance columns | Wait for signal | Real cost; speculative without a consumer. |
| 7 | **C.1** Vector DSL Phase 4 | Wait for signal | No drift surfaced; speculative. |
| 8 | **D.1-5** Tag vocabulary phases | Wait for design partner | All design-partner-gated. |
| 9 | **B.4 / E.5** RDF export, native API-key auth | Wait for signal | Last by design. |

---

## 5. Hand-off protocol for a fresh agent

Read in order:

1. **`CLAUDE.md`** — project conventions, hard rules, terminology.
2. **This file** — what's done, what's open, recommended order.
3. **The ADR for the phase you're picking up.** Each phase entry above links the right one. *Don't* read every ADR cold; they're long.
4. **The contract test suites under `tests/unit/stores/contracts/`** — these are the authoritative behavioural spec for the storage layer. New backend code is judged against them.

Before writing code:

* Run `pytest tests/unit/ -q` → confirm 653 passing baseline.
* If you're touching Neo4j: `set -a && source .env && set +a` then run `pytest tests/unit/stores/test_neo4j_*.py tests/unit/stores/contracts/test_neo4j_graph_contract.py -q` to confirm 110 live-tests still pass against AuraDB. If `.env` is missing, ask the user — credentials are in their local copy.
* Read [`adr-terminology.md`](./adr-terminology.md) §2 if any term feels ambiguous.

When picking up a phase:

* The phase entry above is the contract. If scope is unclear, the ADR is the spec.
* New phases must be ADR-amended before implementation. The "recommended order" table is the queue, not the spec.
* Update this file when a phase lands. The "State" section at the top is the live truth.

---

## 6. File inventory — what this thread changed

For audit and rollback. Files added or modified between 2026-04-23 and 2026-04-25.

### Added

```
.env.example                            # credential template, committed
src/trellis/stores/neo4j/__init__.py
src/trellis/stores/neo4j/base.py
src/trellis/stores/neo4j/graph.py
src/trellis/stores/neo4j/vector.py
src/trellis/schemas/well_known.py
src/trellis/stores/base/graph_query.py
tests/unit/stores/test_neo4j_graph.py
tests/unit/stores/test_neo4j_vector.py
tests/unit/stores/test_graph_query_dsl.py
tests/unit/schemas/test_well_known.py
tests/unit/stores/contracts/__init__.py
tests/unit/stores/contracts/graph_store_contract.py
tests/unit/stores/contracts/vector_store_contract.py
tests/unit/stores/contracts/test_sqlite_graph_contract.py
tests/unit/stores/contracts/test_postgres_graph_contract.py
tests/unit/stores/contracts/test_neo4j_graph_contract.py
tests/unit/stores/contracts/test_sqlite_vector_contract.py
tests/unit/stores/contracts/test_pgvector_contract.py
tests/unit/stores/contracts/test_lancedb_vector_contract.py
tests/integration/conftest.py           # A.1
tests/integration/test_neo4j_e2e.py     # A.1
docs/design/adr-graph-ontology.md
docs/design/adr-canonical-graph-layer.md
docs/design/implementation-roadmap.md   # this file
```

### Modified

```
.gitignore                              # broadened .env coverage; .env.example exception
CLAUDE.md                               # store table + contract suite pointer
TODO.md                                 # multiple sections updated
pyproject.toml                          # [neo4j] extra, mypy override, pytest marker
src/trellis/schemas/enums.py            # docstring pointers to well_known.py
src/trellis/stores/registry.py          # Neo4j backends registered
src/trellis/stores/base/graph.py        # execute_node_query / execute_subgraph_query ABC methods
src/trellis/stores/sqlite/graph.py      # Phase 2 compiler
src/trellis/stores/postgres/graph.py    # Phase 2 compiler (JSONB containment)
src/trellis/stores/neo4j/graph.py       # Phase 2 compiler (Cypher + Python-side property filters)
docs/agent-guide/schemas.md             # canonical name tables
docs/design/adr-terminology.md          # §2.5 graph ontology
docs/design/adr-plugin-contract.md      # contract test suite + DSL requirement
```

### Untracked & not in scope

```
src/trellis/stores/sql/                 # pre-existing, not touched by this thread
.claude/scheduled_tasks.lock            # local IDE state
```

---

*If this roadmap is out of date, update Section 1 ("State of the project") and re-balance Section 3 ("Open phases"). The rest stays stable.*
