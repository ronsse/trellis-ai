# Scenario 5.1 — multi-backend equivalence

> Plan reference:
> [`docs/design/plan-evaluation-strategy.md`](../../../docs/design/plan-evaluation-strategy.md) §5.1.

## What it does

Generates a deterministic synthetic graph (default ~1K nodes, ~5K
edges, ~200 embeddings via [`graph_generator.py`](../../generators/graph_generator.py)),
ingests it into every backend the runtime environment can reach, then
runs a fixed query mix against each and diffs the results.

The query mix:

1. **Type query** — `query(node_type="entity", limit=200)` — exercises
   index lookups and `properties` round-trip.
2. **Subgraph traversal** — `get_subgraph(seed_ids, depth=2)` — exercises
   edge traversal and the temporal filter.
3. **Vector top-k** — for each of N seed embeddings,
   `vector_store.query(top_k=10)` — exercises the similarity index.

## Backends

The scenario probes the runtime for credentials and runs against
whichever backends are reachable:

| Backend | Required env | Construction |
|---|---|---|
| SQLite | none | always runs (writes to a tmp dir) |
| Postgres + pgvector | `TRELLIS_KNOWLEDGE_PG_DSN` | runs if DSN is set |
| Neo4j | `TRELLIS_NEO4J_URI` + user + password | runs if URI is set |

Backends without credentials are reported as `skipped` in the report
findings. The scenario succeeds with just SQLite — at minimum, it
exercises the harness and confirms the SQLite path doesn't drift from
itself across runs.

## Diff strictness — judgment calls

These choices are deliberate; flag them in review if they look wrong:

* **Set equality on returned ids, not ordering.** Backends differ on
  tie-break order even with `ORDER BY` in place; ordering equivalence
  is too strict for a useful regression signal at this stage.
* **Vector recall, not exact ranking.** We compute recall@10 across
  backend pairs (overlap of top-10 ids divided by 10). Anything below
  0.9 surfaces as a `regress`.
* **Subgraph: equal sets of node ids and edge tuples.** Edge tuples are
  `(source, target, edge_type)`. Property-level diffs are noted as
  `info` findings, not failures — properties may be normalised
  differently per backend (e.g. timestamp tz handling) and that's not
  what this scenario is for.

## Decision this scenario unblocks

Confirms (or surfaces a bug in) the canonical DSL Phase 2 compilers
across all three backends. Validates that the hardening plan's "blessed
Neo4j" claim doesn't hide drift from Postgres-alternative users.

When it lands, paste the measured numbers (per-backend latency, recall
overlap, any drift items) into plan §5.1 and the corresponding Phase 3
deferred item gets de-gated.
