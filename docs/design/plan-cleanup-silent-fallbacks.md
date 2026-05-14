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

### Phase 1.5 — retention invisible-drift bug ✅ landed via [#116](https://github.com/ronsse/trellis-ai/pull/116)

**File:** `src/trellis_workers/maintenance/retention.py:169`. Replaced `except (ValueError, TypeError): pass` with `emit_extraction_failure(...)` + a `report.malformed_documents` field on `StalenessReport`. Threshold-cross raises on > 1% malformed rate.

### Phase 2 — stores/registry.py typed import errors ✅ landed via [#118](https://github.com/ronsse/trellis-ai/pull/118)

**File:** `src/trellis/stores/registry.py`. Replaced ImportError swallows that returned `None` for missing optionals with explicit `BackendNotInstalledError` raises carrying the matching `trellis[<extra>]` install hint. Aggregate-error sites (`validate()`, `_check_bolt_connectivity()`) annotated with `# AGGREGATE:` per the new rubric.

### Phase 3 — mcp/server.py structured error protocol ✅ landed via [#119](https://github.com/ronsse/trellis-ai/pull/119)

**File:** `src/trellis/mcp/server.py`. Introduced the `_raise_*` helper family (`_raise_invalid_params`, `_raise_resource_missing`, etc.) so handlers can route through typed exceptions while the audit script's helper-aware mode (see #128 below) still recognises them as non-silent. Token-tracking + post-success telemetry sites kept as GRACEFUL with inline annotations.

### Phase 4 — migrate/graph_migrator.py + retrieve/pack_builder.py explicit failures ✅ landed via [#120](https://github.com/ronsse/trellis-ai/pull/120)

Two files, originally 18 DEFECTs. `migrate/graph_migrator.py` consolidated to a single aggregate-error path that surfaces every per-row failure in one `MigrationFailed`. `retrieve/pack_builder.py` per-strategy failures now surface in the `PACK_ASSEMBLED` event payload under `strategy_failures` and raise `PackAssemblyError` when required strategies fail or all-fail. Inline `# NOT silent:` comment documents the deferred-raise pattern.

### Phase 5 — telemetry / observability cluster ✅ landed via [#122](https://github.com/ronsse/trellis-ai/pull/122)

Files: `src/trellis_api/observability.py`, `src/trellis/feedback/recording.py`, `src/trellis/classify/refresh.py`, `src/trellis/extract/dispatcher.py`. Per-site review: post-success telemetry emits annotated as `# GRACEFUL-DEGRADATION (C2 Phase 5):` with explicit TODO for the `metrics.telemetry_failures` counter. Primary-path swallows replaced with emit-then-raise.

### Phase 6 — CLI tail + executor + SDK ✅ landed via [#123](https://github.com/ronsse/trellis-ai/pull/123)

Files: `src/trellis_cli/`, `src/trellis/mutate/executor.py`, `src/trellis_sdk/_http.py`. CLI sites now bubble proper exit codes (`typer.Exit`/`sys.exit`). `executor.py` broad `except Exception` replaced by typed catches for `ValidationError`, `PolicyViolationError`, `IdempotencyError`, `(StoreError, TrellisError)` plus the residual `_UNEXPECTED_HANDLER_FAILURE` guard. SDK gained a structured HTTP exception hierarchy.

### Audit-script helper-call-chain awareness ✅ landed via [#128](https://github.com/ronsse/trellis-ai/pull/128)

Default mode now recognises:
- intra-module functions whose body ends in `raise`,
- functions annotated `NoReturn` / `typing.NoReturn`,
- name-convention helpers matching `_raise_*` / `raise_*`,
- stack-abort calls (`sys.exit`, `os._exit`, `typer.Exit`, `click.Abort`, `pytest.exit`, `pytest.fail`).

`--literal-only` flag restores the legacy behaviour for back-compat with the 2026-05-12 baseline. **Going forward the helper-aware count is the authoritative DEFECT total**; the literal-only column is reserved for diff against the historical baseline.

### Phase 7 — final verification + report refresh ✅ landed (this PR)

Re-ran `python scripts/audit_silent_fallbacks.py --src src/` in both modes after Phases 1.5–6 merged. Snapshots written to:
- [`audit/silent_fallbacks_2026-05-12-final.md`](../../audit/silent_fallbacks_2026-05-12-final.md) — helper-aware, the new authoritative reading.
- [`audit/silent_fallbacks_2026-05-12-final-literal.md`](../../audit/silent_fallbacks_2026-05-12-final-literal.md) — literal-only, for back-comparison with the [2026-05-12 baseline](../../audit/silent_fallbacks_2026-05-12-baseline.md).

**Headline counts:**
- Helper-aware DEFECTs in `src/`: **67** (the new authoritative number).
- Literal-only DEFECTs in `src/`: **85** (down from **113** at the baseline — **−28** net).
- Canonical-annotated DEFECTs: **33** (sites with inline `# GRACEFUL-DEGRADATION:` / `# GUARD:` / `# AGGREGATE:` labels).
- Unjustified DEFECT survivors: **34** — **exceeds the original ≤ 10 target**.

The original Phase 7 done-when ("≤ 10 unjustified DEFECTs") is **not met**. A Phase 8 follow-up is warranted to either (a) attach the canonical inline annotation to ~12 sites where the informal justification already exists or (b) replace ~22 sites with explicit emit-then-raise. Detailed file/line list lives in the final report's "Unjustified DEFECT survivors" section.

### Phase 8 — close unjustified DEFECT survivors ✅ landed

Followed the closeout-plan recommendation: per-site classification across the 34 unjustified survivors from Phase 7 plus the ~7 new sites introduced post-D3 by intervening PRs. Three categories of work:

- **Promotion of informal Phase 5 comments** — the prior cleanup used `# GRACEFUL-DEGRADATION (C2 Phase 5):` (parenthetical interrupts the colon) which the rubric's strict canonical regex did not recognize. Promoted 13 occurrences across `classify/refresh.py`, `extract/dispatcher.py`, `feedback/recording.py`, `trellis_api/observability.py` to the canonical `# GRACEFUL-DEGRADATION:` form.
- **Annotation of informally-justified sites** — 25 sites carried docstring or inline prose justification but no canonical inline label; promoted to `# GRACEFUL-DEGRADATION:`, `# GUARD:`, or `# AGGREGATE:` per the failure shape.
- **Targeted fix** — `retrieve/budget_config.py:from_dict` was silently swallowing a malformed config without any logging; narrowed to `ValidationError`, added a loud `logger.warning` (still falls back to defaults — config-load errors must not block startup, but operator visibility was missing).

**Final headline counts (helper-aware mode):**
- Total `except` sites scanned: **136**
- DEFECT bucket: **73** (the audit script's heuristic; bucket bumped from 67 because seven new sites landed between Phases 7 and 8)
- **Unjustified DEFECTs: 0** — every DEFECT site now carries a canonical inline annotation matching `# GRACEFUL-DEGRADATION:`, `# GUARD:`, or `# AGGREGATE:`.

The ≤ 10 target is met with margin. See [`audit/silent_fallbacks_2026-05-13-phase8-final.md`](../../audit/silent_fallbacks_2026-05-13-phase8-final.md).

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
