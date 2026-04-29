# Plan: Evaluation strategy

**Status:** active 2026-04-26
**Owner:** rotating
**Self-contained:** yes — read this top to bottom; you do not need any prior conversation context.

## 1. Premise — what this plan is for

Trellis is unit-test green across all backends and live-tested against
AuraDB Free + Neon Postgres. The Neo4j hardening plan
([`plan-neo4j-hardening.md`](./plan-neo4j-hardening.md)) defines a
production-readiness checklist; only the two items the eval harness
actually needs (`StoreRegistry` context-manager protocol, vector-index
ONLINE-wait) have landed on main — the rest are deliberately
deferred until a real workload signal justifies them. See TODO.md
"Deferred from the Neo4j hardening series" for the full list and what
each defers.

What we don't have: **evidence of how the system behaves on real-shaped
workloads**. The next step is to *generate* that signal — build
evaluation infrastructure that runs the system against realistic data
and tells us:

* What functionality is actually working end-to-end vs only in unit tests?
* Where does retrieval quality degrade?
* Which backend behaves differently from the others on the same input?
* What breaks first as we scale node count, edge density, vector volume?

The point is **decisions, not metrics**. Each evaluation run should make
one or more deferred items concrete: either "ship this now" or "still
fine to wait."

## 2. State of the project at hand-off

### What's live and tested
* All five storage planes wired with multi-backend support (SQLite default; Postgres / pgvector / LanceDB / Neo4j optional).
* Neo4j hardening: Phase 1.2 (`StoreRegistry` context-manager protocol) and Phase 1.4 (vector-index ONLINE-wait after CREATE) landed. The remaining Phase 1 + 2 items (driver lifecycle / pool sharing, opt-in connectivity check, onboarding docs, recommended-config.yaml, `trellis admin migrate-graph`) are deferred until a real workload signal justifies them — see TODO.md "Deferred from the Neo4j hardening series".
* Provisioned: Neo4j AuraDB Free (`cfc3411f.databases.neo4j.io`), Neon Postgres free tier (`ep-lively-sun-an9d71ul...`). Credentials in `.env` (gitignored).
* Test suite: green with all extras + `.env` loaded.

### Existing evaluation surface (what NOT to rebuild)
* `src/trellis/retrieve/evaluate.py` — `EvaluationScenario`, `EvaluationProfile`, `evaluate_pack()`, five-dimension scoring (coverage, relevance, density, diversity, freshness).
* Two pre-built profiles: `CODE_GENERATION_PROFILE`, `RESEARCH_PROFILE`.
* CLI: `trellis analyze pack-quality --scenarios ./scenarios.yaml --profile <name>`.
* Companion telemetry: `analyze_pack_telemetry`, `analyze_pack_sections`, `analyze_advisory_effectiveness` for runtime signals.
* Documented at [`docs/agent-guide/pack-quality-evaluation.md`](../agent-guide/pack-quality-evaluation.md).
* What's missing: persistent **scenario corpus + dataset infrastructure**, multi-backend equivalence harness, performance baselines, automated regression tracking, end-to-end agent-loop convergence tests.

### Phase 3 items waiting on evaluation signal
From [`plan-neo4j-hardening.md`](./plan-neo4j-hardening.md) §5 plus the "exploratory items" section of [`TODO.md`](../../TODO.md):

| Phase 3 item | Signal that would unblock it |
|---|---|
| HNSW vector-index `M` / `efConstruction` tuning | Recall or latency complaint on real query workload |
| `upsert_node` UNWIND-based bulk path | N>1 ingest pattern observed |
| EXPLAIN-validated query plan baseline | Slow query reported, OR populated graph >100K nodes |
| Vector DSL Phase 4 (canonical translation layer C.1) | Vector contract drift surfaces, OR plugin author asks for typed filters |
| Provenance fields as first-class edge columns (B.3) | Policy or retrieval consumer wants to gate on these |
| Graph compaction automation | `as_of` query latency degradation observed |
| Tag filter OR / negation (Gap 3.3) | Retrieval use-case with actual OR-shaped queries |
| Importance-score temporal decay (Gap 3.5) | Stale-score complaint on long-running deployments |
| `WorkflowEngine` tier escalation | Confidence-gate failures observed in enrichment runs |
| Enrichment event-loop wiring | Sustained enrichment workload with measured trigger pattern |
| Blob TTL / graph compaction automation | Real accumulation rates observed |

## 3. The strategic question — where do eval tests live?

### Three options considered

| | Pros | Cons |
|---|---|---|
| **A. Separate repo** (`trellis-eval`) | Clean separation; can pin Trellis versions; can be private even if Trellis goes public; forces public-API hygiene | Dual-repo coordination; slower iteration; setup overhead; discoverability |
| **B. In Trellis, in PyPI package** | Single PR for code + eval; easy discovery; uses internal modules freely | Bloats package shipped to users; risks coupling eval to internals that should stay private |
| **C. In Trellis, EXCLUDED from PyPI package** | Single-repo iteration speed; eval visible to contributors; clean exclusion via `pyproject.toml` build config; can use internal modules deliberately | Eval datasets in repo are heavier than code (mitigated: small fixtures committed, large datasets fetched at runtime) |

### Recommendation: **Option C — in-repo `eval/` excluded from package builds**

POC stage is the wrong time for dual-repo coordination overhead. We don't have a paying customer or external eval consumers; we need fast iteration between the code and the thing measuring it. Single-repo with build-time exclusion gives us that, while still keeping eval out of what users `pip install`.

**The exclusion mechanism**: `pyproject.toml`'s `[tool.hatch.build.targets.wheel]` and `sdist` blocks already use `packages = ["src/trellis", "src/trellis_cli", "src/trellis_api", "src/trellis_sdk", "src/trellis_workers"]`. Adding `eval/` at the repo root (not under `src/`) keeps it out of the package by construction.

**Datasets**: small fixtures (a few KB to a few MB, hand-curated) commit to the repo under `eval/datasets/`. Larger datasets either generated synthetically by code in `eval/generators/` or fetched at runtime from a CDN / S3 bucket. Never ship multi-MB binary corpora in git.

**When to revisit**: extract to a separate repo if (a) a design partner wants to run eval independently against their own Trellis fork, OR (b) eval surface grows so large it dominates repo size / CI time, OR (c) we want public eval methodology while Trellis source stays private. None of these is true today.

## 4. Architecture

### Directory layout

```
eval/
├── README.md                    # entry point; what each subdir is for
├── runner.py                    # CLI: orchestrate scenario runs, write reports
├── conftest.py                  # shared pytest fixtures for scenarios
├── scenarios/                   # named scenarios; one subpackage per scenario
│   ├── multi_backend_equivalence/
│   ├── synthetic_traces/
│   ├── retrieval_quality_regression/
│   └── populated_graph_performance/
├── datasets/                    # small committed fixtures (≤ a few MB total)
│   ├── README.md                # what each dataset is + provenance
│   └── *.json / *.yaml / *.csv
├── generators/                  # synthetic data generators (no committed output)
│   ├── trace_generator.py
│   └── graph_generator.py
├── metrics/                     # per-scenario metric definitions
│   └── *.py
└── reports/                     # gitignored — runtime output
    └── .gitkeep
```

### Build-time exclusion

In `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/trellis", ...]   # unchanged — eval/ never listed

[tool.hatch.build.targets.sdist]
exclude = [".env*", ".github", "docs", "tests", "eval", ...]
# add "eval" to the existing exclude list so source distributions also
# omit it
```

Add `eval/reports/` to `.gitignore`.

### Hand-off between scenarios + the runner

* Each scenario is a Python package under `eval/scenarios/<name>/` with a `scenario.py` exposing a single `run(registry) -> ScenarioReport` function.
* `ScenarioReport` is a dataclass with: name, status (pass/fail/regress), metrics dict, findings list, decision recommendation (which Phase 3 item this updates).
* `eval/runner.py` orchestrates: discovers scenarios, builds a registry per the requested config (local / cloud / postgres-only — pulls from `recommended-config.yaml`), runs each scenario, writes a JSON + markdown report.
* `tests/` contains a smoke test that runs the smallest scenario against in-memory SQLite, just to keep the eval framework from rotting between releases. The smoke test exercises the framework, not the eval semantics.

### Reusing existing surface

* `evaluate_pack()` + scenario / profile / dimension types in `src/trellis/retrieve/evaluate.py` are the building blocks. Eval scenarios call into these.
* `trellis demo load` already produces a small graph — the multi-backend equivalence scenario can use it as a starting fixture.
* The contract test suites (`tests/unit/stores/contracts/`) prove individual backends meet the ABC. Eval scenarios prove they behave equivalently *together*.

## 5. First scenarios to build

Order is intentional: each scenario unblocks one or more Phase 3 items, and earlier scenarios validate infrastructure later scenarios depend on.

### 5.1 Multi-backend equivalence (`eval/scenarios/multi_backend_equivalence/`)

**Goal**: same input → SQLite, Postgres, Neo4j → assert outputs match within tolerance.

**Mechanism**:
1. Generate a deterministic mid-size graph (~1K nodes, ~5K edges, ~200 aliases) via a synthetic generator.
2. Ingest it into all three backends via the same `MutationExecutor` calls.
3. Run a fixed set of read operations: `query` by type, `get_subgraph` from seed, `execute_node_query` with `in` filters, vector similarity search.
4. Diff results across backends. Report any that differ in node IDs returned, ordering, or properties.

**Decision unblocks**: confirms (or surfaces a bug in) the canonical DSL Phase 2 compilers across all three backends. Validates that the hardening plan's "blessed Neo4j" claim doesn't hide drift from Postgres-alternative users.

**Estimate**: ~400 lines (scenario + generator + assertions). 1 day.

### 5.2 Synthetic traces end-to-end (`eval/scenarios/synthetic_traces/`)

**Goal**: ingest a corpus of synthetic agent traces, build a graph, retrieve packs for typical follow-up queries, score them.

**Mechanism**:
1. Generator produces 100-1000 synthetic traces in three "domains" (software engineering, data pipeline ops, customer support) with deterministic structure but varied content.
2. Each trace has known intent / entities / outcome — ground truth labels.
3. Ingest through `MutationExecutor`, build the graph, populate vectors.
4. Run a fixed set of follow-up queries (`get_context`, `get_objective_context`, `get_task_context`).
5. Score each returned pack via `evaluate_pack()` against the scenario's ground truth (what entities SHOULD have been in the pack given the query intent).
6. Track per-dimension scores over time; alert on regression beyond a threshold.

**Decision unblocks**: retrieval quality regression detection. Tells us whether a code change to ranking, recency decay, or pack budget makes packs measurably worse.

**Estimate**: ~600 lines. 2 days.

### 5.3 Populated-graph performance baseline (`eval/scenarios/populated_graph_performance/`)

**Goal**: measure latency + recall + accuracy on a populated graph (>10K nodes) for each backend.

**Mechanism**:
1. Generator produces a 10-50K node graph with realistic structure (long-tail node types, varied edge density, ~20% nodes have vectors).
2. Load into each backend.
3. Run a fixed query mix: 100 entity lookups, 50 subgraph traversals at depth 2, 50 vector top-k searches, 20 multi-filter queries.
4. Record p50 / p95 / p99 latency per query type per backend.
5. For vector queries, also record recall@10 against a brute-force baseline (cosine over all vectors, no index).
6. Run `EXPLAIN` (Postgres) or `PROFILE` (Neo4j) on the slowest queries and capture the plans.

**Decision unblocks**:
* HNSW `M` / `efConstruction` tuning — if recall < 0.95 or vector latency exceeds budget, tune
* `upsert_node` UNWIND bulk path — measure ingest throughput; if <100 nodes/sec, build it
* EXPLAIN-validated query plan baseline — captured here; ongoing diff against future runs catches plan regressions
* Graph compaction automation — measure `as_of` query latency vs closed-row count after a mutation-heavy workload; threshold for automation becomes empirical

**Estimate**: ~700 lines. 2-3 days.

### 5.4 End-to-end agent loop convergence (`eval/scenarios/agent_loop_convergence/`)

**Goal**: simulate an agent using Trellis over time — does the system converge on better packs as feedback accumulates?

**Mechanism** (as implemented):
1. Synthetic "agent" runs N rounds of: ask for context → use it (deterministic success/failure based on whether ground-truth entities were in the pack) → record feedback. Round-robin across the three domain queries from scenario 5.2's corpus.
2. Each round builds a pack via `PackBuilder` with `tag_filters={}` so PackBuilder's default `signal_quality` filter is engaged — without this, noise tags applied by the effectiveness loop would have no read-time effect.
3. Initial documents are seeded with `signal_quality="standard"`. Distractor docs (per-domain, two each, keywords overlap with the query intent but contain no `required_coverage` entity) live alongside real entity docs so the noise loop has something to demote.
4. Every `feedback_batch_size` rounds: `run_effectiveness_feedback` (tags noise items) + `AdvisoryGenerator.generate()` + `run_advisory_fitness_loop` (suppress / restore by fitness).
5. Aggregate per-round metrics; convergence delta = mean weighted score on last quarter minus first quarter.

**Decision unblocks**:
* Advisory fitness loop validation — confirms or refutes the suppression / restoration semantics on a controlled corpus
* `WorkflowEngine` tier escalation gating — if the agent loop produces measurable confidence-gate failures, escalation becomes justified
* Enrichment event-loop wiring — convergence requires sustained enrichment volume; this scenario *generates* that workload

**Measured baselines (SQLite, 30 rounds)**:

| Metric | Pre-fix | Row 1 | Row 2 | Row 3 | Regime shift (post-row-3) |
|---|---|---|---|---|---|
| Mode | default | default | default | default | `regime_shift_round=15`, `advisory_min_sample_size=2` |
| **Status** | regress | pass | pass | **pass** | pass (relaxed gate) |
| `convergence.useful_delta` *(primary gate)* | -0.131 | +0.652 | +0.652 | **+0.571** | -0.095 |
| `convergence.weighted_delta` *(informational)* | -0.136 | +0.107 | +0.107 | **+0.097** | +0.098 |
| `round_useful_fraction_overall` | 0.214 | 0.539 | 0.539 | **0.647** | varies |
| `round_success_rate` | 0.333 | 0.333 | 0.333 | **1.0** | 0.5 |
| `per_domain.software_engineering.success_rate` | 0.0 | 0.0 | 0.0 | **1.0** | n/a |
| `per_domain.data_pipeline.success_rate` | 1.0 | 1.0 | 1.0 | **1.0** | n/a |
| `per_domain.customer_support.success_rate` | 0.0 | 0.0 | 0.0 | **1.0** | n/a |
| `loops.noise_items_tagged_total` | 53 | 86 | 86 | 100 | varies |
| `loops.advisories_generated_total` | 72 | 54 | 1 | 2 | 1 |
| `loops.advisories_boosted_total` | 0 | 0 | 1 | 10 | 4 |
| `loops.advisories_suppressed_total` | 0 | 0 | 0 | 0 | 0 |
| `loops.advisories_restored_total` | 0 | 0 | 0 | 0 | 0 |
| Wall time | ~1.4s | ~1.4s | ~1.4s | ~1.2s | ~1.2s |

The **primary convergence gate switched from `weighted_delta` to `useful_delta`** as part of the row-3 fix. `useful_delta` directly tracks the fraction of served items the agent referenced — exactly what the noise + advisory loops aim to improve. `weighted_delta` (from `evaluate_pack` against `domain_context`) weights breadth at 0.30, so a successful noise-tagging pass that trims non-referenced items can correctly drive it negative even when the loop is working as intended.

**Findings — first baseline (pre-fix)**:

1. **Convergence is negative under naive feedback.** ~~The per-item success_rate that `analyze_effectiveness` uses to flag noise candidates does not distinguish "this item is bad" from "this item was in a pack that failed for unrelated reasons".~~ **Fixed 2026-04-28** by switching `analyze_effectiveness` to use `helpful_item_ids` (the agent's positive per-item label) when the corpus carries it, with a `usage_rate = referenced / appearances` metric. Back-compat fallback to the old success-rate heuristic when `helpful_item_ids` is absent. See `src/trellis/retrieve/effectiveness.py::_score_items` and `tests/unit/retrieve/test_effectiveness.py::TestUsageRateNoiseFlagging`.
2. **Advisory suppression never fires.** ~~54 advisories generated across 6 fitness passes, still 0 suppressed after the row-1 fix.~~ **Wiring corrected 2026-04-28** by (a) passing `advisory_store` into `PackBuilder` so attached advisories show up in `PACK_ASSEMBLED.advisory_ids`, and (b) generating advisories on only the first periodic pass so IDs stay stable for presentations to accumulate against. Net effect: 1 advisory generated, 1 boost — wiring is end-to-end correct. Production gates (`_ADVISORY_MIN_PRESENTATIONS = 3`, `_MIN_SAMPLE_SIZE = 5`) **were not changed**; the original symptom was driven by the scenario, not by production thresholds being too high. The row-1 noise-tagging fix simultaneously cleaned the corpus enough that no advisory deserves suppression on this small synthetic workload — the suppression branch is unit-tested (`test_suppresses_failing_advisory`, `TestAdvisorySuppressionReversibility`) but unexercised end-to-end in 5.4. Triggering it organically requires a regime-shift demonstration mode (§5.5.2 row 1).
3. ~~**Per-domain skew.**~~ **Fixed 2026-04-28** as §5.5.1 row 3. The diagnosis "FTS5 tokenization of underscored names" was wrong: actual root causes were (a) some `required_coverage` entities never sampled into traces under the seeded RNG, and (b) entity docs sharing no token overlap with the domain query (`auth_module`'s doc had no overlap with "How do we structure session token validation?"). Two-part fix: `_entity_subset_with_anchor` guarantees required entities show up in the first N traces per domain; `DOMAIN_TEMPLATES.query_intent` strings rewritten to mention every required entity by name. All three domains now at `success_rate=1.0`.

**What the fix changed**:

* Useful fraction more than doubled (0.214 → 0.539): the agent now sees packs where the items it actually references are no longer crowded out by distractors that earlier passes mistakenly tagged as required.
* Coverage_mean nearly doubled (0.278 → 0.533): required entities stay in the candidate pool instead of being noise-tagged out.
* Per-domain weighted scores normalised (software_engineering 0.278 → 0.851; customer_support 0.677 → 0.829; data_pipeline 0.816 → 0.932).
* The advisory generator emitted 54 advisories instead of 72 — a smaller, cleaner set because the per-domain corpus is more coherent under the new noise tagging.

**Estimate**: ~700 lines (scenario + tests + README). Landed in one PR on 2026-04-28.

### 5.5 Open follow-ups + coverage gaps

This section is the live punch list of work the existing scenarios have
either *surfaced as a finding* (suggested-tasks, below) or *don't yet
exercise* (coverage gaps). Each entry names what's needed to act on it
in a fresh session — cloud-session or local — so a worker picking it
up can size the setup before starting.

#### 5.5.1 Suggested follow-up tasks (surfaced by 5.1-5.4 baseline runs)

| Title | Source signal | Files in scope | Live backends needed? | Verification |
|---|---|---|---|---|
| ~~Per-item noise tagging false positives~~ **(landed 2026-04-28)** | 5.4 baseline: weighted-delta -0.136, 53 items wrongly tagged noise | `src/trellis/retrieve/effectiveness.py::_score_items` (extracted) + `analyze_effectiveness` switched to `helpful_item_ids`-driven `usage_rate`; `tests/unit/retrieve/test_effectiveness.py::TestUsageRateNoiseFlagging` (4 new tests) | n/a | Done. Re-run shows `convergence.weighted_delta` = **+0.107** (was -0.136); `useful_delta` = **+0.652** (was -0.131); 47 scoped tests + 655 broader tests pass |
| ~~Advisory suppression never fires at small corpora~~ **(landed 2026-04-28)** | 5.4 baseline: 72 advisories generated, 0 suppressed | Scenario wiring: `eval/scenarios/agent_loop_convergence/scenario.py` — `PackBuilder(..., advisory_store=...)` + generate-once gating; new tests `test_advisory_wiring_attaches_advisories_to_packs` + `test_advisories_generated_only_once_across_periodic_passes`. Production code unchanged. | n/a | Done. Wiring verified end-to-end: 1 advisory generated, 1 boost (presentations accumulate, fitness loop scores it). 0 suppressions on this corpus is honest — see §5.4 finding 2 + §5.5.2 row 1. |
| ~~FTS5 tokenization skew~~ → **per-domain retrieval skew (corpus, not tokenizer)** **(landed 2026-04-28)** | SE and CS at 0% success rate while DP at 100% | `eval/generators/trace_generator.py` — `_entity_subset_with_anchor` guarantees required entities are sampled; `query_intent` strings rewritten to mention every required entity by name. Convergence gate pivoted from `weighted_delta` to `useful_delta` since the row-3 corpus fix combined with `domain_context`'s heavy breadth weight made `weighted_delta` the wrong signal. | n/a | Done. All three domains at `success_rate=1.0`; `useful_delta=+0.571` (climbs 0.43 → 1.0 over 30 rounds). 14/14 scenario tests + 371 broader tests pass. Side effect: corpus is now uniform enough that anti-pattern advisories no longer organically form, so regime-shift mode no longer fires suppressions — see §5.5.2 row 1. |
| Runner UTF-8 encoding (already fixed) | 2026-04-28: `Path.write_text` defaulted to cp1252 on Windows, blew up on `→` | `eval/runner.py::write_report` | n/a | Done in PR landing 5.4 |
| StoreRegistry plane-split silent fallback (already fixed) | Memory note `project_eval_silent_fallback_planesplit` | `src/trellis/stores/registry.py::__init__` | n/a | Landed PR #33 |

#### 5.5.2 Coverage gaps (dimensions no scenario exercises today)

In priority order — earlier gaps gate later ones. Pick from the top.

| # | Gap | Why it matters | Cloud-session prereqs | Recommended next |
|---|---|---|---|---|
| 1 | **Suppression + restoration unexercised end-to-end in scenario 5.4 (post-row-3).** Initial regime-shift demo on the pre-row-3 corpus suppressed 3 anti-pattern advisories. The §5.5.1 row 3 fix levels per-domain success rates to 1.0 — which makes the corpus uniform enough that anti-pattern advisories no longer naturally form, so regime-shift no longer fires suppression either. The suppression branch is unit-tested (`test_suppresses_failing_advisory`). The restoration branch is unit-tested (`test_auto_restore_when_evidence_recovers`) but unreachable end-to-end for the architectural reason in [TODO.md "Advisory restoration unreachable in scenario context"](../../TODO.md). | Two coupled gaps: (i) **corpus uniformity vs anti-pattern advisories** — to demo suppression organically you need pre-shift differentiation (some packs failing for entity-correlated reasons); the §5.5.1 row 3 fix removed that. (ii) **suppressed advisories leave the delivery set** — see TODO entry. | None — same scenario, same backend | Three options: (A) accept that scenario 5.4 demonstrates *convergence* but the suppression / restoration branches stay unit-test-only; (B) add a third opt-in mode — "deliberate failure injection" — that randomly fails some pre-shift rounds so anti-pattern advisories can form; (C) split into two scenarios. **Recommended: A**. The unit tests cover the branches; the scenario covers convergence. Don't engineer corpus complexity to satisfy a metric. |
| 2 | ~~**JSONL → `learning.scoring` promote half.**~~ **EventLog promote half landed 2026-04-29.** Pre-fix: `learning.scoring` had a complete `analyze_learning_observations` → `prepare_learning_promotions` chain producing precedent entity + edge payloads, with **zero callers in the source tree** — only synthetic unit-test fixtures fed it. The dual-loop's *demote* half was wired end-to-end; the *promote* half was implementation-only. | New module [`src/trellis/learning/observations.py`](../../src/trellis/learning/observations.py) — `build_learning_observations_from_event_log` joins `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` events on `pack_id` and produces the observation shape `analyze_learning_observations` consumes. New test file [`tests/unit/learning/test_observations.py`](../../tests/unit/learning/test_observations.py) covers (a) the bridge in isolation (5 tests) and (b) the full chain feedback events → analyze → write artifacts → operator approval → entity / edge payload (4 tests, including precedent + guidance + noise outcomes). 10/10 pass; 535 broader tests pass. | None — SQLite + tmpdir only. | Done. The file-only JSONL promote variant (read `pack_feedback.jsonl` without an EventLog) is **deferred** — `PackFeedback` carries no per-item `item_type` / `source_strategy`, so a JSONL-only bridge would need a sibling `pack_assembly.jsonl` or a schema extension to be self-sufficient. That's an ADR-shaped piece of work; the EventLog bridge is the authoritative path per CLAUDE.md's dual-loop framing. |
| 3 | ~~**Multi-backend feedback loop.**~~ **(landed 2026-04-29 as scenario 5.5)** Pre-fix: scenarios 5.1-5.4 covered cross-backend retrieval and single-backend convergence, but no scenario exercised the EventLog-driven effectiveness + advisory loops on Postgres or Neo4j EventLogs. Different `get_events` ordering / limit semantics across backends could have silently changed advisory output. | New scenario [`eval/scenarios/multi_backend_feedback/`](../../eval/scenarios/multi_backend_feedback/scenario.py) runs the same convergence loop scenario 5.4 measures against three handles (sqlite / postgres / neo4j_op_postgres) and diffs the loop counters + convergence deltas. `vector_store` + `document_store` pinned to SQLite across all handles so any drift is attributable to the feedback path under test (event_log + trace + graph). 9 unit tests (SQLite-only) + live 3-handle run on Neon Postgres + AuraDB Free showed **identical loop output** across all three: `noise_items_tagged_total=41`, `advisories_generated_total=1`, `advisories_boosted_total=2`, `convergence.useful_delta=0.5417` — diff is exactly 0.0 on every counter. Wall time: ~5.5 min for the full 3-backend run on free-tier infra. | Done. The `EventLog.get_events` query layer is empirically equivalent across backends — `analyze_effectiveness`, `AdvisoryGenerator`, and `run_advisory_fitness_loop` give the same answers regardless of which backend an operator configured. Closes the §5.5.2 open queue. |
| 4 | **Other retrieval surfaces.** No vector strategy, no reranker, no `SemanticDedupConfig`, no `SectionedPack`, no MCP `get_objective_context` / `get_task_context` paths. | Each is a separate code path with its own failure modes; today the loop only proves the keyword path. | None for vector/dedup/sectioned (SQLite has all three); none for MCP (in-process). LLM key if a reranker uses an embedder. | Add as **opt-in kwargs to 5.4** (`strategy="keyword|vector|both"`, `enable_dedup=...`) rather than building 5.5; keeps the scenario count flat. |
| 5 | **Real extraction tier.** Scenarios fake entity extraction with direct upserts; `JSONRulesExtractor` / `OpenLineageExtractor` flowing through `MutationExecutor` is untested. | Convergence claims rely on the *whole* pipeline; today we test feedback on a hand-populated graph. | None — extractors are pure; SQLite only. | Wait until extraction tier Phase 2 lands more first-class extractors (see TODO.md "Tiered Extraction Pipeline — Phase 2 Plan"); then a 5.6 plugs them in upstream of 5.4. |
| 6 | **Drift + ParameterRegistry.** No `ADVISORY_DRIFT_DETECTED`, no parameter-tuner-driven threshold updates. | Drift detection (Gap 2.4) is wired but never fires in our scenarios (windows too short); parameter tuning is wired but never invoked. | None — pure compute. | Lowest priority; defer until a real workload signals tuning is needed. |

Every row above is intentionally *not* yet a scenario or PR. Ordering
discipline: **don't open multiple of these in parallel** — gaps #1 and
#2 of §5.5.1 should land sequentially against the 5.4 baseline so each
fix's contribution to `convergence.weighted_delta` is attributable.

#### 5.5.3 Cross-cutting reviews (worth a separate pass)

These are not scenario or fix work — they're *audits* of shapes that
have grown organically and are due for a coherence check. Running these
reviews now catches drift before any of §5.5.1 / §5.5.2's downstream
consumers compound the misalignment.

| Review | Scope | When |
|---|---|---|
| **Agent feedback + context notes alignment.** Validate that `PackFeedback` (`src/trellis/feedback/models.py`), `FEEDBACK_RECORDED` event payload (`PackFeedback.to_event_payload`), `OutcomeEvent` bridge (`src/trellis/ops`), and any context-notes / advisory metadata fields are (a) shape-aligned across the EventLog vs JSONL paths, (b) flexible enough that a richer agent (partial credit, per-item ratings, follow-up references) doesn't need a schema break, (c) covered by the documented `extra="forbid"` rule per CLAUDE.md. Today the loop is functional but uses a coarse `outcome ∈ {success, failure, partial, unknown}` and a flat `helpful_item_ids` list — those served the convergence baseline but may pinch later. | All `feedback/`, `ops/outcomes.py`, `schemas/advisory.py`, `schemas/pack.py` advisory + metadata fields | After §5.5.1 lands fully — defer until usage data shows which fields are actually load-bearing. Worth a dedicated 1-day audit + ADR addendum, not a PR mixed in with this work. |
| **Item-type semantics + summary-generation path.** `PackItem.item_type` is free-form; the schema docstring lists `"trace, evidence, precedent, entity"` but strategies only stamp three concrete values: `"document"` (KeywordSearch), `"vector"` (SemanticSearch), `"entity"` (GraphSearch — traverses edges but returns destination nodes only, never edges/links). Summaries are docs tagged `metadata.content_type="entity_summary"`, riding the same ranking path as full docs. Precedents become `item_type="entity"` once retrieved, not `"precedent"`, even though `tier_mapping.py:30` heuristics expect `{"precedent", "owner", "team"}` and line 42 expects `{"trace"}` — those branches are unreachable through standard strategies. Net effect: `relevance_score` is the only ranking knob, and only `BreadthScorer` reads `expected_categories` (against `content_type`, not `item_type`). A pack with 5 redundant precedents scores identically to a pack with 1 summary + 1 precedent + 3 full docs at equal total relevance. **Tracked in [TODO.md "Item-type semantics + summary-generation path — ADR-shaped lift"](../../TODO.md)** — this is genuinely large work: needs a corpus that contains both full docs and pre-generated summaries, a sanctioned summary-generation path (LLM worker / deterministic extractive / both), aligned tier-map heuristics, and a type-aware density dimension in `evaluate_pack`. Defer until §5.5.2 row 2 (JSONL promote) lands so the typed shapes have a producer. | All `retrieve/` plus `schemas/precedent.py` + `trellis_workers/learning/scoring.py` — see TODO entry for full file list and validation plan. | Picked up by the TODO entry. ADR addendum + likely 5.5 / 5.6 follow-up scenario, not a one-PR change. |

## 6. What each scenario costs to run

| Scenario | Backends needed | Approx wall time | External resources |
|---|---|---|---|
| 5.1 Multi-backend equivalence | SQLite + Postgres + Neo4j | 1-2 min | `.env` for live PG/Neo4j |
| 5.2 Synthetic traces e2e | Single backend (default SQLite; optional sweep) | 2-5 min | None for default; LLM API key if running enrichment scenarios |
| 5.3 Populated-graph performance | One backend per run; expected to run all three sequentially | 10-30 min per backend | `.env`; fast disk for SQLite, real Postgres / AuraDB for the others |
| 5.4 Agent loop convergence | Single backend (SQLite default) | ~1.4s for N=30 rounds (measured 2026-04-28); ~5s for N=100 | None for the SQLite default. Optional LLM key only when §5.5.2 row 4's reranker variant is enabled. |

CI: only the multi-backend equivalence smoke (with SQLite-only) runs in CI by default. The full suite is a manual / scheduled run via `trellis eval run --scenario all` or a GitHub Actions workflow with a `workflow_dispatch` trigger.

## 7. Execution order

1. **Land the eval skeleton** (~1 day): `eval/` directory, `runner.py`, `pyproject.toml` exclusion, `tests/` smoke test that exercises a no-op scenario, doc page in [`eval.md`](./eval.md) cross-linked from this plan.
2. **Scenario 5.1 multi-backend equivalence** (1 day): smallest, highest immediate value, validates infrastructure.
3. **Scenario 5.2 synthetic traces e2e** (2 days): proves the retrieval-quality regression loop works.
4. **Scenario 5.3 populated-graph performance** (2-3 days): generates the workload signal that unblocks the most Phase 3 items.
5. **Scenario 5.4 agent loop convergence** (3 days): largest; depends on 5.2's generator. Defer until 5.3's signal has informed Phase 3 priorities.

After 5.1-5.3 are landed, **stop and re-read the Phase 3 deferred list** in [`plan-neo4j-hardening.md`](./plan-neo4j-hardening.md). The signal from these scenarios should make at least 2-3 items concretely actionable. Pick those, do them, then come back for 5.4.

Once 5.4 is landed (done 2026-04-28), the next-action queue is **§5.5,
not new scenarios**:

1. ~~Work §5.5.1 in row order (per-item noise tagging → suppression-gate
   density → tokenization skew).~~ **All three rows landed 2026-04-28.**
   Row 1 (per-item noise tagging via `helpful_item_ids`) flipped
   `convergence.weighted_delta` -0.136 → +0.107. Row 2 (advisory
   wiring) brought 1 advisory + 1 boost. Row 3 (per-domain corpus
   skew) leveled all three domains to `success_rate=1.0` and pivoted
   the convergence gate to `useful_delta` (`+0.571`).
2. **§5.5.2 row 1 closed (option A).** The regime-shift mode landed
   for the suppression demo on the pre-row-3 corpus (3 suppressions),
   but the row-3 fix made the post-fix corpus uniform enough that
   anti-pattern advisories don't naturally form — so suppression /
   restoration stay unit-test-only end-to-end. Don't engineer corpus
   complexity to satisfy a metric; the branches are covered.
3. ~~Pick up **§5.5.2 rows 2+3** (JSONL promote, multi-backend
   feedback) before considering new scenarios.~~ **Both landed
   2026-04-29.** Row 2 via the EventLog promote bridge in
   `src/trellis/learning/observations.py`; the file-only JSONL
   variant is logged as a deferred ADR item in TODO.md. Row 3 via
   new scenario 5.5
   [`eval/scenarios/multi_backend_feedback/`](../../eval/scenarios/multi_backend_feedback/scenario.py)
   — 3-handle live run against Neon + AuraDB Free showed identical
   loop counters across SQLite / Postgres / Neo4j-op-Postgres. The
   §5.5.2 open queue is closed; remaining rows (4-6) fold cleanly
   into 5.4 as opt-in kwargs rather than warranting a 5.6 / 5.7.

### 7.0.1 Live-data revisit — landed 2026-04-29

After 5.4 + 5.5 landed, the project memory committed in-conversation
2026-04-26 to a dedicated revisit pass once a real-data run produced
evidence: simplify code, remove premature abstraction, audit for silent
fallbacks. Findings + fixes:

* **Silent-fallback audit: clean.** Every multi-backend scenario uses
  explicit env-var checks + `try/except` with `logger.warning` +
  `Finding(severity="info", message="X backend skipped")`. The
  plane-split bug class was the only one and landed in PR #33. No new
  fixes needed.
* **Cross-backend equivalence broke at 1K nodes — root cause was stale
  rows, not a backend bug.** First live run of 5.1 at plan-spec size
  (1K nodes / 5K edges / 200 embeddings) reported `fail` because PG
  + Neo4j returned 350 entity-typed rows where SQLite returned 331.
  After wiping PG + AuraDB and re-running, all three backends
  returned exactly 331. The accumulation came from 5.1 + 5.3 having
  no wipe step (unlike 5.5) on a shared test DB.
* **Fix: extracted `eval/_live_wipe.py`** with a single
  `wipe_live_state(registry)` orchestrator that dispatches by store
  type, not handle name — works against 5.1's `"neo4j"` handle, 5.3's
  identical handle, and 5.5's `"neo4j_op_postgres"` handle without
  per-scenario branching. 5.5 refactored to use it; 5.1 and 5.3
  call it before each handle's run. SQLite is a no-op.
* **`embedding_dim` default aligned to contract DIMS=3.** PR #64 added
  fail-fast at PgVectorStore construction when the existing
  `vectors.embedding` dim doesn't match — the live 5.1 run hit it
  immediately because eval default was 16 vs contract suite's 3 in
  the shared Neon DB. Aligned 5.1 + 5.3 + `generate_graph` defaults
  to 3 with cohabit comments. Cosine equivalence at dim=3 still
  surfaces cross-backend drift; vector quality is not what 5.1
  measures.
* **Dead field removed.** `EvalQuery.expected_categories` in
  `trace_generator.py` was defined but never set or read — scenarios
  passed `expected_categories=["entity_summary"]` directly to
  `EvaluationScenario` at score time. Field deleted; scenario calls
  unaffected.
* **Audit-flagged "premature abstraction" candidates not removed.**
  Five helpers (`_round_query`, `_time_call`, `_recall_at_k`,
  `_percentiles`, `QueryMixCounts`) flagged by the survey are
  minor code-smell, not load-bearing. Per POC scope discipline they
  stay until a design partner pushes — removing them now is churn
  without signal.

**Live-run measurements at production sizes (post-fix):**

* 5.1 at 1K/5K/200 against Neon + AuraDB Free: `pass`, wall time
  45s, vector recall 1.0 on both pairs, all id sets match exactly.
  Per-backend ingest: `sqlite=0.8s` / `neo4j=2.0s` / `postgres=34s`
  — Neon free tier RTT dominates the PG bulk ingest path.

### 7.1 Deferred — robust eval-test discipline

Once the four scenarios are functional we want to harden them as a *test
asset*, not just a one-shot measurement: deterministic seeds asserted in
fixtures, baseline metric files committed and diffed across runs,
threshold-based regression gates, retry/flake handling for live-backend
runs, and a scheduled CI workflow that publishes the report artifact.

This is deliberately **not** Phase 1-of-this-plan work. The right
sequence is: build the scenarios → use them manually → notice which ones
are flaky or which thresholds are right → *then* codify the regression
discipline. Codifying it earlier locks in the wrong shape.

Track this as the work to revisit after §7.5 lands; treat it as a Phase 5
of this plan, not a Phase 0.

## 8. What this plan deliberately rejects

* **A separate `trellis-eval` repository** — premature dual-repo overhead with no external eval consumers asking for it.
* **Shipping eval in the PyPI package** — bloats user installs; lets internal-only access patterns leak into eval; harder to take a different dependency posture from Trellis itself.
* **Large datasets committed to git** — generators + small fixtures cover the same surface without bloating clone size.
* **Adopting an external eval framework** (BEIR, MTEB, RAGAS, DeepEval) — too much surface; their abstractions don't match Trellis's pack-builder + advisory + parameter-tuner shape. The existing `evaluate_pack()` framework is small and Trellis-shaped; build on it.
* **Production-grade eval rigor** — randomization seeds, statistical significance harnesses, CI-blocking regression gates. All useful when a paying customer needs proof; premature now. POC scope: human-readable metric reports + reasonable thresholds.
* **Auto-running eval on every PR** — eval is heavyweight (scenario 5.3 alone is 30 min). Run on a schedule (nightly or weekly) and on `workflow_dispatch`, not on push.

## 9. Hand-off protocol for a fresh agent

When picking up this plan in a new session:

1. Read [`CLAUDE.md`](../../CLAUDE.md) for project conventions and hard rules.
2. Read this doc top to bottom.
3. Read [`plan-neo4j-hardening.md`](./plan-neo4j-hardening.md) §5 (Phase 3 deferred items) and §1 (premise) — that's the work this plan exists to unblock.
4. Read [`docs/agent-guide/pack-quality-evaluation.md`](../agent-guide/pack-quality-evaluation.md) — the existing eval surface you'll build on.
5. Skim [`src/trellis/retrieve/evaluate.py`](../../src/trellis/retrieve/evaluate.py) — the actual API.

Before writing code:

* Run the smoke command: `pytest tests/unit/ -q` (expect 2415 passed; live-test counts grow with `.env` loaded).
* Confirm `trellis analyze pack-quality --help` runs (proves the existing eval CLI is wired).

When picking up a scenario:

* The scenario entry in §5 is the contract. If scope is unclear, ask before extending.
* New scenarios go in `eval/scenarios/<name>/` with a `scenario.py` exposing `run(registry) -> ScenarioReport`.
* When a scenario lands, update §5 here with the actual measured numbers + which Phase 3 item became actionable.

Stack discipline (lessons from prior sessions):

* Use `--base <prev-branch>` on chained PRs.
* **Never** use `--delete-branch` on a PR with dependents — GitHub auto-closes the children.
* Each scenario is one PR; let them land independently.

## 10. File inventory expected after Phase 1 of this plan lands

```
eval/                                  # new top-level (excluded from PyPI)
├── README.md
├── runner.py
├── conftest.py
├── scenarios/
│   ├── __init__.py
│   └── _example/
│       └── scenario.py                # no-op placeholder; smoke-tested
├── datasets/
│   └── README.md
├── generators/
│   ├── __init__.py
│   └── README.md
├── metrics/
│   └── __init__.py
└── reports/
    └── .gitkeep                       # gitignored content
docs/design/eval.md                    # operator-facing eval reference
tests/unit/eval/test_runner_smoke.py   # smoke test against _example
pyproject.toml                         # eval/ added to sdist exclude list
.gitignore                             # eval/reports/* added
```

Subsequent scenario PRs add files under `eval/scenarios/<name>/` plus
their datasets and metrics, without touching this skeleton.
