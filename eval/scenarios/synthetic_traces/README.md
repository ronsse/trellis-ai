# Scenario 5.2 — synthetic traces e2e

> Plan reference:
> [`docs/design/plan-evaluation-strategy.md`](../../../docs/design/plan-evaluation-strategy.md) §5.2.

## What it does

1. Generates a deterministic corpus of synthetic traces in three
   domains — software engineering, data pipelines, customer support —
   via [`trace_generator.py`](../../generators/trace_generator.py). Each
   trace carries a known set of entity names that should resurface in a
   follow-up retrieval.
2. Ingests every trace into the operational `TraceStore`.
3. Mirrors minimal entity extraction: every entity named in a trace
   becomes a graph node + a document the keyword-search strategy can
   find.
4. For each domain's pre-baked follow-up query, builds a pack via
   `PackBuilder(strategies=[KeywordSearch(...)])` and scores it with
   `evaluate_pack()` against the domain's `required_coverage`.
5. Aggregates per-dimension scores and the weighted score across
   domains; reports them as scenario metrics.

## Decision this scenario unblocks

Plan §5.2: retrieval quality regression detection. Pin a baseline of
the per-dimension scores from a green run, then every subsequent run
diffs against the baseline — a code change that drops `completeness`
from 0.8 to 0.6 surfaces here.

## Scope discipline — what this MVP does *not* do yet

These are deliberate omissions, deferred to follow-up work that the
plan's §7.1 "robust eval-test discipline" pass will pick up:

* **Single retrieval surface.** Only `PackBuilder.build()` with the
  keyword strategy. The plan calls for `get_context`,
  `get_objective_context`, `get_task_context` coverage — adding the
  other two MCP-tool entry points is a follow-up, not part of this
  PR. They share the same `evaluate_pack` scoring mechanism, so the
  shape is reusable.
* **No vector strategy yet.** Embeddings are out-of-scope for this MVP
  because keyword + graph already proves the loop. Adding
  `SemanticSearch(vector_store)` is a one-line change in the strategy
  list once we want to measure semantic-vs-keyword tradeoffs.
* **No baseline diff / regression gate.** This scenario *measures*; it
  doesn't yet *gate*. That's plan §7.1.

## Counts

Default corpus: 30 traces (10 per domain × 3 domains), 1 query per
domain. The scenario kwargs `traces_per_domain` and `entities_per_trace`
let scheduled runs scale up to plan §5.2's 100-1000 traces. Wall time
on the default corpus: under one second on SQLite.
