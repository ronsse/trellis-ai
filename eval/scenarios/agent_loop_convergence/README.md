# Scenario 5.4 — agent loop convergence

> Plan reference:
> [`docs/design/plan-evaluation-strategy.md`](../../../docs/design/plan-evaluation-strategy.md) §5.4.

## What it does

1. Generates the same domain-templated synthetic corpus scenario 5.2
   uses (software engineering, data pipelines, customer support).
2. Populates the document store with one `entity_summary` doc per real
   entity **plus** a small set of *distractor* docs whose content
   overlaps with query keywords but does not contain any required
   coverage entity. The distractors are what the feedback loop has to
   learn to suppress — without them there is nothing for convergence to
   fix.
3. Runs `rounds` iterations (default 30) of a synthetic-agent step:
   * pick a query (round-robin across the three domains),
   * build a pack via `PackBuilder` with `tag_filters={}` so the
     PackBuilder's default `signal_quality` filter excludes
     noise-tagged items,
   * grade success deterministically — a round is a "success" when
     `coverage_fraction >= success_coverage_threshold` (default 0.6),
   * synthesize `items_referenced` from the served items whose ids are
     in the query's `required_coverage`,
   * call `record_feedback(...)` with the registry's EventLog so the
     advisory + effectiveness analysers see the signal,
   * score the pack with `evaluate_pack` for the per-round quality
     trace.
4. Every `feedback_batch_size` rounds (default 5):
   * `run_effectiveness_feedback` — flag noise items and tag them so
     the next round's pack excludes them,
   * `AdvisoryGenerator.generate()` — produce advisories from the
     observed pack/feedback joins,
   * `run_advisory_fitness_loop` — grade each advisory against the
     same evidence, suppress under-performing ones, restore those
     whose fitness recovers.
5. Aggregates per-round metrics and computes a simple convergence
   delta: mean weighted score on the last quarter of rounds minus the
   first quarter. Positive ⇒ the loop converged on better packs.

## Decision this scenario unblocks

Plan §5.4:

* **Advisory fitness loop validation** — if
  `loops.advisories_suppressed_total > 0` and the weighted-delta is
  non-negative, the suppression / restoration semantics work on a
  controlled corpus.
* **`WorkflowEngine` tier escalation** — track `round_success_rate`
  over the run; sustained low coverage signals confidence-gate failure
  patterns worth escalating.
* **Enrichment event-loop wiring** — this scenario *generates* the
  sustained-volume workload the plan calls for; pin a baseline of
  `convergence.weighted_delta` and watch for drift.

## Scope discipline — what this MVP does *not* do yet

These are deliberate omissions, deferred to follow-up work the plan's
§7.1 "robust eval-test discipline" pass will pick up:

* **Single retrieval surface.** Only `PackBuilder.build()` with the
  keyword strategy. The plan calls for the agent loop to drive packs
  via `get_context` / `get_objective_context` / `get_task_context` too;
  those entry points use the same scoring shape so the extension is
  additive.
* **Deterministic agent.** The simulated agent always references the
  ground-truth subset of served items. A noisier agent (with random
  flips or partial credit) is a follow-up that would test the
  effectiveness loop's robustness to label noise.
* **No baseline diff / regression gate.** The scenario *measures*; it
  doesn't yet *gate* on `convergence.weighted_delta` against a pinned
  baseline. That's plan §7.1.
* **Modest defaults.** 30 rounds × 3 domains × ~6 traces/domain is
  enough to surface qualitative convergence on dev-machine SQLite. Plan
  §5.4 cites N=100 rounds; scheduled runs dial `rounds` up.

## Counts

Default: 30 rounds, 18 traces (6 per domain × 3 domains), 6
distractor docs (2 per domain), feedback batch size 5 (so 6
effectiveness + advisory passes). Wall time on SQLite: under one
second.

## Regime-shift demo (opt-in)

Default mode shows convergence; it does **not** organically exercise
the advisory suppression branch because the post-row-1 noise filter
keeps the corpus clean enough that only one high-confidence advisory
forms.

To demonstrate suppression end-to-end, pass two kwargs:

```python
run(
    registry,
    rounds=30,
    feedback_batch_size=5,
    regime_shift_round=15,           # mid-run swap of required entities
    advisory_min_sample_size=2,      # let per-entity advisories form on batch 1
    convergence_delta_regress_threshold=-1.0,  # regime shift can swing the delta
)
```

Mechanism: at round `regime_shift_round`, the agent stops grading
against `required_coverage[0..N]` and grades against unreachable
placeholders instead. Coverage drops, post-shift packs fail, and
advisories formed pre-shift see their pack-level success_rate
collapse → confidence blends down → at least one suppression fires.
Measured outcome on the default `seed=0, rounds=30, shift=15` run:
3 anti-pattern advisories suppressed.

**Restoration is not exercised in this mode.** Once an advisory is
suppressed it leaves PackBuilder delivery, so no new presentations
accrue, so the fitness loop never sees rebound evidence. The
restoration branch is unit-tested in
`tests/unit/retrieve/test_effectiveness.py::test_auto_restore_when_evidence_recovers`
via manual evidence injection — see plan §5.5.2 row 1 for the
deferred work.
