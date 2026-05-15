# Program-convergence eval

`program_convergence` is the master eval scenario for the Trellis
self-improvement program. It runs the same per-round `context → use →
feedback` loop as `agent_loop_convergence`, but on every round it also
captures **nine signal axes** drawn from the seven self-improvement
items. The output is a multi-axis convergence report — the
chart-renderable artefact the program promised at design time.

This page is the operator-facing reference. For the design rationale,
see [`docs/design/plan-program-level-eval.md`](../design/plan-program-level-eval.md).

## What the scenario produces

For each of N rounds (default 30; set via `rounds=`) the scenario
captures the nine axis values into a single per-round record. After
the loop completes it emits:

- **Per-axis metrics** in the report's `metrics` dict, keyed
  `axis.<label>.first_quarter_mean` / `axis.<label>.last_quarter_mean` /
  `axis.<label>.delta`. The `delta` is the last-quarter mean minus the
  first-quarter mean, same shape as the legacy dual-loop delta
  (`convergence.weighted_delta`).
- **Nine `info` findings** — one per axis — with first / last quarter
  values plus the delta.
- **One composite finding** summarising every axis's delta in a single
  detail block. This is the nine-line chart the program promised, in
  textual form. Phase 3 of the plan adds the actual PNG renderer.

The nine axes:

| Axis | Label | Source machinery | Expected trajectory |
|---|---|---|---|
| **A** | `A_pack_quality` | `evaluate_pack` weighted score | rises |
| **B** | `B_useful_item_fraction` | `items_referenced / items_served` | rises |
| **C** | `C_advisory_hit_rate` | advisory hits / total active advisories | rises |
| **D** | `D_observation_enrichment` | Observations/Measurements seeded per round | rises, plateaus |
| **E** | `E_provenance_queryability` | seed-entity `confidence < 0.5` query success rate | flat at 1.0 |
| **F** | `F_extraction_failure_clusters` | open `(source_hint, failure_kind)` signatures | falls |
| **G** | `G_schema_evolution_candidates` | new `WELL_KNOWN_CANDIDATE` events per cadence | rises, then resets per ADR |
| **H** | `H_meta_trace_density` | `Activity` nodes added per cadence round | flat |
| **I** | `I_self_authored_proposals` | `PROPOSAL_DRAFTED` events per cadence round | rises in lockstep with F's decay |

A clean run is: A, B, C, D, G, I climb; F falls; E, H stay flat. A
*regressing* axis is the program's bug signal — see "How to interpret
the output" below.

## How to invoke it

```bash
# Run the master scenario against the default registry (writes a
# report under eval/reports/).
uv run python -m eval.runner --scenario program_convergence

# Run with a custom registry config dir.
uv run python -m eval.runner --scenario program_convergence \
    --config-dir /path/to/trellis

# Programmatic invocation (e.g., from a Python REPL or a test).
from eval.runner import run_scenario
from trellis.stores.registry import StoreRegistry

with StoreRegistry.from_config_dir() as registry:
    report = run_scenario("program_convergence", registry)
    print(report.metrics)
```

The scenario package lives at `eval/scenarios/program_convergence/`.
The entry point is `eval.scenarios.program_convergence.scenario.run(registry)`.

### Knobs

| Argument | Default | Effect |
|---|---|---|
| `seed` | `0` | Drives both the corpus generator and the per-round RNG. Re-running with the same seed produces byte-identical metrics — that's the determinism contract. |
| `rounds` | `30` | Total per-round iterations. CI typically runs `rounds=10` for speed; the regression suite (plan §4.2) bumps it to 50. |
| `feedback_batch_size` | `5` | How often the periodic effectiveness + advisory loops fire. Same shape as `agent_loop_convergence`. |
| `analyzer_cadence` | `5` | How often the Item-5 (schema-evolution), Item-6 (meta-trace), and Item-7 (proposal) analyzers fire. Lower values flood axis H with Activity nodes; higher values starve axis G's signal. |
| `traces_per_domain` | `6` | Synthetic corpus size — same default as `agent_loop_convergence`. |

## Strict mode — no silent fallbacks

The master scenario refuses to silently emit zero for an axis when its
source machinery is missing. Specifically, it raises
`ProgramConvergenceError` (which the runner surfaces as a `fail` status
with a clear message) when:

- The runner-supplied `StoreRegistry` has no `EventLog` wired (axes C, F,
  G, H, I all read from it).
- The `GraphStore` is missing (axes D, E, H depend on it).
- The graph backend can't compile an `EdgeQuery` with a `confidence < 0.5`
  predicate (axis E — Item 2's provenance column hasn't landed for this
  backend).
- `record_meta_analysis` returns no `activity_id` (axis H —
  `TRELLIS_META_TRACES` is set to `off`).
- The parameter store can't be seeded for the well-known analyzer (axis G).

The point of this discipline is that a green report from
`program_convergence` is supposed to mean *every* item works. A
half-wired run that returns "looks fine" hides regressions. The plan's
§3 POC directive is explicit about this.

## How to interpret the output

A representative `info` finding for axis A looks like:

```
A_pack_quality: 0.412 → 0.683 (Δ +0.271)
```

That reads as: in the first quarter of rounds the pack-quality weighted
score averaged 0.412; in the last quarter it averaged 0.683; the delta is
+0.271. Per the plan §2.1 table, axis A should *rise*, so a +0.271 delta
is the right shape.

A *regressing* axis looks like:

```
F_extraction_failure_clusters: 1.000 → 4.000 (Δ +3.000)
```

Axis F should fall as the proposal generator clears clusters. A
positive delta means open clusters are growing faster than the
generator can address them — that's a bug in Item 7's clustering logic
or Item 4's failure analyzer.

The composite summary finding's `detail.axis_deltas` block is the
machine-readable version of the nine deltas:

```json
{
  "axis_deltas": {
    "A_pack_quality": 0.271,
    "B_useful_item_fraction": 0.184,
    "C_advisory_hit_rate": 0.640,
    "D_observation_enrichment": 1.500,
    "E_provenance_queryability": 0.000,
    "F_extraction_failure_clusters": -2.000,
    "G_schema_evolution_candidates": 0.250,
    "H_meta_trace_density": 0.000,
    "I_self_authored_proposals": 1.000
  }
}
```

That's the chart, in numbers. Phase 3 of the plan adds the matching
PNG; consumers wanting an at-a-glance shape can pipe the JSON through
their tool of choice in the meantime.

## What is NOT this scenario

- **Regression thresholds.** Phase 2 of the plan (§4.2) lands
  `program_regression_suite.py`, which asserts hard thresholds against
  the nine deltas. Phase 0 (this scenario) is the data-gathering half;
  it never fails on a per-axis number.
- **The PNG chart.** Phase 3 (§4.3) adds the matplotlib renderer. The
  data is exposed via `axis.*` metrics today — any consumer can render
  the chart externally.
- **Live LLM calls.** The scenario uses a TF-IDF/keyword search proxy
  for retrieval; advisory generation and pack scoring are deterministic
  math. The operator-only LLM-backed runs (plan §8) live in a separate
  scenario package.

## See also

- [`docs/design/plan-program-level-eval.md`](../design/plan-program-level-eval.md)
  — the full design.
- [`eval/scenarios/agent_loop_convergence/`](../../eval/scenarios/agent_loop_convergence/)
  — the legacy single-axis convergence scenario. The master borrows
  its per-round loop verbatim.
- [`eval/scenarios/_convergence_common.py`](../../eval/scenarios/_convergence_common.py)
  — the shared dataclasses (`_AxisTrack`, `_NineAxisRound`,
  `_MultiAxisStats`). New convergence scenarios should compose on top
  of these, not duplicate them.
