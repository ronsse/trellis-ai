# Pack Quality Evaluation

Assembly-time scoring of context packs against declared scenarios. This is the complement to *context effectiveness* (runtime outcome correlation): it asks "does this pack look right for what the agent needs?" before the agent ever runs, not "did the agent succeed after using it?"

Both loops are independent and neither subsumes the other. See the matrix at the bottom for when to reach for which.

## TL;DR

```bash
trellis analyze pack-quality --scenarios ./scenarios.yaml --profile code_generation
```

Each scenario declares an intent, optional domain, required coverage keywords, and expected content categories. The CLI assembles a pack via the live `PackBuilder`, scores it on five dimensions, and prints the per-dimension scores plus any missing coverage or low-score findings.

For programmatic use:

```python
from trellis.retrieve import (
    CODE_GENERATION_PROFILE,
    EvaluationScenario,
    evaluate_pack,
)

scenario = EvaluationScenario(
    name="dedup_sql_generation",
    intent="Generate a deduplication SQL pattern",
    domain="billing",
    required_coverage=["dedup", "row_number", "partition"],
    expected_categories=["reference", "tutorial"],
)
report = evaluate_pack(pack, scenario, profile=CODE_GENERATION_PROFILE)
print(report.weighted_score, report.missing_coverage, report.findings)
```

## The five dimensions

All dimensions are deterministic and return values in `[0, 1]`. Higher is better.

| Dimension | Measures | Formula (simplified) |
|---|---|---|
| `completeness` | How many required keywords landed in the pack | `hits / len(required_coverage)` (case-insensitive substring match across item excerpts). Empty `required_coverage` → 1.0. |
| `relevance` | Mean strategy-assigned relevance of included items | `mean(item.relevance_score)` clamped to `[0, 1]`. Empty pack → 0.0. |
| `noise` | How much of the pack is on-domain | `1 - (mismatched / scored)` where "scored" is items with a tagged domain and "mismatched" is items whose domain is neither the scenario's nor `"all"`. Untagged items are excluded from both sides. No scenario domain → 1.0. |
| `breadth` | Variety of content types represented | `hits / len(expected_categories)` where a hit is any item with that `content_type`. Empty `expected_categories` → 1.0. |
| `efficiency` | Fraction of pack tokens carrying useful content | `useful_tokens / total_tokens` where "useful" = item excerpt mentions at least one required keyword. Empty `required_coverage` → 1.0. Empty pack → 0.0. |

### Why these five

Each dimension isolates a different failure mode visible only at assembly time:

- Low **completeness** means the pack won't let the agent answer the question at all — a recall problem upstream in retrieval.
- Low **relevance** means strategies returned items but ranked them weakly — a scoring problem.
- Low **noise** means the domain filter or tag coverage is broken — an ingestion / classification problem.
- Low **breadth** means the pack has depth but not variety — classification gaps or under-fetching one of the retrieval tiers.
- Low **efficiency** means budget is being spent on tangentially-relevant items — often a ranking problem downstream of strong recall.

These failure modes produce different follow-up fixes, so collapsing them into a single number hides the signal. The per-dimension breakdown is the point; the weighted aggregate is a convenience for trending and gating.

## Built-in profiles

Profiles are named weight sets applied to the per-dimension scores when computing `weighted_score`. Weights must sum to 1.0.

| Profile | When to use | Weights |
|---|---|---|
| `code_generation` | Agent is writing SQL, Python, configs — specific task with clear right/wrong | completeness 0.35, relevance 0.25, noise 0.20, breadth 0.10, efficiency 0.10 |
| `domain_context` | Agent needs organizational understanding — "who owns this", "what are the conventions" | completeness 0.20, relevance 0.20, noise 0.15, breadth 0.30, efficiency 0.15 |

Rationale: code generation leans on completeness because missing a single required API call breaks the output. Domain context leans on breadth because the value is in seeing structure, ownership, and conventions side-by-side rather than any single item.

```python
from trellis.retrieve import BUILTIN_PROFILES, EvaluationProfile

my_profile = EvaluationProfile(
    name="investigation",
    weights={
        "completeness": 0.25,
        "relevance": 0.25,
        "noise": 0.10,
        "breadth": 0.25,
        "efficiency": 0.15,
    },
)
```

When the profile omits a dimension that a scorer produced, the dimension still appears in `report.dimensions` but is excluded from the weighted aggregate. When the profile names a dimension that no scorer produced, it's ignored. The weighted aggregate renormalizes over the intersection — a partial profile still returns a defensible number in `[0, 1]`.

## Scenario fixture format (YAML)

The CLI accepts either a top-level list or a `scenarios:` key holding the list:

```yaml
scenarios:
  - name: dedup_sql_generation
    intent: "Generate a deduplication SQL pattern for daily rollup"
    domain: billing
    required_coverage: [dedup, row_number, partition]
    expected_categories: [reference, tutorial]
    seed_entity_ids: []
    metadata:
      use_case: pipeline_gen

  - name: billing_ownership_lookup
    intent: "Who owns the billing fact table and what's the SLA"
    domain: billing
    required_coverage: [owner, sla, fact_billing]
    expected_categories: [reference, metadata]
```

`seed_entity_ids` is currently accepted but not wired through to `PackBuilder.build()` — it exists so scenarios stay stable when entity-seeded retrieval gains a top-level knob (tracked in TODO).

## CLI

```bash
# Score scenarios against a live store; print Rich tables + findings
trellis analyze pack-quality --scenarios ./scenarios.yaml --profile code_generation

# Simple mean across dimensions (no profile)
trellis analyze pack-quality --scenarios ./scenarios.yaml

# Validate YAML parsing only — no store access
trellis analyze pack-quality --scenarios ./scenarios.yaml --no-assemble

# Machine output
trellis analyze pack-quality --scenarios ./scenarios.yaml --format json
```

The colour coding on `weighted_score` uses the same thresholds as other `analyze` commands: green ≥ 0.7, yellow ≥ 0.4, red below.

## Python API

```python
from trellis.retrieve import (
    BreadthScorer,
    CompletenessScorer,
    EvaluationProfile,
    EvaluationScenario,
    QualityDimension,
    QualityReport,
    evaluate_pack,
)
```

### Custom scorers

Any object matching the `QualityDimension` Protocol plugs in:

```python
class SemanticRelevanceScorer:
    name = "semantic_relevance"

    def __init__(self, embedder):
        self._embedder = embedder

    def score(self, pack, scenario) -> float:
        ...  # LLM-as-judge, embedding similarity, whatever you like

report = evaluate_pack(
    pack,
    scenario,
    dimensions=[CompletenessScorer(), SemanticRelevanceScorer(embedder)],
)
```

The five built-in scorers are pure functions of the pack + scenario — no I/O, no store access, no LLM calls. Custom scorers that hit external services should handle their own failure modes; `evaluate_pack` does not wrap them in try/except.

## Optional live wiring

`PackBuilder` accepts an `evaluator` callable that runs post-assembly. When supplied, the callable receives the freshly-assembled `Pack` and returns a `QualityReport` (or `None` to skip). The report is attached under `pack.metadata["quality_report"]`.

```python
from trellis.retrieve import PackBuilder, evaluate_pack, EvaluationScenario

def score_pack(pack):
    scenario = _scenario_for(pack.agent_id, pack.intent)  # your lookup
    if scenario is None:
        return None
    return evaluate_pack(pack, scenario)

builder = PackBuilder(strategies=[...], evaluator=score_pack)
pack = builder.build(intent="...", agent_id="worker-a")
pack.metadata["quality_report"]  # dict form of QualityReport, or absent
```

Guarantees:

- **Zero behavior change when unset.** `evaluator=None` (the default) means no metadata mutation, no extra work, no events.
- **Fail-soft.** Exceptions inside the evaluator are logged at error level and swallowed — evaluation must never block pack assembly. The pack is returned without a `quality_report`.
- **Per-pack opt-out.** Returning `None` from the callable leaves `pack.metadata` untouched, so consumers can score selectively (e.g., only when a scenario is registered for this agent).
- **Event emission is automatic when an `event_log` is configured.** When the evaluator returns a report and the `PackBuilder` was constructed with an `event_log`, a `PACK_QUALITY_SCORED` event fires with `pack_id` as the join key to `PACK_ASSEMBLED` and `FEEDBACK_RECORDED`. Event emission is also fail-soft — an event-log failure will not break pack assembly.

Consumers own scenario resolution. Likely strategies:

- Lookup table keyed by `(agent_id, skill_id)` or `(agent_id, intent)`.
- YAML fixture loaded at startup.
- A registered callable that derives a scenario from the intent + store state.

## Relationship to context effectiveness

| | Pack Quality Evaluation | Context Effectiveness |
|---|---|---|
| **Question answered** | "Does this pack look right for what the agent needs?" | "Did packs containing this item correlate with task success?" |
| **When it runs** | Assembly time — before the agent executes | Runtime — after `FEEDBACK_RECORDED` events arrive |
| **Data source** | The pack itself + a declared scenario | `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` events joined over time |
| **Output** | `QualityReport` with 5 dimension scores | `EffectivenessReport` with per-item success rates + noise candidates |
| **Mutates state?** | No (pure scoring) | Yes — `apply_noise_tags()` writes `signal_quality="noise"` to low-performers |
| **CLI command** | `trellis analyze pack-quality` | `trellis analyze context-effectiveness`, `apply-noise-tags` |
| **Module** | [`trellis.retrieve.evaluate`](../../src/trellis/retrieve/evaluate.py) | [`trellis.retrieve.effectiveness`](../../src/trellis/retrieve/effectiveness.py) |

They meet at the learning loop through **dimension predictiveness validation** (below) — the first point where the two independent signals (pack-quality scores and feedback outcomes) join to answer "which dimensions actually predict task success?"

## Dimension predictiveness validation

Once a `PackBuilder` evaluator is wired and `FEEDBACK_RECORDED` events are arriving, this CLI joins the two streams and reports per-dimension correlation:

```bash
trellis analyze dimension-predictiveness --days 30
```

Under the hood: joins `PACK_QUALITY_SCORED` events with `FEEDBACK_RECORDED` by `pack_id`, computes the Pearson correlation between each dimension score and the binary success indicator (mathematically equivalent to the point-biserial correlation), and classifies each dimension:

| Classification | Threshold | Meaning |
|---|---|---|
| `strong` | `|r| ≥ 0.5` | Dimension robustly separates success from failure. Keep or boost its weight. |
| `moderate` | `|r| ≥ 0.3` | Dimension has real signal but is not dominant. |
| `weak` | `|r| ≥ 0.1` | Dimension moves with outcomes but weakly. |
| `noise` | `|r| < 0.1` | Dimension does not distinguish success from failure at this sample size. Candidate for weight reduction. |
| `insufficient_data` | `n < 20` or undefined | Too few samples, or zero variance in one variable (constant dimension or all-success / all-failure). |

The `weighted_score` (the output of the author-set profile weights) is reported as a separate row so you can see whether the aggregate does better or worse than individual dimensions.

```python
from trellis.retrieve import analyze_dimension_predictiveness

report = analyze_dimension_predictiveness(event_log, days=30)
for dim in report.dimensions:
    print(dim.dimension, dim.correlation, dim.signal_classification)
```

The report is **read-only** — it never mutates profiles, scorers, or classifier state. Auto-calibration of profile weights from these correlations (e.g., reducing weight on `noise`-classified dimensions) is deliberately a separate step tracked in [TODO.md](../../TODO.md) under *Pack Quality P3 — Feedback-driven dimension calibration*. The reason for the split: automatic weight tuning without operator review can drive the scorer into a local minimum that looks good by current outcomes but masks a regression. The separation lets you inspect the signal before acting on it.

### When the report is unusable

- **Zero events** — no evaluator is wired on any `PackBuilder`. Wire one per the section above.
- **Zero matched feedback** — evaluators are wired but `record_feedback` isn't being called with `pack_id`. Check the MCP / SDK feedback path.
- **All `insufficient_data`** — sample count is below the 20-pack threshold, or a dimension is constant across the window. Run longer or widen the scenario mix.
- **All `noise`** — either genuinely none of the dimensions correlate (revisit your scoring design), or the success signal itself is too noisy (check the rating threshold).

## What this does **not** do (yet)

- **No profile-weight auto-calibration** — weights are author-set. The `dimension-predictiveness` CLI surfaces the signal; acting on it automatically is P3 work, deliberately gated behind a review step.
- **No event-log replay mode in the `pack-quality` CLI** — today that command only assembles fresh packs; scoring historical packs from `PACK_ASSEMBLED` events needs a scenario↔pack_id join format that isn't yet defined.
- **No sectioned-pack evaluation** — `evaluate_pack` scores a flat `Pack`. Scoring a `SectionedPack` per-section is a small extension but needs a scenario shape that declares per-section expectations.
- **No per-scenario predictiveness breakdown** — correlations today aggregate across scenarios. A per-scenario view is a natural next step once scenario volume justifies it.

These are deferred by design, not oversight. See [`TODO.md`](../../TODO.md) → *Pack Quality Evaluation Framework* for the full phase plan.
