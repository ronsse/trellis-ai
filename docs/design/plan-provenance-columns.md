# Plan: Provenance columns (Phase 3 of graph-ontology)

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable
**ADR:** [`adr-graph-ontology.md`](./adr-graph-ontology.md) §6.4 Phase 3 (already accepted; this plan executes it)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 2
**Depends on:** Item 1 ([`plan-observation-entity-type.md`](./plan-observation-entity-type.md)) — Observations are the consumer signal that makes this Phase no longer speculative.

## 1. Premise

`adr-graph-ontology.md` §6.4 Phase 3 specifies promoting `source_trace_id`, `agent_id`, `confidence`, `evidence_ref`, `extractor_tier` from edge `properties` JSON to first-class columns on the `edges` table — gated on "a policy or retrieval consumer wants to gate on these fields."

That consumer now exists: Observation/Measurement nodes ship with `wasDerivedFrom` edges carrying a `source_trace_id` provenance link, and retrieval surfaces ("show me observations derived from trace X" — Item 6's dogfooding pattern) need this to be column-queryable, not JSON-extract-queryable. The Phase 3 trigger has fired.

## 2. Scope

**In scope:**

- SQLite: ALTER `edges` table to add 5 columns; backwards-compatible writers; read-and-write through to new columns.
- Postgres: ALTER `edges` table to add 5 columns.
- Neo4j: edge properties are already first-class — add canonical property keys + an index on `source_trace_id` for retrieval.
- ArcadeDB: ALTER edge type with new properties; add `LSM_TREE` index on `source_trace_id`.
- Contract test extension: round-trip + filter by `confidence < 0.7` + `source_trace_id = "abc"`.
- DSL extension: `FilterClause` accepts `confidence` / `source_trace_id` / `extractor_tier` as top-level filterable keys (today they'd be `properties.source_trace_id` JSON-extract).

**Out of scope:**

- Node-level provenance (already lives in `generation_spec`; no change).
- Backfilling old rows. Greenfield per POC: existing data without these columns reads as `NULL`. Callers querying `confidence < 0.7` get rows where `confidence IS NOT NULL AND confidence < 0.7` — no auto-default to 0.0 or 1.0.

## 3. POC directives applied

- **No silent JSON fallback.** Reading an edge whose `confidence` column is NULL **does not** fall through to `properties["confidence"]`. The new columns are the source of truth. If a caller wrote to properties, the writer migration log will surface it as a one-time WARN.
- **No on-disk migration of legacy data.** Greenfield: any edges written before this change keep their JSON properties; readers see them as NULL on the column path. A one-shot CLI `trellis admin migrate-provenance` is available for operators who want to materialize old data.
- **Strict types.** `confidence` is `FLOAT NOT NULL CHECK (confidence BETWEEN 0 AND 1)` where present — no silent clamping at write time. Out-of-range values raise.
- **Loud on conflicting writes.** If a writer passes both `confidence=0.7` and `properties={"confidence": 0.6}`, the mutation handler raises. No "last wins" silent precedence.

## 4. Files to touch

| File | Change |
|---|---|
| `src/trellis/stores/sqlite/graph.py` | Add 5 columns to `_CREATE_EDGES_SQL`; modify `upsert_edge` / `upsert_edges_bulk` SQL to write them; modify `get_edges` / `query` to read them; extend the Phase 2 DSL compiler to compile filters against the new columns. |
| `src/trellis/stores/postgres/graph.py` | Same shape; uses native `DECIMAL` / `TEXT` types; ANALYZE recommendation in deployment docs. |
| `src/trellis/stores/neo4j/graph.py` | Edge properties were already keys; add `CREATE INDEX edge_source_trace_id` migration; ensure write path stamps the canonical keys. |
| `src/trellis/stores/arcadedb/graph.py` | Add property declarations + `LSM_TREE` index. |
| `src/trellis/stores/base/graph_query.py` | Add `source_trace_id` / `agent_id` / `confidence` / `evidence_ref` / `extractor_tier` to the FilterClause accepted-keys list. |
| `src/trellis/schemas/graph.py` | Add the 5 fields as Optional on the `Edge` model with explicit ge/le validators. |
| `tests/unit/stores/contracts/graph_store_contract.py` | Add `test_provenance_columns_round_trip`, `test_filter_by_confidence`, `test_filter_by_source_trace_id`, `test_conflicting_write_raises`. |
| `src/trellis_cli/admin.py` | Add `migrate-provenance` subcommand (read JSON properties, populate columns, idempotent). |

## 5. Per-backend specifics

### SQLite

```sql
ALTER TABLE edges ADD COLUMN source_trace_id TEXT;
ALTER TABLE edges ADD COLUMN agent_id TEXT;
ALTER TABLE edges ADD COLUMN confidence REAL;
ALTER TABLE edges ADD COLUMN evidence_ref TEXT;
ALTER TABLE edges ADD COLUMN extractor_tier TEXT;
CREATE INDEX idx_edges_source_trace_id ON edges(source_trace_id);
```

The CHECK constraint on `confidence` is **enforced at write time in Python**, not via SQL CHECK (SQLite CHECK works but errors are opaque — Python raises with the offending value in the message).

### Postgres

```sql
ALTER TABLE edges
  ADD COLUMN source_trace_id TEXT,
  ADD COLUMN agent_id TEXT,
  ADD COLUMN confidence NUMERIC(4,3) CHECK (confidence >= 0 AND confidence <= 1),
  ADD COLUMN evidence_ref TEXT,
  ADD COLUMN extractor_tier TEXT;
CREATE INDEX CONCURRENTLY idx_edges_source_trace_id ON edges(source_trace_id);
```

Use `NUMERIC(4,3)` over `REAL` for deterministic comparison semantics.

### Neo4j

Edges already store arbitrary properties as native key-values. The change is:

1. Stamp the five keys canonically — writers must use `source_trace_id` exactly, not variations.
2. Create an edge-property index: `CREATE INDEX edge_source_trace_id IF NOT EXISTS FOR ()-[r]-() ON (r.source_trace_id)`.

### ArcadeDB

```sql
ALTER TYPE Edge ADD PROPERTY source_trace_id STRING;
-- repeat for the four other keys
CREATE INDEX ON Edge(source_trace_id) LSM_TREE;
```

ArcadeDB enforces the type per-property; CHECK semantics handled at write time in Python.

### DSL compiler

`FilterClause("confidence", "lt", 0.7)` compiles to:

- SQLite / Postgres: `WHERE confidence < ?`
- Neo4j: `WHERE r.confidence < $param`
- ArcadeDB: `WHERE confidence < ?`

The DSL gains one new operator: `"lt"`, `"gte"`, `"lte"`. Spec the operator set explicitly in `graph_query.py`. Backend compilers fail loud on unsupported operators (already do — verify).

## 6. Tests

Five new contract tests (every backend gets them):

1. `test_provenance_round_trip` — write edge with all 5 fields, read back via `get_edges`, assert equality.
2. `test_filter_by_source_trace_id` — write 10 edges, 3 with `source_trace_id="x"`, filter → 3 returned.
3. `test_filter_by_confidence_range` — write edges with confidence ∈ {0.2, 0.5, 0.8}, filter `confidence >= 0.5` → 2 returned.
4. `test_confidence_out_of_range_raises` — write `confidence=1.5` raises ValueError.
5. `test_conflicting_write_raises` — write with both column and `properties["confidence"]` raises ConflictError.

## 7. Done when

- All five contract tests pass on SQLite, Postgres, Neo4j, ArcadeDB.
- mypy clean.
- `trellis admin migrate-provenance --dry-run` against a synthetic graph reports the count of legacy edges with JSON-only provenance.
- DSL filter against `confidence` works through PackBuilder's retrieval path.

## 8. Estimated size

| Component | LOC |
|---|---|
| 4 backend graph stores | ~400 (100 each, mostly schema + write/read plumbing) |
| DSL extension | ~80 |
| Edge model + validators | ~40 |
| Contract tests | ~250 |
| Per-backend backend-specific tests | ~150 |
| Admin migrate CLI | ~150 |
| **Total** | **~1070** |

Single PR scope, but tight — could split into "schema + writes" and "filters + DSL" if reviewers prefer.

## 9. Cleanup considerations

After landing, audit `src/trellis/extract/` and `src/trellis_workers/extract/` for places that stuff provenance into `properties` JSON. Each gets a 2-line refactor to use the new keyword args on the `EdgeDraft` constructor. Adds to [`plan-cleanup-dead-code.md`](./plan-cleanup-dead-code.md) as a sweep item.

## 10. Risks

- **ALTER on a populated Postgres table.** `ADD COLUMN` is fast (no rewrite) on modern PG; the index build is the long pole. Mitigation: `CREATE INDEX CONCURRENTLY` documented in the runbook; expected duration noted in deployment docs.
- **Neo4j edge-property index limits.** Single index per (label, property). Confirm with AuraDB Free; if there's drift the index name needs to be uniqued. The unit-test-vector-index gotcha (per `implementation-roadmap.md` A.1) is precedent — same care needed.
- **Schema versioning.** A future ADR may want to add a 6th provenance column. The current plan does not introduce a schema-version table for edges. If Item 5 (well-known promotion loop) or Item 7 (coding-agent loop) proposes adding `proposal_id`, that's an additive ALTER — same shape as this Phase. The plan does not pre-build a versioning scaffold.
