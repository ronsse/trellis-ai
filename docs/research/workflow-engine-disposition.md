# WorkflowEngine disposition memo

**Status:** Wave 1 of Phase F, Unit C
**Base SHA:** `af374d2` (`origin/main` HEAD at time of analysis)
**Author:** swarm sub-agent, 2026-05-18
**Recommendation:** **DELETE** (staged on this branch)

## 1. Inventory

- **Files in scope:** 2 source files + 2 test files (the package's whole footprint).
  - `src/trellis_workers/engine/__init__.py` — 1 LOC (module docstring only, no re-exports).
  - `src/trellis_workers/engine/thinking.py` — 299 LOC.
  - `tests/unit/workers/engine/__init__.py` — 0 LOC.
  - `tests/unit/workers/engine/test_thinking.py` — 349 LOC.
- **Total: 649 LOC** (300 source + 349 test).
- **Public API surface (8 names):** `WorkflowTier` (enum), `ReasoningEffort` (enum), `TierConfig`, `DEFAULT_TIERS` (const dict), `EscalationConfig`, `ThinkingPolicy`, `EscalationAttempt`, `WorkflowSession`, `WorkflowEngine`.
- **Module-level docstring:** "Workflow engine — thinking policy for internal curation workers."
- **Intent (from `thinking.py:128-135`):** implements an "Attempt -> Gate -> Escalate" pattern. Cognition tiers (FAST / STANDARD / DEEP / CRITICAL) carry per-tier `TierConfig` (model, reasoning_effort, max_tokens, temperature, max_context_tokens, use_verification). A `WorkflowSession` tracks the current tier + escalation history; `WorkflowEngine.should_escalate()` decides whether to bump tier based on confidence-below-threshold, gate failures, or task errors; `escalate()` advances the tier and records an attempt; `determine_initial_tier()` is a heuristic on intent / risk_level / context_size.
- **FIXME / TODO comments inside the package:** none. (Grep for `FIXME|TODO|XXX|HACK` in `src/trellis_workers/engine/` returns zero hits.)
- **Test coverage:** the 349-LOC `test_thinking.py` covers every method and enum exhaustively (`TestWorkflowTier`, `TestReasoningEffort`, `TestTierConfig`, `TestDefaultTiers`, `TestWorkflowSession`, `TestWorkflowEngineCreateSession`, `TestWorkflowEngineGetPolicy`, `TestWorkflowEngineShouldEscalate`, `TestWorkflowEngineEscalate`, `TestWorkflowEngineDetermineInitialTier`). The tests pass; the production wiring does not exist.

## 2. Consumers

Grep was run against `src/`, `tests/`, `docs/`, `eval/`, and `examples/` for every public symbol listed above (`WorkflowEngine`, `WorkflowTier`, `WorkflowSession`, `ThinkingPolicy`, `TierConfig`, `DEFAULT_TIERS`, `EscalationConfig`, `EscalationAttempt`, `ReasoningEffort`) **and** the module paths `trellis_workers.engine` / `from trellis_workers import engine` / `trellis_workers/engine`.

| Caller class | Hits | Status |
|---|---|---|
| `src/` (production) | **0** | The package has no production consumer in `src/trellis`, `src/trellis_cli`, `src/trellis_api`, `src/trellis_sdk`, or `src/trellis_workers` (outside the package itself). |
| `tests/unit/workers/engine/test_thinking.py` | every public symbol | The package's own unit tests are the only callers. |
| CLI subcommands | **0** | `src/trellis_cli/admin.py`, `analyze.py`, `demo.py`, `extract_refresh.py`, `admin_migrate_provenance.py` contain no engine references (case-insensitive grep matches the word "engine" in CSS / migration / pgvector contexts only). |
| `eval/` scenarios | 1 prose reference | `eval/scenarios/agent_loop_convergence/README.md:49` mentions "`WorkflowEngine` tier escalation" as a *future use* the scenario unblocks. No Python import. |
| `eval/corpora/github_trellis/snapshot_raw.json` | 1 hit | Captured GitHub PR description prose mentioning the engine as an unwired hook. Not a live consumer. |
| Docs / ADRs | 5 files | `docs/design/adr-llm-client-abstraction.md` (3 hits — Phase 3 deferred consumer list), `plan-evaluation-strategy.md`, `plan-next-swarm-wave.md`, `plan-cleanup-dead-code.md` C1.6, `audit-trellis-workers-orphans-2026-05-14.md`. All reference the engine by name as either "deferred consumer" or "no-action per C1.6." |
| `TODO.md` | 1 hit | C1.6 entry: "WorkflowEngine — deliberate no-action per 'validate before deleting'." |
| Reserved names that won't be re-needed | All 8 public symbols | Nothing else in `src/` re-exports or shadows them. |

The audit doc [`audit-trellis-workers-orphans-2026-05-14.md:48-49`](../design/audit-trellis-workers-orphans-2026-05-14.md) classifies the package as "deliberate-no-action per C1.6" but otherwise records the same zero-caller finding.

## 3. F1 harness comparison

The F1 harness (per the spec: skill loader, tool registry with allowlist enforcement, agent loop, budget tracker, per-step event emission, `LLMClient`-abstraction-backed, exactly 5 tools: `read_node` / `read_document` / `search_graph` / `propose_mutation` / `emit_event`, all mutations through `MutationExecutor`, `SKILL_STEP` telemetry per step). Skill frontmatter carries `skill_id`, `version`, `triggers`, `tools_allowed`, `budget_cents`, `max_steps`, `success_criteria`.

| Engine surface point | Does F1 harness need it? |
|---|---|
| `WorkflowTier` (FAST/STANDARD/DEEP/CRITICAL enum) | No. F1 budget gating is per-skill (`budget_cents`, `max_steps` in frontmatter), not a global cognition tier. Per-skill budgets are sharper than tier classes. |
| `ReasoningEffort` (LOW/MEDIUM/HIGH enum) | No. F1 passes model + reasoning_effort directly to `LLMClient.generate(...)` when the skill needs to. Coupling it to a tier enum adds indirection without value. |
| `TierConfig` (model / reasoning_effort / max_tokens / temperature / max_context_tokens / use_verification) | Partially overlapping but **redundant**. F1's per-skill frontmatter (`budget_cents`, `max_steps`, plus model selection at `LLMClient` construction time) covers the same configuration territory with the right grain. `use_verification` has no F1 analogue and no current consumer — the engine ships it as configuration that nothing reads. |
| `DEFAULT_TIERS` (preset configs) | No. F1 ships defaults via skill frontmatter authoring conventions, not a hardcoded preset table. |
| `EscalationConfig` (enabled / max_escalations / confidence_threshold) | No. F1 stops a skill on `max_steps` exhaustion or budget overrun, then surfaces the outcome to the operator. Auto-escalation across cognition tiers is the engine's signature feature, and **the engine never had a confidence signal to feed it** — F1 does not need to inherit that gap. |
| `ThinkingPolicy` / `EscalationAttempt` / `WorkflowSession` (state-tracking dataclasses) | No. F1 emits `SKILL_STEP` events per step into the EventLog; the EventLog *is* the per-step history. A separate `WorkflowSession` object would duplicate that record without adding a query path. |
| `WorkflowEngine.should_escalate()` / `escalate()` | No. F1 has no current escalation behavior in v1. If a skill exhausts its budget or step count, the harness terminates the skill loop with a failure outcome — there is no "promote to a higher tier and retry" path. |
| `determine_initial_tier()` (heuristic: keyword scan on intent + risk_level + context_size) | No. F1 selects skills via `triggers` in frontmatter, not by classifying the request into a tier. The keyword-scan heuristic ("deep" / "complex" / "quick" / "simple") is too coarse to drive real routing. |

**Concrete overkill identified in the engine that F1 doesn't need:**

- A four-level tier ladder (FAST → STANDARD → DEEP → CRITICAL) with auto-promotion. F1's "one skill, one budget, one outcome" shape is simpler and matches the actual operator mental model.
- A separate `WorkflowSession` object holding escalation history. F1 puts the history in the EventLog where it already belongs.
- `use_verification: bool` on `TierConfig`. Nothing in the codebase reads it.
- `determine_initial_tier()`'s string-matching heuristic. Skill selection in F1 is explicit (`triggers`), not heuristic.

**What the engine gives the harness that the harness will not rebuild:**

- Nothing concrete. Two structural patterns *could* inspire the harness (per-step config carrying `max_tokens` / `temperature`, a "stop on threshold" gate), but those patterns are common enough that re-deriving them in 50-100 LOC inside the harness is cleaner than carrying the engine's ladder + escalation machinery as substrate. The harness has its own clear shape; adopting the engine forces it to either ignore most of the engine's API or absorb concepts (tiers, escalation) that it does not need.

## 4. Recommendation

**DELETE.**

The package has been on the tree since `3b9cedb` (initial Trellis commit, 2026-04-17) with zero production callers. C1.6 left it pending "real workload signal" 6 months ago; no signal arrived. F1's harness ADR (parallel Unit A) builds a richer, more directly applicable substrate for bounded operations against the data layer — per-skill budgets, EventLog-as-history, explicit `triggers`-based routing. The engine's value-add (tier-based escalation with auto-promotion on confidence-gate failures) was always speculative; F1 makes a deliberate choice not to need it. Keeping 649 LOC of unused implementation + tests as "we might wire this someday" is the kind of carrying cost the cleanup-track discipline exists to retire once the *future thing that might consume it* arrives and chooses a different shape — which is the situation here.

**Files to remove (exhaustive):**

1. `src/trellis_workers/engine/__init__.py` (1 LOC).
2. `src/trellis_workers/engine/thinking.py` (299 LOC).
3. `tests/unit/workers/engine/__init__.py` (0 LOC).
4. `tests/unit/workers/engine/test_thinking.py` (349 LOC).
5. The two now-empty parent directories `src/trellis_workers/engine/` and `tests/unit/workers/engine/`.

**Imports to clean up:** **none.** Grep confirms zero `from trellis_workers.engine` / `import trellis_workers.engine` outside the test module being deleted. The top-level `src/trellis_workers/__init__.py` (1 LOC, docstring only) does not re-export engine symbols.

**Doc references to update:**

1. `TODO.md` C1.6 line — replace "**deliberate no-action** per 'validate before deleting'" with a status note pointing at this memo + the deletion commit. The cleanup-track checklist should reflect that C1.6 is now closed.
2. `TODO.md` deferred-features section line 305 — "WorkflowEngine / EnrichmentService event-loop / Blob TTL / Graph compaction wiring | Gated on production data signals" — drop the `WorkflowEngine` clause (the other three deferrals remain).
3. `docs/design/audit-trellis-workers-orphans-2026-05-14.md` — strike the `engine/__init__.py` and `engine/thinking.py` rows from the audit table and from the "deliberate-no-action per C1.6" summary row.
4. `docs/design/plan-cleanup-dead-code.md` C1.6 section (lines 103-109) — append a "**Status:** Closed YYYY-MM-DD by Phase F Wave 1 Unit C — see `docs/research/workflow-engine-disposition.md`" line, leave the historical reasoning intact.
5. `docs/design/plan-evaluation-strategy.md:62, :208` — the two prose mentions of `WorkflowEngine` tier escalation as an unblock target should be amended to note "(deleted Phase F Wave 1 Unit C; F1 harness budgets supersede)" or simply removed from the unblock-targets list.
6. `docs/design/plan-next-swarm-wave.md:305` — drop the `WorkflowEngine` clause from the "gated on production data signals" row.
7. `docs/design/adr-llm-client-abstraction.md` — three references (lines 60, 185, 219, 259). The ADR's Phase 3 "deferred consumer" list (`TierConfig` has no `LLMClient` plumbed yet) should be amended with a "since-superseded" note pointing at this memo and at the F1 harness ADR. The ADR's value-prop bullet ("`TierConfig` connects to reality") loses its example; the ADR's broader case (Protocol-in-core for `LLMClient` / `EmbedderClient`) is unaffected.
8. `eval/scenarios/agent_loop_convergence/README.md:49` — the "Decision this scenario unblocks → `WorkflowEngine` tier escalation" bullet should be replaced with "F1 harness budget-exhaustion patterns" or removed; the scenario itself still validates advisory fitness, which is the load-bearing decision.

(Doc edits 5-8 are listed for completeness; doing them in this PR is appropriate but not load-bearing — they are prose-only and can ship in a follow-up if PR size matters.)

## 5. Risk + reversibility

**Worst case if we delete and later regret (cost-of-rebuild estimate):** ~2 hours.

If a future workload signal genuinely asks for tier-based auto-escalation against a confidence gate — i.e., the original engine premise — re-deriving the engine from scratch is straightforward. The implementation is two enums (~30 LOC), four dataclasses (~80 LOC), and one class with five methods (~190 LOC). The tests can be regenerated against the new shape in ~3 hours of swarm time. Total rebuild cost is bounded by ~5 hours of focused work. Git history preserves the deleted source for cherry-pick if the future use case wants the exact prior shape rather than a re-derivation; `af374d2` is the base SHA.

The bigger latent risk is *the original speculation was wrong*. The engine was designed before F1's per-skill-budget shape existed; if F1 ships and proves out, the tier ladder is the wrong substrate, not just an under-utilized one. Re-introducing it would be a deliberate regression.

**Worst case if we adopt and later find it doesn't fit (cost-to-rip-out estimate):** ~1 day.

If F1 adopted `WorkflowEngine` as substrate (e.g., mapped each skill to a tier; routed via `should_escalate` on confidence gates) and then concluded the tier model is the wrong abstraction, the rip-out has three costs: (a) decoupling the F1 control-flow from `WorkflowEngine` calls scattered through the harness — likely ~10-20 sites; (b) reworking the per-step telemetry to remove tier-tagged `SKILL_STEP` payloads; (c) updating tests that assume tier-based routing. ~6-8 hours of focused work, plus the design tax of having shipped a misleading abstraction to skill authors who wrote `tier: deep` in their frontmatter.

**Net:** the rebuild risk (5 hours) is lower than the rip-out risk (8 hours), and the rebuild risk only materializes if a future signal *explicitly* asks for the engine's pattern. The deletion path has the lower expected cost.

## 6. Action staged on this branch

**Commits applied:** one commit on this worktree branch (`worktree-agent-a515a244be362ae75`) staged on top of `af374d2`:

- Deletes the four files listed in §4: `src/trellis_workers/engine/__init__.py`, `src/trellis_workers/engine/thinking.py`, `tests/unit/workers/engine/__init__.py`, `tests/unit/workers/engine/test_thinking.py`.
- Removes the two now-empty parent directories.
- Adds this memo at `docs/research/workflow-engine-disposition.md`.

Doc edits (§4 items 1-8) are **deferred to a follow-up** so this PR stays single-scoped to the code deletion + memo and the user can review the memo before any prose drift propagates. A `## Deferred Findings` block at the end of the swarm report lists the doc-edit follow-ups for tracking.

**Test status post-staging:** see swarm report `pytest tests/unit/ -q` count.

**No push to `main`. No edit to the parallel units' files** (`docs/design/adr-graph-skill-harness.md`, `docs/design/adr-inner-curation-loop.md`, `eval/scenarios/skill_loop_convergence/`).
