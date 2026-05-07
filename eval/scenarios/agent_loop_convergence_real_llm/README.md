# Scenario — agent loop convergence (real LLM + real embeddings)

> Plan reference:
> [`docs/design/plan-real-corpus-eval.md`](../../../docs/design/plan-real-corpus-eval.md) §5.1.

## What this is

Phase A of the real-corpus eval plan. A **fork** of
[`agent_loop_convergence`](../agent_loop_convergence/) — same domain
templates, same agent-loop machinery, same convergence math — that
swaps the mocked LLM and absent embedder for live providers:

- **Chat (entity-summary generation):** Moonshot / Kimi
  (`kimi-k2-0905-preview`) via OpenAI-compatible API.
- **Embeddings:** OpenAI `text-embedding-3-small` (1536-dim).

The synthetic baseline at `agent_loop_convergence/` stays unchanged so
the runner can produce per-seed diffs against it.

Provider split was forced by the
[`eval/_smoke/moonshot_probe.py`](../../_smoke/moonshot_probe.py)
verdict (2026-05-06): Moonshot's international (`.ai`) endpoint
returned 403 `permission_denied_error` on all three candidate
embedding model names, while OpenAI embeddings work unmodified.

## What this exercises end-to-end

1. **Real chat path** — at setup time, every entity gets an
   LLM-generated paragraph summary (one call per entity, ~150 tokens
   in / ~150 tokens out). Validates auth, base_url, model
   availability, `TokenUsage` extraction.
2. **Real embedding path** — every entity-summary doc and every
   distractor doc is embedded once and upserted into the registry's
   vector store. Validates the OpenAI embedder + the vector store
   roundtrip + dimension alignment.
3. **Real semantic retrieval** — `SemanticSearch` joins
   `KeywordSearch` in PackBuilder's strategy list. Per-round packs
   now blend keyword and vector hits.
4. **Cost / latency tracking** — wrapper records every chat call and
   every embedding call, totals tokens, computes USD spend at run
   end using known per-M-token rates for both models.

## What this does *not* exercise (deliberate, per Phase A scope)

- The LLM does not fire during the agent loop itself — the
  synthetic agent stays deterministic. The "real LLM in the loop"
  budget lives at setup time only. Phase C is when an LLM-driven
  agent enters the per-round path.
- Per-strategy retrieval contribution attribution (which item came
  from `KeywordSearch` vs. `SemanticSearch`) is not yet wired into
  PackBuilder telemetry. The scenario reports `embedder.calls_total`
  and `llm.calls_total` but not the per-round split. Follow-up.
- Enrichment-mode classification fallback — defer until corpus
  complexity (B-1) gives the deterministic classifiers something to
  fall over on.

## Running

```bash
op run --env-file=.env -- uv run python -m eval.runner \
    --scenario agent_loop_convergence_real_llm
```

Requires `MOONSHOT_API_KEY` and `OPENAI_API_KEY` in `.env` (as
`op://` secret references). See `.env.example` and
[`docs/design/plan-real-corpus-eval.md`](../../../docs/design/plan-real-corpus-eval.md) §5.1
for the secret-injection pattern.

## Cost expectations (per run)

| Surface | Calls | Tokens | Cost |
|---|---|---|---|
| Moonshot chat (kimi-k2, summaries) | ~18 (one per entity) | ~3K in / ~3K out | ~$0.01 |
| OpenAI embeddings | 1-2 batch calls | ~1.5K total | <$0.001 |
| **Total per run** | | | **~$0.01** |

Cost is dominated by the setup phase; the round loop adds nothing
because the agent is deterministic. Increasing `rounds` changes
wall-time but not API spend. Hard cap in the scenario: $1 per run as
a safety guard against pricing regressions.

## Counts

Defaults inherited from the synthetic baseline: 30 rounds, 18 traces
(6 per domain × 3 domains), 6 distractor docs, feedback batch size 5.
Use `--scenario-kwargs` (TBD on runner) or invoke `run()`
programmatically to override.

## Smoke configuration

For a fast end-to-end validation (under $0.02), pass:

```python
run(registry, rounds=5, feedback_batch_size=5)
```

Five rounds is enough to fire one feedback batch and exercise the
dual loop.
