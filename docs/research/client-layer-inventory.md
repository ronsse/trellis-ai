# Research: Trellis Client & Core Layer Inventory

**Date:** 2026-04-11
**Purpose:** Baseline architectural inventory of Trellis itself as of this date, captured during the landscape comparison work. This is the "us" side of the comparison — companion to [`memory-systems-landscape.md`](./memory-systems-landscape.md).

This document is a **point-in-time snapshot**, not a user guide. For live operational reference see [`../agent-guide/operations.md`](../agent-guide/operations.md) and [`../agent-guide/schemas.md`](../agent-guide/schemas.md).

---

## 1. Layer overview

Trellis is organized into two axes:

- **Core layer** (`src/trellis/`) — schemas, stores, mutation pipeline, retrieval, classification. The library everything else depends on.
- **Client layer** — five surfaces that expose the core to different audiences:
  - `trellis_sdk` — Python library (dual-mode local/remote)
  - `trellis_cli` (`trellis`) — operator CLI
  - `trellis_api` (`trellis-api`) — FastAPI REST
  - `trellis` MCP server (`trellis-mcp`) — agent-facing macro tools
  - `trellis_workers` — background ingestion and enrichment

---

## 2. Client layer inventory

### 2.1 `trellis_sdk` — Python SDK

**Location:** `src/trellis_sdk/`
**Audience:** Agents, orchestrators, embedded Python code
**Entry point:** `TrellisClient(base_url: str | None = None)`
**Key characteristic:** Dual-mode dispatch — switches between local imports and remote httpx based on `base_url`.

**Public methods:**

| Category | Method | Notes |
|---|---|---|
| Ingest | `ingest_trace(trace: dict) -> trace_id` | |
| Ingest | `ingest_evidence(evidence: dict) -> evidence_id` | |
| Retrieve | `search(query, domain, limit) -> results` | |
| Retrieve | `get_trace(trace_id) -> dict` | |
| Retrieve | `list_traces(domain, limit) -> list[dict]` | |
| Retrieve | `get_entity(entity_id) -> dict` | |
| Pack | `assemble_pack(intent, domain, agent_id, max_items, max_tokens) -> Pack` | |
| Pack | `assemble_sectioned_pack(intent, sections, domain, agent_id) -> SectionedPack` | |
| Context | `get_objective_context(intent, domain, max_tokens) -> str` | Markdown |
| Context | `get_task_context(intent, entity_ids, domain, max_tokens) -> str` | Markdown |
| Curate | `create_entity(name, entity_type, properties) -> node_id` | Goes through `MutationExecutor` |
| Curate | `create_link(source_id, target_id, edge_kind) -> edge_id` | Goes through `MutationExecutor` |
| Lifecycle | `close()` | |

**Mode dispatch:**
- **Remote:** POST `/api/v1/traces`, `/api/v1/evidence`; GET `/api/v1/search`, `/api/v1/traces/{id}`, `/api/v1/entities/{id}`; POST `/api/v1/packs`, `/api/v1/packs/sectioned`; POST `/api/v1/entities`, `/api/v1/links`
- **Local:** direct lazy imports of `StoreRegistry`, `PackBuilder`, `Trace`, `Evidence` schemas

**Skills helpers** (`trellis_sdk/skills.py`): pre-wrapped convenience functions for common agent patterns:
- `get_context_for_task(client, intent, domain, max_tokens) -> str`
- `get_latest_successful_trace(client, task_type, domain) -> str`
- `save_trace_and_extract_lessons(client, trace) -> str`
- `get_recent_activity(client, domain, limit, max_tokens) -> str`
- `get_objective_context_for_workflow(client, intent, domain, max_tokens) -> str`
- `get_task_context_for_step(client, intent, entity_ids, domain, max_tokens) -> str`

### 2.2 `trellis_cli` — Operator CLI

**Location:** `src/trellis_cli/main.py`
**Entry point:** `trellis`
**Audience:** Operators, CI/CD, human operators
**JSON output:** all commands support `--format json` for machine parsing.

| Group | Command | Purpose |
|---|---|---|
| `admin` | `init [--data-dir PATH] [--force] [--format]` | Initialize stores & config |
| `admin` | `health [--format]` | Health check |
| `admin` | `stats [--format]` | Store statistics |
| `admin` | `claude-integration` | Register MCP server with Claude Desktop |
| `ingest` | `trace [FILE\|stdin] [--format]` | Ingest trace JSON |
| `ingest` | `evidence FILE [--format]` | Ingest evidence JSON |
| `ingest` | `dbt-manifest PATH [--format]` | Ingest dbt manifest → nodes + edges + docs |
| `ingest` | `openlineage PATH [--format]` | Ingest OpenLineage events → lineage graph |
| `retrieve` | `pack --intent STR [...] [--format]` | Assemble pack |
| `retrieve` | `search QUERY [...] [--format text\|json\|jsonl\|tsv]` | Full-text search |
| `retrieve` | `trace TRACE_ID [--format]` | Get trace by ID |
| `retrieve` | `entity ENTITY_ID [--format]` | Get entity by ID |
| `retrieve` | `traces [...] [--format]` | List traces |
| `retrieve` | `precedents [...] [--format]` | List promoted traces |
| `curate` | `promote TRACE_ID --title --description [...]` | Promote trace → precedent |
| `curate` | `link SOURCE_ID TARGET_ID [--kind]` | Create graph edge |
| `curate` | `label TARGET_ID LABEL_VALUE` | Add label to entity |
| `curate` | `entity ENTITY_TYPE NAME [--properties JSON]` | Create entity |
| `curate` | `feedback TARGET_ID RATING [--comment]` | Record feedback |
| `analyze` | `context-effectiveness [--days --min-appearances]` | Effectiveness report |
| `analyze` | `token-usage [--days]` | Token usage across layers |

All curate commands go through `MutationExecutor` and return `CommandResult(status, operation, message, created_id)`.

### 2.3 `trellis_api` — FastAPI REST

**Location:** `src/trellis_api/`
**Entry point:** `trellis-api` (listens on `http://0.0.0.0:8420`)
**Audience:** HTTP clients, dashboards, headless orchestrators
**App init:** `StoreRegistry` in FastAPI lifespan; `get_registry()` dependency injection

| Router | Method | Path | Purpose |
|---|---|---|---|
| Ingest | POST | `/api/v1/traces` | Ingest trace |
| Ingest | POST | `/api/v1/evidence` | Ingest evidence |
| Ingest | POST | `/api/v1/vectors` | Batch upsert vectors |
| Retrieve | GET | `/api/v1/search` | Full-text search |
| Retrieve | POST | `/api/v1/packs` | Assemble pack |
| Retrieve | GET | `/api/v1/graph/search` | Graph search |
| Retrieve | GET | `/api/v1/traces` | List traces |
| Retrieve | GET | `/api/v1/traces/{trace_id}` | Get trace |
| Retrieve | GET | `/api/v1/entities/{entity_id}` | Get entity |
| Retrieve | GET | `/api/v1/precedents` | List precedents |
| Curate | POST | `/api/v1/precedents` | Promote trace → precedent |
| Curate | POST | `/api/v1/links` | Create edge |
| Curate | POST | `/api/v1/entities` | Create entity |
| Curate | POST | `/api/v1/feedback` | Record feedback |
| Admin | GET | `/api/v1/health` | Health check |
| Admin | GET | `/api/v1/stats` | Store statistics |
| Admin | POST | `/api/v1/config/reload` | Reload config |

Request/response schemas in `src/trellis_api/models.py` (Pydantic `TrellisModel`): `IngestResponse`, `PackRequest`, `PackResponse`, `PromoteRequest`, `LinkRequest`, `EntityCreateRequest`, `FeedbackRequest`, `CommandResponse`, `HealthResponse`, `StatsResponse`.

**Auth model:** none currently. Relies on network isolation.

### 2.4 MCP Server — agent-facing macro tools

**Location:** `src/trellis/mcp/server.py` (new) or `src/trellis/mcp_server.py` (legacy)
**Entry point:** `trellis-mcp` (register in Claude Desktop or any MCP client)
**Audience:** Claude, other MCP-compatible LLMs
**Design:** all tools return markdown strings (LLM-friendly), track token usage via `track_token_usage()` → event log, share a `StoreRegistry` singleton.

| Tool | Signature | Returns |
|---|---|---|
| `get_context` | `(intent, domain, max_tokens)` | Markdown pack |
| `save_experience` | `(trace_json)` | Confirmation |
| `save_knowledge` | `(name, entity_type, properties, relates_to, edge_kind)` | Confirmation |
| `save_memory` | `(content, metadata, doc_id)` | Confirmation |
| `get_lessons` | `(domain, limit, max_tokens)` | Markdown |
| `get_graph` | `(entity_id, depth, max_tokens)` | Markdown |
| `record_feedback` | `(trace_id, success, notes)` | Confirmation |
| `search` | `(query, limit, max_tokens)` | Markdown |

### 2.5 `trellis_workers` — background processing

**Location:** `src/trellis_workers/`
**Audience:** Background processes, CLI-triggered, enrichment services

| Type | Class | Pattern |
|---|---|---|
| Ingestion | `IngestionWorker` (base) | `discover(source_path) → extract(items) → load(nodes, edges) → counts` |
| Ingestion | `DbtManifestWorker(registry)` | dbt manifest → models/sources/tests as nodes; `depends_on` as edges |
| Ingestion | `OpenLineageWorker(registry)` | OpenLineage events → lineage nodes/edges |
| Enrichment | `EnrichmentService` | LLM-based classification fallback |
| Learning | `Miner` | Extract reusable patterns from traces (TBD in detail) |
| Maintenance | `RetentionService` | Cleanup, archival, retention policies |

---

## 3. Client-layer agent journeys

### 3.1 Ingest a trace

```
Agent (Python/CLI/HTTP)
  ↓
  (A1) SDK: client.ingest_trace(trace_dict)
       → if remote: POST /api/v1/traces
       → if local: Trace.validate() → trace_store.append()
  ↓
  Trace stored immutably; TRACE_INGESTED event emitted
  ↓
  (Optional) ClassifierPipeline tags trace async if LLM enricher configured
```

**Files:** `src/trellis_sdk/client.py:ingest_trace()`, `src/trellis_api/routes/ingest.py:ingest_trace()`, `src/trellis/stores/base/trace.py`

### 3.2 Retrieve a context pack

```
Agent (Python/MCP/CLI/HTTP)
  ↓
  Intent: "Build a transformation for user telemetry"
  ↓
  (B1) SDK/MCP: client.assemble_pack(intent, domain="data", max_tokens=4000)
       → PackBuilder instantiated with strategies: [KeywordSearch, GraphSearch]
       → Filter: domain="data"
       → Deduplicate, budget (max_items=50, max_tokens=4000)
       → PACK_ASSEMBLED event logged with effectiveness metadata
  ↓
  (B2) PackBuilder.build() calls strategies in order:
       - KeywordSearch: doc_store.search(intent, limit=10)
       - GraphSearch: graph_store.query() with filtering
  ↓
  (B3) Items scored by relevance_score + tag-based importance
  ↓
  Return: Pack {items: [...], retrieval_report: {...}}
          (markdown formatted if MCP/skills return path)
```

**Files:** `src/trellis_sdk/client.py:assemble_pack()`, `src/trellis/retrieve/pack_builder.py`, `src/trellis_api/routes/retrieve.py:assemble_pack()`, `src/trellis/mcp/server.py:get_context()`

### 3.3 Promote a trace to a precedent

```
Agent (CLI/HTTP)
  ↓
  trellis curate promote TRACE_ID --title "..." --description "..."
       OR
  POST /api/v1/precedents {trace_id, title, description}
  ↓
  Command created: Operation.PRECEDENT_PROMOTE
  ↓
  MutationExecutor.execute(cmd) runs 5-stage pipeline:
     1. Validate (Trace schema, args shape)
     2. Policy check (injected PolicyGate — approve/deny)
     3. Idempotency (check if same promotion already exists)
     4. Execute (handler creates Precedent from Trace; emits PRECEDENT_PROMOTED event)
     5. Emit (EventLog.emit for audit trail)
  ↓
  CommandResult: {status: SUCCESS, created_id: precedent_id, operation: "PRECEDENT_PROMOTE"}
```

**Files:** `src/trellis_cli/curate.py:promote()`, `src/trellis_api/routes/curate.py:promote()`, `src/trellis/mutate/executor.py`, `src/trellis/mutate/handlers.py:create_curate_handlers()`, `src/trellis/mutate/commands.py`

---

## 4. Client-layer gaps and thin spots

Identified during the landscape comparison; carried forward to the recommendations in `memory-systems-landscape.md` §10.

1. **Async/sync split is inconsistent.** SDK sync, CLI sync, API async, workers mixed. No `AsyncTrellisClient`.
2. **Sectioned pack assembly underexposed.** `assemble_sectioned_pack()` exists in SDK and API but has no MCP macro tool, so Claude Code agents can't use the tiered retrieval story.
3. **No bulk mutation API.** Batches go through 1000 individual executor calls. No `POST /api/v1/commands/batch`.
4. **Policy gate not surfaced to operators.** `PolicyGate` Protocol exists; no `trellis policy` CLI to manage it. Handlers are hardcoded defaults.
5. **Feedback loop is manual.** `record_feedback()` emits an event; `apply_noise_tags()` must be run by hand via `trellis analyze context-effectiveness`.
6. **Worker plugin mechanism missing.** `DbtManifestWorker` and `OpenLineageWorker` are hardcoded. No way to register custom workers.
7. **Vector store optional.** `/api/v1/vectors` endpoint exists but `SemanticSearch` is not in the default `PackBuilder` strategy list.

---

## 5. Core layer inventory

### 5.1 Schemas (`src/trellis/schemas/`)

| Model | File | Key fields | Notes |
|---|---|---|---|
| `Trace` | `trace.py` | `trace_id`, `source`, `intent`, `steps`, `evidence_used`, `artifacts_produced`, `outcome`, `feedback`, `context`, `metadata` | Immutable once appended |
| `TraceStep` | `trace.py` | `step_type`, `name`, `args`, `result`, `error`, `duration_ms`, `started_at` | |
| `Outcome` | `trace.py` | `status: OutcomeStatus`, `metrics`, `summary` | |
| `Feedback` | `trace.py` | `feedback_id`, `rating`, `label`, `comment`, `given_by`, `given_at` | |
| `TraceContext` | `trace.py` | `agent_id`, `team`, `domain`, `workflow_id`, `parent_trace_id` | |
| `Entity` | `entity.py` | `entity_id`, `entity_type`, `name`, `properties`, `source`, `metadata`, `node_role`, `generation_spec` | `node_role` immutable across versions; `generation_spec` required iff `node_role == CURATED` |
| `EntitySource` | `entity.py` | `origin`, `detail`, `trace_id` | |
| `EntityAlias` | `entity.py` | `alias_id`, `entity_id`, `source_system`, `raw_id`, `raw_name`, `match_confidence`, `is_primary` | Cross-system identity mapping |
| `GenerationSpec` | `entity.py` | `generator_name`, `generator_version`, `generated_at`, `source_node_ids`, `source_trace_ids`, `parameters` | Provenance for curated nodes |
| `Edge` | `graph.py` | `edge_id`, `source_id`, `target_id`, `edge_kind`, `properties` | Not versioned — always current |
| `Evidence` | `evidence.py` | `evidence_id`, `evidence_type`, `content`, `uri`, `content_hash`, `source_origin`, `attached_to`, `metadata` | `content_hash` auto-computed |
| `Precedent` | `precedent.py` | `precedent_id`, `source_trace_ids`, `title`, `description`, `pattern`, `applicability`, `confidence`, `promoted_by`, `evidence_refs`, `feedback` | Reusable institutional patterns |
| `ContentTags` | `classification.py` | `domain`, `content_type`, `scope`, `signal_quality`, `custom`, `classified_by`, `classification_version` | Four orthogonal facets |
| `Pack` | `pack.py` | `pack_id`, `intent`, `items`, `retrieval_report`, `policies_applied`, `budget`, `domain`, `agent_id`, `target_entity_ids`, `assembled_at` | |
| `PackItem` | `pack.py` | `item_id`, `item_type`, `excerpt`, `relevance_score`, `included`, `rank`, `selection_reason`, `score_breakdown`, `estimated_tokens`, `metadata` | |
| `PackBudget` | `pack.py` | `max_items`, `max_tokens` | Two-stage enforcement |
| `RetrievalReport` | `pack.py` | `queries_run`, `candidates_found`, `items_selected`, `duration_ms`, `strategies_used` | |
| `SectionedPack` | `pack.py` | `sections: list[PackSection]` | Per-section budgets |
| `Policy` | `policy.py` | `policy_id`, `policy_type`, `scope`, `rules`, `enforcement` | |
| `Command` | `commands.py` | `command_id`, `operation`, `target_id`, `args`, `requested_by`, `idempotency_key`, `metadata` | |
| `CommandResult` | `commands.py` | `command_id`, `status`, `operation`, `created_id`, `message`, `warnings` | |

**Enums** (`schemas/enums.py`):

| Enum | Type | Values | Closed? |
|---|---|---|---|
| `NodeRole` | StrEnum | `structural`, `semantic`, `curated` | **Closed + immutable across versions** |
| `EntityType` | StrEnum | `person`, `service`, `team`, `document`, `concept`, `domain`, `file`, `project`, `tool`, `system` | Open at storage boundary, closed defaults at schema |
| `EdgeKind` | StrEnum | `trace_used_evidence`, `entity_depends_on`, etc. (18 values) | Open at storage boundary |
| `EvidenceType` | StrEnum | `document`, `snippet`, `link`, `config`, `image`, `file_pointer` | |
| `TraceSource` | StrEnum | `agent`, `human`, `workflow`, `system` | |
| `OutcomeStatus` | StrEnum | `success`, `failure`, `partial`, `unknown` | |
| `PolicyType` | StrEnum | `mutation`, `access`, `retention`, `redaction` | |
| `Enforcement` | StrEnum | `enforce`, `warn`, `audit_only` | |
| `Operation` | StrEnum | 14 mutation verbs (`trace.ingest`, `entity.create`, `precedent.promote`, `label.add`, etc.) | |
| `CommandStatus` | StrEnum | `success`, `rejected`, `failed`, `duplicate` | |
| `BatchStrategy` | StrEnum | `sequential`, `stop_on_error`, `continue_on_error` | |

**Validators and invariants:**
- `Entity`: `node_role == CURATED ⟺ generation_spec is not None`
- `Entity`: `node_role` immutable across versions (enforced at store layer, not just Pydantic)
- `Evidence`: `content_hash` auto-computed if content set
- All models extend `TrellisModel(extra="forbid")`, `VersionedModel`, `TimestampedModel`

### 5.2 Stores (`src/trellis/stores/`)

**Six abstract base classes** in `stores/base/`:

| Store | File | Key methods |
|---|---|---|
| `TraceStore` | `trace.py` | `append(trace)` (raises on duplicate), `query(source, domain, agent_id, since, until, limit)` |
| `DocumentStore` | `document.py` | `put(doc_id, content, metadata)`, `search(query, limit, filters)`, `get_by_hash(content_hash)` |
| `GraphStore` | `graph.py` | `upsert_node(node_id, node_type, properties, node_role, generation_spec)`, `get_node(node_id, as_of)`, `get_node_history(node_id)`, `get_subgraph(seed_ids, depth, edge_types, as_of)`, `query(node_type, properties, limit, as_of)`, `upsert_edge`, `get_edges`, `delete_node`, `delete_edge`, `upsert_alias`, `resolve_alias` |
| `VectorStore` | `vector.py` | `upsert(item_id, vector, metadata)`, `query(vector, top_k, filters)` |
| `EventLog` | `event_log.py` | `append(event)`, `get_events(event_type, entity_id, source, since, until)`, `has_idempotency_key(key)` |
| `BlobStore` | `blob.py` | `put(key, data, metadata)`, `get_uri(key)` |

**StoreRegistry** (`registry.py`):
- Reads `~/.config/trellis/config.yaml` or env vars (`TRELLIS_DATA_DIR`, `TRELLIS_PG_DSN`, `TRELLIS_S3_BUCKET`, `TRELLIS_EMBEDDING_FN`)
- Late-binding via `importlib.import_module()` — config determines which backend class to instantiate
- Built-in backends:

| Store | Default | Cloud |
|---|---|---|
| Trace/Document/Graph/EventLog | `sqlite` | `postgres` |
| Vector | `sqlite` | `pgvector`, `lancedb` |
| Blob | `local` | `s3` |

- Embedding function: loaded via `_import_callable(dotted_path)` or `_build_openai_embedding_fn()` from config
- Lazy caching: stores instantiated once, reused across property accesses

### 5.3 SCD Type 2 graph implementation

**SQLite** (`stores/sqlite/graph.py`):
```sql
nodes (
  version_id PK,
  node_id,
  node_type,
  node_role DEFAULT 'semantic',
  generation_spec_json,
  properties_json,
  created_at,
  updated_at,
  valid_from,
  valid_to
)
-- UNIQUE partial index on (node_id) WHERE valid_to IS NULL
-- idx_nodes_role on node_role
```

**Upsert path:**
1. SELECT current version WHERE `node_id = ? AND valid_to IS NULL`
2. Validate `node_role` matches (raises `"Cannot change node_role"` if not)
3. UPDATE current version SET `valid_to = now()`
4. INSERT new version (preserves `created_at`, new `valid_from = now()`)

**Time-travel predicate:**
```sql
WHERE node_id = ?
  AND valid_from <= ?
  AND (valid_to IS NULL OR valid_to > ?)
```

**Migration history:**
- v1 → v2: adds temporal columns
- v2 → v3: adds `node_role` + `generation_spec_json` columns (the work that just landed)

**Postgres** (`stores/postgres/graph.py`): identical schema with `TIMESTAMPTZ` instead of TEXT for timestamps and `JSONB` for JSON columns. Same upsert logic, same unique partial index. Same `as_of` predicate.

**Role immutability validator** (`stores/base/graph.py`):
```python
VALID_NODE_ROLES = frozenset({"structural", "semantic", "curated"})

def validate_node_role_args(node_role, generation_spec):
    if node_role not in VALID_NODE_ROLES:
        raise ValueError(f"Invalid node_role {node_role!r}")
    if node_role == "curated" and generation_spec is None:
        raise ValueError("generation_spec is required when node_role is 'curated'")
    if node_role != "curated" and generation_spec is not None:
        raise ValueError("generation_spec must be None unless node_role is 'curated'")
```

### 5.4 Mutation pipeline (`src/trellis/mutate/`)

**`MutationExecutor`** (`executor.py`) runs 5 stages:

1. **Validate** — `OperationRegistry.validate(command)` checks args against registered schema
2. **Policy check** — `PolicyGate.check(command) → (allowed, message, warnings)`
3. **Idempotency check** — in-memory set + `event_log.has_idempotency_key()` for cross-restart dedup
4. **Execute** — `CommandHandler.handle(command) → (created_id, message)`
5. **Emit** — `event_log.append(EventType.MUTATION_EXECUTED, ...)`

**Protocols** (injected, not hardcoded):
```python
class PolicyGate(Protocol):
    def check(self, command: Command) -> tuple[bool, str, list[str]]: ...

class CommandHandler(Protocol):
    def handle(self, command: Command) -> tuple[str | None, str]: ...
```

**`DefaultPolicyGate`** (`policy_gate.py`):
- Scope priority: global < domain < team < entity_type
- Rule matching: exact operation match OR wildcard suffix (`entity.*` matches `entity.create`)
- Actions: `allow`, `deny` (blocks), `require_approval` (blocks), `warn` (allows + flags)
- Enforcement levels: `ENFORCE` (blocks), `WARN` (allows + warns), `AUDIT_ONLY` (silent log)

**Handlers** (`handlers.py`):
- `PrecedentPromoteHandler` — emits `PRECEDENT_PROMOTED` event
- `LabelAddHandler` / `LabelRemoveHandler` — read node, modify properties, re-upsert with preserved `node_role` + `generation_spec` (required because role is immutable)
- `EntityCreateHandler` — accepts and persists `node_role` + `generation_spec`

**Idempotency pattern:**
```python
if idempotency_key in _seen_idempotency_keys:
    return DUPLICATE
if event_log.has_idempotency_key(idempotency_key):
    _seen_idempotency_keys.add(idempotency_key)
    return DUPLICATE
_seen_idempotency_keys.add(idempotency_key)
```

**Batch strategies**: `SEQUENTIAL`, `STOP_ON_ERROR`, `CONTINUE_ON_ERROR`

### 5.5 Retrieval (`src/trellis/retrieve/`)

**`PackBuilder`** (`pack_builder.py`) orchestration:

1. Run all strategies with merged filters (`include_structural` flag propagated)
2. Collect `PackItem`s from each strategy
3. Deduplicate by `item_id` (keep highest `relevance_score`)
4. Filter structural nodes unless `include_structural=True` (defense-in-depth via `metadata.node_role` check)
5. Sort by `relevance_score` DESC
6. Slice to `budget.max_items`
7. Slice to `budget.max_tokens` (estimate via `estimate_tokens(text) = len(text) // 4 + 1`)
8. Build `RetrievalReport`
9. Emit `PACK_ASSEMBLED` event

**`SearchStrategy` Protocol** (`strategies.py`):
```python
class SearchStrategy(ABC):
    @property
    def name(self) -> str: ...  # "keyword", "semantic", "graph"
    def search(query, *, limit, filters) -> list[PackItem]: ...
```

**Built-in strategies:**

| Strategy | Backend | Scoring |
|---|---|---|
| `KeywordSearch` | `DocumentStore.search()` FTS | `doc.rank` × importance boost |
| `SemanticSearch` | `VectorStore.query()` via embedding_fn | `score` × importance boost |
| `GraphSearch` | `GraphStore.query()` | Base (1.0 − position×0.05) × domain-match boost × **curated boost (1.3x)** × has-description boost × importance boost. Over-fetches `limit × 4` to leave room for structural filter. Filters `node_role == "structural"` client-side unless `include_structural=True`. |

**Effectiveness analysis** (`effectiveness.py`):
- Correlates `PACK_ASSEMBLED` events with `FEEDBACK_RECORDED` events
- Computes per-item success rate across task outcomes
- Flags noise candidates (`success_rate < 30%`, min 2 appearances)
- Returns `EffectivenessReport(total_packs, total_feedback, success_rate, item_scores, noise_candidates)`

**Token tracking** (`token_tracker.py`): `track_token_usage(event_log, layer, operation, response_tokens, budget_tokens, trimmed, agent_id)` → emits `TOKEN_TRACKED` event

### 5.6 Classification (`src/trellis/classify/`)

**`ClassifierPipeline`** (`pipeline.py`) two-mode design:
- **Ingestion mode** (no LLM): deterministic only, microseconds, runs inline
- **Enrichment mode** (with LLM): deterministic + LLM fallback, async, fires only when confidence < `llm_threshold` (default 0.7) OR `needs_llm_review=True`

Pipeline flow:
1. Run all deterministic classifiers in order
2. Merge results per facet (higher confidence wins)
3. If LLM configured and confidence below threshold, invoke LLM
4. Merge LLM result (LLM fills missing facets, overrides low-confidence)

**Four deterministic classifiers:**

| Classifier | File | Mechanism | Confidence |
|---|---|---|---|
| `StructuralClassifier` | `classifiers/structural.py` | Regex over content (code fences, defs, numbered steps, error+fix keywords, config files) | 0.95 if tags found, 0.3 otherwise (`needs_llm_review=True`) |
| `KeywordDomainClassifier` | `classifiers/keyword.py` | Dict lookup with ~100 keywords across 9 built-in domains (data-pipeline, infrastructure, api, frontend, backend, ml-ops, security, testing, observability) | `min(0.95, 0.6 + 0.05 × max_hits)` |
| `SourceSystemClassifier` | `classifiers/source_system.py` | Origin rules (dbt→data-pipeline, obsidian→documentation; file path rules `/tests/`→testing, `/docs/`→documentation) | 0.9 if signal, 0.3 otherwise |
| `GraphNeighborClassifier` | `classifiers/graph_neighbor.py` | Majority vote on propagatable facets from 1-hop neighbors with confidence ≥ 0.8 | `confidence_decay × consensus` (default decay 0.85) |

**`LLMFacetClassifier`** (`classifiers/llm.py`) wraps `EnrichmentService`:
- Maps enrichment output to faceted format
- Async/sync wrapper

**`ContentTags` structure:**
- `domain`: multi-label, extensible
- `content_type`: single-label (pattern, decision, error-resolution, procedure, constraint, configuration, code, documentation)
- `scope`: single-label (universal, org, project, ephemeral)
- `signal_quality`: computed single-label (high, standard, low, noise)
- `retrieval_affinity`: multi-label (domain_knowledge, technical_pattern, operational, reference)

Stored in metadata JSON; filtered via `json_extract`/`json_each` in SQLite.

**Importance and feedback loop:**
```python
# importance.py
compute_importance(tags, base_importance) =
    clamp(base + quality_boost + scope_boost, 0, 1)
# quality_boost: high +0.3, standard 0, low -0.2, noise -0.5
# scope_boost: universal +0.15, org +0.05, project 0, ephemeral -0.2

# feedback.py
apply_noise_tags(noise_candidates, document_store)
# Sets signal_quality="noise" on items flagged by EffectivenessReport
```

### 5.7 Temporal story

`as_of: datetime | None` supported consistently across `GraphStore` reads:
- `get_node(node_id, as_of)`
- `get_nodes_bulk(node_ids, as_of)`
- `get_edges(node_id, direction, edge_type, as_of)`
- `get_subgraph(seed_ids, depth, edge_types, as_of)`
- `query(node_type, properties, limit, as_of)`
- `get_node_history(node_id)` — full version chain, newest first, no `as_of` parameter

**Edges are not versioned** — only current edges exist. Historical navigation reconstructs the graph state at a point in time by applying `as_of` to node and edge queries independently. This is the current-known weakness relative to Graphiti, which historizes edges but not nodes.

Entity merging: tracked via `ENTITY_MERGED` events; merged-away entity's current version is closed, `merged_into` added to properties. History remains searchable.

### 5.8 Type extensibility contract

From CLAUDE.md:
> Entity types and edge types are **any string** at the storage and API layers. The `EntityType`/`EdgeKind` enums in `schemas/enums.py` are well-known defaults for agent-centric use, not a closed set.

**Enforcement points:**

At schema layer (`schemas/entity.py` and `schemas/graph.py`), `entity_type: str` and `edge_kind: str` are open strings — custom values pass validation verbatim. *(Relaxed 2026-04-15; previously typed as `EntityType`/`EdgeKind` StrEnums which silently rejected unknown strings, contradicting the CLAUDE.md claim.)* The enums still exist as named constants for the well-known agent-centric values.

At store layer (`stores/base/graph.py`), `upsert_node` takes `node_type: str` — unconstrained. SQLite and Postgres store `node_type`/`edge_type` as unconstrained TEXT/VARCHAR. No database-level checks.

**Practical extensibility for custom types:**
1. Define custom types in the application's own enum (not in core)
2. Pass strings directly to any layer — `Entity(entity_type="uc_table", ...)`, `Command(args={"entity_type": "uc_table", ...})`, or `GraphStore.upsert_node(node_type="uc_table", ...)` all work
3. No need to bypass the Pydantic `Entity` / `Edge` schemas for custom types

This is an area where the contract is aspirational but not fully enforced; worth a follow-up to either tighten or loosen consistently.

---

## 6. Foundational primitives

At the bottom of the architecture, Trellis is built on seven ideas:

1. **Immutable traces.** Once ingested via `TraceStore.append()`, traces cannot be modified or deleted. Audit trail is permanent. Feedback and precedent promotion happen via new entities/edges, never trace mutation.

2. **SCD Type 2 graph versioning with role immutability.** Node updates close the old version (`valid_to` set) and insert a new version row. Role immutability is enforced: a node's role cannot change across versions. This preserves history and enables time-travel queries via `as_of` predicates.

3. **Governed 5-stage mutation pipeline.** All writes flow through `validate → policy_check → idempotency_check → execute → emit_event`. Handlers and policy gates are Protocols, not hardcoded. Batch execution is stratified (sequential / stop-on-error / continue-on-error).

4. **Pluggable store abstraction via late-binding ImportLib.** Six ABCs define contracts; `StoreRegistry` dynamically loads backends from config via `importlib`. No dependency on any specific SQL dialect or vector DB. Applications choose deployment topology (SQLite local, Postgres cloud, pgvector vs. lancedb) without code changes.

5. **Deterministic-first classification with LLM fallback.** `ContentTags` facets are populated by four lightweight deterministic classifiers first. LLM classifier runs only if confidence is below threshold or a classifier flags `needs_llm_review`. Keeps costs low and latency predictable.

6. **Budget-enforced retrieval with multi-strategy search.** `PackBuilder` orchestrates pluggable search strategies, deduplicates by `item_id`, enforces two-stage budgets (`max_items` then `max_tokens`). Structural nodes excluded by default. Effectiveness analysis measures which items correlate with task success and feeds back to tag low-value items as noise.

7. **Three-role node taxonomy with generation provenance.** Entities are categorized into STRUCTURAL (fine-grained plumbing, regenerable from source), SEMANTIC (ground-truth named entities), and CURATED (synthesized derivations with `GenerationSpec` tracking how they were produced). Immutable across versions. Filters retrieval: structural nodes are surfaced only as context of their parent.

---

## 7. Cross-references

- [`memory-systems-landscape.md`](./memory-systems-landscape.md) — How Trellis compares to Graphiti, Zep, Mem0, Letta, cognee, Cursor, and Claude Code
- [`../design/adr-deferred-cognition.md`](../design/adr-deferred-cognition.md) — The architectural principle that falls out of the comparison ("LLM enrichment is not in the write path")
- [`../agent-guide/schemas.md`](../agent-guide/schemas.md) — User-facing schema catalog
- [`../agent-guide/operations.md`](../agent-guide/operations.md) — User-facing CLI/API/MCP reference
- [`../design/architecture-design.md`](../design/architecture-design.md) — Original architecture design document
