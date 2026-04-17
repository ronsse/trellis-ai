# Integration Findings from trellis-platform Backfill

Issues discovered while building the XPG graph backfill in fd-data-architecture-poc.
Each item is a gap in the trellis-ai core that caused friction.

## P0 — Blocks correct usage

### 1. POST /api/v1/entities uses ULID IDs, not caller-supplied entity_id
**Impact:** Entities created via the API get ULID-based node_ids. Edges that reference
the caller's ID scheme (e.g., `git://repo/file`, `uc://catalog.schema.table`) fail
with "Source node not found" because the API's `POST /api/v1/links` looks up by
internal ULID, not by external entity_id.

**Workaround:** Use `PostgresGraphStore.upsert_node()` directly (requires PG access).
This accepts arbitrary node_id strings.

**Fix:** Either:
a. Allow `POST /api/v1/entities` to accept a caller-supplied `entity_id` that becomes the `node_id`
b. Implement `POST /api/v1/ingest/bulk` (already in TODO) that accepts entities + edges + aliases
   in one request with caller-supplied IDs
c. Have `POST /api/v1/links` resolve source_id/target_id by searching entity properties,
   not just node_id

### 2. No bulk ingest API endpoint
**Impact:** Ingesting thousands of entities requires one HTTP request per entity.
At 7,766 entities this takes ~15 minutes of serial POSTs. The bastion's direct PG
path does it in seconds via batch upserts.

**Fix:** `POST /api/v1/ingest/bulk` accepting `{entities: [...], edges: [...], aliases: [...]}`
with batch transaction semantics.

## P1 — Causes friction

### 3. Document store FTS only indexed content, not metadata
**Impact:** 561 query pattern documents had doc_ids like `query_pattern://foundation.sportsbook.bet_events`
but searching "bet_events" returned 0 results because the tsvector only indexed the content body.

**Fix:** (DONE in v0.2.4) Weighted tsvector: `doc_id` (A) + `metadata.title` (A) + `metadata.domain` (B) + `content` (C).
Auto-migration in `_init_schema()`.

### 4. GraphStore.upsert_edge() signature differs from trellis-platform's Edge model
**Impact:** The `writer.py` in trellis-platform calls `upsert_edge(edge_id=..., source_id=..., target_id=...)` but
`PostgresGraphStore.upsert_edge()` doesn't accept `edge_id` — it auto-generates one. Causes TypeError.

**Workaround:** Removed `edge_id` kwarg from caller code.

**Fix:** Either accept optional `edge_id` in `upsert_edge()` or document that edge IDs are
auto-generated and callers should not supply them.

### 5. MCP server not documented as a sidecar deployment pattern
**Impact:** Workers need runtime graph search but there's no guidance on running
`trellis.mcp.server` alongside `trellis_api` in production.

**Fix:** (Added to TODO) Document the sidecar pattern + `.mcp.json` config.

### 10. Pack builder GraphSearch doesn't filter by domain — returns noise
**Impact:** `POST /api/v1/packs` with `domain=sportsbook` returns trellis-ai
test files (`git://trellis-ai/tests/unit/...`) with score=1.0, drowning out
relevant query pattern documents (score=0.67). The graph strategy returns the most
recently created nodes without domain filtering.

**Document search works correctly** — `GET /api/v1/search?q=sportsbook+bet` returns
10 highly relevant query pattern documents. The issue is ONLY in the pack builder's
graph strategy.

**Fix:** The `GraphSearch` strategy in `retrieve/strategies.py` should:
a. Filter by domain when `domain` is specified in the pack request
b. Filter by node_type (exclude `git_file` nodes from pack results, or weight them lower)
c. Use relevance scoring based on property matching, not just recency
d. Respect the `domain` field in node properties

### 11. UC/dbt entities have no document store entries — invisible to keyword search
**Impact:** 10,845 UC table entities and 2,819 dbt model entities are ONLY in the
graph store. Searching for "bet_events" only finds 2 knowledge docs, not the actual
UC_TABLE entity. The entity exists as a node but has no document store representation.

**Fix:** During ingestion, entities with descriptions (UC `comment` field, dbt `description`
field) should ALSO create a document store entry for searchability. The ingestion rules
config has `uc_descriptions` and `dbt_descriptions` rules for this — they need implementation
in the runners.

## P2 — Nice to have

### 12. GET /entities/{entity_id} fails for URI-style IDs (uc://, git://)
**Impact:** Entity lookup by ID returns 404 for all entities with `://` in their ID,
even when URL-encoded. FastAPI's `{entity_id}` path parameter doesn't capture the
full URI because `://` is interpreted as URL scheme separator.

**Fix:** Change the route to `{entity_id:path}` to capture the full remaining path:
```python
@router.get("/entities/{entity_id:path}")
```
Or add a query parameter alternative: `GET /api/v1/entities?id=uc://...`

**Status:** NOT FIXED in v0.2.5. Search and pack builder work as the primary retrieval paths.

### 6. No batch/checkpoint support in document store or graph store
**Impact:** Large ingestion (100K+ edges for UC lineage) runs as one giant transaction.
If it fails mid-way, nothing is committed. Need checkpoint-every-N support.

### 7. StoreRegistry.from_config_dir() doesn't log which backends it selected
**Impact:** Hard to debug "why isn't pgvector working" — the registry silently falls back
to defaults without logging what it chose.

### 8. S3 Vectors (aws s3vectors) not supported as a vector store backend
**Impact:** New AWS service, would complement pgvector for large-scale deployments.
Requires `boto3.client('s3vectors')` and new `S3VectorsStore` backend.

### 9. EC2 bastion deployment is fragile
**Impact:** Cloud-init userdata failures are silent, pre-signed URLs expire, OAuth tokens
expire during bootstrap. Need a more robust deployment (EKS or at minimum a launch template
with IAM role).

### 10. POST /api/v1/links rejects edges when target node doesn't exist
**Impact:** Structural enrichment edges (e.g., `dbt://model.pkg.a` → `dbt://model.pkg.b`)
fail with errors when the target entity hasn't been ingested yet. This forces strict
ingestion ordering: all entity nodes must exist before any edges referencing them.

**Context:** During backfill, we extract 739 READS_FROM and JOINS_WITH edges from dbt SQL
files but can't push them because the dbt evaluator ingestion hasn't populated the dbt entity
nodes yet. The edges are structurally valid.

**Fix:** Either:
a. Allow dangling edges (create stub nodes for missing targets) so edges can be pushed
   independently of entity population order
b. Add a `create_missing_stubs: true` option to `POST /api/v1/links`
c. Implement `POST /api/v1/ingest/bulk` (P0 #2) which can create nodes and edges atomically

### 11. PG proxy on bastion not functional
**Impact:** Direct PostgreSQL access via the bastion's port 5432 fails with "server closed
the connection unexpectedly". Only the REST API on port 8420 works from outside VPC. This
limits operations that need direct PG (vectorization, bulk ingestion via `PostgresGraphStore`).

**Workaround:** Use REST API endpoints or SSH tunnel to bastion.

**Fix:** Fix the PG proxy (likely haproxy or socat config), or expose vector store operations
via REST API (e.g., `POST /api/v1/vectors`).
