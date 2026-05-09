# Degraded-retrieval convergence

> The chart Trellis's "improves with use" claim depends on.

## What this is for

The baseline `agent_loop_convergence` scenario measures whether the
loop *holds* a precise corpus. This scenario measures whether the loop
*makes* a sloppy corpus precise — which is the actual claim.

Before this scenario existed, runs of the baseline came back with
`useful_delta` ≈ 0. That number was correct: round 1 already served
the right entities, so the dual loop had nothing to clean up. It also
proved nothing about the loop's value.

Here we deliberately degrade retrieval so the loop has measurable work
to do, then watch it work.

## Mechanism

Same domain-templated synthetic corpus as `agent_loop_convergence`,
plus three differences:

1. **Heavy distractor pool.** 15 distractors per domain (45 total),
   vs 2 per domain in the baseline. Each distractor's content
   mentions query tokens — including entity names — so KeywordSearch
   BM25 ranks it competitively. Doc ids are
   `doc:distractor:<domain>:<n>`, so the grader (which keys on
   `doc:<entity_id>`) cannot count them as covering required
   entities even though their content mentions those names.
2. **Tight pack budget.** `max_items=4` (vs 8 in baseline). Forces
   distractors and real entities to compete for the same slots.
3. **Long run.** 200 rounds × `feedback_batch_size=10` ⇒ 20
   periodic passes. The first batch produces no signal; the last
   operates on ~200 packs of accumulated evidence.

The dual loop's noise-tagging half is what closes the gap:
`run_effectiveness_feedback` flags items whose `usage_rate` falls
below `noise_rate_threshold` (default 0.3). Distractors win pack
slots but never appear in `items_referenced` (their doc_ids don't
match `required_coverage`), so their `usage_rate` is 0. After
`min_appearances=2` they get tagged `signal_quality="noise"`. The
PackBuilder default tag filter excludes noise items, so the next
round's pack opens up a slot for a real entity.

## What "pass" means

Status `pass` requires `convergence.useful_delta >= 0.10` — a
modest-but-not-negligible climb. The expected trajectory:

* Q1 (rounds 0-49): useful_fraction ≈ 0.3-0.5 — distractors win
  ~half the slots.
* Q4 (rounds 150-199): useful_fraction climbs as accumulated noise
  tags suppress the distractors and real entities take the slots.

The four-quarter trajectory is surfaced as separate metrics:

```
convergence.quarters.useful_q1_mean
convergence.quarters.useful_q2_mean
convergence.quarters.useful_q3_mean
convergence.quarters.useful_q4_mean
```

Read the trajectory before the delta. A clean monotonic climb across
all four quarters is what proves the loop is doing the work; a single
jump (e.g., flat for three quarters then a leap) might be a corpus
artifact.

## What this scenario does NOT exercise

* **Advisory suppression branch.** The advisory loop's
  suppression/restoration semantics are exercised by
  `agent_loop_convergence`'s opt-in `regime_shift_round` mode and by
  the unit tests in `tests/unit/retrieve/test_effectiveness.py`. The
  dual loop's two halves are complementary; this scenario leans on
  the noise-tagging half because that's the half that closes the gap
  on a degraded corpus.
* **Real LLM / embeddings.** Pure deterministic synthetic corpus +
  KeywordSearch only. Phase A (`agent_loop_convergence_real_llm`)
  tests the LLM-driven side.
* **Multi-backend.** Single in-process backend. Equivalence across
  SQLite/Postgres/Neo4j is `multi_backend_feedback`'s job.

## Tuning knobs

The defaults are calibrated for SQLite-on-laptop wall time (~4s for
200 rounds). Knobs callers may want to dial:

| Kwarg | Default | When to change |
|---|---|---|
| `rounds` | 200 | Lower for unit-test smoke, higher for sustained-volume soak |
| `feedback_batch_size` | 10 | Lower to fire the loop more often (faster convergence, more wall time) |
| `pack_max_items` | 4 | Raise to make the run easier (less competition); lower to make it harder |
| `distractors_per_domain` | 15 | Capped at 15 (the per-domain pool); lower to soften the corpus |
| `useful_delta_climb_threshold` | 0.10 | Raise to demand a stronger climb; lower for run-to-run variance tolerance |
