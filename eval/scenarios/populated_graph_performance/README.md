# Scenario 5.3 — populated-graph performance baseline

> Plan reference:
> [`docs/design/plan-evaluation-strategy.md`](../../../docs/design/plan-evaluation-strategy.md) §5.3.

## What it does

Generates a populated synthetic graph via the shared
[`graph_generator.py`](../../generators/graph_generator.py) (default 1K
nodes / 4K edges / 200 embeddings; the kwargs let scheduled runs dial
up to plan §5.3's 10-50K target), ingests it into every reachable
backend, and runs a fixed query mix while timing each call:

| Query type | Default count | What it measures |
|---|---|---|
| Entity lookup (`get_node`) | 20 | Index-lookup hot path |
| Type+filter query (`query`) | 10 | Property-filter cost on populated table |
| Subgraph traversal (`get_subgraph`, depth 2) | 10 | Edge-traversal cost |
| Vector top-k | 10 | Similarity-index cost |

Per-query-type metrics: ingest seconds, p50/p95/p99 latency in ms.
Per-backend metric: vector recall@10 against an in-Python brute-force
cosine baseline.

## Backends

Same probe-and-skip pattern as scenario 5.1: SQLite always runs;
Postgres + pgvector if `TRELLIS_KNOWLEDGE_PG_DSN` is set; Neo4j if
`TRELLIS_NEO4J_URI` + user + password are set.

## Decision this scenario unblocks

Plan §5.3's deferred items become actionable once this scenario has
real numbers from a live run:

* **HNSW `M` / `efConstruction` tuning** — fires if vector recall@10 is
  below the threshold (default 0.95) on any backend.
* **`upsert_node` UNWIND bulk path** — fires if ingest throughput drops
  below 100 nodes/sec on any backend.
* **EXPLAIN-validated query plan baseline** — recorded latencies form
  the first such baseline; future runs diff against it.
* **Graph compaction automation** — gives an empirical threshold for
  `as_of` query latency vs closed-row count.

## Scope discipline — what this MVP does not do yet

These are deliberate MVP omissions, deferred to follow-up:

* **No EXPLAIN / PROFILE plan capture.** The plan calls for capturing
  Postgres `EXPLAIN` and Neo4j `PROFILE` output on the slowest
  queries; that's backend-specific code that lives best in the live-data
  revisit pass (see project memory note: "Eval Phase 1-3 revisit after
  live-data run" — same applies to 5.3).
* **No regression gates.** The scenario records numbers; pinning a
  baseline file and gating on regression is plan §7.1 work.
* **Query counts are smaller than plan §5.3** by default (20/10/10/10
  instead of 100/50/50/20). Same ratios, scaled for dev-machine
  iteration. Scheduled runs use the kwargs to scale up.

## Recall baseline

For each query embedding we compute the brute-force top-10 in Python
(cosine over all known embeddings) and report `recall@10 =
|brute ∩ backend| / 10`. The brute-force baseline does not use any
index — it's the ground truth against which approximate-search
quality is measured.
