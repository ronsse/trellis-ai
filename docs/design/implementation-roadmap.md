# Implementation Roadmap

**Last updated:** 2026-07-02 (backlog fully triaged — every open issue is gated; §1 rewritten, ADR-phase statuses corrected)
**Purpose:** Single-page hand-off for any agent (fresh or returning) picking up Trellis implementation work. Self-contained. Read this top-to-bottom before touching code.

> **Picking up evaluation work?** The eval harness and all planned scenarios are built — see [`../plans/2026-06-17-step3-assessment.md`](../plans/2026-06-17-step3-assessment.md) for what each scenario substantiates (its §6 evidence rules are authoritative). [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) is the historical plan they grew from.
>
> **Picking up the memory-layer work?** [`adr-memory-layer-interop.md`](./adr-memory-layer-interop.md) is Proposed with zero implementation, and is **deliberately parked** — the owner will open a dedicated session for it (which may start with an ADR revision). Do not start it from here.
>
> **Picking up Phase F (inner agent loop)?** TODO.md "Phase F" is the staged program (F1 harness ≈ 1500–2000 LOC; F5 gated on 30 days of F4 signal). The F6 eval scenario is already implemented with reference drivers at the F1–F5 plug-in seam (`eval/scenarios/skill_loop_convergence/`).

---

## 1. State of the project — 2026-07-02

### What's live and tested

* **All storage planes wired** (trace / document / graph / vector / event_log / blob) via `StoreRegistry` (`src/trellis/stores/registry.py`), plus a **`null` event_log backend** for knowledge-plane-only deployments (governed graph mutations with no operational plane — issue #196).
* **Four graph backends:** SQLite (default), Postgres, **ArcadeDB (blessed)**, Neo4j. ArcadeDB + Neo4j share the `BoltOpenCypherGraphStore` base. All pass `GraphStoreContractTests` — including the edge re-ingest idempotency tests that caught (and whose fix closed) a Bolt SCD-2 bug where re-upserting an unchanged node stranded its edges (#195, `ab36af6`).
* **Vector backends:** SQLite, pgvector, ArcadeDB (SQL-over-HTTP, `LSM_VECTOR`), Neo4j shape #2. Note: the Neo4j vector `SEARCH` clause is **AuraDB-only** — self-hosted Docker (community *and* enterprise) lacks it; the e2e test self-skips there.
* **Scoped REST auth landed** (`TRELLIS_AUTH_MODE=off|optional|required`, Bearer keys with read/write/admin scopes — PR #242) — the old §E.5 entry below is closed.
* **Edge provenance as first-class columns** (`source_trace_id` / `agent_id` / `confidence` / `evidence_ref` / `extractor_tier`) across all backends — the old §B.3 entry below is closed.
* **Security floor for machine surfaces:** leak-safe error payloads (`trellis.core.error_sanitize`, #206 — CLI JSON + `/readyz` + admin routes) and an ArcadeDB admin/runtime credential split (#193, `TRELLIS_ARCADEDB_ADMIN_*`).
* **Wire-level `allow_dangling`** on LINK_CREATE (REST + SDK, #211) — with the recorded caveat that Bolt backends still require both endpoint vertices at the store layer; materialize-a-stub-endpoint-first is the supported pattern there (#215 discussion).
* **Agent-integration surface complete** (2026-06-12 hand-off, all 13 WPs): HTTP-only SDK with `record_feedback` + hooks, trace→graph extraction, `trellis worker curate/tune/enrich/mine-precedents`, review-queue UI, metrics dashboard, `quickstart --with-skills`.
* **Eval harness complete:** every planned scenario implemented, including `skill_loop_convergence` (reference-driver build, #249 — axis Q citable for C1; axis R measurement-path-only). Evidence rules live in the step-3 assessment §6.

### Changed since the 2026-04-27 revision (digest)

| When | What | Where to read |
|---|---|---|
| 2026-05 | ArcadeDB blessed as the graph+vector substrate; LanceDB removed | [`adr-arcadedb-blessed-substrate.md`](./adr-arcadedb-blessed-substrate.md) |
| 2026-05-18 | Phase F (inner agent loop) proposed; WorkflowEngine retired (`1291210`) | TODO.md "Phase F"; [`workflow-engine-disposition.md`](../research/workflow-engine-disposition.md) |
| 2026-06-04 | Memory-layer interop ADR proposed (parked — dedicated session) | [`adr-memory-layer-interop.md`](./adr-memory-layer-interop.md) |
| 2026-06-11 | Public-repo scrub; scoped API-key auth (#242) + UI key flow (#244) | strategy notes (private repo) |
| 2026-06-12 | Agent-integration hand-off executed (13 WPs) | [`../plans/2026-06-12-agent-integration-handoff.md`](../plans/2026-06-12-agent-integration-handoff.md) |
| 2026-06-17→24 | Step-3 assessment: dashboard↔eval parity resolved as intentional divergence; §6 items closed | [`../plans/2026-06-17-step3-assessment.md`](../plans/2026-06-17-step3-assessment.md) |
| 2026-06-30→07-02 | Pilot core fixes (#211/#196/#195 + Bolt SCD-2 fix), CI de-AuraDB'd, security floor (#206/#193), backlog triage (35 issues closed), `skill_loop_convergence` implemented (#249) | [`../plans/2026-06-30-pilot-core-fixes.md`](../plans/2026-06-30-pilot-core-fixes.md) |

### Test suite shape (2026-07-02)

`pytest tests/unit/ -q` collects **3962 tests by default** (4510 total; 548 backend-marked tests deselect without their env toggles). CI runs five required checks (Tests, Lint, Type Check, OpenAPI, CodeQL) plus **Live infrastructure tests** — which since `586aee6` runs against **ephemeral service containers** (`neo4j:2025.12`, `pgvector/pgvector:pg16`), needs **no external instance and no repo secrets**, and includes the storage contract suites.

### Live test credentials — current reality

* **The AuraDB free-tier instance is GONE** (auto-deleted; DNS-dead). Do **not** follow older instructions that say "leave it running". CI no longer needs it. For local live Neo4j/ArcadeDB tests, run containers — the fixture header of `tests/unit/stores/test_arcadedb_graph.py` and `.github/workflows/live-infra.yml` show the exact `docker run` incantations and `TRELLIS_TEST_*` variables.
* **Repo-root `.env` no longer exists** (only `.env.example`, which documents every recognised variable). Recreate locally as needed.
* Remaining credential hygiene (rotate dead AuraDB creds in 1Password, Neon status) is tracked in **#250** (operator-only).

## 2. Recently completed (historical — the 2026-04-23 → 2026-04-25 thread)

| Work | Files |
|---|---|
| `Neo4jGraphStore` + `Neo4jVectorStore` (shape #2) + tests | `src/trellis/stores/neo4j/`, `tests/unit/stores/test_neo4j_*.py` |
| Graph ontology ADR Phase 0 — `well_known.py` + alias maps + 41 tests + docs | `src/trellis/schemas/well_known.py`, `tests/unit/schemas/test_well_known.py`, ontology section in `docs/agent-guide/schemas.md`, terminology row in `docs/design/adr-terminology.md` |
| Canonical translation layer ADR Phases 0-3 — contract suites, DSL, per-backend compilers, plugin contract requirement | `tests/unit/stores/contracts/`, `src/trellis/stores/base/graph_query.py`, compilers in `src/trellis/stores/{sqlite,postgres,neo4j}/graph.py`, plugin-contract section in `docs/design/adr-plugin-contract.md` |
| Live-tested 110 Neo4j tests against AuraDB Free | one fixture fix (DB env var); no production code changes needed |
| **A.1** — End-to-end Neo4j integration suite (6 tests) | `tests/integration/conftest.py`, `tests/integration/test_neo4j_e2e.py`. Covers ENTITY_CREATE / LINK_CREATE through MutationExecutor, audit-event emission, JSONRulesExtractor → drafts → batch → graph rows, PackBuilder against Neo4j-backed graph, and SemanticSearch through the shape #2 vector store. Test-only changes; no production code touched. |
| **B.1 + B.2** — Graph ontology Phase 1 + 2 (canonical names in extractors + alias-bucketing in retrieval) | `src/trellis/schemas/well_known.py`, `src/trellis/extract/json_rules.py`, `src/trellis/extract/llm.py`, `src/trellis/retrieve/strategies.py` + ~150 lines of new tests. Canonicalises every emitted draft, stamps `schema_alignment` URIs on canonical types, and routes alias-expanding `GraphSearch` queries through the canonical DSL `in` filter so a query for `"Person"` buckets alongside legacy `"person"` rows. |
| **E.2** — Uvicorn log unification | `src/trellis_api/logging.py`. `configure_logging` now installs a `structlog.stdlib.ProcessorFormatter` on the root handler and clears uvicorn's per-logger handlers so `uvicorn`, `uvicorn.error`, and `uvicorn.access` all render through the shared structlog processor chain. Stdlib `extra={...}` kwargs are promoted via `ExtraAdder`. JSON-mode containers see exactly one log shape per line. |
| **E.3** — Fail-fast config validation | `src/trellis/stores/registry.py` — new `RegistryValidationError` + `StoreRegistry.validate()` walks every (or a subset of) store_type, instantiates each, and aggregates errors into one multi-line message so an operator sees every misconfiguration at once. `src/trellis_api/app.py` lifespan calls `validate()` after construction so missing DSNs, unset S3 buckets, and plugin-import failures crash startup before uvicorn accepts requests. |
| **A.2** — pgvector + Postgres live tests against Neon | `src/trellis/stores/pgvector/store.py` (latent two-bug fix surfaced by first live run), `tests/unit/stores/test_postgres_stores.py` (stale `Trace(...)` fixtures missing the now-required `context` field). Provisioned a Neon free-tier project (pgvector 0.8.0) and ran the full PG-gated suite: 14 PG store + 13 pgvector store + 11 PG-graph contract + 25 pgvector contract = 63 tests, all green. |

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
* `JSONRulesExtractor` → `result_to_batch` → `MutationExecutor.execute_batch` → Neo4j rows (verified via direct graph reads).
* `PackBuilder.build()` with `GraphSearch` over a Neo4j-backed graph store assembles a pack and emits `PACK_ASSEMBLED`.
* `vector.upsert(node_id, embedding)` on the shape #2 store, then `SemanticSearch` through `PackBuilder`, returns the node — exercising the shared `:Node` row across both stores.

**Gotcha logged for future agents:** `_wipe_neo4j` between tests must NOT drop the vector index. AuraDB provisions vector indexes asynchronously, so a fresh `CREATE` followed by an immediate `db.index.vector.queryNodes` call races and fails with "no such vector schema index". Sharing the persistent unit-suite index avoids the wait entirely.

**Files added (final):**
* `tests/integration/conftest.py` (~160 lines) — `registry` + `executor` fixtures, Neo4j wipe helper, vector-index constants.
* `tests/integration/test_neo4j_e2e.py` (~330 lines) — 6 tests covering the four bullet points above plus an event-log audit assertion.

#### A.2 — pgvector + Postgres live test ✅ landed 2026-04-25

**What landed:** Provisioned a Neon free-tier Postgres project (pgvector 0.8.0 preinstalled), enabled `vector` on the default database, set `TRELLIS_TEST_PG_DSN` in `.env`, and ran the full PG-gated test suite. With the env var set, `pytest tests/unit/stores/contracts/` reports **166 passed / 0 Postgres skips** (the 2 remaining skips are LanceDB / Neo4j *optional dep* skips — unrelated). The four PG-gated suites are 100% green: 14 PG store + 13 pgvector store + 11 PG-graph contract + 25 pgvector contract = 63 tests.

**Drift surfaced + fixed (2 bugs that had been latent since pgvector was first written):**

1. `PgVectorStore.upsert/query` passed Python `list` values into `INSERT ... %s::vector`. `pgvector.psycopg.register_vector` only registers a Dumper for `numpy.ndarray`, so plain lists were sent as Postgres `smallint[]` arrays and the cast to `vector` failed with `cannot cast type smallint[] to vector`. Fix: format the vector as the pgvector text literal `'[1.0,0.0,0.0]'` before binding, so the explicit `::vector` cast does text→vector. New `_format_vector()` helper at module scope keeps it in one place.
2. `PgVectorStore.query` built params as `[vec, vec, top_k]` and inserted filter JSONs at index `-1`. With one filter, the resulting list `[vec, vec, json, top_k]` did not match the SQL placeholder order — the `ORDER BY embedding <=> %s::vector` placeholder received the JSON instead of a vector. Fix: build params positionally as `[vec, *filter_params, vec, top_k]` so the order is unambiguous.

**Trace test fixtures updated:** `tests/unit/stores/test_postgres_stores.py::TestPostgresTraceStore._make_trace` was constructing `Trace(...)` without the now-required `context: TraceContext` field — a stale fixture that had never been live-run. Added `context=TraceContext(agent_id="agent-1", domain="platform")` to match the SQLite trace tests.

**Files touched:**
* `src/trellis/stores/pgvector/store.py` — +12 lines (`_format_vector` helper + 3 call-site updates) + reordered `params` construction in `query()`
* `tests/unit/stores/test_postgres_stores.py` — +2 lines on the trace fixture
* `.env` — added `TRELLIS_TEST_PG_DSN`

**Gotcha logged:** Neon ships pgvector binaries preinstalled but the extension is *not* enabled on a fresh database. Run `CREATE EXTENSION IF NOT EXISTS vector;` once via the Neon SQL Editor or via psycopg before pointing tests at the DSN.

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

#### B.3 — Phase 3: provenance fields as columns ✅ landed (2026-05 series)

> **Status correction (2026-07-02):** dedicated `source_trace_id` / `agent_id` /
> `confidence` / `evidence_ref` / `extractor_tier` columns exist on every backend
> (SQLite / Postgres / Bolt), validated per row and covered by contract tests
> (`test_edge_provenance_round_trip` and friends). The original entry below is
> retained for the historical scope statement.

#### ~~B.3 — original scope statement~~

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

#### E.1 — Docker + compose smoke test ✅ landed 2026-06-12

**What landed:** built and ran the compose stack end-to-end (`docker compose up --build`) — image, Postgres+pgvector init, and all four probes (`/healthz`, `/readyz` with per-backend latencies, `/api/version`, `/ui/`) came up green first try, the skills package data and static UI ship in the image, and a trace round-trips through the containerized Postgres path via the REST API (`POST/GET /api/v1/traces`, `POST /api/v1/packs`). `deploy/smoke.sh` passes 10/10. Corrected `docs/deployment/local-compose.md`: `trellis demo load`/`trellis retrieve` are local-only (no `TRELLIS_API_URL` remote target), so the verification loop now drives the REST API directly; documented the UI-gating / `TRELLIS_AUTH_MODE=off` / Postgres-vs-SQLite env shape with real pasted outputs.

#### E.2 — Uvicorn log unification ✅ landed 2026-04-25

**What landed:** `configure_logging` in `src/trellis_api/logging.py` now factors the structlog processor chain into a `shared_processors` list reused by both the structlog `configure(...)` call and a `structlog.stdlib.ProcessorFormatter`. The formatter is attached to the root handler; `uvicorn`, `uvicorn.error`, and `uvicorn.access` have their own handlers cleared and `propagate=True` so every line flows through the shared bridge. Stdlib `extra={...}` kwargs are surfaced via `ExtraAdder` in `foreign_pre_chain`. Container log drivers now see exactly one shape per line in JSON mode.

**Files touched:** 1 source file (~60 lines including the new `_UVICORN_LOGGERS` constant + bridge setup), 15 new tests in `tests/unit/api/test_logging.py` covering wiring, JSON / console rendering, level honoring, and shape parity between bridged and native lines.

#### E.3 — Fail-fast config validation on startup ✅ landed 2026-04-25

**What landed:** New `RegistryValidationError` (carries the full `(store_type, exception)` list so a programmatic consumer can introspect) plus `StoreRegistry.validate(*, store_types=None)` in `src/trellis/stores/registry.py`. `validate()` walks every store_type the registry knows about (or a caller-supplied subset), forces a side-effecting `_instantiate` call per store, and accumulates exceptions into a single aggregate so an operator sees every misconfiguration at once rather than fixing them serially across deploy attempts. Successful stores stay warm in the cache so post-validation access is free. The `app.py` lifespan calls `validate()` after `from_config_dir()` so missing DSNs, unset S3 buckets, and plugin import failures crash uvicorn before it accepts requests.

**Deliberately skipped:** explicit "ping" validation for backends that connect lazily (Neo4j Bolt, S3 boto3 client). Adding it requires a per-backend round-trip, which raises real cost and the false-positive surface (transient network blips, IAM role bootstrap delays) without a current incident to motivate. Logged in the docstring as the natural next step.

**Files touched:** `src/trellis/stores/registry.py` (+90 lines), `src/trellis_api/app.py` (+1 call + 5 lines of docstring), `tests/unit/stores/test_registry_validation.py` (new file, 9 tests covering happy paths, partial failures, error rendering).

#### E.4 — End-to-end AWS deployment dry-run

**Scope:** provision RDS + S3 + ECS in a sandbox account per `docs/deployment/aws-ecs.md`, push the image, boot the task, confirm green. Catches IAM / VPC endpoint / Secrets Manager wiring gaps the compose stack can't see.

**Gating:** AWS sandbox account access.

#### E.5 — Native API-key auth ✅ landed 2026-06-11 (PR #242)

> **Status correction (2026-07-02):** `TRELLIS_AUTH_MODE=off|optional|required`,
> Bearer tokens against the `api_key` store, read/write/admin scopes via FastAPI
> dependencies, plus the UI key flow (PR #244). The original gating note below is
> historical.

#### ~~E.5 — original scope statement~~

**Scope:** `TRELLIS_AUTH_MODE=off|optional|required` env toggle, Bearer tokens validated against a new `trellis_api_keys` table, scopes `read`/`write`/`admin`, wired via FastAPI `Depends`.

**Gating:** VPN-only assumption breaks. **Deferred until then.**

---

## 4. Recommended execution order for a fresh swarm

**As of 2026-07-02 there is no unblocked queue.** Every open GitHub issue is gated,
and the ADR phases above are either landed or deliberately signal-gated. A fresh
agent should NOT invent work from this table — pick up whichever gate has fired:

| Gate | Items | Fires when |
|---|---|---|
| Production pilot resumes | #200 (usage families) · #201 (BI-metadata extractor) · #202 (matching guardrails) · #203 (scouting primitive) | consumer-kg pilot restarts and produces real query-history flow |
| Design partner asks | #194 / Tag-vocab phases 1–5 (§D) · B.4 RDF export | partner wants enforced classification / RDF interop |
| Vector-contract drift | C.1 vector DSL | contract suite shows backend drift, or a plugin author asks |
| Infra access | E.4 AWS ECS+RDS dry-run · #208 | sandbox account / ArcadeDB secret + SSO available |
| Operator console access | #250 credential hygiene | 1Password / Neo4j console session |
| Deliberate scheduling | Phase F waves F1–F5 (TODO.md) · memory-layer ADR (dedicated session) · #248 organic-generation corpus tuning | owner schedules them |

---

## 5. Hand-off protocol for a fresh agent

Read in order:

1. **`CLAUDE.md`** — project conventions, hard rules, terminology.
2. **This file** — what's done, what's gated, where the gates are (§4).
3. **The ADR / plan for whichever gate fired.** Don't read every ADR cold; they're long.
4. **The contract test suites under `tests/unit/stores/contracts/`** — the authoritative behavioural spec for the storage layer. They run in CI against SQLite, Postgres, and a containerized Neo4j on every push to main.

Before writing code:

* Run `pytest tests/unit/ -q` — expect ~3962 collected / green by default (548 backend-marked tests deselect cleanly without env toggles; exact counts drift as tests land — CI's Tests job is the source of truth).
* Touching graph/vector backends? Spin up local containers and export the `TRELLIS_TEST_*` toggles — copy the incantations from `.github/workflows/live-infra.yml`. **There is no shared cloud instance anymore** (the AuraDB free-tier instance was auto-deleted; CI is container-based since `586aee6`).
* **Bolt-substrate discipline** (hard-won, twice): the SQLite graph store tolerates behaviours the Bolt backends reject — dangling edges, and (pre-`ab36af6`) unchanged-node re-versioning that strands edges. Never validate Bolt-path changes on SQLite alone; run the contract suites against a Neo4j container, and run `tests/unit/stores/` with `TRELLIS_TEST_NEO=1` so the marker-deselected mock suites execute.
* Read [`adr-terminology.md`](./adr-terminology.md) §2 if any term feels ambiguous.

When picking up a phase:

* The phase entry above (or the gated issue) is the contract. If scope is unclear, the ADR is the spec.
* New phases must be ADR-amended before implementation. §4's gate table is the queue, not the spec.
* Update this file when a phase lands. Section 1 is the live truth; §2 and §6 are historical records.

---

## 6. File inventory — what the 2026-04 thread changed (historical)

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
src/trellis/stores/pgvector/store.py    # A.2 — list→vector adapter + param-order fix
tests/unit/stores/test_postgres_stores.py  # A.2 — TraceContext field on _make_trace
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
