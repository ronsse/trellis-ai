# Plan: Cleanup — dead-code removal

**Status:** Proposed 2026-05-11; landed 2026-05-15 (4-unit swarm + rollup PR — per-sub-item closures noted inline below)
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

**Status:** Closed 2026-05-15 (C1 swarm Unit A, rollup PR).

**Current state:** [`TODO.md`](../../TODO.md) line ~70 documents this: the "file-only" path is **structurally underspecified** because `PackFeedback` JSONL lacks per-item shape that `analyze_learning_observations` needs. Today the JSONL is demote-only de facto; CLAUDE.md still describes a "dual-loop" promote path that nobody runs.

**Action:**
- Update CLAUDE.md to remove the "JSONL feedback path drives promotion" framing.
- Keep `pack_feedback.jsonl` writing (it's still useful as an audit log) — only remove the *bridge* claim.
- Delete any code paths that *attempt* to read JSONL for promotion. Grep for callers of `pack_feedback.jsonl` that route into `analyze_learning_observations` without first going through EventLog.
- Update the dual-loop ADR ([`adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md)) to mark the file-only promote path as **rejected** (per the same TODO.md entry's "option (c)").

**Estimated size:** ~30 LOC code delete + ~50 LOC docs/ADR updates.
**Tests required:** verify `tests/unit/feedback/` and `tests/unit/learning/` still pass; any test that asserted the file-only bridge gets deleted along with the code.

### C1.2 — Delete `Operation.TRACE_INGEST` stub — **STALE — DO NOT EXECUTE**

**Status:** Premise is wrong as of 2026-05-15. Discovered by C1 swarm Unit A when it refused to delete the enum.

**Why this entry is stale:** `TraceIngestHandler` was wired into the governed pipeline by `swarm/d1-trace-ingest-decision` (commit `ec478c1`) and trace ingestion was routed through `MutationExecutor` by `swarm2/b-7-trace-bypass-sites` (commit `b143e84`). The handler at [`src/trellis/mutate/handlers.py:26`](../../src/trellis/mutate/handlers.py:26) implements the full validate → idempotency → store → emit `TRACE_INGESTED` pipeline, and three production surfaces submit `Operation.TRACE_INGEST` Commands today: [`src/trellis_cli/ingest.py:67`](../../src/trellis_cli/ingest.py:67), [`src/trellis_api/routes/ingest.py:47`](../../src/trellis_api/routes/ingest.py:47), [`src/trellis/mcp/server.py:641`](../../src/trellis/mcp/server.py:641).

The plan also pointed at the wrong file path: the `Operation` enum lives at [`src/trellis/mutate/commands.py:19`](../../src/trellis/mutate/commands.py:19), not `mutate/operations.py` (which does not exist). Recorded here so the next sweep doesn't waste a grep cycle.

**Action: NONE. Do not delete the enum.** Deletion would break all three production surfaces and contradict the Hard Rule that all mutations flow through `MutationExecutor`.

### C1.3 — Delete hard-coded learning thresholds

**Status:** Closed by PR #109 (commit `e672ef5`, 2026-05-12). Verified by C1 swarm Unit B at base SHA `2ca9584`: zero grep hits for `_NOISE_SUCCESS_THRESHOLD` / `_NOISE_RETRY_THRESHOLD` / `_lookup_threshold_from_registry` in `src/`. The scoring module now uses `_resolve_required_threshold(registry, scope, key)` — the loud, no-fallback path required by the POC directive.

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

**Status:** Closed 2026-05-15 by C1 swarm Unit C. Remediation was already complete on `origin/main` at base SHA `2ca9584` (PR #110 plus the C2 sweep PRs #115-#140). The C1 rollup PR adds the audit trail at [`audit/silent_fallbacks_2026-05-14-c1-5-extraction-slice.md`](../../audit/silent_fallbacks_2026-05-14-c1-5-extraction-slice.md): 13 in-scope sites — 4 emit-then-raise, 8 `# GRACEFUL-DEGRADATION:` annotated, 1 loud env-var parser. Two M-severity follow-ups in `EnrichmentService` ([`src/trellis_workers/enrichment/service.py:169,228,233`](../../src/trellis_workers/enrichment/service.py:169)) recorded in TODO.md under "Follow-ups surfaced by C1 swarm 2026-05-15".

**Dependency:** Item 4 ([`plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md)) Phase 1 must land first.

**Current state:** Item 4 Phase 1 replaces silent except in two known sites (`src/trellis/extract/llm.py`, `src/trellis_workers/learning/miner.py`). A wider audit may surface 3-5 more.

**Action:**
- After Item 4 Phase 1 lands, grep `src/` for `except (json.JSONDecodeError|JSONDecodeError|ValueError)` paired with `return []` or `return None`.
- Each hit: either (a) replace with the new emit-then-raise pattern, or (b) document why it stays as graceful-degradation with an inline comment naming the rationale.
- Add the audit list to the PR description.

**Estimated size:** ~10-30 LOC per site × 5 sites = ~100 LOC delta.
**This item is the bridge into [`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md)** — that plan is broader; this is the extraction-layer slice.

### C1.6 — Decide on `WorkflowEngine`

> **Resolved (2026-05-18):** the signal arrived via the Phase F engine-disposition
> memo (`docs/research/workflow-engine-disposition.md`) — zero production callers,
> superseded by the graph-skill harness design. **Deleted in `1291210`** (Phase F
> F0 Wave 1). The validate-before-deleting posture below is retained as written
> for the historical record.

**Current state:** [`TODO.md`](../../TODO.md) "Workflow engine / tier-based escalation" — fully designed, unit-tested, **zero production callers**.

**Action:** **Validate before deleting.** This plan does **not** delete. The TODO entry says "leave as-is until real workload signal tells us between (a) wire it up, (b) collapse, (c) delete." This cleanup plan **upholds that decision**. Cleanup-track items that lack signal are not in scope for C1.

Listed here so the swarm doesn't pick it up by mistake. **Do not delete.**

### C1.7 — Decide on `EnrichmentService` event-loop wiring stubs

**Current state:** Same shape as C1.6. The service is wired (`LLMFacetClassifier` uses it) but the triggered-consumer pattern (scheduled sweep / event handler / Claude Code skill) is **unwired by design**.

**Action:** **Do not delete.** Same reasoning as C1.6. Listed for the swarm's awareness.

### C1.8 — Remove now-stale "self-learning" framing in CLAUDE.md and docs

**Status:** Closed 2026-05-15 (C1 swarm Unit A, rollup PR). CLAUDE.md normalized; carve-out for the self-improvement program docs recorded in [`adr-terminology.md`](./adr-terminology.md) §2.6. Three `src/` docstring leftovers (`src/trellis/schemas/outcome.py`, `src/trellis/stores/base/tuner_state.py`, `src/trellis/ops/__init__.py`) deferred to "next substantive touch" per the carve-out; tracked in TODO.md under "Follow-ups surfaced by C1 swarm 2026-05-15".

**Current state:** Per the project's terminology ADR ([`adr-terminology.md`](./adr-terminology.md)), "self-learning" is not a project term — the canonical phrase is "feedback loop." CLAUDE.md uses both. Drift.

**Action:**
- Grep CLAUDE.md and `docs/` for "self-learning". Replace with "feedback loop" or "advisory loop" as fits.
- Keep the term in this self-improvement program documentation **only** because it's the term the user used when scoping this work; flag the discrepancy here.

**Estimated size:** ~20 LOC docs delta.

### C1.9 — Audit `src/trellis_workers/` for orphan modules

**Status:** Audit landed 2026-05-15 (C1 swarm Unit D, rollup PR) at [`docs/design/audit-trellis-workers-orphans-2026-05-14.md`](./audit-trellis-workers-orphans-2026-05-14.md). Three orphan-suspect modules surfaced (876 LOC combined): `extract/query_pattern_observer.py`, `learning/miner.py`, `maintenance/retention.py`. Per the C1.6/C1.7 discipline, deletion deferred until a human decision attaches.

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
