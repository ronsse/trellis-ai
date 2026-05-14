# C2 Phase 8 — final verification (2026-05-13)

> **Purpose.** Closeout snapshot for the C2 silent-fallback cleanup
> program defined in
> [`docs/design/plan-cleanup-silent-fallbacks.md`](../docs/design/plan-cleanup-silent-fallbacks.md).
> Phase 8 closed the unjustified-DEFECT survivors that Phase 7
> ([D3 report](silent_fallbacks_2026-05-12-final.md)) surfaced.
>
> **Mode note.** Generated in the new default *helper-aware* mode
> (PR [#128](https://github.com/ronsse/trellis-ai/pull/128)). The
> legacy *literal-only* numbers are preserved in
> [`silent_fallbacks_2026-05-12-final-literal.md`](silent_fallbacks_2026-05-12-final-literal.md)
> for historical back-compare.

## Headline counts

| Reading | Phase 7 (D3) | Phase 8 (now) | Delta |
|---|---:|---:|---:|
| Helper-aware — total `except` sites | 125 | **136** | +11 |
| Helper-aware — DEFECT | 67 | **73** | +6 |
| Helper-aware — GRACEFUL-DEGRADATION | 11 | **13** | +2 |
| Helper-aware — GUARD | 47 | **50** | +3 |
| Helper-aware — TEST-ONLY | 0 | 0 | 0 |
| **Unjustified DEFECTs** (no canonical inline annotation) | **34** | **0** | **−34** |

The bucket totals creep up because seven new sites landed between Phase
7 and Phase 8 (Items 1, 6, 7 plus the followup PRs). The headline
result is **0 unjustified DEFECTs**: every site bucketed as DEFECT by
the audit heuristic now carries an inline canonical annotation
matching the strict rubric (`# GRACEFUL-DEGRADATION:`, `# GUARD:`,
or `# AGGREGATE:`).

## What Phase 8 did

The 34 D3 survivors plus ~7 new sites broke into three groups:

### 1. Promotion of informal Phase 5 comments (13 sites)

The Phase 5 cleanup ([PR #122](https://github.com/ronsse/trellis-ai/pull/122))
shipped inline comments of the form `# GRACEFUL-DEGRADATION (C2 Phase
5): …`. The parenthetical interrupts the colon, so the rubric's strict
regex (`# GRACEFUL-DEGRADATION:`) did not recognize them. Promoted to
the canonical form so the audit cross-check matches:

- `trellis/classify/refresh.py` (3)
- `trellis/extract/dispatcher.py` (3)
- `trellis/feedback/recording.py` (3)
- `trellis_api/observability.py` (4)

### 2. Annotation of informally-justified sites (25 sites)

These carried docstring or inline prose justification but no canonical
inline label. Promoted to the appropriate label based on the failure
shape:

- **GRACEFUL-DEGRADATION** (telemetry, optional plugins, MCP-tool
  surface that "never raises to client"): `trellis/extract/registry.py`,
  `trellis/extract/telemetry.py`, `trellis/feedback/recording.py:_parse_timestamp`,
  `trellis/ops/recording.py`, `trellis/ops/registry.py`,
  `trellis/plugins/loader.py` (2), `trellis/retrieve/effectiveness.py`,
  `trellis/retrieve/observation_strategy.py` (3),
  `trellis/retrieve/semantic_seeds.py`, `trellis/retrieve/strategies.py`,
  `trellis/stores/advisory_store.py`, `trellis/stores/policy_store.py`,
  `trellis/stores/sqlite/base.py:close`,
  `trellis/mcp/server.py` (3 new observation-tool sites),
  `trellis_api/routes/admin.py`, `trellis_api/routes/health.py`,
  `trellis_cli/admin.py` (3), `trellis_cli/admin_migrate_provenance.py:event_log`,
  `trellis_cli/claude_integration.py`,
  `trellis_workers/enrichment/service.py`,
  `trellis_workers/learning/miner.py`.
- **GUARD** (defensive parsers, optional imports):
  `trellis/retrieve/observation_strategy.py` (2 parse helpers),
  `trellis/stores/pgvector/store.py:_init_schema` (type-literal parser),
  `trellis/stores/sqlite/base.py:_current_mode`,
  `trellis_sdk/_http.py` (2 parse helpers).
- **AGGREGATE** (per-row failures collected and surfaced later):
  `trellis/retrieve/pack_builder.py:build` + `build_sectioned`,
  `trellis_api/routes/ingest.py:upsert_vectors` + `ingest_bulk`,
  `trellis_workers/maintenance/retention.py:run`,
  `trellis_cli/admin_migrate_provenance.py:_migrate_one_edge`.

### 3. Targeted fix (1 site)

`trellis/retrieve/budget_config.py:from_dict` was silently swallowing a
malformed config dict (broad `except Exception: return cls()`). Even
though falling back to hardcoded defaults is the right operational
shape (a misconfig must not block startup), the silent swallow left no
operator signal. Narrowed the catch to `pydantic.ValidationError`,
added a `logger.warning("budget_config_validation_failed", exc_info=True)`
above the fallback, and labeled with `# GRACEFUL-DEGRADATION:`.

## Per-bucket annotation counts (this PR)

| Bucket | Sites annotated |
|---|---:|
| GRACEFUL-DEGRADATION (new + promoted) | 41 |
| GUARD | 6 |
| AGGREGATE | 7 |
| Fixed (narrow catch + add logging) | 1 |
| **Total addressed** | **55** |

## Cross-check methodology

A short Python script reads the audit's DEFECT line list, then for
each `(file, lineno)` pair scans a window (`lineno - 6` to
`lineno + 4`) for a canonical comment matching the strict regex
`#\s*(GRACEFUL-DEGRADATION:|GUARD:|AGGREGATE:)`. Anything that
matches counts as justified.

```text
Total DEFECT: 73
Unjustified DEFECT: 0
```

## Hardest classification call

`trellis/stores/{advisory_store,policy_store}.py:_load` — both load
JSON files at construction time and currently swallow any decode /
validation error to an empty in-memory store. The argument for FIX
(narrow + raise) is real: a corrupted policy file silently disables
all policy checks. The argument for ANNOTATE-GRACEFUL: every consumer
of the policy store already deny-by-defaults when no policy is loaded
(`MutationExecutor`'s policy stage), and crashing the registry at
construction time would break the CLI's recovery paths (you can't
`trellis admin policies import` if the registry won't start). Settled
on ANNOTATE-GRACEFUL with a strong inline note pointing at the
deny-by-default semantics — Phase 8.1 can revisit if a real
mis-config-on-startup incident shows the silent path is dangerous in
practice.

## Done-when (Phase 7 target restated)

- [x] `audit/silent_fallbacks_2026-05-13-phase8-final.md` exists
- [x] Unjustified DEFECT count ≤ 10 — **actual: 0**
- [x] No silent-swallow site introduces a behavior change without an
      inline justification

The C2 cleanup program is now complete.
