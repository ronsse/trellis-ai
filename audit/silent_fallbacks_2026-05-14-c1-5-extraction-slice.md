# C1.5 — Extraction-layer silent-except audit (2026-05-14)

> **Purpose.** Closeout audit for cleanup item **C1.5** defined in
> [`docs/design/plan-cleanup-dead-code.md`](../docs/design/plan-cleanup-dead-code.md)
> §3. C1.5 is the *extraction-layer slice* of the broader silent-fallback
> sweep ([`plan-cleanup-silent-fallbacks.md`](../docs/design/plan-cleanup-silent-fallbacks.md))
> that landed across PRs #115 – #140.
>
> **Base SHA.** `2ca9584` — v0.9.0 CHANGELOG bump.
>
> **In scope.** `src/trellis/extract/` and `src/trellis_workers/learning/`.
> Sites outside these directories are recorded as Deferred Findings.

## Audit method

Used `scripts/audit_silent_fallbacks.py` (helper-aware mode) for the two
in-scope subtrees, plus a hand grep for every `except` clause in those
subtrees, plus a hand sweep of all `_parse_*` helpers across `src/` to
catch any "looks like `_parse_candidates`" duplicates the C2 sweep
might have missed.

### `src/trellis/extract/`

```
[audit] 8 silent-fallback candidates — DEFECT: 5, GRACEFUL: 0, GUARD: 3
```

The 5 DEFECT-flagged sites all already carry canonical
`# GRACEFUL-DEGRADATION:` inline annotations (added by the C2 Phase 8
sweep, PR #140). The audit script's heuristic doesn't read comments,
so the DEFECT bucket count is a false positive that the
`tools/check_silent_fallback_annotations.py` cross-checker (if/when
extended into a strict cross-check) would reconcile. The 3 GUARD sites
are specific-catch helpers that capture exceptions for the caller to
re-raise (`_try_json_loads_with_exc`, `_clamp_confidence`,
`_load_sample_cap`).

### `src/trellis_workers/learning/`

```
[audit] 1 silent-fallback candidate — DEFECT: 1, GRACEFUL: 0, GUARD: 0
```

The single flagged site (`miner.py:189` in `generate_precedent_candidates`)
carries a canonical `# GRACEFUL-DEGRADATION:` annotation. The
`_parse_candidates` helper itself was remediated under Item 4 Phase 1
(PR #110) to the canonical emit-then-raise pattern; the audit script
correctly classifies it as NOT-SILENT (the body raises
`ExtractionFailureError`).

## Per-site audit list + decision

| File:line | Function | Pattern | Decision | Rationale |
|---|---|---|---|---|
| `src/trellis/extract/dispatcher.py:127` | `dispatch` | `except ExtractionFailureError` | (b) — degrader | ADR-extraction-failure-telemetry §2.2: dispatcher is the ONE legitimate degrader; documented in-file. |
| `src/trellis/extract/dispatcher.py:300` | `_emit_fallback` | log-only, broad | (b) — graceful | `# GRACEFUL-DEGRADATION` annotation already present (line 296). Post-decision telemetry emit-failure must not derail extraction. |
| `src/trellis/extract/dispatcher.py:323` | `_collect_findings` | log + synthetic-finding, broad | (b) — graceful | `# GRACEFUL-DEGRADATION` annotation already present (line 318). Buggy validator is converted to a loud rejection (synthetic `validator_error` finding), not silently swallowed. |
| `src/trellis/extract/dispatcher.py:480` | `_emit_extraction_rejected` | log-only, broad | (b) — graceful | `# GRACEFUL-DEGRADATION` annotation already present (line 476). Rejection is enforced in-memory before the emit; emit failure cannot undo it. |
| `src/trellis/extract/llm.py:201` | `extract` | re-raise after emit | (a) — emit-then-raise | Remediated under Item 4 Phase 1 (PR #110). Uses `emit_extraction_failure` + `raise ExtractionFailureError`. |
| `src/trellis/extract/llm.py:316` | `_try_json_loads_with_exc` | return-tuple-with-exc, specific | (b) — guard | Helper captures the exception so the caller can re-raise with full telemetry. Function name signals try-style intent. |
| `src/trellis/extract/llm.py:444` | `_clamp_confidence` | return default, specific | (b) — guard | Per-record field coercion; absence is meaningful (drop to caller-supplied default 0.5). Plan §3 C1.5 explicit allowance ("optional-field decode; absence is meaningful"). |
| `src/trellis/extract/registry.py:102` | `load_entry_points` | log + continue, broad | (b) — graceful | `# GRACEFUL-DEGRADATION` annotation already present (line 100). One broken plugin must not block the registry. |
| `src/trellis/extract/telemetry.py:112` | `_load_sample_cap` | re-raise as ValueError, specific | (a) — loud | Env-var parser; misconfiguration raises with the variable name in the message ("loud on misuse"). |
| `src/trellis/extract/telemetry.py:332` | `emit_extraction_failure` | log + return, broad | (b) — graceful | `# GRACEFUL-DEGRADATION` annotation already present (line 329). A broken event log must not break the extractor's emit-then-raise contract; caller still raises. |
| `src/trellis_workers/learning/miner.py:189` | `generate_precedent_candidates` | log + return [], broad | (b) — graceful | `# GRACEFUL-DEGRADATION` annotation already present (line 185). Precedent mining is best-effort on cron; LLM outage signals "no candidates this round." |
| `src/trellis_workers/learning/miner.py:228` | `_parse_candidates` | re-raise after emit | (a) — emit-then-raise | Remediated under Item 4 Phase 1 (PR #110). Uses `emit_extraction_failure` + `raise ExtractionFailureError`. |
| `src/trellis_workers/learning/miner.py:275` | `_parse_candidates` (loop) | re-raise after emit | (a) — emit-then-raise | Remediated under Item 4 Phase 1 (PR #110). Per-precedent validation also raises loudly. |

**Total in-scope sites:** 13.
- **(a) emit-then-raise / loud:** 5 (4 already remediated under Item 4 Phase 1; 1 env-parser).
- **(b) graceful or guard, annotated:** 8.
- **Sites needing new code change in C1.5:** **0.**

## Conclusion

The extraction-layer slice of the silent-fallback sweep is **complete
at base SHA `2ca9584`.** Every silent-`except` in `src/trellis/extract/`
and `src/trellis_workers/learning/` is either:

1. Routed through the emit-then-raise pattern (Item 4 Phase 1, PR #110), or
2. A specific-catch guard with semantic justification, or
3. A graceful-degradation site with a canonical inline
   `# GRACEFUL-DEGRADATION:` annotation (C2 Phase 8, PR #140).

C1.5's `[x]` checkbox in TODO.md is earned by this audit. No new code
changes are required; this audit log is the deliverable.

## Out-of-scope hits → Deferred Findings

One extraction-adjacent site was located outside the in-scope paths
and is captured here for the parent PR's Deferred Findings block:

- **`src/trellis_workers/enrichment/service.py:228, 233`** —
  `EnrichmentService._parse_response` silently returns
  `EnrichmentResult(success=False, error=...)` on JSON decode failure
  rather than emitting `EXTRACTION_FAILED` + raising. The audit script
  classifies these as GUARD (specific-catch with non-trivial body) and
  the C2 sweep did not promote them, but they share the original
  `_parse_candidates` shape that C1.5 is supposed to surface. Could be
  considered a follow-up cleanup if the enrichment path is wired
  through the new failure-telemetry analyzer in the future.
  [severity:M] [scope:follow-up]
