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

**Mechanism**:
1. Synthetic "agent" runs N rounds of: ask for context → use it (deterministic success/failure based on whether ground-truth entities were in the pack) → record feedback.
2. Track per-round metrics: pack quality score, fraction of "useful" items, advisory generation + suppression events, parameter changes from the rule tuner.
3. Plot score over rounds. Should improve monotonically (with noise) if the loop is functioning.

**Decision unblocks**:
* Advisory fitness loop validation — confirms or refutes the suppression / restoration semantics on a controlled corpus
* `WorkflowEngine` tier escalation gating — if the agent loop produces measurable confidence-gate failures, escalation becomes justified
* Enrichment event-loop wiring — convergence requires sustained enrichment volume; this scenario *generates* that workload

**Estimate**: ~800 lines. 3 days.

## 6. What each scenario costs to run

| Scenario | Backends needed | Approx wall time | External resources |
|---|---|---|---|
| 5.1 Multi-backend equivalence | SQLite + Postgres + Neo4j | 1-2 min | `.env` for live PG/Neo4j |
| 5.2 Synthetic traces e2e | Single backend (default SQLite; optional sweep) | 2-5 min | None for default; LLM API key if running enrichment scenarios |
| 5.3 Populated-graph performance | One backend per run; expected to run all three sequentially | 10-30 min per backend | `.env`; fast disk for SQLite, real Postgres / AuraDB for the others |
| 5.4 Agent loop convergence | Single backend | 30-60 min for N=100 rounds | Optional LLM key for the realistic enrichment path |

CI: only the multi-backend equivalence smoke (with SQLite-only) runs in CI by default. The full suite is a manual / scheduled run via `trellis eval run --scenario all` or a GitHub Actions workflow with a `workflow_dispatch` trigger.

## 7. Execution order

1. **Land the eval skeleton** (~1 day): `eval/` directory, `runner.py`, `pyproject.toml` exclusion, `tests/` smoke test that exercises a no-op scenario, doc page in [`eval.md`](./eval.md) cross-linked from this plan.
2. **Scenario 5.1 multi-backend equivalence** (1 day): smallest, highest immediate value, validates infrastructure.
3. **Scenario 5.2 synthetic traces e2e** (2 days): proves the retrieval-quality regression loop works.
4. **Scenario 5.3 populated-graph performance** (2-3 days): generates the workload signal that unblocks the most Phase 3 items.
5. **Scenario 5.4 agent loop convergence** (3 days): largest; depends on 5.2's generator. Defer until 5.3's signal has informed Phase 3 priorities.

After 5.1-5.3 are landed, **stop and re-read the Phase 3 deferred list** in [`plan-neo4j-hardening.md`](./plan-neo4j-hardening.md). The signal from these scenarios should make at least 2-3 items concretely actionable. Pick those, do them, then come back for 5.4.

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
