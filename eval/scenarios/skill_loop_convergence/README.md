# Scenario — skill loop convergence

> Phase F, Wave 1, Unit D — implemented as the **reference-driver
> build** (issue #249). The measurement path for axes P / Q / R runs
> end-to-end against real Trellis subsystems; deterministic
> scenario-local drivers stand in for the F1–F5 machinery that has not
> landed yet. `run()` returns `status="skip"` unless an operator opts
> in via `TRELLIS_EVAL_SKILL_LOOP=1`.

## What this measures

Whether the inner agent loop — curator enrichment + pack-quality
feedback + score-based evolution — converges over time on a synthetic
corpus. Three axes:

| Axis | Reducer | What climbs / falls |
|---|---|---|
| **P. Coverage** | `metrics.coverage_curve` | % of seed under-populated nodes enriched by end of period N. Climbs to 1.0 when the curator reaches every node. |
| **Q. Retrieval lift** | `metrics.retrieval_lift_curve` | Mean `evaluate_pack` weighted score on a fixed query panel at end of period N, minus the pre-loop baseline. Non-decreasing and positive once consolidation lands in the document store. Supports claim C1. |
| **R. Variant survival** | `metrics.variant_survival_rate` | Fraction of the evolver's initial prompt-variant pool alive at end of period N. Falling-then-flattening means score-based pruning found the weak variants. |

A clean run: P climbs to 1.0, Q is non-decreasing and ends above 0,
R falls then plateaus, and `stability_delta` (panel of queries
*unrelated* to enrichment) stays ~0.

## Real vs. reference — read before citing

**Real (production subsystems under test):**

- Every enrichment write goes through the **governed mutation
  pipeline** (`ENTITY_CREATE` upsert via `build_curate_executor`) —
  the sparse seed version survives in SCD-2 history.
- Packs are assembled by the real **`PackBuilder`** (KeywordSearch
  over the real document store) and scored by the real
  **`evaluate_pack`** through the assembly-time evaluator hook, which
  emits real **`PACK_QUALITY_SCORED`** events to the real EventLog.
- Axis Q is therefore a genuine measurement: consolidating fragmented
  source notes into one summary measurably lifts pack quality under a
  fixed `max_items` budget, without disturbing unrelated queries.

**Reference (scenario-local stand-ins, replaced when F1–F5 land):**

- `_ReferenceCurator` stands in for the F2 curator skill: it
  deterministically consolidates a node's source notes instead of
  running an agent loop.
- `_ReferenceEvolver` stands in for the F5 score-based evolver. Its
  pruning decisions are driven **only by measured pack scores** (never
  the variant's hidden quality parameter), so R validates that
  measurement alone finds weak variants — but the pool itself is
  synthetic. **Do not cite axis R as evidence of F5 value**; the
  report carries this disclaimer as a finding.

## Mechanism

1. **Seed.** `periods × nodes_per_period` under-populated nodes
   (governed writes; name only, no description), `docs_per_node`
   fragmented source notes per node (one fact each), plus a background
   corpus (scenario-5.2 generator) that supplies retrieval competition
   and the stability panel.
2. **Loop.** Per period: the reference curator consolidates that
   period's node slice — each node assigned a prompt variant whose
   `fact_recall` bounds summary completeness — then the fixed panel
   (one completeness-weighted `EvaluationScenario` per node) runs
   through `PackBuilder` + the evaluator hook.
3. **Evolve.** Every `periods_per_evolution` periods, variants whose
   measured mean pack score trails the best by more than `CULL_MARGIN`
   are culled (never below `MIN_POOL_SIZE`).
4. **Measure.** Reduce captured payloads into the three curves;
   `status="regress"` when final lift is non-positive or coverage
   falls short of 1.0.

Why the lift is real: `docs_per_node` (4) sits above the panel budget
(`PANEL_MAX_ITEMS=2`), so baseline packs physically cannot cover a
node's facts with fragmented notes — consolidation converts that
headroom into measured lift, and a weak variant's dropped facts can't
all be back-filled by the remaining budget slots.

## The F1–F5 seam

When the real machinery lands it replaces the two `_Reference*`
drivers; the seed helpers, panel, reducers, and report shape stay:

| Lands | Replaces |
|---|---|
| F1 harness + F2 curator skill | `_ReferenceCurator` (and the in-memory enrichment records switch to the F2 `node.enriched` event type) |
| F3 feedback path | the scenario's period-stamping of `PACK_QUALITY_SCORED` payloads |
| F5 evolver | `_ReferenceEvolver` (its per-period `{period, alive, culled}` snapshots already match the reducer contract) |

The event-type enum delta (`node.enriched`,
`curation.feedback_recorded`, evolver trace events) remains owned by
the F-phase implementations, per the Wave-1 follow-ups in TODO.md.

## Running

```bash
TRELLIS_EVAL_SKILL_LOOP=1 uv run python -m eval.runner \
    --scenario skill_loop_convergence
```

Without the env var the scenario emits `status="skip"` and exits
cleanly (CI never sets it).

Use a **fresh `--data-dir` per run** (the standard convergence-scenario
assumption): re-running against stores that already contain a prior
run's enrichments makes the baseline panel score high from period 0,
which zeroes the measured lift and reports a spurious `regress`.

## References

- `docs/design/adr-graph-skill-harness.md` — the F1 harness contract.
- `docs/design/adr-inner-curation-loop.md` — the F2 curator design.
- `eval/scenarios/agent_loop_convergence/` — the sibling
  deterministic-loop scenario this one's seeding + panel pattern
  mirrors.
- `docs/agent-guide/pack-quality-evaluation.md` — the `evaluate_pack`
  vocabulary and the assembly-time evaluator hook axis Q rides.
- `tests/unit/eval/test_skill_loop_convergence.py` — axis-shape,
  real-subsystem, and determinism coverage.
