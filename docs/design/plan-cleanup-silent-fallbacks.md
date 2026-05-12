# Plan: Cleanup — silent-fallback hardening

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable (per-call-site decomposable)
**ADR:** the POC directive in [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §2 is the spec
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) cleanup track C2
**Depends on:** Item 4 Phase 0 ([`plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md)) — provides the `emit_extraction_failure()` helper that the audit replacements use.

## 1. Premise

The program-level POC directive forbids silent fallbacks:

> Every `try: X except: pass` is a defect. Every `except SomeError: log.warning(...); return None` is a defect unless the code path is *explicitly* documented as graceful-degradation with a stated reason. Default behavior on unexpected input or state is **raise**.

This cleanup plan operationalizes that directive: systematically audit `src/`, classify each silent-handler call site, and either replace it with explicit emit-then-raise or annotate it inline with a graceful-degradation justification.

This is not a one-PR job. It's a **scheduled sweep** decomposed into per-area swarm units.

## 2. POC directives applied (recursive)

The cleanup itself follows the directives it's enforcing:

- The audit script reports findings; it does not silently skip files it can't parse. Parse errors raise.
- Each replacement PR ships with at least one failure-injection test demonstrating the new loud behavior.
- No replacement PR ships with a `# type: ignore` workaround for a typing issue surfaced by the audit. Type errors get fixed; if a fix requires a redesign, the PR is split.

## 3. Audit method

Per swarm unit, run this grep + manual classify:

```bash
# Silent except-pass patterns
grep -rn "except.*:\s*$\s*pass" src/

# Silent except-return-empty patterns
grep -rn "except.*:\s*$\s*return\s*\(\[\]\|None\|{}\|0\|False\)" src/

# Silent except-log-return patterns
grep -rn "except.*:" src/ -A 5 | grep -B 1 "return\s*\(\[\]\|None\|{}\|0\|False\)"

# Silent fallthrough patterns (catch-too-broad)
grep -rn "except\s*Exception\|except\s*BaseException\|except\s*:" src/
```

Each hit gets classified into one of four buckets:

| Bucket | Description | Action |
|---|---|---|
| **DEFECT** | Hides a failure that the caller would want to know about. | Replace with `emit_*_failure()` + `raise`. |
| **GRACEFUL-DEGRADATION** | Documented best-effort behavior (e.g., optional cache lookup). | Add inline comment naming the rationale + emit a `*_DEGRADED` event so degradation is observable. |
| **GUARD** | Validating input at a boundary; raising is correct, but the message is bad. | Improve the exception message + add the offending value to the message. |
| **TEST-ONLY** | Silent in tests for fixture cleanup. | Acceptable; mark with a `# test-only` comment for future audits. |

## 4. Audit findings — actual concentrations (2026-05-11)

Phase 0 landed on branch `cleanup/c2-phase0-silent-fallback-audit` (commit `a8c41ca`); audit script at `scripts/audit_silent_fallbacks.py`, full report at `audit/silent_fallbacks_2026-05.md`.

**Totals:** 153 sites flagged; **112 DEFECT (73%)**, 9 GRACEFUL-DEGRADATION, 32 GUARD, 0 TEST-ONLY. Spot-check accuracy ≈83% on the bucket heuristics.

**Per-directory:**

| Subpackage | Total | DEFECT |
|---|---:|---:|
| `trellis/` | 118 | 88 |
| `trellis_cli/` | 15 | 9 |
| `trellis_api/` | 9 | 7 |
| `trellis_sdk/` | 4 | 3 |
| `trellis_workers/` | 7 | 5 |

**Top file-level concentrations** (drive the phase split below):

| File | Sites | DEFECT |
|---|---:|---:|
| `src/trellis/mcp/server.py` | 34 | 31 |
| `src/trellis/stores/registry.py` | 19 | 16 |
| `src/trellis_cli/admin.py` | 10 | 4 |
| `src/trellis/migrate/graph_migrator.py` | 9 | 9 |
| `src/trellis/retrieve/pack_builder.py` | 9 | 9 |
| `src/trellis_api/observability.py` | 4 | 3 |
| `src/trellis/feedback/recording.py` | 4 | 4 |
| `src/trellis_cli/extract_refresh.py` | 3 | 3 |
| `src/trellis/classify/refresh.py` | 3 | 3 |
| `src/trellis/extract/dispatcher.py` | 3 | 3 |

### 4.1 Known DEFECTs — covered by Item 4 Phase 1 (landed 2026-05-11 in branch `worktree-agent-a4bacebb2e5f8bd5f`)

- `src/trellis/extract/llm.py` — silent parse-fallback in `LLMExtractor.extract()` (`llm_extractor_parse_failed` log line). ✅ replaced with emit + raise.
- `src/trellis_workers/learning/miner.py:211` `_parse_candidates` — JSONDecodeError + ValueError swallow. ✅ replaced with emit + raise.

### 4.1.1 Sibling defect surfaced by Item 4 — track for sweep

- `src/trellis_workers/learning/miner.py:185-187` — `PrecedentMiner.generate_precedent_candidates` has `except Exception: return []` around the LLM call itself. `model_error`-class failure that should likewise emit + raise. Helper now available (`emit_extraction_failure(failure_kind="model_error")`).

### 4.2 Surprise find — retention silently masks parse errors (P0 standalone fix)

`src/trellis_workers/maintenance/retention.py:169`:

```python
try:
    updated_at = datetime.fromisoformat(updated_str)
    ...
    if updated_at < cutoff:
        report.stale_documents.append(doc["doc_id"])
except (ValueError, TypeError):
    pass
```

A corrupt `updated_at` string makes the doc **invisibly skip the stale flag**. Retention drift is invisible to operators. Track as the standalone P0 fix in Phase 1.5 below — does not need to wait for the broader sweep.

### 4.3 Surprise find — what's *not* there

The pre-audit plan §4.5 (embedder / LLM provider error swallowing) and §4.6 (policy-gate deny-on-error) both **scanned clean**. Both layers correctly re-raise. Original Phase 2 (embedders) and the policy/executor part of Phase 3 are **redirected** below; the original speculation was wrong about where the defects live.

### 4.4 Other DEFECT-bucket call sites worth naming up front

- `src/trellis/mutate/executor.py:190` — broad `except Exception` in `execute()` (§4.7-shaped). Single site, but a critical one.
- `src/trellis/stores/registry.py` — 16 DEFECTs, mostly `ImportError` swallows that silently return `None` instead of telling the operator the optional extra isn't installed. The `build_llm_client` at :1554 returning None for missing Anthropic SDK is the most user-visible.
- `src/trellis/mcp/server.py` — 31 DEFECTs concentrated in tool handlers (`get_context`, `save_memory`, `_build_llm_client_from_env`, etc.). Pattern: broad `except` + log + return JSON error. MCP has a structured error protocol; using `raise` is the loud-and-observable path.

## 5. Phases — restructured around actual audit concentrations

Phase 0 landed; subsequent phases sized by real hit counts. Each phase is one PR; phases are **independently parallelizable** unless noted.

### Phase 0 — audit script + 2026-05 report ✅ landed 2026-05-11

Commit `a8c41ca` on branch `cleanup/c2-phase0-silent-fallback-audit`. 730-LOC script + 1669-line report. Re-runnable; output is deterministic so a future audit diffs cleanly against today's report.

### Phase 1 — extract layer (covered by Item 4)

The 2 known DEFECTs in `src/trellis/extract/llm.py` and `src/trellis_workers/learning/miner.py` ship with Item 4 Phase 1. This plan does not duplicate.

### Phase 1.5 — retention invisible-drift bug (P0 standalone, ~30 LOC) — NEW

**File:** `src/trellis_workers/maintenance/retention.py:169`.

**Action:** replace `except (ValueError, TypeError): pass` with:
1. `emit_extraction_failure(failure_kind="parse_error", extractor_id="retention.staleness", error_class=type(exc).__name__, ...)` once Item 4 Phase 0 helper exists — or use `logger.error(...)` interim and add the doc_id to a `report.malformed_documents` field on `StalenessReport`.
2. Raise on totals-mismatch: if `len(malformed_documents) / total > 0.01`, raise (10× normal → operator must look).

**Why standalone:** invisible retention drift is an operational defect that doesn't share scope with the MCP / registry / migrate sweeps. Ship as its own small PR.

**Estimated size:** ~30 LOC delta + ~50 LOC tests.

### Phase 2 — stores/registry.py (19 sites, 16 DEFECT)

**File:** `src/trellis/stores/registry.py`.

**Action:** the 16 DEFECTs are mostly `ImportError` swallows that return `None` for missing optional backends (e.g., Anthropic SDK at :1554, vector backends, blob backends). Replace with explicit raise + a clear "install `trellis[<extra>]` to enable" message. The 3 GRACEFUL sites should already be annotated; verify and add inline comments where missing.

**Done when:** importing the package on a fresh install with no optionals succeeds; instantiating a backend that needs an optional dependency raises with the install instruction.

**Estimated size:** ~250 LOC delta + ~200 LOC tests.

### Phase 3 — mcp/server.py (34 sites, 31 DEFECT) — biggest single-file effort

**File:** `src/trellis/mcp/server.py` (+ any helpers `mcp/` imports).

**Action:** MCP has a structured error protocol. Replace each `except Exception: return {"error": str(exc)}` pattern with `raise` — the MCP framework wraps and surfaces it to the client correctly. For tool handlers that legitimately want to degrade (e.g., search returns empty when index is cold), annotate the case and emit a `MCP_TOOL_DEGRADED` event.

**Done when:** every flagged site is either `raise` or has an inline comment naming the rationale + emits an event. MCP integration tests pass against the loud-failure mode.

**Estimated size:** ~400 LOC delta + ~250 LOC tests.

**Risk:** MCP tool behavior changes are observable to clients. If any external integration relies on `{"error": "..."}` payloads, this is a breaking change. POC stage: no external integrations exist; document the change in CHANGELOG.

### Phase 4 — migrate/graph_migrator.py (9 DEFECT) + retrieve/pack_builder.py (9 DEFECT)

Two files, 18 DEFECTs total, both all-DEFECT (no GRACEFUL among them). Pair into one PR since both are core-library concerns.

**Action:**
- `migrate/graph_migrator.py`: migration steps that swallow errors silently leave the graph in an inconsistent state. Replace with raise + a clear `MigrationFailed("step={n}, source={...}, target={...}, ...")`.
- `retrieve/pack_builder.py`: unavailable-store strategies must raise at construction. Optional-strategy degradation must emit an event.

**Done when:** failure-injection tests demonstrate loud failure on each site. Existing PackBuilder tests still pass against the new construction-time validation.

**Estimated size:** ~250 LOC delta + ~200 LOC tests.

### Phase 5 — telemetry / observability cluster

Files: `src/trellis_api/observability.py` (3 DEFECT), `src/trellis/feedback/recording.py` (4 DEFECT), `src/trellis/classify/refresh.py` (3 DEFECT), `src/trellis/extract/dispatcher.py` (3 DEFECT).

These share a pattern: "swallow exceptions inside the observability path so the primary operation isn't broken by a telemetry failure." This is the closest thing to legitimate graceful degradation in the audit. **Per-site review needed:**

- If the primary operation already succeeded and the swallow is on a post-success telemetry emit → annotate as GRACEFUL with an explicit `TELEMETRY_EMIT_FAILED` event going somewhere durable (process-local sentinel queue + admin reconciliation command).
- If the swallow is on the primary operation itself → DEFECT, replace.

**Estimated size:** ~200 LOC delta + ~150 LOC tests.

### Phase 6 — CLI tail + executor + remaining scattered (~30 sites)

Files: `src/trellis_cli/` (15 sites, 9 DEFECT), `src/trellis/mutate/executor.py:190` (the one critical DEFECT), `src/trellis_sdk/_http.py` + `async_client.py` (4 sites, 3 DEFECT), and any small remaining concentrations.

CLI sites are mostly user-facing — they should bubble exit codes, not swallow. `executor.py:190` is the broad `except Exception` in `execute()` — replace with specific catches + emit `EXECUTION_FAILED` event with reason.

**Estimated size:** ~250 LOC delta + ~200 LOC tests.

### Phase 7 — final verification + report refresh

Re-run `python scripts/audit_silent_fallbacks.py --src src/ --output audit/silent_fallbacks_2026-NN.md` after Phases 1-6 land. Compare against the 2026-05 baseline. Any remaining DEFECTs should be either justified GRACEFUL with inline comments or explicitly tracked as known-future work.

**Done when:** DEFECT bucket count drops to ≤ 10 (from 112), and each remaining DEFECT has an inline justification comment or a tracked TODO.

**Estimated size:** ~50 LOC docs + audit re-run.

## 6. Total size estimate — revised

| Phase | LOC delta | LOC tests | Sites |
|---|---|---|---:|
| 0 (audit) ✅ | 730 | 0 | — |
| 1 (extract) | — | — | covered by Item 4 |
| 1.5 (retention P0) | 30 | 50 | 1 |
| 2 (stores/registry) | 250 | 200 | 16 |
| 3 (mcp/server) | 400 | 250 | 31 |
| 4 (migrate + pack_builder) | 250 | 200 | 18 |
| 5 (telemetry cluster) | 200 | 150 | 13 |
| 6 (CLI + executor + SDK tail) | 250 | 200 | ~30 |
| 7 (re-audit + close) | 50 | — | — |
| **Estimate** | **~1430** | **~1050** | **~109** |

Phases 1.5, 2, 3, 4, 5, 6 are **all independently parallelizable** — six concurrent swarm units feasible.

## 7. Done when

- `audit/silent_fallbacks_2026-05.md` exists and every entry is either resolved or annotated.
- `grep -rn "except.*:\s*$\s*pass" src/` returns zero hits.
- `grep -rn "except.*:\s*$\s*return\s*\[\]" src/` returns only hits that are annotated as GRACEFUL-DEGRADATION with a justification comment.
- A failure-injection test exists for at least one site per phase, demonstrating loud failure.
- CHANGELOG entry: "behavior change — formerly-silent failures now raise. See `audit/silent_fallbacks_2026-05.md` for the full list."

## 8. Risks

- **Behavior change for existing operators (if any).** POC stage: no operators. But the CHANGELOG entry is the contract for future adopters.
- **Test breakage.** Tests that asserted old empty-return behavior will fail. Update tests to assert raise; do not revive the silent path.
- **Over-correction.** Some sites are *correctly* silent (defensive programming around third-party libraries known to raise spurious exceptions). The four-bucket classification has GRACEFUL-DEGRADATION for these; over-correcting to DEFECT and forcing raise is regress. Reviewer discipline.
- **Audit-script false positives.** The greps catch syntactic patterns; semantic intent requires reading the code. The script produces a draft; humans classify. Don't auto-merge audit findings.

## 9. Why this is a cleanup track and not a feature

This plan does not add capability. It changes behavior at known and unknown call sites from quiet-and-wrong to loud-and-observable. Every change is in service of the POC discipline; once the POC ends and real users exist, some of these calls may need a softer fallback — which is the right time to add it, with a real signal. Today's job is to remove the *silent* part, not to permanently forbid graceful behavior.
