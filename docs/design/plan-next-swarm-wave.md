# Plan: Next swarm wave — close M-severity backlog + L-severity sweep + 3 architectural follow-ups

**Status:** Proposed 2026-05-16

> *Historical note: references to WorkflowEngine below predate its retirement (2026-05-18); see `docs/research/workflow-engine-disposition.md`.*

**Owner:** swarm-pickable (decomposable into 23 independent units across 5 waves)
**Depends on:** main at `0e9e2de` or later (post 14-PR merge of the program_convergence + C1 cleanup work)
**Estimated wall time:** 4-6 hours across 5 sequential waves with intra-wave parallelism

## 1. Premise

The 14-PR merge wave (PRs #143–#156) shipped the program_convergence eval stack + C1 dead-code cleanup + 5 follow-up PRs. The deep alignment audit before merge surfaced **no H-severity findings** but recorded:

- **5 M-severity items** the per-PR reviews missed
- **6 M-severity items** as Phase 4-5 deferred findings
- **~15 L-severity hygiene items** scattered across TODO.md

Plus three architectural threads were deliberately deferred during the merge wave:

- **Axis C semantic tighten** — requires `advisory_id` provenance on `PackItem.metadata` (didn't exist yet)
- **Item 7 Cohort 2 scoping** — gated on operator review + ADR amendment; the scoping doc itself is autonomous work
- **Real-LLM eval scenario** — TODO.md flagged "no eval scenario exercises a real LLM yet"

This plan ships all of the above.

## 2. POC directives applied

Per [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §2:

- No silent fallbacks; loud on misuse; no half-finished implementations
- Type extensibility preserved (entity types + edge kinds stay open strings)
- New event types get docstrings describing payload contract before they're emitted
- Each unit is independently decidable + shippable; no half-merged stacks

## 3. Wave structure

| Wave | Units | Parallelism | Wall time |
|---|---:|---|---:|
| 1 | A1–A8 (8) | parallel | ~45 min |
| 2 | B1–B6 (6) | parallel after Wave 1 | ~60 min |
| 3 | C1–C3 + E2 + E3-prep (5) | parallel after Wave 2 | ~60 min |
| 4 | D1 + D2 + D3 + E1 (4) | parallel after Wave 3 | ~60 min |
| 5 | E3 (1) | after Wave 4 | ~30 min |
| **Total** | **24** | | **~4-5h** |

Each unit's prompt is self-contained — agent gets full context inline + the base SHA expectation + the Review-Agent Output Contract (patches + `## Deferred Findings` block tagged `[severity:H/M/L]` and `[scope:this-pr/follow-up]`).

## 4. Wave 1 — Independent hygiene + test coverage

All units have non-overlapping file scopes. True parallelism, no merge risk.

### A1 — Phase 5C axis helper test coverage

**Scope:** Add `tests/unit/eval/test_program_regression_suite.py` coverage for the 8 axis helpers not already tested by Phase 5B + `_call_satellite` import/lookup/execute branches.

**Files:** `tests/unit/eval/test_program_regression_suite.py` (extend existing)

**Closes:** Alignment audit [M] — 8 of 9 regression-suite axis helpers lack unit tests.

**Acceptance:** Each of `_assert_axis_b`, `_assert_axis_c`, `_assert_axis_d`, `_assert_axis_e`, `_assert_axis_f`, `_assert_axis_g`, `_assert_axis_h`, `_assert_axis_i` has a happy-path test + a regress test. `_call_satellite` has import-error, lookup-error, execute-error, and success-path tests. Target ~200 LOC.

### A2 — `_ADVISORY_HIT_LOOKBACK_ROUNDS` becomes a kwarg

**Scope:** Make the hardcoded 5-round lookback a `run()` kwarg on the master scenario + thread through `_compute_advisory_hit_rate`. Default stays at 5.

**Files:** `eval/scenarios/program_convergence/scenario.py`

**Closes:** Phase 0 [M] finding.

**Acceptance:** Existing tests still pass. New parametrized test asserts axis C behavior at lookback=3 vs lookback=10.

### A3 — Document calibration values in plan

**Scope:** Add a paragraph in `plan-program-level-eval.md` §2.1 or §4.2 documenting the synthetic-profile calibration values (`DEFAULT_OBSERVATION_BATCH=10`, `well_known_count_threshold=3`) and the rationale for why real corpora keep the production thresholds (2 and 10).

**Files:** `docs/design/plan-program-level-eval.md`

**Closes:** Phase 4 [M] finding (calibration only in code comments).

### A4 — Resolve `SCHEMA_EVOLUTION_COMPONENT_ID` alias drift

**Scope:** `src/trellis_cli/analyze.py:31` aliases `PARAM_COMPONENT_ID as SCHEMA_EVOLUTION_COMPONENT_ID`; the public re-export in `trellis.learning.__init__.py` (post-#154) uses `SCHEMA_EVOLUTION_PARAM_COMPONENT_ID`. Rename the trellis_cli local alias to match the public name OR import the public name directly.

**Files:** `src/trellis_cli/analyze.py`, possibly its tests

**Closes:** L finding from #154 review.

### A5 — Runner CLI `--scenario-arg key=value` flag

**Scope:** `eval/runner.py` accepts repeated `--scenario-arg name=value` flags; the values get parsed (with `Literal` validation for known kwargs) and passed to `scenario.run(**kwargs)`. Use cases: `--scenario-arg profile=real`, `--scenario-arg rounds=50`.

**Files:** `eval/runner.py`, `tests/unit/eval/test_runner.py`

**Closes:** Phase 5B L finding (operators currently must invoke `run()` programmatically for `profile="real"`).

### A6 — `pytestmark.skipif` propagation fix

**Scope:** Move the `skipif(not URI)` skipif from module-level `pytestmark` in `tests/integration/conftest.py` into the `registry` fixture itself (`pytest.skip(...)` inside the fixture body). Otherwise future tests in `tests/integration/cli/` etc. that accidentally pull the registry fixture fail with confusing connection errors instead of skipping cleanly.

**Files:** `tests/integration/conftest.py`

**Closes:** L finding from #154 review.

### A7 — Eval tree ruff cleanup

**Scope:** Fix the 66 pre-existing ruff errors in `eval/` (PLR2004 magic-value comparisons, RUF100 unused-noqa, etc.) + add `eval/` to the `lint` target in `Makefile`.

**Files:** `eval/**/*.py` (many), `Makefile`

**Closes:** L finding repeated across PRs #146, #147, #148, #149.

**Risk:** This unit touches many files. If it lands AFTER Wave 2/3 starts, Wave 2/3 units that touch `eval/` will conflict. Mitigation: land A7 first or coordinate carefully.

### A8 — C1.9 orphan-suspect decision frames

**Scope:** Produce a structured "decision frame" doc for each of the three orphan-suspect modules: `query_pattern_observer.py` (384 LOC), `learning/miner.py` (272 LOC), `maintenance/retention.py` (220 LOC). For each: what the module does, who it was intended for, what would have to happen to use it, what would be lost by deleting. Decision stays with the user; agent prepares analysis only.

**Files:** `docs/design/audit-trellis-workers-orphan-decision-frames.md` (new)

**Closes:** C1.9 follow-up — surfaces the decision for the user.

## 5. Wave 2 — Enrichment/classification + renderer enhancements

Depends on Wave 1 only for clean baseline. Different file scopes within Wave 2 — parallel.

### B1 — `EnrichmentResult.failure_kind` structured field

**Scope:** Add `failure_kind: ExtractionFailureKind | None = None` to `EnrichmentResult`. Update `EnrichmentService.enrich` + `_parse_response` emit sites to populate it (`model_error` / `parse_error` / `validation_error`).

**Files:** `src/trellis_workers/enrichment/service.py`, schema test

**Closes:** [M] from #155 review — classifier can only emit generic `enrichment_failure` slug today.

### B2 — `batch_enrich` source-hint plumbing

**Scope:** Allow `batch_enrich` callers to pass an `item_id` or `correlation_id` per-item so the collector-error events get bucketed by source instead of `None`. The proposal-generator clustering path (`src/trellis_workers/code_authoring/clustering.py:158-164`) silently skips events missing `source_hint`/`failure_kind` — fixing this makes batch-collector escapes visible to the Item 7 coding-agent loop.

**Files:** `src/trellis_workers/enrichment/service.py`, test

**Closes:** [M] from #155 review.

### B3 — `LLMFacetClassifier` production wiring + integration test

**Scope:** Wire `LLMFacetClassifier(event_log=registry.operational.event_log)` in the registry/pipeline builder. Add `tests/integration/test_classification_telemetry.py` asserting `CLASSIFICATION_DEGRADED` fires end-to-end when enrichment fails.

**Files:** Whichever module instantiates `LLMFacetClassifier` in production (likely `src/trellis/classify/pipeline.py` or `src/trellis/stores/registry.py`), `tests/integration/test_classification_telemetry.py`

**Closes:** [M] from #155 review — telemetry is dormant in production today; only tests construct the classifier with `event_log`.

### B4 — Chart renderer kwargs

**Scope:** Thread `output_dir: Path | None = None`, `figsize: tuple[float, float] | None = None`, `dpi: int | None = None` through `render_program_convergence_chart` + the scenario's `run(render_chart=True)`. Defaults preserve current behavior.

**Files:** `eval/reports/program_convergence_chart.py`, `eval/scenarios/program_convergence/scenario.py`

**Closes:** L findings from #146 (figsize/dpi hardcoded) and #147 (`_render_chart()` hardcodes `Path("eval/reports")`).

### B5 — `_render_chart()` anchor against `__file__`

**Scope:** When `output_dir` is None, compute the default as `Path(__file__).parent.parent.parent / "eval" / "reports"` (or similar — anchor against the repo root, not CWD). Operators running from any CWD get the expected output.

**Files:** `eval/scenarios/program_convergence/scenario.py`

**Closes:** L finding from #147. Pairs with B4.

### B6 — Real-data validation hygiene

**Scope:** Two small fixes from the TODO.md eval-framework-gaps section:
- Align `multi_backend_equivalence` `embedding_dim` default to 3 to match Neon pgvector. Currently the scenario defaults to 16 and silently mismatches.
- Document the AuraDB "single vector index per `(:Node, embedding)`" cohabitation rule in `docs/deployment/neo4j-auradb.md` (production users must coordinate `trellis_test_node_embeddings` vs `trellis_node_embeddings`).

**Files:** `eval/scenarios/multi_backend_equivalence/scenario.py`, `docs/deployment/neo4j-auradb.md`

**Closes:** Two open TODO.md eval-framework-gap items.

## 6. Wave 3 — Architectural foundations + scoping

Depends on Wave 2 for B1 (failure_kind) and B3 (LLMFacetClassifier wiring). C1/C2/C3 + E2/E3-prep have non-overlapping file scopes — parallel within Wave 3.

### C1 — PackItem advisory provenance

**Scope:** Add `injected_advisory_ids: list[str] = []` to `PackItem.metadata` (or `PACK_ASSEMBLED.payload` if metadata is too tight). Update `PackBuilder` to attach advisory IDs when advisories influence pack composition (which items were boosted/suppressed by which advisory). This is the foundation for D1 (axis C semantic tightening) — analyzers can join `advisory_id → outcome` per-item instead of using the domain-scope proxy.

**Files:** `src/trellis/schemas/pack.py`, `src/trellis/retrieve/pack_builder.py`, `src/trellis/retrieve/advisory_generator.py`, tests

**Closes:** Foundation for Phase 0 [M] axis C tightening.

**Risk:** Schema change. Use `extra="forbid"` per project Hard Rules; ensure existing callers don't break.

### C2 — `_drive_master` deduplication

**Scope:** Extract a `_run_loop(...)` helper in `eval/scenarios/program_convergence/scenario.py` that returns `(round_results, seed_entity_count)`. Both the master `run()` and the regression suite's `_drive_master` call it. Eliminates ~120 LOC of duplicated orchestration scaffold.

**Files:** `eval/scenarios/program_convergence/scenario.py`, `eval/scenarios/program_regression_suite/scenario.py`

**Closes:** Phase 2 L finding.

**Risk:** Test refactor needs to keep both end-to-end paths green. Run full eval unit suite after.

### C3 — Satellite `pytest` import at module level

**Scope:** Move `import pytest` out of module-level scope in the 4 satellite scenarios (`observation_retrieval.py`, `proposal_generation.py`, `meta_trace_round_trip.py`, `parameter_registry_passthrough.py`). Either wrap in `TYPE_CHECKING:` or split the runner-callable `run()` into a dependency-free module. Otherwise production envs without `[dev]` extras fail to import these scenarios, and the regression suite would emit 4 hard-fail Findings even though the satellites are logically fine.

**Files:** 4 satellite scenario files

**Closes:** Phase 2 L finding.

### E2 — Item 7 Cohort 2 scoping doc

**Scope:** Doc-only. Produce:
- `docs/design/adr-coding-agent-loop-cohort2-amendment.md` — ADR amendment covering the autonomous-spawn security model: sandboxed worktree creation per proposal, GitHub PR proposer via `gh` CLI, per-cycle budget ledger (LOC + token cap), file-allowlist enforcement, secret scrubbing on diffs, draft-PR opening only (no auto-merge).
- `docs/design/plan-coding-agent-loop-cohort2.md` — phase breakdown for the implementation, mirroring how Cohort 1 was scoped.
- Sample budget-ledger JSON schema (inline in the plan or as `src/trellis_workers/code_authoring/budget_schema.json`).
- Draft `trellis admin spawn-coder` CLI signature.

**Files:** 2 new docs + maybe 1 schema file

**Closes:** Provides the artifact the user reviews before authorizing Item 7 Cohort 2 implementation. Per TODO.md: "Do not unblock without (a) operator review of N real Cohort 1 proposals + (b) ADR amendment authorizing autonomous spawn." This unit provides (b).

### E3-prep — Real-LLM scenario design

**Scope:** Read existing `eval/scenarios/agent_loop_convergence_real_llm/scenario.py` to understand the real-LLM pattern. Draft `eval/scenarios/program_convergence_real_llm/scenario.py` (skeleton only, ~100 LOC) reusing the master scenario's per-axis logic but with real OpenAI/Anthropic embeddings. Document expected cost (~$0.50/run at 50 rounds × N seeds × 1 embed per seed × $0.00002), credential-gating pattern, and budget audit hook. Actual implementation happens in E3 (Wave 5).

**Files:** `eval/scenarios/program_convergence_real_llm/__init__.py` (new), `eval/scenarios/program_convergence_real_llm/scenario.py` (skeleton)

**Closes:** Prep for E3.

## 7. Wave 4 — Capstone

Depends on Wave 3 for C1 (provenance) and B4 (renderer kwargs).

### D1 — Axis C semantic tighten

**Scope:** Refactor `_compute_advisory_hit_rate` to use `advisory_id` provenance from `PackItem.metadata` (now exists after C1). Tighten the proxy to plan-prose definition: "advisories whose recommendation was *followed* AND outcome=success". Update Phase 2 regression suite threshold if needed.

**Files:** `eval/scenarios/program_convergence/scenario.py`, regression suite, tests

**Closes:** Phase 0 [M] finding — closes the original axis C semantic gap discovered during Phase 0 design.

### D2 — Single-figure overlay chart variant

**Scope:** Add `style: Literal["grid", "overlay"] = "grid"` kwarg to `render_program_convergence_chart`. When `style="overlay"`, render a single-figure 9-line plot using `_AXIS_DISPLAY_TITLES` + `_AXIS_EXPECTED_SHAPE` constants for the legend. Y-axis normalization (since axes have different scales) uses each axis's first-quarter mean as the baseline (1.0 = no change, >1.0 = improvement, <1.0 = regression).

**Files:** `eval/reports/program_convergence_chart.py`, tests

**Closes:** Phase 3 L finding.

### D3 — Advisory restoration ADR addendum

**Scope:** Doc-only. Write an ADR addendum (`docs/design/adr-dual-loop-evolution.md` §9 or new sibling `adr-dual-loop-advisory-restoration.md`) documenting the design question per TODO.md: does production agent activity need restoration to fire automatically? Document the three options:
- (a) Age out failure evidence by time window (requires longer-lived corpora than synthetic scenarios produce)
- (b) Dedicated "rescore-suppressed" pass that reads from EventLog directly for SUPPRESSED ids without re-delivering them
- (c) Change `AdvisoryStore.list()` semantics so the fitness loop sees SUPPRESSED entries while PackBuilder still doesn't

Plus an "operator-driven manual restore" baseline option. User picks the path.

**Files:** ADR addendum

**Closes:** TODO.md "Advisory restoration unreachable in scenario context" item.

### E1 — `evaluate_pack` typed-shape density scoring

**Scope:** Add a 6th dimension to `evaluate_pack` (or extend the existing `density` dimension): `shape_composition`. Measures whether the pack contains the expected mix of `item_type` values per the scenario's `expected_shapes` declaration. Example: `expected_shapes = {"summary": 1, "precedent": ">=1", "full_doc": ">=2"}` means the pack should have at least one summary, at least one precedent, and at least two full docs.

**Files:** `src/trellis/retrieve/evaluate.py`, `src/trellis/schemas/pack.py` (if `expected_shapes` lives there), scenarios that declare expected shapes, tests

**Closes:** TODO.md "Item-type semantics + summary-generation path — ADR-shaped lift" item.

**Risk:** Substantial architectural addition. Stay scope-disciplined — implement the dimension + tests + one example scenario using it; defer wiring all existing scenarios to declare expected_shapes to a follow-up.

## 8. Wave 5 — Real-LLM scenario implementation

### E3 — `program_convergence_real_llm` full implementation

**Scope:** Implement the scenario sketched in E3-prep. Real OpenAI/Anthropic embeddings for axes D + I. Live-credentials gating (skip cleanly without `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`). Budget audit emits a `BUDGET_CONSUMED` event per run with token + dollar totals.

**Files:** `eval/scenarios/program_convergence_real_llm/scenario.py` (full impl), tests with credential-mocking, budget audit hook

**Closes:** TODO.md "No eval scenario exercises a real LLM yet" item.

**Cost:** ~$0.50 per run at 50 rounds. Operator-gated; never runs in CI by default.

## 9. Cross-cutting risks

- **A7 (eval ruff cleanup) blocks Wave 2/3** if it lands after Wave 2 starts. Land A7 first or coordinate carefully.
- **B1 (failure_kind) consumed by future D1**. Order: B1 → D1.
- **C1 (PackItem provenance) consumed by D1**. Order: C1 → D1.
- **B4 (chart renderer kwargs) consumed by D2**. Order: B4 → D2.
- **C2 (_drive_master dedup) cross-cuts master + regression suite**. Single agent for coherence.
- **E1 (typed-shape density) cross-cuts evaluate_pack + many scenarios**. Stay scope-disciplined to one dimension + one example consumer.

## 10. Done when

- All 24 units' PRs merged to main.
- All M-severity items in TODO.md "Follow-ups surfaced by..." sections marked closed or explicitly deferred-with-reason.
- `make lint` (extended to include `eval/`) + `make typecheck` + `make test` all green on main.
- `docs/design/adr-coding-agent-loop-cohort2-amendment.md` exists for user review.
- `eval/scenarios/program_convergence_real_llm/scenario.py` exists and can be invoked end-to-end with API credentials.

## 11. What this doesn't include

| Deferred | Reason |
|---|---|
| Delete C1.9 orphan-suspect modules | A8 surfaces the decision frame; deletion is the user's call. |
| Item 7 Cohort 2 implementation | E2 ships the scoping; implementation is the next wave after operator reviews proposals. |
| Phase 8.1 loud-fail promotion | Conditional on real misconfig incident. |
| ~~WorkflowEngine~~ (retired in Phase F F0 (`1291210`, see `docs/research/workflow-engine-disposition.md`)) / EnrichmentService event-loop / Blob TTL / Graph compaction wiring | Gated on production data signals. |
| Full ScenarioReport metric widening across 8 scenarios | Phase 4 widened `ScenarioReport.metrics` to `Mapping[str, float \| str]`; other scenarios' local `dict[str, float]` annotations still work but ideally narrow to match. Tracked as L follow-up. |

All deferred items are explicitly "validate before designing" per the C1.6/C1.7 discipline.
