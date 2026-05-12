# Plan: Cleanup — dead-code removal

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable (decomposable into N independent units)
**ADR:** none — cleanup
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) cleanup track C1
**Depends on:** Item 3 ([`plan-parameter-registry-wiring.md`](./plan-parameter-registry-wiring.md)) for item 3 below; other items independent.

## 1. Premise

The codebase has a handful of dead-or-stub code blocks that survive because deletion was risky during ongoing feature work. They aren't bugs — they're just lossy. The program-level POC directive ("no half-finished implementations") puts these on the cleanup track.

Each item below is **independently decidable and independently shippable**. A swarm of N agents can pick them up in parallel.

## 2. POC directives applied

- Deletion is preferred over deprecation. No `_deprecated.py` shim files.
- Each deletion ships with one PR that includes:
  - The deletion itself.
  - Updates to any grep-discoverable referrers.
  - A CHANGELOG entry if the deletion is observable from the public API.
- If the deletion surfaces a hidden caller (a test was inadvertently asserting old behavior), the fix is to update the test to the current behavior, not to revive the dead code.

## 3. Items

### C1.1 — Delete the JSONL→learning.scoring file-only bridge

**Current state:** [`TODO.md`](../../TODO.md) line ~70 documents this: the "file-only" path is **structurally underspecified** because `PackFeedback` JSONL lacks per-item shape that `analyze_learning_observations` needs. Today the JSONL is demote-only de facto; CLAUDE.md still describes a "dual-loop" promote path that nobody runs.

**Action:**
- Update CLAUDE.md to remove the "JSONL feedback path drives promotion" framing.
- Keep `pack_feedback.jsonl` writing (it's still useful as an audit log) — only remove the *bridge* claim.
- Delete any code paths that *attempt* to read JSONL for promotion. Grep for callers of `pack_feedback.jsonl` that route into `analyze_learning_observations` without first going through EventLog.
- Update the dual-loop ADR ([`adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md)) to mark the file-only promote path as **rejected** (per the same TODO.md entry's "option (c)").

**Estimated size:** ~30 LOC code delete + ~50 LOC docs/ADR updates.
**Tests required:** verify `tests/unit/feedback/` and `tests/unit/learning/` still pass; any test that asserted the file-only bridge gets deleted along with the code.

### C1.2 — Delete `Operation.TRACE_INGEST` stub

**Current state:** [`TODO.md`](../../TODO.md) "Operation.TRACE_INGEST exists in the registry but has no handler." Per the roadmap A.1 gotcha, the actual data flow uses `ENTITY_CREATE` / `LINK_CREATE` directly. The TRACE_INGEST enum value is a lie that will trip someone.

**Action:**
- Remove `Operation.TRACE_INGEST` from the `Operation` enum in `src/trellis/mutate/operations.py`.
- Remove any tests that reference it.
- Verify no extractor or worker emits a Command with this operation — grep + mypy.
- Update CLAUDE.md governed-mutation section to remove the implication that traces flow through MutationExecutor.

**Estimated size:** ~30 LOC delete + ~20 LOC test cleanup.
**Risk:** if any external integration relies on this enum value, removing it is breaking. Mitigate by grepping all SDK + worker code. POC stage: no external integrations exist, so this is greenfield.

### C1.3 — Delete hard-coded learning thresholds

**Dependency:** Item 3 ([`plan-parameter-registry-wiring.md`](./plan-parameter-registry-wiring.md)) must land first.

**Current state:** After Item 3 wiring, `_NOISE_SUCCESS_THRESHOLD` and `_NOISE_RETRY_THRESHOLD` constants in `src/trellis/learning/scoring.py` are no longer read. They survive as cruft.

**Action:**
- Delete the constants.
- Delete the now-unused `_lookup_threshold_from_registry` helper if it was orphaned (Item 3's plan deletes its hard-coded fallback; verify the helper itself is now used everywhere through the new path).
- Verify mypy and tests.

**Estimated size:** ~20 LOC delete.
**Note:** this is actually covered by Item 3's plan, but listed here for completeness of the cleanup track.

### C1.4 — Consolidate `eval/_scenario_common.py` — ALREADY DONE

**Status:** landed 2026-05-09 in commit `689c7ef` ("refactor(eval): consolidate convergence-scenario boilerplate"). Verified 2026-05-11 by the C1.4 swarm unit.

**Current state of the shared modules:**
- `eval/scenarios/_convergence_common.py` (~351 LOC) — `_RoundOutcome` Protocol, `_LoopStats`, `_ConvergenceStats`, `_quarter_means`, `_convergence_stats`, `_run_periodic_loops`, `_record_round_feedback`, `_convergence_summary_finding`, `_loops_summary_finding`, `_validate_basic_kwargs`.
- `eval/scenarios/_strategies.py` (~74 LOC) — `_SeededGraphSearch`.
- `eval/scenarios/_telemetry.py` (~85 LOC) — `_EmbedRecord`, `_EmbedTelemetry`, `_make_embedding_fn` (embed-only variant).

**Per-scenario LOC reduction from the consolidation:**
- `agent_loop_convergence`: 856 → 625 (−231)
- `dbt_corpus_convergence`: 970 → 634 (−336)
- `github_corpus_convergence`: 889 → 578 (−311)

**Intentionally not unified** (verified correct):
- `agent_loop_convergence_real_llm` keeps its own `_Telemetry` (tracks chat+embed; shared module is embed-only).
- `_RoundResult` stays scenario-local because the discriminator differs per scenario (`domain` vs `skill + difficulty`).

**Action: none.** This cleanup item is closed. The TODO.md entry below ("Consolidate eval scenario boilerplate") should be marked `[x]`.

### C1.5 — Audit and remove duplicated `_parse_candidates` patterns

**Dependency:** Item 4 ([`plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md)) Phase 1 must land first.

**Current state:** Item 4 Phase 1 replaces silent except in two known sites (`src/trellis/extract/llm.py`, `src/trellis_workers/learning/miner.py`). A wider audit may surface 3-5 more.

**Action:**
- After Item 4 Phase 1 lands, grep `src/` for `except (json.JSONDecodeError|JSONDecodeError|ValueError)` paired with `return []` or `return None`.
- Each hit: either (a) replace with the new emit-then-raise pattern, or (b) document why it stays as graceful-degradation with an inline comment naming the rationale.
- Add the audit list to the PR description.

**Estimated size:** ~10-30 LOC per site × 5 sites = ~100 LOC delta.
**This item is the bridge into [`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md)** — that plan is broader; this is the extraction-layer slice.

### C1.6 — Decide on `WorkflowEngine`

**Current state:** [`TODO.md`](../../TODO.md) "Workflow engine / tier-based escalation" — fully designed, unit-tested, **zero production callers**.

**Action:** **Validate before deleting.** This plan does **not** delete. The TODO entry says "leave as-is until real workload signal tells us between (a) wire it up, (b) collapse, (c) delete." This cleanup plan **upholds that decision**. Cleanup-track items that lack signal are not in scope for C1.

Listed here so the swarm doesn't pick it up by mistake. **Do not delete.**

### C1.7 — Decide on `EnrichmentService` event-loop wiring stubs

**Current state:** Same shape as C1.6. The service is wired (`LLMFacetClassifier` uses it) but the triggered-consumer pattern (scheduled sweep / event handler / Claude Code skill) is **unwired by design**.

**Action:** **Do not delete.** Same reasoning as C1.6. Listed for the swarm's awareness.

### C1.8 — Remove now-stale "self-learning" framing in CLAUDE.md and docs

**Current state:** Per the project's terminology ADR ([`adr-terminology.md`](./adr-terminology.md)), "self-learning" is not a project term — the canonical phrase is "feedback loop." CLAUDE.md uses both. Drift.

**Action:**
- Grep CLAUDE.md and `docs/` for "self-learning". Replace with "feedback loop" or "advisory loop" as fits.
- Keep the term in this self-improvement program documentation **only** because it's the term the user used when scoping this work; flag the discrepancy here.

**Estimated size:** ~20 LOC docs delta.

### C1.9 — Audit `src/trellis_workers/` for orphan modules

**Current state:** Several worker modules may exist without callers after recent simplifications. Grep for modules under `src/trellis_workers/` whose only test reference is `tests/unit/workers/*` and whose `__init__.py` doesn't export them.

**Action:** for each orphan, **validate before deleting** (per C1.6's discipline). List orphans in the PR description; the decision per-module belongs to a human, not the cleanup swarm. The PR is read-only audit if no human signal is attached.

**Estimated size:** the audit step is ~1 hour of swarm time; deletions depend on signal.

## 4. Total size estimate

| Item | LOC | Independent? |
|---|---|---|
| C1.1 | 80 | yes |
| C1.2 | 50 | yes |
| C1.3 | 20 | needs Item 3 |
| C1.4 | 650 | yes |
| C1.5 | 100 | needs Item 4 Phase 1 |
| C1.6 | 0 | (no-action) |
| C1.7 | 0 | (no-action) |
| C1.8 | 20 | yes |
| C1.9 | audit only | yes |
| **Total** | **~920** | five concurrent swarm units feasible |

## 5. Done when

Each item is `[x]`-able in TODO.md. The full track is done when:

- `grep -rn "TRACE_INGEST" src/` returns no hits.
- `grep -rn "_NOISE_SUCCESS_THRESHOLD\|_NOISE_RETRY_THRESHOLD" src/` returns no hits.
- The dual-loop ADR has been amended with the JSONL bridge rejection.
- `eval/_scenario_common.py` (or split) exists and the three convergence scenarios import from it.
- CHANGELOG carries entries for the observable deletions.

## 6. Risks

- **Hidden callers of `Operation.TRACE_INGEST`.** Mitigate by mypy strict mode + grep before deleting.
- **Test assertions encoding stale behavior.** Update tests, don't revive code.
- **Cleanup colliding with feature work.** Coordinate by landing the cleanup PR *after* its blocker (Item 3 or Item 4) but before any follow-on feature touches the same area.
