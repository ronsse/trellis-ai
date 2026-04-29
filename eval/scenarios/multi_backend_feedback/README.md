# Scenario 5.5 — multi-backend feedback loop

> Plan reference:
> [`docs/design/plan-evaluation-strategy.md`](../../../docs/design/plan-evaluation-strategy.md) §5.5.2 row 3.

## What it does

Runs the convergence loop scenario 5.4 measures (`PackBuilder` →
deterministic agent → `record_feedback` → periodic
`run_effectiveness_feedback` + `AdvisoryGenerator` +
`run_advisory_fitness_loop`) against multiple backend combinations
and diffs the loop counters across them.

Closes the §5.5.2 row 3 gap: scenario 5.4's loop is SQLite-only by
construction, so different `EventLog.get_events` ordering / limit
semantics on Postgres or Neo4j could change advisory output without
any unit-test surface to catch it. This scenario surfaces that drift.

## Backends

The scenario probes the runtime for credentials and runs against
whichever backends are reachable. Every handle holds `document_store`
**and** `vector_store` at SQLite — the feedback loop reads neither.
Pinning them keeps `KeywordSearch` deterministic across backends so
any cross-backend diff is attributable to the feedback path under
test, and dodges the pgvector fixed-dimension constraint when the
test DB has a `vectors` table from a prior run at a different dim.

| Handle | knowledge.graph | knowledge.vector | knowledge.document | operational.trace | operational.event_log | Required env |
|---|---|---|---|---|---|---|
| `sqlite` | sqlite | sqlite | sqlite | sqlite | sqlite | none — always runs |
| `postgres` | postgres | sqlite | sqlite | postgres | postgres | `TRELLIS_KNOWLEDGE_PG_DSN` (or legacy `TRELLIS_PG_DSN`) |
| `neo4j_op_postgres` | neo4j | sqlite | sqlite | postgres | postgres | `TRELLIS_NEO4J_URI` + user + password **and** Postgres DSN (Neo4j has no `event_log` backend) |

Backends without credentials are reported as `info` findings ("X
backend skipped — credentials not in env"). The scenario succeeds
with just SQLite — at minimum, it exercises the harness and pins the
SQLite path to itself across runs.

## What "equivalence" means here

The corpus, seed, and `run()` kwargs are identical across backends.
`tag_filters={}` engages PackBuilder's default `signal_quality`
filter so noise tagging takes effect on the next round, exactly as
in 5.4.

* **Loop counters must match exactly** — they're integer functions
  of EventLog content, which the scenario seeds identically.
  Mismatch = `fail` finding. Counters checked:
  `loops.effectiveness_runs`, `loops.advisory_runs`,
  `loops.noise_items_tagged_total`, `loops.advisories_generated_total`,
  `loops.advisories_suppressed_total`, `loops.advisories_restored_total`,
  `loops.advisories_boosted_total`, `round_success_rate`,
  `round_total_items_served`, `round_total_items_referenced`.
* **Convergence deltas match within tolerance** — same packs, same
  scoring formula, only floating-point rounding can differ.
  `CONVERGENCE_DELTA_TOLERANCE` is 0.01 by default. Drift larger
  than this surfaces as a `warn` and means pack contents diverged
  (usually traces back to graph_store ordering on a cross-backend
  upsert).

## Wipe between backends

Postgres + Neo4j state persists across runs and the `postgres` and
`neo4j_op_postgres` handles share a Postgres DSN, so the scenario
truncates `events` + `traces` (operational PG, both handles) and
the knowledge tables (`nodes` / `edges` / `entity_aliases` for the
postgres handle; `:Node` rows for the neo4j_op_postgres handle)
before each backend's run. SQLite gets a fresh `stores_dir`
subdirectory and needs no wipe.

`StoreRegistry` is lazy — schemas are created on first store
property access — so the scenario eagerly touches every store the
run will write to before truncating, otherwise the wipe would error
on tables that don't exist yet.

## Decision this scenario unblocks

Plan §5.5.2 row 3:

* Counter equivalence across all three handles confirms the
  EventLog query layer is consistent — `analyze_effectiveness`,
  `AdvisoryGenerator`, and `run_advisory_fitness_loop` will give
  the same answers regardless of which backend an operator
  configured. Drift here is a real bug in the offending backend.
* `info` findings for skipped backends keep CI runs honest about
  what was actually exercised — passing a SQLite-only run does
  *not* validate the feedback loop on Postgres / Neo4j.

## Counts

Defaults: 15 rounds × 3 domains × 4 traces/domain, feedback batch
size 5. Wall time on SQLite alone: ~1s. With Postgres added: ~5-15s
(network round-trip dominated). With Neo4j added: ~10-30s (AuraDB
free tier, first-write cold-start). Scheduled runs dial the round
count up via the `rounds` kwarg.

## What this scenario deliberately does *not* do

* **No FTS / KeywordSearch comparison.** The `document_store` is
  pinned to SQLite to avoid drift on FTS implementation
  differences — that comparison is scenario 5.1's job.
* **No latency gates.** Per-backend duration is captured for
  context but does not gate pass/fail. Latency baselines belong in
  scenario 5.3 (populated-graph performance).
* **No advisory restoration end-to-end.** Same architectural reason
  as scenario 5.4 — once an advisory is suppressed it leaves
  PackBuilder delivery and never accumulates new presentations.
  Plan §5.5.2 row 1 documents the full picture.
