# Scenario — dbt corpus convergence (Phase B-1)

> Plan reference:
> [`docs/design/plan-real-corpus-eval.md`](../../../docs/design/plan-real-corpus-eval.md) §5.2.

## What this is

The first **real-corpus** convergence scenario — a fork of
[`agent_loop_convergence_real_llm`](../agent_loop_convergence_real_llm/)
that replaces the hand-templated synthetic corpus with the
[Jaffle Shop dbt fixture](../../corpora/jaffle_shop/) (21 entities,
22 `dependsOn` edges, real descriptions from the dbt manifest), and
swaps the round-robin domain queries for
[12 hand-authored ground-truth queries](../../corpora/jaffle_shop/queries.py)
covering column-level transformations, multi-hop lineage,
change-impact analysis, and test cross-referencing.

## What this exercises that Phase A didn't

| Dimension | Phase A (synthetic) | Phase B-1 (dbt) |
|---|---|---|
| Corpus authorship | hand-templated for the loop | sourced from a real dbt project |
| Entity types | one (`entity`) | three (`dbt_model`, `dbt_source`, `dbt_test`) |
| Edges | none | 22 `dependsOn` edges (canonical PROV-O / SKOS form) |
| Distractor density | 6 hand-written distractor docs | organic — overlapping names (`customers` mart vs. `stg_customers` vs. `raw.customers`) |
| Query difficulty | uniform | 5 column-transformation, 4 multi-hop lineage, 2 test-cross-ref, 1 layer-aware |
| Required-coverage size | 3 entities per query | 1-6 entities per query (variable difficulty) |

This is the chart that's harder to dismiss — it lives on a corpus a
reviewer will recognize as "the dbt example everyone uses."

## What this does *not* do (deliberate, scope-controlled deferrals)

- **`GraphSearch` is not in the strategy list.** `GraphSearch` ignores
  the natural-language query and needs `seed_ids` filters extracted
  from the intent — entity-name extraction we don't have yet. The
  plan's open question "does GraphSearch add signal?" is answered
  for now: not without an entity-name extractor in front. Adding one
  is a follow-up; it would meaningfully help the lineage queries.
- **Tests get templated descriptions, not LLM-generated ones.** The
  manifest has no description for tests; the scenario synthesizes a
  short string at load time (`"dbt test '<name>' validates ..."`)
  so they're findable by KeywordSearch and SemanticSearch. We do
  *not* fire the LLM for these — Phase B-1 is about corpus realism,
  not LLM cost-per-round.
- **Models and sources keep their manifest descriptions verbatim.**
  Unlike Phase A which generated LLM summaries, we use the dbt
  manifest's `description` field directly. They're already concise
  and accurate; LLM rewriting would add cost without quality gain.
- **No regime-shift demo.** Same as Phase A's default mode.

## Strategy mix

`PackBuilder` is built with `[KeywordSearch, SemanticSearch]`. Same
pair as Phase A's fork. SemanticSearch reads from the registry's
vector store (populated at scenario setup with OpenAI
`text-embedding-3-small` 1536-dim vectors). KeywordSearch reads from
the document store (populated by the dbt loader's description-indexing
pass plus the test-description templating in this scenario's setup).

## Cost expectations

| Surface | Calls | Cost |
|---|---|---|
| Moonshot chat | **0** — no LLM in setup or round loop | $0 |
| OpenAI embeddings (setup batch + per-round queries) | ~1 batch + N rounds | ~$0.0001 |

Phase B-1 is intentionally cheap. The chart's value is in the
convergence dynamics, not the API budget.

## Running

```bash
op run --env-file=.env -- uv run python -m eval._smoke.dbt_phase_b1_smoke
op run --env-file=.env -- uv run python -m eval._smoke.dbt_phase_b1_smoke --rounds 100 --feedback-batch-size 10
```

Note: `op run` is still required because OpenAI embeddings need
`OPENAI_API_KEY`. `MOONSHOT_API_KEY` is loaded but not used.

## Ground-truth grading — both id forms accepted

The grader for this scenario (unlike Phase A's) accepts a pack item
as covering a required entity if EITHER:
- `item_id == f"doc:{entity_id}"` — the form KeywordSearch / SemanticSearch produce, OR
- `item_id == entity_id` — the form GraphSearch would produce if it were in the strategy list (forward-compatibility for when we add it)

## Convergence expectations

Unlike Phase A where `round_success_rate=1.0` from round 1 (corpus too
clean), Phase B-1 is designed to start lower. Hard queries
(multi-hop lineage with 6 required entities at threshold 0.6 means
needing 4-of-6 — coverage of 0.67) will fail unless the system
retrieves transitive dependencies. The dual-loop should improve
coverage over rounds by tagging noise items and reinforcing the
right item-ids per intent.

If `convergence.useful_delta` climbs from a starting value below 0.5
to a final value above 0.6, the noise + advisory loops are working
on a real corpus. That's the chart.
