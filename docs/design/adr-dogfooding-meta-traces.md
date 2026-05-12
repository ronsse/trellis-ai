# ADR: Dogfooding meta-traces

**Status:** Proposed
**Date:** 2026-05-11
**Deciders:** Trellis core
**Related:**
- [`./plan-dogfooding-meta-traces.md`](./plan-dogfooding-meta-traces.md) — implementation plan
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — the type Trellis writes back into its own graph
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — defines the plane separation this ADR deliberately crosses (sanctioned)
- [`./plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.5 — Scenario 5.4 is the test surface this enables
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program

---

## 1. Context

Trellis records traces of *user* work. It does not record traces of *its own* work — the analytics CLI commands (`analyze context-effectiveness`, `analyze advisory-effectiveness`, `analyze learning-observations`, `tune`, `promote`) run, produce reports, and leave no graph artifact behind.

The cost of that asymmetry:

- **No retrieval over past analyses.** If an operator wants to know "what analyses have touched this entity in the last month?" there is no answer in the graph. The information lives in CLI invocation history (or rather, nowhere).
- **No convergence evidence.** Scenario 5.4 ([`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.5) is "the chart you show people" — the system improving over time. Without meta-traces, there's no graph-shaped record of *what* the system did to itself across rounds; it's all log-shaped.
- **No first user.** A self-improving system that doesn't use itself is suspicious.

Scenario 5.4 is unwritten (per [`TODO.md`](../../TODO.md) "scenario 5.4 — agent loop convergence"). One of the missing pieces is the meta-trace abstraction itself. Build it now; Scenario 5.4 lands cleanly on top.

## 2. Decision

Every Trellis-internal analysis command (analyze, tune, promote, schema-evolution, extraction-health) records a meta-trace into the knowledge plane. The meta-trace is an `Activity` node connected by:

- `wasInformedBy` edges to the source `PACK_ASSEMBLED` / `FEEDBACK_RECORDED` / `MUTATION_EXECUTED` events it consumed.
- `wasGeneratedBy` edges (inverse) on the `Observation` / `Advisory` / `WellKnownCandidate` nodes it produced.
- `wasAssociatedWith` edge to a synthetic `Agent` node representing the analysis subsystem (`trellis_meta_analyzer`, `trellis_meta_tuner`, etc.).

### 2.1 What's a meta-trace?

```
(Activity {
    activity_id: "act-2026-05-11-abc123",
    node_type: "Activity",
    properties: {
        analyzer: "context-effectiveness",
        invocation_id: "inv-...",   # CLI invocation correlation
        started_at: ..., ended_at: ...,
        input_window_start: ..., input_window_end: ...,
        events_consumed: 1247,
        observations_emitted: 12,
        advisories_emitted: 3,
        operator: "ci|cli|cron|...",
    }
})
-[wasInformedBy]-> (Event {event_id: "evt-..."})  # one per consumed PACK_ASSEMBLED, sampled
-[wasInformedBy]-> (Event {event_id: "evt-..."})
...
-[wasAssociatedWith]-> (Agent {agent_id: "trellis_meta_analyzer", node_type: "Agent"})

# Observation nodes produced by this Activity carry the inverse edge:
(Observation {
    kind: "noise_candidate",
    value: {item_id: "...", reason: "..."},
    window_start: ..., window_end: ...,
    method: "context-effectiveness",
})
-[wasGeneratedBy]-> (Activity {activity_id: "act-..."})
```

### 2.2 Plane crossing

This is the first **sanctioned** crossing between operational and knowledge planes:

- The meta-trace **reads** operational EventLog data (input).
- It **writes** to the knowledge plane (Activity + Observation + Advisory).
- The two planes stay otherwise separate.

`adr-planes-and-substrates.md` says cross-plane handlers must be explicit. This ADR names the meta-trace machinery as the *only* cross-plane handler. Generalization to other read-operational/write-knowledge patterns requires a separate ADR.

### 2.3 Sampling

A `trellis analyze` invocation might consume 100,000 PACK_ASSEMBLED events. Stamping 100K `wasInformedBy` edges per Activity is too many — the graph balloons, retrieval over Activities becomes useless.

Sampling rule: per analyzer invocation, retain `wasInformedBy` edges to:

- The first 10 events in the window.
- The last 10 events in the window.
- A reservoir sample of 30 events from the middle.

So each Activity has at most 50 `wasInformedBy` edges, regardless of input size. The `events_consumed` count on the Activity carries the true total.

### 2.4 Backpressure

If `events_consumed == 0` (analyzer found nothing new), **no Activity is written**. The CLI logs INFO and exits. We do not pollute the graph with no-op invocations.

If two invocations of the same analyzer happen within a 5-minute window and consume overlapping events, the second invocation **merges** into the first Activity (extends `ended_at`, increments `events_consumed`, appends new produced observations). This prevents scheduled-task setups from stamping a meta-trace every minute.

### 2.5 What's *not* a meta-trace

- Routine read-only operations (`trellis admin graph-health`, `trellis admin list-stores`) do not produce Activities.
- Ingestion paths (`trellis demo load`, `trellis ingest`) do not produce meta-Activities — the user-data trace they record is the right shape.
- The analyze CLI in `--dry-run` mode does not produce Activities, but it produces a `META_ANALYSIS_DRY_RUN` event so operators can see what *would* have been recorded.

## 3. Why this shape

### 3.1 Why write back to the graph (not just log)

Logs are not queryable, not subject to retention guarantees, and not retrievable via PackBuilder. The graph is. The whole point: an agent asking "what does Trellis know about this entity?" should get back not just structural facts but also "this entity was the subject of 3 noise demotions in the last week."

### 3.2 Why `Activity` (PROV-O) and not a Trellis-specific node type

The graph-ontology ADR ([`adr-graph-ontology.md`](./adr-graph-ontology.md)) ships `Activity` precisely for this. A meta-analysis is exactly the PROV-O concept of an Activity: a unit of work, with inputs (`used`/`wasInformedBy`), outputs (`wasGeneratedBy`), and an associated Agent (`wasAssociatedWith`). The canonical type already exists; using it is correct.

### 3.3 Why sampling is OK

Per-event provenance for analyzer inputs is a luxury, not a requirement. The total count is preserved; the event_ids of the sampled members are preserved for spot-checking. The full history is recoverable from the operational EventLog by `event_type=PACK_ASSEMBLED AND occurred_at IN [window]` — the meta-trace is an index into operational data, not a duplicate of it.

### 3.4 Why merge within 5-minute window

Scheduled-task setups will run analyzers every minute. Without merging, every minute produces an Activity, and the graph fills with near-duplicate noise. Merging keeps the Activity count proportional to *change*, not to *invocation frequency*.

## 4. Guardrails

### 4.1 No agent context contamination

When a non-meta agent calls `get_context(seed_entity_id=...)`, the retrieved pack **filters out** Activities whose `properties.agent_id startswith "trellis_meta_"` by default. The opt-in `include_meta=True` flag exists for operators specifically asking. This prevents a user agent's pack from being polluted with the system's own analyses unless requested.

### 4.2 Privacy

Meta-Activities reference event_ids from the operational plane. They do not copy event payloads. PackBuilder retrieval that surfaces a meta-Activity will fetch the referenced events through the existing operational-plane access path — which honors `DataClassification` (where enforced). The meta-trace itself does not bypass.

### 4.3 No automatic emit on the analyze CLI

The default behavior is meta-trace-recording on. Operator can disable per-invocation with `--no-meta-trace`, or globally with env var `TRELLIS_META_TRACES=off`. Disabling produces a one-time WARN per invocation (POC discipline: loud about opting out of telemetry).

### 4.4 The `trellis_meta_*` Agent node

The synthetic `Agent` nodes (`trellis_meta_analyzer`, `trellis_meta_tuner`, etc.) are created on first use, never deleted. They are versioned as semantic nodes. Their `properties` include the Trellis version that wrote them — so a meta-trace from v0.5 vs v0.6 is distinguishable.

## 5. Consequences

### 5.1 What this enables

- Scenario 5.4 (loop convergence) — the chart-producing scenario — gets a graph-shaped record.
- An agent asking "what does Trellis know about this entity?" gets richer context, including past Trellis-internal findings (when opted in).
- Item 7 (coding-agent loop) consumes meta-Activity provenance to determine which analyses produced which proposals.

### 5.2 What this does not do

- Does not change the operational EventLog at all (read-only consumer).
- Does not introduce a new substrate or store.
- Does not introduce auto-curation of the meta-trace graph (the graph compaction Phase that `compact_versions()` enables is the right cleanup path — call it on a schedule per [`TODO.md`](../../TODO.md) "Graph version compaction automation").

### 5.3 What this costs

- Graph storage proportional to (number of analyses × sampling cap). Bounded.
- One additional EventLog read per analyze CLI invocation (for merge-window check).

## 6. References

- `adr-graph-ontology.md` — `Activity`, `Agent`, `wasInformedBy`, `wasAssociatedWith`, `wasGeneratedBy`
- `adr-planes-and-substrates.md` — plane separation contract
- `adr-observation-entity-type.md` — `Observation` is the primary output type of meta-traces
- `plan-evaluation-strategy.md` §5.5 — Scenario 5.4 (loop convergence)
