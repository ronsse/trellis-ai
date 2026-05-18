# Scenario — skill loop convergence

> Phase F, Wave 1, Unit D. Scaffolding only — every callable raises
> `NotImplementedError` until F1-F5 land. The scenario is discoverable
> via `eval/runner.py`, but `run()` returns `status="skip"` unless an
> operator opts in.

## What this measures

Whether the inner agent loop — graph-skill harness (F1) + curator skill
(F2) + feedback path (F3) + score-based evolver (F5) — converges over
time on a synthetic corpus. Three axes:

| Axis | Reducer | What climbs / falls |
|---|---|---|
| **P. Coverage** | `metrics.coverage_curve` | % of seed under-populated nodes that have received at least one `NODE_ENRICHED` event by end of period N. Should climb toward 1.0 if the curator is doing its job. |
| **Q. Retrieval lift** | `metrics.retrieval_lift_curve` | Mean `evaluate_pack` weighted score on a fixed query panel at end of period N, minus the same score at period 0 (baseline). Should be non-negative once enrichment lands in the document store. |
| **R. Variant survival** | `metrics.variant_survival_rate` | Fraction of the F5 evolver's initial prompt-variant pool still alive at end of period N. Pattern of falling-then-flattening means the evolver is converging on the winners. |

A clean run looks like: P climbs to ~1.0, Q is non-decreasing and ends
above 0, R falls then plateaus.

## Run shape

Four conceptual phases:

1. **Seed.** `_seed()` calls `seed.seed_under_populated_nodes`,
   `seed.seed_documents_for_nodes`, `seed.seed_baseline_corpus`.
2. **Loop.** Per-period curator dispatch. Captures `NODE_ENRICHED`,
   `CURATION_FEEDBACK_RECORDED`, `PACK_QUALITY_SCORED` events.
3. **Evolve.** Every `periods_per_evolution` periods, F5 scores the
   variant pool against captured feedback and prunes / promotes.
4. **Measure.** `_measure()` reduces events into `CoverageCurve`,
   `LiftCurve`, `VariantSurvival`.

## F-phase fill-in map

| File | Helper | Phase |
|---|---|---|
| `scenario.py` | `_seed` | F6 (this scenario) |
| `scenario.py` | `_loop` | F2 + F3 + F5 jointly |
| `scenario.py` | `_measure` | F6 (this scenario) |
| `scenario.py` | `_summarise` | F6 (this scenario) |
| `scenario.py` | `_validate_run_kwargs` | F6 (this scenario) |
| `seed.py` | `seed_under_populated_nodes` | F1 |
| `seed.py` | `seed_documents_for_nodes` | F2 |
| `seed.py` | `seed_baseline_corpus` | F6 |
| `metrics.py` | `coverage_curve` | F1 + F2 (event source + emit point) |
| `metrics.py` | `retrieval_lift_curve` | F3 |
| `metrics.py` | `variant_survival_rate` | F5 |

## In scope (F6 — this scenario)

- File layout, signatures, registration with `eval.runner`.
- Opt-in gate via `TRELLIS_EVAL_SKILL_LOOP` env var. CI does not set
  it; the scenario stays inert.
- Result schemas (`CoverageCurve`, `LiftCurve`, `VariantSurvival`)
  as `TrellisModel` subclasses with `extra="forbid"`.
- The orchestration glue between phases (the body of `run()`).

## Deferred (later F-phases)

- F1 — graph-skill harness machinery + the `NODE_ENRICHED` event
  type (does not yet exist on the EventType enum; see
  `src/trellis/stores/base/event_log.py`).
- F2 — curator skill + the `CURATION_FEEDBACK_RECORDED` event type
  (also not yet on the enum).
- F3 — per-period `PACK_QUALITY_SCORED` emission tied to the
  retrieval-lift query panel.
- F4 — feedback loop integration.
- F5 — score-based evolver and its variant-pool events.

The three event types this scenario consumes (`NODE_ENRICHED`,
`CURATION_FEEDBACK_RECORDED`, plus whatever F5 emits for variant
turnover) are F1-F5's responsibility to add to
`trellis.stores.base.event_log.EventType`. The scenario reads them by
string, but the F-phase swarms own the enum delta.

## Running (once F1-F5 land)

```bash
TRELLIS_EVAL_SKILL_LOOP=1 uv run python -m eval.runner \
    --scenario skill_loop_convergence
```

Without the env var the scenario emits `status="skip"` and exits
cleanly.

## References

- `docs/design/adr-graph-skill-harness.md` (authored in parallel by
  Wave 1 Unit A — path only, do not assume contents).
- `docs/design/adr-inner-curation-loop.md` (authored in parallel by
  Wave 1 Unit B — path only, do not assume contents).
- `docs/research/workflow-engine-disposition.md` (authored in
  parallel by Wave 1 Unit C — path only, do not assume contents).
- `eval/scenarios/program_convergence_real_llm/` — the closest
  existing parallel for the gating + telemetry pattern.
- `docs/agent-guide/pack-quality-evaluation.md` — the
  `evaluate_pack` vocabulary the retrieval-lift axis builds on.
- `docs/design/plan-program-level-eval.md` — the program-level
  framing this scenario plugs into as axis P / Q / R.
