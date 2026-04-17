# Graph UI Integration Design

A comprehensive design for the Trellis web UI, with emphasis on the **Evolution View** — visualizing how the knowledge graph self-improves over time.

## Overview

The UI serves three audiences:
1. **Agent operators** — see what agents learned, what's working, what's noise
2. **Platform engineers** — monitor graph health, token efficiency, store stats
3. **Evaluators/adopters** — understand the value proposition through visible evolution

---

## Part 1: Core UI Views

### 1.1 Graph Explorer

Interactive force-directed graph visualization (Cytoscape.js).

**Features:**
- Nodes color-coded by `EntityType` (service=blue, team=green, concept=purple, file=gray, tool=orange)
- Node size proportional to edge count (more connected = larger)
- Edges styled by `EdgeKind` — dashed for `depends_on`, solid for `part_of`, dotted for `related_to`
- Click node to expand neighborhood (`GET /api/v1/entities/{id}?depth=1`)
- **Time slider**: Scrub through time using SCD Type 2 `as_of` parameter — watch the graph evolve
- Side panel showing node properties, aliases, version history, connected traces
- Search + filter bar: filter by `node_type`, domain tags, date range

**Data sources:**
- `GET /api/v1/entities/{id}?depth=N` — subgraph
- `GET /api/v1/search?q=...` — entity search
- Graph store `get_subgraph()` and `get_node_history()` for temporal queries

### 1.2 Trace Timeline

Chronological feed of agent work.

**Features:**
- Timeline cards: intent, step count, outcome badge (green/red/yellow), agent_id, domain
- Expandable detail: step-by-step execution (tool calls, durations, errors)
- Linked entities: which entities a trace touched, evidence used, precedent promotions
- Filter by: domain, agent, outcome status, date range

**Data sources:**
- `GET /api/v1/traces?domain=...&agent_id=...&limit=20`
- `GET /api/v1/traces/{trace_id}`

### 1.3 Improvement Dashboard

Surfaces actionable opportunities.

**Panels:**
- **Effectiveness scorecard**: Overall success rate, total packs, feedback coverage
- **Noise candidates table**: Items with high appearance count but low success rate (<30%)
- **Orphan entities**: Graph nodes with zero edges (disconnected knowledge)
- **Stale precedents**: Not referenced in recent packs
- **Domain coverage heatmap**: Which domains have rich vs. sparse knowledge
- **Suggested actions**: "Remove noisy item X", "Enrich item Y", "Link orphan Z"

**Data sources:**
- `GET /api/v1/effectiveness?days=30&min_appearances=2`
- `GET /api/v1/stats`
- Proposed: `GET /api/v1/entities/orphans`
- Proposed: `GET /api/v1/classifications/summary`

### 1.4 Precedent Library

Browsable catalog of proven patterns.

**Features:**
- Card grid: title, description, confidence score, applicability domains, source trace count
- Lineage view: which traces produced this precedent, supporting evidence
- Impact metric: how often this precedent appears in successful packs
- Promote flow: UI to promote a trace to precedent

**Data sources:**
- `GET /api/v1/precedents?domain=...&limit=20`
- `POST /api/v1/precedents` (promote)

### 1.5 System Health

- Store stats: trace/document/node/edge/event counts
- Token usage: by layer (CLI/MCP/SDK), by operation, budget violations
- Event stream: live feed of system events
- Classification distribution: content by domain, content_type, scope, signal_quality

**Data sources:**
- `GET /api/v1/stats`
- `GET /api/v1/health`
- CLI: `trellis analyze token-usage --format json`

---

## Part 2: Evolution View — The Signature Feature

### Why This Is the Killer Feature

Every RAG system retrieves. XPG's unique value is that it **self-improves**. The Evolution View makes that improvement visible. Without it, users take the value on faith. With it, they watch it happen.

This is not over-engineering. This is the differentiator.

### 2A. Learning Curve Chart (Primary)

A **stepped line chart** showing pack success rate over time with annotated inflection points.

```
Success   |
Rate      |    ^ Precedent: "retry logic"           ^ Noise: removed 3 items
100% -----|    | promoted                            |
          |    |     .-------.                       |   .------
 75% -----|    |   .-'       '-.                     | .-'
          |    | .-'            '-.         .------. |.'
 50% -----|    .'                  '---------'      '-'
          |  .-'
 25% -----|-'
          |'
  0% -----+------+------+------+------+------+------+-------> Time
          Week 1  Week 2  Week 3  Week 4  Week 5  Week 6  Week 7
```

- X-axis: time (configurable: day/week/month buckets)
- Y-axis: success rate of context packs (0-100%)
- **Annotated events**: Vertical markers at key mutations (precedent promoted, noise removed, entity merged)
- **Stepped rendering**: Emphasizes discrete improvements, not gradual drift
- The annotations are the "Darwinian" moments — cause and effect visible at a glance

**Data flow:**
1. Query `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` events for time range
2. Bucket by week, compute success rate per bucket
3. Query `PRECEDENT_PROMOTED`, noise-tagging events for annotations
4. Render stepped line with annotation markers

### 2B. Pack Composition Drift (Alluvial/Stacked Area)

Shows how the mix of items in context packs shifts over time.

```
100% |==========|==========|==========|
     | keyword  | keyword  | keyword  |
     | 60%      | 35%      | 20%      |
     |----------|----------|----------|
     | traces   | traces   | precdnt  |
     | 30%      | 25%      | 40%      | <-- precedents dominate over time
     |----------|----------|----------|
     | graph    | precdnt  | semantic |
     | 10%      | 30%      | 25%      |
     |          |----------|----------|
     |          | semantic | traces   |
     |          | 10%      | 15%      |
  0% |==========|==========|==========|
      Week 1     Week 4     Week 8
```

**Story it tells:** Early packs are dominated by raw keyword hits. As the graph matures, precedents and semantic search take over — the system is retrieving better, curated knowledge instead of raw noise.

**Data flow:**
1. Parse `PACK_ASSEMBLED` event payloads for `strategies_used` and per-item `source_strategy`
2. Bucket by time period, compute strategy proportions
3. Render as stacked area chart

**Required backend change:** Propagate `source_strategy` from `PackItem.metadata` into `PACK_ASSEMBLED` event payload (one-line fix in `pack_builder.py` `_emit_telemetry()`).

### 2C. Item Lifecycle View

Track individual knowledge items through their evolutionary journey.

```
Item: "API rate limiting pattern"
+------------------------------------------------------------+
| Day 1:  Ingested as trace evidence (signal: standard)      |
| Day 3:  Appeared in 2 packs -> 1 success, 1 failure       |
| Day 7:  Classified as "pattern" by LLM enrichment         |
| Day 12: Promoted to precedent (confidence: 0.75)          |
| Day 15: Appeared in 5 packs -> 4 successes                |
| Day 20: Confidence updated to 0.92                         |
| Day 25: Signal quality upgraded to "high"                  |
+------------------------------------------------------------+
Success Rate: ==================== 80% (8/10 packs)
```

**Story it tells:** Individual knowledge items are tested by reality (appearing in packs), scored by outcomes (success/failure feedback), and either survive (promoted, confidence increases) or die (noise-tagged, excluded). This is literally natural selection on knowledge.

**Data flow:**
1. Query all `PACK_ASSEMBLED` events containing this item_id
2. Join with `FEEDBACK_RECORDED` events for those packs
3. Query item's metadata history (signal_quality changes via document store)
4. Query `PRECEDENT_PROMOTED` events linked to this item
5. Render as vertical timeline

### 2D. Generation View (Domain-Level Evolution)

Per-domain evolutionary progress in discrete "generations."

| Domain | Gen 1 (Wk 1-2) | Gen 2 (Wk 3-4) | Gen 3 (Wk 5-6) | Trend |
|--------|-----------------|-----------------|-----------------|-------|
| backend | 45% success, 12 items | 62% success, 18 items, 3 noise removed | 78% success, 15 items, 2 precedents | Improving |
| frontend | 30% success, 5 items | 35% success, 8 items | 40% success, 10 items | Slow growth |
| data-pipeline | -- | 50% success, 4 items | 65% success, 7 items, 1 precedent | New & growing |

**Story it tells:** Different domains evolve at different rates. Mature domains have high success rates and many precedents. New domains are in "cold start." This tells operators where to focus curation effort.

### 2E. Pack Comparison ("Same Question, Better Answer")

Side-by-side showing what a pack for the same intent looked like N weeks ago vs. today. Leverages SCD Type 2 `as_of`.

```
"deploy to production" - 4 weeks ago     "deploy to production" - today
+-----------------------------------+    +-----------------------------------+
| 1. Raw trace: failed deploy (0.6) |    | 1. Precedent: smoke test (0.95)   |
| 2. Raw trace: config issue (0.5)  |    | 2. Precedent: rollback plan (0.91)|
| 3. Keyword hit: deploy docs (0.4) |    | 3. Evidence: deploy checklist(0.88|
| 4. Keyword hit: old runbook (0.3) |    | 4. Graph: service deps (0.82)     |
| 5. [NOISE] stale config (0.2)     |    | 5. Recent trace: success (0.75)   |
+-----------------------------------+    +-----------------------------------+
Success rate: 40%                        Success rate: 85%
```

This is the most visceral proof of evolution: same question, dramatically better answer.

---

## Part 3: Backend Requirements

### 3.1 Evolution Snapshot Event Type

Add `EVOLUTION_SNAPSHOT` to `EventType` enum in `src/trellis/stores/base/event_log.py`.

Payload schema:
```python
{
    "period_start": "ISO datetime",
    "period_end": "ISO datetime",
    "total_packs": int,
    "total_feedback": int,
    "success_rate": float,          # 0.0-1.0
    "avg_items_per_pack": float,
    "avg_tokens_per_pack": float,
    "noise_items_removed": int,     # in this period
    "precedents_promoted": int,     # in this period
    "strategy_breakdown": {
        "keyword": {"packs_contributed": int, "item_success_rate": float},
        "semantic": {"packs_contributed": int, "item_success_rate": float},
        "graph": {"packs_contributed": int, "item_success_rate": float}
    },
    "domain_metrics": {
        "backend": {"success_rate": float, "item_count": int, "precedent_count": int},
        ...
    },
    "graph_size": {"nodes": int, "edges": int, "documents": int}
}
```

Stored as events — reuses immutable event log, no new store needed.

### 3.2 On-the-Fly Aggregation (No Worker Required)

New module: `src/trellis/retrieve/evolution.py`

```python
def compute_evolution_timeline(
    event_log: EventLog,
    days: int = 90,
    bucket: str = "week",  # "day" | "week" | "month"
) -> list[EvolutionSnapshot]:
    """Compute evolution snapshots from raw events.

    Works without the snapshot worker by aggregating PACK_ASSEMBLED
    and FEEDBACK_RECORDED events into time buckets on the fly.
    """

def compute_strategy_attribution(
    event_log: EventLog,
    days: int = 30,
) -> dict[str, StrategyMetrics]:
    """Per-strategy success rate by joining pack items with feedback."""

def compute_item_lifecycle(
    event_log: EventLog,
    item_id: str,
) -> ItemLifecycle:
    """Track a single item's journey through the system."""
```

### 3.3 New API Endpoints

New router: `src/trellis_api/routes/evolution.py`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/evolution/timeline` | GET | Time-series of evolution snapshots |
| `/api/v1/evolution/current` | GET | Current period vs. previous with deltas |
| `/api/v1/evolution/strategy-attribution` | GET | Per-strategy success tracking |
| `/api/v1/evolution/item/{item_id}/lifecycle` | GET | Single item's evolutionary journey |
| `/api/v1/evolution/domains` | GET | Per-domain generation view |

### 3.4 Pack Builder Fix

In `src/trellis/retrieve/pack_builder.py` `_emit_telemetry()`: add `source_strategy` to per-item payload entries. Currently strategies set `metadata["source_strategy"]` on items but this isn't propagated into the `PACK_ASSEMBLED` event.

### 3.5 Snapshot Worker (Optional Optimization)

New file: `src/trellis_workers/learning/evolution_snapshot.py`

Follows `RetentionWorker` pattern. Runs periodically (daily/weekly), calls the on-the-fly aggregation, persists result as `EVOLUTION_SNAPSHOT` event. Optimization for large event volumes — the API works without it.

---

## Part 4: Demo Scenario

### `trellis demo evolution`

Seeds 8 weeks of synthetic data showing a realistic improvement arc:

**Week 1-2 (Cold Start):**
- 12 traces ingested across "backend" and "deployment" domains
- Mixed outcomes: 40% success rate
- Packs assembled using keyword search only
- No precedents, no curation

**Week 3-4 (Learning Phase):**
- Effectiveness analysis runs, identifies 3 noise items
- Noise items tagged with `signal_quality="noise"`
- 2 precedents promoted from successful traces
- Success rate jumps to 62%
- Semantic search begins contributing

**Week 5-6 (Maturity):**
- Precedents appear in 30% of packs
- Graph search activates (enough entity relationships)
- Another noise item removed
- Success rate reaches 78%

**Week 7-8 (Steady State):**
- 80% success rate
- Precedents dominate pack composition
- New "data-pipeline" domain starts appearing (cold start for new domain)
- 1 precedent promoted in new domain

Each week seeds: `PACK_ASSEMBLED` events with realistic item compositions, `FEEDBACK_RECORDED` events, `PRECEDENT_PROMOTED` events, `ENTITY_CREATED` events, and noise-tagging mutations. All backdated with realistic timestamps.

---

## Part 5: Agent Integration

### Claude Code (via MCP + Hooks)

The OpenClaw MCP skill already provides 8 tools. Deeper integration:

1. **Auto-trace capture**: A `session-end` hook that converts Claude Code's tool call history into XPG trace format and ingests it automatically
2. **Pre-task context injection**: A `session-start` hook that calls `get_context(intent=<task>)` and injects relevant precedents
3. **Inline precedent suggestions**: When the MCP server detects a query matching a high-confidence precedent, surface it proactively
4. **Feedback capture**: Post-task prompt to record success/failure, closing the effectiveness loop

### Other Agent Frameworks

| Framework | Integration |
|-----------|-------------|
| LangChain/LangGraph | Existing `integrations/langgraph/tools.py` — expand with auto-tracing `RunnablePassthrough` |
| CrewAI | Custom Tool subclass wrapping `TrellisClient` SDK |
| AutoGen | Agent plugin intercepting message history -> traces |
| OpenAI Assistants | Function calling definitions matching MCP tool schemas |
| Cursor/Windsurf/VS Code | MCP server registration (`trellis-mcp`) |

### The Core Loop

```
Before task -> get_context(intent) -> inject into prompt
During task -> (agent works normally)
After task  -> save_experience(trace) + record_feedback(success/fail)
Background  -> effectiveness analysis -> noise tagging -> graph improves
             -> evolution view shows the improvement
```

---

## Part 6: Open-Source Adoption Strategy

### Critical for first impression
1. **`trellis demo`** — One-command demo with seeded data showing evolution
2. **Docker Compose** — `docker compose up` for API + UI + seeded data
3. **30-second video** — Screen recording of evolution view showing stair-step improvement

### Critical for stickiness
4. **Framework adapters** — Pre-built for LangChain, CrewAI, AutoGen
5. **GitHub Actions workflow** — Auto-ingest traces from CI runs
6. **VS Code extension** — Show relevant precedents inline while coding

### Nice to have
7. **Grafana dashboard templates** — JSON dashboards for metrics
8. **Export/import** — Portable knowledge as JSON-LD
9. **Multi-tenant support** — Workspace isolation for teams
10. **Webhooks** — Push events to Slack/Discord

---

## Part 7: Outcome Improvement Assessment

### Expected Impact

| Scenario | Expected Improvement | Confidence |
|----------|---------------------|------------|
| Repeated tasks (same domain, same patterns) | **20-40% fewer failures** after 10+ traces | High |
| Novel tasks with related precedents | **10-20% faster resolution** | Medium |
| Cross-agent knowledge transfer | **Significant** — eliminates "starting from scratch" | High |
| Cold start (< 5 traces) | **Minimal** — not enough data | Certain |
| With evolution view visible | **Teams curate 3x more** (visibility drives action) | Medium-High |

### Why the Evolution View Drives Outcomes

The evolution view doesn't just display improvement — it **causes** improvement:
- Teams see noise candidates and remove them (action from visibility)
- Teams see which domains lack coverage and fill gaps
- Teams see precedent impact and promote more patterns
- Teams see the stair-step and are motivated to keep the loop running

### Key Risk: Adoption Friction

The biggest risk is not "does it work?" but "will teams bother?" Mitigations:
- Zero-config auto-tracing (hooks capture traces without manual effort)
- Visible value within 60 seconds (the demo)
- No performance overhead (deterministic classification is microseconds)

---

## Technical Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Graph rendering | Cytoscape.js | Purpose-built for graph visualization, better than D3 for this use case |
| Charts/dashboards | Recharts or Nivo | React-native, good for time-series and stacked areas |
| Data fetching | TanStack Query | Caching, refetching, optimistic updates against REST API |
| Styling | Tailwind CSS | Utility-first, fast iteration |
| Build | Vite + React + TypeScript | Fast dev server, type safety |
| Serving | FastAPI static mount or standalone | Can embed in existing API or serve separately |

---

## Implementation Phases

### Phase 1: MVP (Evolution View + Graph Explorer)
- Backend: evolution aggregation module + API endpoints
- Frontend: learning curve chart, pack composition drift, summary cards
- Demo: `trellis demo evolution` with seeded data
- Graph explorer with time slider

### Phase 2: Full Dashboard
- Improvement dashboard (noise candidates, orphans, coverage)
- Trace timeline
- Precedent library with lineage
- Item lifecycle view

### Phase 3: Advanced
- Pack comparison ("same question, better answer")
- What-if replay (past pack with current graph state)
- Automated insight generation (LLM-written summaries of evolution)
- Precedent impact attribution
