# Plan: Dogfooding meta-traces

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable
**ADR:** [`adr-dogfooding-meta-traces.md`](./adr-dogfooding-meta-traces.md)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 6
**Depends on:**
- Item 1 ([`plan-observation-entity-type.md`](./plan-observation-entity-type.md)) — Observation type exists.
- Item 2 ([`plan-provenance-columns.md`](./plan-provenance-columns.md)) — `wasInformedBy` / `wasGeneratedBy` edges carry provenance columns.

**Unblocks:**
- Scenario 5.4 (loop convergence) can land.
- Item 7 (coding-agent loop) consumes meta-Activity provenance.

## 1. Scope

**In scope:**
- `src/trellis/meta/__init__.py` (new) — meta-trace machinery.
- `record_meta_analysis(...)` helper consumed by analyze/tune/promote CLI commands.
- Merge-within-window logic.
- Sampling (first 10 / last 10 / reservoir 30).
- Synthetic `Agent` node creation (`trellis_meta_analyzer`, `trellis_meta_tuner`, etc.).
- Wire into existing CLI commands: `analyze context-effectiveness`, `analyze advisory-effectiveness`, `analyze learning-observations`, `analyze extraction-health` (Item 4), `analyze schema-evolution` (Item 5), `tune`, `promote`.
- PackBuilder default filter for `trellis_meta_*` Agents (opt-in via `include_meta=True`).
- `--no-meta-trace` and `TRELLIS_META_TRACES=off` opt-outs.
- Eval scenario.

**Out of scope:**
- New analyzers (each existing analyzer gets the meta-trace wiring; new ones are out of scope here).
- Meta-trace compaction. The existing `compact_versions()` handles it; this plan does not schedule it.
- PackBuilder ranking of meta-Activities — they're filtered by default; ranking is moot until an operator opts in.

## 2. POC directives applied

- `TRELLIS_META_TRACES` env var: any value other than `on|off` **raises** at CLI startup. No silent default flip.
- A meta-Activity creation fails if its synthetic Agent node cannot be ensured-exists — **raises**, does not silently skip.
- If sampling reservoir fails (e.g., random module state corrupted), **raises** — no silent "skip provenance edges" path.
- The merge-window check requires reading the prior Activity from GraphStore. If the read fails (transient backend issue), the helper **raises** — does not silently create a duplicate Activity.

## 3. Phases

### Phase 0 — meta-trace primitive

**Files to touch:**
- `src/trellis/meta/__init__.py` (new) — public API.
- `src/trellis/meta/recorder.py` (new) — `record_meta_analysis()` implementation.
- `src/trellis/meta/agents.py` (new) — synthetic agent registry (`META_ANALYZER`, `META_TUNER`, etc.).
- `src/trellis/meta/sampling.py` (new) — reservoir sample with deterministic test-only seed.
- `tests/unit/meta/test_recorder.py` (new).
- `tests/unit/meta/test_sampling.py` (new).

**API:**

```python
@contextmanager
def record_meta_analysis(
    *,
    analyzer: str,                    # "context-effectiveness", "tune", etc.
    operator: Literal["cli", "ci", "cron", "test"],
    invocation_id: str,
    input_window_start: datetime,
    input_window_end: datetime,
    graph_store: GraphStore,
    event_log: EventLog,
    merge_window: timedelta = timedelta(minutes=5),
    sample_size: int = 50,
) -> MetaAnalysisContext:
    """Record an analyzer invocation as a meta-Activity.

    On enter: checks merge window; either creates a new Activity or returns
    an existing one for merge.

    Within the context: caller invokes `ctx.consume_event(event_id)` for each
    operational event consumed (reservoir sampled to sample_size), and
    `ctx.attach_output(node_id)` for each Observation / Advisory / etc.
    produced.

    On exit: stamps `events_consumed`, `ended_at`, finalizes wasInformedBy
    edges from the sampled set, finalizes wasGeneratedBy inverse edges
    on each attached output.

    If `events_consumed == 0` on exit: rolls back (no Activity persisted).
    """
```

Sampling is reservoir-with-edges sample (first 10 + last 10 + reservoir 30 of middle). The first/last buckets are FIFO/LIFO bounded; the reservoir uses Algorithm R seeded by `random.Random(invocation_id)` for test determinism.

**Tests (10):**

1. Empty consumption → no Activity persisted.
2. Single consumption → Activity has one wasInformedBy edge.
3. 100 consumptions → 50 sampled edges (10 first, 10 last, 30 reservoir).
4. Two invocations within merge window, same analyzer → second merges into first; events_consumed sums.
5. Two invocations outside merge window → distinct Activities.
6. Output attachment → wasGeneratedBy edge from output → Activity.
7. Synthetic Agent node creation on first use.
8. Reservoir determinism with seeded invocation_id.
9. `consume_event` raises if called outside context.
10. EventLog read failure during merge check → raises (not silent).

**Estimated size:** ~600 LOC code + ~450 LOC tests.

### Phase 1 — CLI wiring

**Files to touch:**
- `src/trellis_cli/analyze.py` — wrap each subcommand body in `with record_meta_analysis(...)`.
- `src/trellis_cli/tune.py` (or wherever tune lives).
- `src/trellis_cli/admin.py` (for any admin commands that produce graph-readable findings).
- `tests/unit/cli/test_analyze.py` — add 2 tests per wired subcommand verifying meta-Activity persisted.

**CLI flags added globally:**

- `--no-meta-trace` per command.
- `TRELLIS_META_TRACES=on|off` env var (read at CLI startup; missing or invalid value → raise).
- `--operator cli|ci|cron|test` (default `cli`; can be set explicitly when invoked from CI/cron).

**Estimated size:** ~150 LOC + ~200 LOC tests.

### Phase 2 — PackBuilder default filter

**Files to touch:**
- `src/trellis/retrieve/pack_builder.py` — add default exclusion for nodes whose `wasAssociatedWith` edge points to a `trellis_meta_*` Agent.
- `src/trellis/retrieve/strategies.py::GraphSearch` — same filter.
- New `include_meta: bool = False` parameter on the relevant strategies and on the `get_context` MCP tool.
- `tests/unit/retrieve/test_pack_builder.py` — 2 tests: meta-Activities filtered by default; included when opt-in.

**Estimated size:** ~120 LOC + ~150 LOC tests.

### Phase 3 — eval scenario

**File:**
- `eval/scenarios/meta_trace_round_trip.py` (new).

**Behavior:** synthetic scenario runs `analyze context-effectiveness` against a populated graph + EventLog. Verify:

- Exactly one meta-Activity created (`analyzer="context-effectiveness"`).
- `events_consumed` matches the EventLog event count.
- `wasInformedBy` edges count ≤ 50.
- Observations produced are linked back via `wasGeneratedBy`.
- PackBuilder query for the analyzed entity returns the meta-Activity *only* when `include_meta=True`.

**Estimated size:** ~400 LOC.

### Phase 4 — Scenario 5.4 (loop convergence) — separate plan

This Phase is **not** owned by this plan but is mentioned because it depends on Phase 0–3 landing first. See [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.5.2 and the existing TODO.md entry for Scenario 5.4. After Item 6 lands, Scenario 5.4 becomes ~3 days as scoped, instead of ~3 days + meta-trace primitive design.

## 4. Total size estimate

| Phase | LOC code | LOC tests |
|---|---|---|
| 0 | 600 | 450 |
| 1 | 150 | 200 |
| 2 | 120 | 150 |
| 3 | 400 | 0 (scenario *is* the test) |
| **Total** | **~1270** | **~800** |

Sized for **two swarm units**: Phase 0 alone (primitive + tests, single PR), then Phases 1+2+3 together.

## 5. Done when

- All tests pass.
- Running `trellis analyze context-effectiveness` against a populated graph produces exactly one meta-Activity (or zero, if nothing was found).
- `trellis analyze context-effectiveness --no-meta-trace` produces zero Activities.
- `TRELLIS_META_TRACES=garbage trellis analyze ...` raises with a clear error.
- PackBuilder for any seed entity excludes meta-Activities by default; surfaces them with `include_meta=True`.
- Eval scenario green.
- mypy clean.

## 6. Cleanup considerations

- After landing, the existing `TODO.md` item "Scenario 5.4 — agent loop convergence (NOT WRITTEN)" becomes unblocked. Add a forward-pointer in TODO.md.
- The `trellis_meta_*` agent node namespace should be documented in `docs/agent-guide/schemas.md` as a reserved namespace — operators must not emit user-data nodes with `agent_id` matching that prefix.

## 7. Risks

- **Graph bloat from CI runs.** A CI job that runs `analyze` on every commit produces a meta-Activity per run × per analyzer. Mitigation: `--operator=ci` flag lets analyzers apply a coarser merge window (default 1 hour for ci, 5 min for cli). Tune in operator config.
- **Privacy leak via meta-Activity properties.** If an analyzer stores its findings verbatim in Activity properties (e.g., "demoted item id X due to feedback Y"), and Y contained user content, it leaks via the knowledge plane. Mitigation: `properties` on meta-Activities are restricted to scalar values and IDs only — verified by a schema validator in the recorder. Outputs go on `Observation` nodes, not Activity properties.
- **Cross-plane consistency under partial failure.** If the GraphStore write succeeds but the EventLog write of a `META_ANALYSIS_RECORDED` event fails, we have an Activity in the graph with no corresponding operational record. Mitigation: write the event *first*, then the Activity. If Activity write fails, the event survives but with `consume_event_count=0` indicating the rollback. Document the audit-tool query that detects this state.
- **Filter regression.** Without the default filter (Phase 2), every user pack contains meta-Activities — disastrous for relevance. Mitigation: contract test in `tests/unit/retrieve/test_pack_builder.py` enforces the default filter; CI gates against regression.
