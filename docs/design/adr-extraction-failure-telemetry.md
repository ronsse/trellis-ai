# ADR: Extraction-failure telemetry

**Status:** Proposed
**Date:** 2026-05-11
**Deciders:** Trellis core
**Related:**
- [`./plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md) — implementation plan
- [`./plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md) — the same audit dimension applied broadly
- [`./adr-extraction-validation.md`](./adr-extraction-validation.md) — current extraction validation contract
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program this sits inside

---

## 1. Context

Today extractors fail silently in three places, all of which destroy signal a self-improving system needs:

| Site | Today's behavior | What we lose |
|---|---|---|
| `LLMExtractor._parse_candidates()` | catches `json.JSONDecodeError`, logs WARN, returns empty list | No per-prompt failure rate; no signal that a model upgrade broke our extraction prompt; no way to triage "extractor X is now 30% broken on input shape Y." |
| `ExtractionDispatcher` tier fallback | When deterministic tier returns empty, falls through to LLM tier silently | No signal that deterministic rules are degrading; LLM cost rises without explanation. |
| Worker miner `_parse_candidates` (trellis_workers/learning/miner.py) | catches JSONDecodeError, logs WARN, returns [] | Same shape; learning miner drops candidates without surfacing how many. |

All three log at WARN. None emit events. None are aggregated. None feed back to anything. They are the canonical example of the POC directive's "silent fallback" defect ([`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §2.1).

## 2. Decision

Introduce a new operational EventLog event type `EXTRACTION_FAILED` emitted at every failure site. Provide a `src/trellis/extract/telemetry.py` helper. Replace silent except blocks with explicit emit-then-raise (or emit-then-degrade-with-reason, only where the call site is *documented* graceful-degradation).

### 2.1 Event schema

```python
class ExtractionFailedPayload(TypedDict):
    extractor_id: str             # e.g., "LLMExtractor", "JSONRulesExtractor", "DbtManifestExtractor"
    extractor_tier: Literal["deterministic", "hybrid", "llm"]
    failure_kind: Literal[
        "parse_error",            # malformed output from upstream (LLM JSON, manifest schema)
        "validation_error",       # structurally valid but Pydantic rejected
        "policy_violation",       # MutationExecutor policy gate denied
        "low_confidence",         # deterministic classifier under threshold; not in this scope but reuses path
        "tier_fallback",          # deterministic returned empty; dispatcher chose to escalate
        "model_error",            # LLM provider error (timeout, 5xx)
        "budget_exhausted",       # LLM budget cap hit; same shape as model_error for analytics
    ]
    source_hint: str | None       # e.g., "dbt:manifest.json", "openlineage:RunEvent"
    prompt_hash: str | None       # SHA-256 of the LLM prompt template + injected vars; for LLM failures
    source_excerpt_hash: str | None  # SHA-256 of the input being extracted (first 2KB); for clustering
    model: str | None             # e.g., "anthropic:claude-sonnet-4-6"; for LLM failures
    error_class: str              # exception class name; never the message (PII risk)
    error_excerpt: str            # first 200 chars of str(exc), redacted of any property values
    occurred_at: datetime
    correlation_id: str | None    # trace_id or run_id if available
```

**Privacy:** the payload deliberately stores hashes, not raw content. `error_excerpt` is bounded and redacted; full stack traces stay in structlog. Operators wanting full traces consult logs; the EventLog stays compact and shareable.

### 2.2 Loud failure as default

Every extraction failure path emits an `EXTRACTION_FAILED` event and **re-raises** by default. Graceful degradation is opt-in per call site, documented inline, and emits a `tier_fallback` event explicitly.

Concretely, this `LLMExtractor._parse_candidates()` change:

```python
# Today (defect)
try:
    candidates = json.loads(response_text)
except json.JSONDecodeError:
    log.warning("malformed LLM response", model=self.model)
    return []

# After
try:
    candidates = json.loads(response_text)
except json.JSONDecodeError as exc:
    emit_extraction_failure(
        extractor_id=self.__class__.__name__,
        extractor_tier="llm",
        failure_kind="parse_error",
        prompt_hash=self._current_prompt_hash,
        source_excerpt_hash=self._current_source_hash,
        model=self.model,
        error_class=type(exc).__name__,
        error_excerpt=str(exc)[:200],
        correlation_id=self._current_correlation_id,
    )
    raise  # POC directive: no silent fallback
```

Callers that *want* to degrade (e.g., `ExtractionDispatcher` choosing to skip a row rather than abort a batch) catch the exception explicitly, emit a `tier_fallback` event with the original failure_kind, and continue. The fallback is **explicit and observable**, not silent.

### 2.3 Sampling and rate limiting

Extraction failures can be high-frequency (one model upgrade can fire 10K events in an hour). The emitter applies **sampling at high cardinality**: for any `(extractor_id, prompt_hash, failure_kind)` triple, the first 10 events in a 10-minute window are emitted in full; subsequent events in the window emit aggregate-only updates (count delta) on the most recent event_id. This keeps the EventLog from drowning.

The sampling cap is `EXTRACTION_FAILURE_SAMPLE_CAP` env var (default 10). Operators with tight cost budgets can set it lower.

## 3. Consequences

### 3.1 What changes for callers

- LLM extractors that previously returned `[]` on parse error now raise. The single caller — `ExtractionDispatcher` — must explicitly opt into "skip this row" behavior. Every other LLM extractor caller in tests and worker code gets an explicit decision.
- Worker miners (`trellis_workers/learning/miner.py`) lose their silent skip. They must explicitly opt into row-skip with an event emission.

This is a behavioral change for any code that depended on extractors returning empty on failure. The migration is mechanical: wrap the call in try/except, emit `tier_fallback`, continue.

### 3.2 What this unblocks

- Item 7 (coding-agent loop) consumes `EXTRACTION_FAILED` clusters as one of its three signal types.
- The `trellis analyze extraction-health` CLI (see plan) lets operators see degradation trends.
- Per-extractor confidence decay (a future tuning loop) has the signal it needs.

### 3.3 What this does *not* do

- Does not change `ContentTags` classification confidence handling — that's a separate axis.
- Does not introduce an extractor-level retry loop. Retry behavior is up to the caller; the event records the failure regardless.
- Does not change MutationExecutor policy gate behavior. Policy violations were already loud (they raise). The new event type is *also* emitted there for unified analytics; the existing `MUTATION_REJECTED` event continues to exist.

## 4. Guardrails

### 4.1 PII

`error_excerpt` is bounded at 200 chars and the helper applies a regex-based redactor for common patterns (email, UUIDs that look like account IDs, SSN-shaped strings). Full content stays in structlog logs at DEBUG level; the EventLog payload is shareable.

### 4.2 Sampling is not silencing

The first 10 events per cluster per window are emitted in full. Sampling kicks in only beyond that. Operators always see the first failure of any new cluster; they only lose detail on already-known clusters that re-fire heavily.

### 4.3 Test surface

The helper has a `EXTRACTION_FAILURE_NO_SAMPLE` env var test-only override that disables sampling. The contract tests verify sampling math; integration tests verify the no-sample mode for deterministic assertions.

## 5. Alternatives considered

- **Reuse existing `MUTATION_REJECTED`.** Different layer of the stack (post-extraction). Rejected — events should match the layer that produced them so analytics can group cleanly.
- **Structured log fields only, no event.** Logs are not queryable and not subject to retention guarantees the EventLog provides. Rejected.
- **Generic `FAILURE_RECORDED` event with category=...** Too generic; the analytics consumer wants per-extractor breakdowns that benefit from a typed payload. Rejected.

## 6. References

- Existing `Operation` registry: `src/trellis/mutate/operations.py`
- Existing EventLog schema: `src/trellis/schemas/event.py`
- Existing `extract_telemetry.py` (deterministic fallback counter): `src/trellis/extract/telemetry.py` if present, else this ADR's helper creates it
