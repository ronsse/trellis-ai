# Implementation Plan: Pluggable Storage & Cloud-Native Architecture

Reference: [architecture-design.md](./architecture-design.md)

---

## Current State

- Five store ABCs already exist (`TraceStore`, `DocumentStore`, `GraphStore`, `VectorStore`, `EventLog`) with SQLite implementations
- No formal dependency injection; stores are instantiated directly in CLI commands and MCP server
- No BlobStore abstraction; Obsidian integration is a separate indexer
- No REST API; only CLI + MCP server
- No temporal versioning on graph nodes/edges
- Pack builder exists with keyword and semantic search strategies
- Mutation executor pipeline is in place with policy gate and idempotency

## Phase 1: Formalize Store ABCs & Introduce DI Container

**Goal:** Make store implementations swappable via configuration without changing consumer code.

### Tasks

1. **Extract formal ABCs from existing stores**
   - Current store classes mix ABC definition and SQLite implementation in the same file
   - Create `src/trellis/stores/base/` package with pure ABCs:
     - `base_trace.py` -> `TraceStoreBase(ABC)`
     - `base_document.py` -> `DocumentStoreBase(ABC)`
     - `base_graph.py` -> `GraphStoreBase(ABC)`
     - `base_vector.py` -> `VectorStoreBase(ABC)`
     - `base_event.py` -> `EventLogBase(ABC)`
   - Move SQLite implementations to `src/trellis/stores/sqlite/`
   - Keep backward-compatible imports in `stores/__init__.py`

2. **Create a StoreRegistry / DI container**
   - Add `src/trellis/stores/registry.py`
   - `StoreRegistry` class that:
     - Reads store backend config from `trellis.yaml` (e.g., `stores.graph.backend: "sqlite"`)
     - Maps backend names to implementation classes via entry points or a simple dict
     - Lazily instantiates and caches store instances
     - Provides `registry.graph_store`, `registry.trace_store`, etc.
   - Example config:
     ```yaml
     stores:
       graph:
         backend: sqlite
         path: ~/.config/trellis/data/stores/graph.db
       vector:
         backend: sqlite
         path: ~/.config/trellis/data/stores/vectors.db
     ```

3. **Refactor CLI and MCP server to use StoreRegistry**
   - Replace direct `SQLite*Store()` instantiation with `StoreRegistry.from_config()`
   - CLI: inject via config loaded in `trellis_cli/config.py`
   - MCP server: replace `_get_*_store()` context managers with registry

4. **Add BlobStore abstraction**
   - Create `src/trellis/stores/base/base_blob.py` -> `BlobStoreBase(ABC)`
     - Methods: `put(key, data, metadata)`, `get(key)`, `delete(key)`, `list(prefix)`, `exists(key)`
     - Returns URIs (`file:///...`, `s3://...`)
   - Implement `LocalBlobStore` in `stores/local/blob.py`
     - Filesystem-backed, understands folder hierarchies
     - Handles Obsidian vault paths natively
   - Register in StoreRegistry

### Acceptance Criteria
- All existing tests pass with refactored store instantiation
- A new store backend can be added by implementing the ABC and adding a config entry
- `trellis admin init` creates config with default (sqlite) backends
- BlobStore can store and retrieve files with URI references

---

## Phase 2: REST API via FastAPI

**Goal:** Expose XPG operations over HTTP for distributed deployments and SDK consumption.

### Tasks

1. **Create `src/trellis_api/` package**
   - `app.py` - FastAPI app factory with lifespan (initialize StoreRegistry)
   - `deps.py` - FastAPI dependency injection (yields stores from registry)
   - Add `trellis_api` to pyproject.toml packages
   - Add `fastapi`, `uvicorn` dependencies

2. **Define API routes (mirroring CLI structure)**
   - `routes/ingest.py`:
     - `POST /api/v1/traces` - ingest trace
     - `POST /api/v1/evidence` - ingest evidence
   - `routes/retrieve.py`:
     - `GET /api/v1/search?q=...&domain=...&limit=...` - full-text search
     - `POST /api/v1/packs` - assemble context pack
     - `GET /api/v1/entities/{entity_id}` - get entity with neighborhood
   - `routes/curate.py`:
     - `POST /api/v1/precedents` - promote trace to precedent
     - `POST /api/v1/links` - create graph edge
     - `POST /api/v1/labels` - add/remove labels
   - `routes/admin.py`:
     - `GET /api/v1/health` - health check
     - `GET /api/v1/stats` - store statistics

3. **Shared logic extraction**
   - Move business logic from CLI command handlers into reusable service functions in `src/trellis/services/`
   - Both CLI and API call the same service layer
   - Services take store instances as arguments (no global state)

4. **Add `trellis-api` entry point**
   - Script: `trellis-api = "trellis_api.app:main"` (runs uvicorn)
   - CLI command: `trellis admin serve --port 8420`

5. **OpenAPI spec auto-generation**
   - FastAPI generates this automatically
   - Add `GET /api/v1/openapi.json` (built-in)
   - Export static spec via `trellis admin export-openapi`

### Acceptance Criteria
- All mutation operations available via REST with JSON request/response
- OpenAPI spec is auto-generated and accurate
- `trellis admin serve` starts the API server
- Health endpoint returns store status

---

## Phase 3: Context Observability & Feedback Loop

**Goal:** Track which context packs are useful and surface telemetry for pack quality improvement.

### Tasks

1. **Enhance PackBuilder with telemetry**
   - On `assemble()`, emit `ContextRetrievalEvent` to EventLog containing:
     - `pack_id`, `intent`, `agent_id`, `domain`
     - `injected_item_ids` (list of node/doc IDs included in pack)
     - `strategy_scores` (per-strategy breakdown)
     - `budget_used` (items, tokens)
   - Add `pack_id` field to `Pack` schema

2. **Add feedback ingestion endpoint**
   - `POST /api/v1/feedback` / `trellis ingest feedback`
   - Links a `pack_id` to an outcome score (success/partial/failure + optional freetext)
   - Stored as `FEEDBACK_RECORD` operation via mutation executor

3. **Context effectiveness query**
   - `trellis analyze context-effectiveness --days 30`
   - Joins `ContextRetrievalEvent` -> `FeedbackRecord` -> injected item IDs
   - Returns per-entity/per-precedent success rate
   - Highlights items that correlate with failure (noise candidates)

### Acceptance Criteria
- Every pack assembly emits a telemetry event
- Feedback can be recorded against a pack_id
- Effectiveness report shows which injected items correlate with success/failure

---

## Phase 4: Temporal Graph Features (SCD Type 2)

**Goal:** Enable time-travel queries on graph nodes and edges.

### Tasks

1. **Extend graph schemas**
   - Add to `Entity` schema: `valid_from: datetime`, `valid_to: datetime | None`
   - Add to graph edge schema: `valid_from: datetime`, `valid_to: datetime | None`
   - `valid_to = None` means "currently active"

2. **Modify GraphStore ABC**
   - `upsert_node()` becomes version-aware:
     - If node exists with same `entity_id`, cap `valid_to` on old version, insert new version
     - Assign new internal version ID
   - Add `get_node(entity_id, as_of: datetime | None = None)`
     - `as_of=None` -> filter `valid_to IS NULL`
     - `as_of=<timestamp>` -> filter `valid_from <= ts < valid_to`
   - Add `get_subgraph(root_id, depth, as_of: datetime | None = None)`

3. **Update SQLiteGraphStore**
   - Add `valid_from`, `valid_to` columns to nodes and edges tables
   - Migration script for existing data (set `valid_from = created_at`, `valid_to = NULL`)
   - Update queries to filter by temporal range

4. **Update retrieval strategies**
   - `GraphSearch` strategy accepts optional `as_of` parameter
   - PackBuilder passes `as_of` from pack request to graph strategy

5. **CLI support**
   - `trellis retrieve entity <id> --as-of 2026-01-15`
   - `trellis retrieve pack --intent "..." --as-of 2026-01-15`

### Acceptance Criteria
- Updating an entity creates a new version, old version preserved
- Queries without `as_of` return current state (backward compatible)
- Queries with `as_of` return state as it existed at that timestamp
- Existing data migrated with `valid_to = NULL`

---

## Phase 5: Cloud Store Backends (Postgres + S3)

**Goal:** Provide production-grade store backends for distributed deployment.

### Tasks

1. **PostgresGraphStore**
   - Implements `GraphStoreBase` using asyncpg
   - Same recursive CTE traversal logic, adapted for Postgres syntax
   - Temporal columns supported natively
   - Connection pooling via asyncpg pool

2. **PgVectorStore**
   - Implements `VectorStoreBase` using pgvector extension
   - `CREATE INDEX ... USING ivfflat` for approximate nearest neighbor
   - Same API surface as SQLiteVectorStore

3. **S3BlobStore**
   - Implements `BlobStoreBase` using boto3
   - `put()` -> `s3.put_object()`, returns `s3://bucket/key` URI
   - `get()` -> `s3.get_object()`, streams content
   - Configurable bucket, prefix, region

4. **PostgresEventLog**
   - Implements `EventLogBase` using asyncpg
   - Partitioned by month for retention management

5. **Configuration**
   - `trellis.yaml` supports cloud backends:
     ```yaml
     stores:
       graph:
         backend: postgres
         dsn: postgresql://user:pass@host/db
       vector:
         backend: pgvector
         dsn: postgresql://user:pass@host/db
       blob:
         backend: s3
         bucket: trellis-artifacts
         region: us-east-1
     ```

6. **Add optional dependency group**
   - `[cloud]` extras: `asyncpg`, `boto3`, `pgvector`

### Acceptance Criteria
- All store ABC tests pass with Postgres backends (using testcontainers or similar)
- S3BlobStore round-trips files correctly
- Mixed configurations work (e.g., sqlite graph + s3 blob)

---

## Phase 6: Automated Knowledge Ingestion Workers

**Goal:** Auto-populate the graph from external data tools (dbt, Spark, etc.).

### Tasks

1. **Ingestion worker framework**
   - `src/trellis_workers/ingestion/base.py` -> `IngestionWorker(ABC)`
     - Methods: `discover()`, `extract()`, `load()`
     - Standardized entity/edge output format

2. **dbt manifest ingester**
   - `src/trellis_workers/ingestion/dbt.py`
   - Parses `target/manifest.json`
   - Creates entities for models, seeds, snapshots, sources
   - Creates edges for `depends_on` relationships
   - Indexes descriptions in DocumentStore

3. **OpenLineage event ingester**
   - `src/trellis_workers/ingestion/openlineage.py`
   - Consumes OpenLineage JSON events
   - Creates entities for datasets and jobs
   - Creates edges for `reads_from` / `writes_to`

4. **CLI commands**
   - `trellis worker ingest-dbt <manifest-path>`
   - `trellis worker ingest-lineage <events-path>`
   - `trellis worker ingest-dbt --watch` (watches for manifest changes)

### Acceptance Criteria
- dbt manifest creates correct entity graph
- OpenLineage events create dataset lineage graph
- Re-running is idempotent (uses mutation executor)
- `trellis retrieve search "model_name"` finds ingested models

---

## Dependency Order

```
Phase 1 (Store ABCs + DI)
  |
  +---> Phase 2 (REST API)
  |       |
  |       +---> Phase 3 (Context Observability)
  |
  +---> Phase 4 (Temporal Graph)
  |
  +---> Phase 5 (Cloud Backends)  -- requires Phase 1
  |
  +---> Phase 6 (Ingestion Workers) -- requires Phase 1
```

Phase 1 is the foundation. Phases 2-6 can largely proceed in parallel after Phase 1 is complete, with Phase 3 depending on Phase 2.
