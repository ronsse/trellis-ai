# Plan: Extraction-failure telemetry + analyzer

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable
**ADR:** [`adr-extraction-failure-telemetry.md`](./adr-extraction-failure-telemetry.md)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 4
**Depends on:** [`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md) (the broader audit) — but this plan is the canonical example and can land first; cleanup picks up sibling sites.
**Unblocks:** Item 7 (coding-agent loop) consumes the new event stream.

## 1. Scope

**In scope:**
- New `EXTRACTION_FAILED` event type registered in the operational EventLog.
- `src/trellis/extract/telemetry.py::emit_extraction_failure()` helper with sampling.
- Replace silent `except json.JSONDecodeError: return []` in `LLMExtractor._parse_candidates()` and `trellis_workers/learning/miner.py::_parse_candidates`.
- `src/trellis/retrieve/extraction_health.py::analyze_extraction_health()` analyzer.
- CLI: `trellis analyze extraction-health`.
- Eval scenario with deterministic failure injection.

**Out of scope:**
- Replacing every silent fallback in the codebase ([`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md) owns the rest).
- Per-extractor confidence decay (future tuning loop).
- LLM-prompt-rewriting autofix (Item 7 owns).

## 2. POC directives applied

- All replaced `except: return []` sites **emit + re-raise** by default. Graceful degradation requires explicit opt-in by the *caller*, never by the failing function.
- `analyze_extraction_health()` reads from EventLog; if the EventLog backend hasn't been wired (config error), the analyzer **raises**. No silent "empty report" fallback.
- The sampling cap defaults to 10 but the analyzer **logs WARN** if it detects >5 clusters that hit the cap in the last hour — the operator should consider raising the cap or addressing the underlying degradation.

## 3. Phases

### Phase 0 — event registration + helper

**Files to touch:**
- `src/trellis/stores/base/event_log.py` — add `EXTRACTION_FAILED` to the `EventType` enum. *(Plan originally pointed at `src/trellis/schemas/event.py`; corrected after Item 4 implementation — the enum actually lives in `stores/base/event_log.py`.)*
- `src/trellis/extract/telemetry.py` — extend existing module with `emit_extraction_failure(*, ...)`, sampling state, redaction.
- `tests/unit/extract/test_telemetry.py` — extend with 8 new tests: emit shape, sampling math, redaction, env-var bypass, cap-warning.

**Helper signature:**

```python
def emit_extraction_failure(
    *,
    event_log: EventLog,
    extractor_id: str,
    extractor_tier: Literal["deterministic", "hybrid", "llm"],
    failure_kind: ExtractionFailureKind,
    source_hint: str | None = None,
    prompt_hash: str | None = None,
    source_excerpt_hash: str | None = None,
    model: str | None = None,
    error_class: str,
    error_excerpt: str,
    correlation_id: str | None = None,
) -> None:
    """Emit an EXTRACTION_FAILED event. Applies redaction + sampling."""
```

The sampling state is a per-process LRU keyed by `(extractor_id, prompt_hash, failure_kind)`. POC scope: in-process state only; multi-process aggregation deferred.

**Redaction:** the helper applies `_REDACTORS: list[tuple[re.Pattern, str]]` over `error_excerpt`. POC seed: email pattern, UUID-shaped IDs, SSN-shaped 3-2-4 digits. Patterns are conservative — false positives (over-redaction) are acceptable; false negatives (PII leak) are not.

**Estimated size:** ~250 LOC + ~200 LOC tests.

### Phase 1 — replace silent except in LLMExtractor + miner

**Files to touch:**
- `src/trellis/extract/llm.py` — replace the silent parse-fallback branch (`llm_extractor_parse_failed` log line, in `LLMExtractor.extract()` — *not* `_parse_candidates()` which the original plan named; that method doesn't exist) with the new helper + raise. Also catch `pydantic.ValidationError` separately as `failure_kind="validation_error"`.
- `src/trellis_workers/learning/miner.py` — same shape.
- `src/trellis/extract/dispatcher.py` (ExtractionDispatcher) — the *one* caller that legitimately wants degradation. Catch the new raises, emit `failure_kind="tier_fallback"` event with the original failure_kind in `error_class`, continue. Document inline why this site degrades.
- `tests/unit/extract/test_llm.py` — update existing tests for the new raise behavior. Add 3 new tests: parse_error raises, validation_error raises, dispatcher fallback path still works end-to-end.

**Estimated size:** ~80 LOC modified + ~150 LOC test updates.

### Phase 2 — analyzer

**Files to touch:**
- `src/trellis/retrieve/extraction_health.py` — new module.
- `tests/unit/retrieve/test_extraction_health.py` — new file. 6 tests.

**Analyzer API:**

```python
@dataclass
class ExtractionHealthReport:
    window_start: datetime
    window_end: datetime
    total_failures: int
    by_extractor: dict[str, int]                # extractor_id -> count
    by_failure_kind: dict[str, int]
    clusters: list[ExtractionFailureCluster]    # top-N by count
    sampling_capped_clusters: list[str]         # cluster_keys that hit the sample cap
    drift_alerts: list[ExtractionDriftAlert]    # extractor whose failure rate jumped

@dataclass
class ExtractionFailureCluster:
    cluster_key: str             # hash of (extractor_id, prompt_hash, failure_kind)
    extractor_id: str
    failure_kind: str
    count: int
    first_seen: datetime
    last_seen: datetime
    sample_event_ids: list[str]  # first 3 event_ids in the cluster
    model: str | None
    prompt_hash: str | None

def analyze_extraction_health(
    *,
    event_log: EventLog,
    since: datetime,
    until: datetime | None = None,
    top_n_clusters: int = 20,
    drift_window: timedelta = timedelta(hours=24),
) -> ExtractionHealthReport:
    ...
```

**Drift detection:** for each extractor, compare its failure rate in `[until - drift_window, until]` to `[until - 2*drift_window, until - drift_window]`. Alert when the recent rate is ≥ 2× the prior rate AND prior rate ≥ N=5. Threshold tunable via parameter registry (Item 3 wiring).

**Estimated size:** ~400 LOC code + ~250 LOC tests.

### Phase 3 — CLI

**Files to touch:**
- `src/trellis_cli/analyze.py` — add `extraction-health` subcommand.
- `tests/unit/cli/test_analyze.py` — add test for the subcommand.

**CLI:**

```
trellis analyze extraction-health
    --since 24h            # default
    --until now            # default
    --top 20               # number of clusters to show
    --format json|table    # default table
    --drift-only           # only show clusters with drift alerts
```

The command exits with code 1 if `--strict` is set and any drift alerts fire. Otherwise exit 0. The `--strict` mode is what CI gates against.

**Estimated size:** ~150 LOC + ~100 LOC tests.

### Phase 4 — eval scenario

**File:**
- `eval/scenarios/extraction_failure_clustering.py` — new.

**Behavior:** deterministically inject 50 malformed-JSON failures from LLMExtractor across 3 distinct prompt-hash buckets. Run `analyze_extraction_health()`. Assert:

- 3 clusters reported, with counts {20, 20, 10}.
- Each cluster has stable `cluster_key` across re-runs (deterministic generator).
- Sampling cap fires for the 2 buckets with count=20; not for the bucket with count=10.
- Drift alert fires when 30 of the 50 are concentrated in the last drift_window.

**Estimated size:** ~250 LOC.

## 4. Total size estimate

| Phase | LOC code | LOC tests |
|---|---|---|
| 0 | 250 | 200 |
| 1 | 80 (delta) | 150 |
| 2 | 400 | 250 |
| 3 | 150 | 100 |
| 4 | 250 | 0 (scenario *is* the test) |
| **Total** | **~1130** | **~700** |

Sized for **two swarm units**: Phases 0+1 (event + replacement, single PR), Phases 2+3+4 (analyzer + CLI + scenario, single PR).

## 5. Done when

- 7 new tests pass (Phase 0); 3 modified tests pass (Phase 1); 6 new tests pass (Phase 2); 1 new CLI test passes (Phase 3); 1 scenario passes (Phase 4).
- `grep -rn "except json.JSONDecodeError" src/trellis/extract/ src/trellis_workers/learning/` returns zero hits where the handler returns empty silently.
- `trellis analyze extraction-health --since 24h` produces a coherent report against a system with no failures (empty report, not error).
- mypy clean.

## 6. Cleanup considerations

This plan is the canonical example for [`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md). Once the helper + EventType exist, the cleanup plan can systematically replace all sibling silent-fallback sites in `src/`. The two plans share the helper module.

After landing, `extract_telemetry.py` (which today counts deterministic fallbacks for the dispatcher) merges with the new module — they're the same shape. Consolidate as part of Phase 0.

## 7. Risks

- **Event volume.** A bad model upgrade could fire 100K parse errors in an hour. Sampling caps the worst case, but the analyzer needs to be efficient: a single EventLog query with a `WHERE event_type = 'EXTRACTION_FAILED' AND occurred_at >= ?` + Python-side aggregation. Index on `(event_type, occurred_at)` already exists in SQLite/Postgres backends. Verify for ArcadeDB.
- **PII regression.** The redactor is conservative but not perfect. A second-line defense: the `error_excerpt` field has a hard 200-char cap. Even a missed redaction is bounded.
- **Test brittleness on sampling state.** Sampling state is process-local; test isolation requires resetting between tests. Use a `pytest` fixture `reset_extraction_telemetry_state` that runs autouse in the test module.
