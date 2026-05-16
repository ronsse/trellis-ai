# Plan: Program-level eval — convergence + new-loop signal scenarios

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable (each scenario is an independent unit)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) — eval coverage for items 1-7 + cleanup tracks
**Related:** [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.5 — Scenario 5.4 (loop convergence) was the chart-producing scenario this program now extends.

## 1. Premise

Scenario 5.4 was scoped in [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.5 as "the chart you can show people" — a synthetic agent runs N rounds of `context → use → feedback`, the pack quality score climbs across rounds, the existing dual-loop demonstrably converges. It was deferred ("NOT WRITTEN, ~3 days, ~800 LOC") pending Item 6 (dogfooding meta-traces) because the convergence story is much richer when there's a graph-shaped record of what changed each round.

With the self-improvement program in flight, the eval surface broadens. **Each of the seven items in the program adds a distinct signal that should produce a measurable curve.** The program-level eval is not one scenario — it's a **scenario suite** that demonstrates the program as a whole works.

## 2. Scope

Land one master scenario plus six per-item satellite scenarios. Each per-item scenario can run independently; the master scenario composes them and produces a unified report.

### 2.1 Master scenario — `program_convergence`

A single scenario that runs the full loop over N rounds and produces a multi-axis convergence chart:

| Axis | Source signal | What climbs / falls |
|---|---|---|
| **A. Pack quality** | existing 5-dim `evaluate_pack` (coverage, relevance, density, diversity, freshness) | rises monotonically as the dual-loop demotes noise and promotes precedents |
| **B. Useful-item fraction** | `items_referenced / items_served` from feedback | rises as PackBuilder learns what the agent actually uses |
| **C. Advisory hit rate** | fraction of injected advisories whose recommendation was followed AND outcome was success | rises as `run_advisory_fitness_loop` suppresses misfiring advisories and reinforces good ones |
| **D. Observation enrichment** | count of `Observation`/`Measurement` nodes per round attached to seed entities | rises as Item 1's sample extractor produces stats; should plateau when the query log is exhausted |
| **E. Provenance queryability** | fraction of seed entities for which `confidence < 0.5` filter returns sane results | flat at 1.0 once Item 2 lands; before that, 0.0 — proves the column path is the source of truth |
| **F. Extraction-failure cluster decay** | count of open `EXTRACTION_FAILED` clusters per round | falls as the operator (or, post-Item 7, the coding agent) addresses each cluster |
| **G. Schema-evolution candidate emergence** | count of unique `WELL_KNOWN_CANDIDATE` events per round | rises as synthetic open-string types accumulate; promotion ADRs reset specific candidates to zero |
| **H. Meta-trace density** | `Activity` nodes written per round | flat — should stay bounded by sampling caps; **regression signal** if it grows unbounded |
| **I. Self-authored proposal count** | `PROPOSAL_DRAFTED` events per round | rises in lockstep with F + G — proves Item 7 sees the same signals operators see |

A clean run looks like: A, B, C, D, G, I climb; F falls; E, H stay flat. The chart is a 9-line plot over 50 rounds.

### 2.2 Per-item satellite scenarios

Already named in [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.1:

| Item | Satellite scenario | Notes |
|---|---|---|
| 1 | `observation_retrieval` ([`plan-observation-entity-type.md`](./plan-observation-entity-type.md) Phase 4) | tests axis D in isolation |
| 2 | `provenance_round_trip` ([`plan-provenance-columns.md`](./plan-provenance-columns.md) §6 contract tests serve as the scenario) | tests axis E |
| 3 | `parameter_registry_passthrough` (this plan §4.3) | regression test — verifies recommendation changes with registry overrides |
| 4 | `extraction_failure_clustering` ([`plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md) Phase 4) | tests axis F |
| 5 | `schema_evolution_candidate_emergence` ([`plan-well-known-promotion-loop.md`](./plan-well-known-promotion-loop.md) Phase 4) | tests axis G |
| 6 | `meta_trace_round_trip` ([`plan-dogfooding-meta-traces.md`](./plan-dogfooding-meta-traces.md) Phase 3) | tests axis H |
| 7 | `proposal_generation` ([`plan-coding-agent-loop.md`](./plan-coding-agent-loop.md) Phase 4) | tests axis I |

## 3. POC directives applied

- The master scenario **raises** if any required item's machinery is absent. No silent skip. Operator running `program_convergence` against a tree where Item 4 hasn't landed gets a clear "Item 4 machinery missing; cannot measure axis F" error. The master scenario does not run partial.
- Each satellite scenario must run **independently** — running `program_convergence` is not a prerequisite. Cheap-to-iterate; expensive-once-composed.
- Determinism: every scenario seeds its random sources with `invocation_id`. Reservoir samples (Item 6) seed from the same. Re-run is byte-identical.
- No live LLM calls in CI. The master scenario uses a deterministic embedding proxy (TF-IDF or fixture-table) per the existing convergence scenarios pattern. LLM-backed runs are operator-triggered only.

## 4. Phases

### 4.1 Phase 0 — scaffolding

**Files:**
- `eval/scenarios/program_convergence.py` (new) — master scenario.
- `eval/_scenario_common.py` (depends on C1.4 landing first) — extended with axis-tracking dataclasses.
- `docs/agent-guide/program-convergence-eval.md` (new) — user-facing doc with the chart.

**Estimated size:** ~250 LOC scenario + ~100 LOC shared additions + ~150 LOC docs.

### 4.2 Phase 1 — wire each axis as it's available

Per-axis wiring is gated on the corresponding item landing. As each item lands, the master scenario gains an axis. Order matches the program's execution order:

- After Item 3 lands → axis C improves (advisory hit rate now tunable per-cell).
- After Item 1 lands → axis D becomes non-zero.
- After Item 4 lands → axis F is measurable.
- After Item 5 lands → axis G is measurable.
- After Item 2 lands → axis E flips from 0 to 1.
- After Item 6 lands → axis H is measurable.
- After Item 7 lands → axis I is measurable.

**Each axis lands with the corresponding item's PR**, not in a separate "wire the eval" follow-on. The eval scenario is part of the item's definition-of-done.

### 4.3 Phase 2 — regression scenarios

**Files:**
- `eval/scenarios/parameter_registry_passthrough.py` (new; satellite for Item 3).
- `eval/scenarios/program_regression_suite.py` (new; runs all satellites + master and asserts threshold lines).

**Regression assertions** (the lines `--strict` mode gates against):

- Axis A: pack quality at round 50 ≥ axis A at round 5 + 0.15 (15-point lift). **Profile-dependent — see below.**
- Axis B: useful-item fraction at round 50 ≥ axis B at round 5 + 0.10.
- Axis C: advisory hit rate at round 50 ≥ 0.6 (absolute threshold).
- Axis D: ≥ 10 observations per seed entity by round 25.
- Axis E: 1.0 after Item 2 lands.
- Axis F: open cluster count at round 50 ≤ axis F at round 25 (declining trend).
- Axis G: ≥ 1 distinct candidate by round 30.
- Axis H: meta-trace nodes ≤ 50 per round per analyzer (sampling cap honored).
- Axis I: ≥ 1 proposal per surfaced cluster by round 40.

A scenario run that violates any threshold exits 1; CI gates against this.

**Corpus profile split on axis A (Phase 5B).** The 0.15 axis A target is calibrated for a **real corpus** with noisy ground truth. The deterministic synthetic corpus the master scenario generates today starts at ~0.94 pack quality and converges to ~1.0, so the observed lift ceiling is ~0.0545 — well below 0.15. The regression suite resolves this with a profile selector: `THRESHOLD_A_DELTA_BY_PROFILE = {"synthetic": 0.05, "real": 0.15}`, `DEFAULT_CORPUS_PROFILE = "synthetic"`. CI runs under `synthetic`; operators driving the suite against a real corpus pass `profile="real"`. Axis A is the only profile-dependent threshold — the other 8 are either absolute (axes C, E, G, H, I) or trend-based (axes B, D, F), so the synthetic-vs-real distinction does not affect them. An invalid profile string raises `ValueError` (no silent fallback). The `profile` kwarg is programmatic-only today; surfacing it on the CLI runner is logged as a follow-up.

**Estimated size:** ~300 LOC.

### 4.4 Phase 3 — the chart

**Files:**
- `eval/reports/program_convergence_chart.py` (new) — Matplotlib renderer (matplotlib is already a dev dep; verify in pyproject.toml).
- Output: a PNG to `eval/reports/program_convergence_<timestamp>.png` (gitignored).

**Estimated size:** ~200 LOC.

## 5. Total size estimate

| Phase | LOC code | LOC docs |
|---|---|---|
| 0 | 350 | 150 |
| 1 | covered by per-item plans | — |
| 2 | 300 | — |
| 3 | 200 | — |
| **Total** | **~850** | **150** |

Plus per-item axis wiring counted against each item's plan.

## 6. Done when

- `eval/scenarios/program_convergence.py` runs against a complete-program tree.
- The 9-axis report renders (text + optional PNG chart).
- All Phase 2 regression assertions are honored.
- `make test` for eval-related unit tests passes.
- A representative run is checked in as `eval/fixtures/program_convergence_baseline.json` (after the program has been live for one release; until then, the run is non-baseline'd per [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §7.1).

## 7. The story this scenario tells

The chart at the end is the proof. A 9-line plot over 50 synthetic agent rounds, showing:

- The original dual-loop converging (axes A, B, C). This is what Scenario 5.4 was always going to demonstrate.
- The new loops adding signal (D, F, G, I). These are the program's deliverable.
- The flat axes (E, H) — provenance is queryable from the start; meta-trace density stays bounded.

If any of those lines is the wrong shape, the program has a bug — and we can see exactly which item is responsible. That's the property a multi-axis eval gives us that a single curve doesn't.

## 8. Considerations

- **Synthetic LLM behavior.** The convergence scenarios use a TF-IDF proxy for embeddings; the agent's "use the pack" behavior is rule-based. This is correct for CI determinism. A separately-invokable LLM-backed run (operator-only, against the same scenarios with real Anthropic/OpenAI calls) generates the numbers we publish externally. Both modes share the scaffolding.
- **Cost of the LLM-backed run.** ~50 rounds × ~$0.02/round = ~$1 per scenario. The full suite (7 satellites + master) is ~$8. Operator opts in.
- **Stability under load.** A run that hits AuraDB Free's connection caps will produce noisy timing. The master scenario records latency but does not gate on it — that's `plan-neo4j-hardening.md` §5's job. Quality axes (A, B, C, etc.) are timing-independent.
- **Per-axis baseline drift.** As the program evolves, the absolute thresholds in §4.3 may need to be re-baselined. Use `--update-baseline` mode (the same shape the existing convergence scenarios use) to capture a new baseline; commit only with explicit human review.
- **Why nine axes and not one summary score.** A summary score hides which loop is misbehaving. The 9-axis view forces "if axis F is regressing, look at Item 4's extractor or analyzer." Composability over compressed metrics.

## 9. Risks

- **Eval scenario coupling to in-flight items.** Until all 7 items land, the master scenario's `--strict` mode is unrunnable. Per Phase 1, each axis wires in with its item. Operators running the suite mid-program get partial results (which is correct; they're mid-program).
- **Determinism drift between Trellis versions.** A code change to `evaluate_pack` weights changes axis A's baseline. Mitigate per Phase 3 baseline-update protocol; document axis A as Trellis-version-pinned.
- **CI runtime.** 50 rounds × N seed scenarios is not trivial. Master scenario in CI uses N=10 rounds; full run is operator-triggered. Documented.
