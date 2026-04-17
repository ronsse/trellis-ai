# Evolution View: Wireframes & Data Flows

Companion to [graph-ui-integration.md](./graph-ui-integration.md). Contains ASCII wireframes, data flow diagrams, and the backend schema for evolution tracking.

---

## 1. Main Evolution Dashboard Layout

```
+------------------------------------------------------------------------+
|  Trellis  |  Graph  |  Traces  |  Dashboard  | [Evolution] |  |
+------------------------------------------------------------------------+
|                                                                        |
|  +------------------+  +------------------+  +-----------+  +--------+ |
|  | Success Rate     |  | Noise Removed    |  | Precedents|  | Graph  | |
|  |    78%           |  |    5 items       |  |   12      |  | 142    | |
|  |  +12% vs 30d ago |  |  +2 this week   |  |  +1 new   |  | nodes  | |
|  +------------------+  +------------------+  +-----------+  +--------+ |
|                                                                        |
|  +------------------------------------------------------------------+  |
|  |  LEARNING CURVE                                    [Day|Wk|Mo]   |  |
|  |                                                                  |  |
|  |  100%|                                                           |  |
|  |      |                              ^ noise      .----------     |  |
|  |   75%|              ^ precedent    | removed   .-'               |  |
|  |      |              | promoted   .-'         .-'                 |  |
|  |   50%|          .---'          .-'       .---'                   |  |
|  |      |      .---'          .---'     .---'                       |  |
|  |   25%|  .---'          .---'     .---'                           |  |
|  |      |--'                                                        |  |
|  |    0%+-----+-----+-----+-----+-----+-----+-----+-----+         |  |
|  |      Wk 1  Wk 2  Wk 3  Wk 4  Wk 5  Wk 6  Wk 7  Wk 8          |  |
|  +------------------------------------------------------------------+  |
|                                                                        |
|  +-------------------------------+  +--------------------------------+ |
|  |  PACK COMPOSITION DRIFT       |  |  DOMAIN GENERATIONS            | |
|  |                               |  |                                | |
|  |  100%|===========|=========|  |  |  Domain    G1    G2    G3  T  | |
|  |      | keyword   |keyword  |  |  |  -------- ----  ----  ---- -  | |
|  |      | 55%       |  20%    |  |  |  backend   45%   62%   78% ^  | |
|  |   50%|-----------|---------|  |  |  frontend  30%   35%   40% ~  | |
|  |      | traces    |precednts|  |  |  data-pl   --    50%   65% ^  | |
|  |      | 35%       |  40%    |  |  |  infra     55%   58%   60% ~  | |
|  |      |-----------|---------|  |  |                                | |
|  |    0%| graph 10% |sem 25% |  |  |  ^ = improving  ~ = stable    | |
|  |      | Wk 1-2    | Wk 7-8 |  |  |  v = declining                | |
|  +-------------------------------+  +--------------------------------+ |
|                                                                        |
+------------------------------------------------------------------------+
```

---

## 2. Item Lifecycle Detail View

Reached by clicking an item in the noise candidates table or any item ID in the dashboard.

```
+------------------------------------------------------------------------+
|  < Back to Evolution                                                   |
|                                                                        |
|  Item: "API rate limiting pattern"                                     |
|  Type: evidence  |  Domain: backend  |  Signal: high                   |
|                                                                        |
|  SUCCESS RATE                                                          |
|  ================================================================ 80%  |
|  8 successes / 10 appearances                                          |
|                                                                        |
|  LIFECYCLE TIMELINE                                                    |
|  +----------------------------------------------------------------+   |
|  |                                                                |   |
|  |  o Day 1   Ingested as trace evidence                          |   |
|  |  |         Source: trace 01JRK5..  signal_quality: standard    |   |
|  |  |                                                             |   |
|  |  o Day 3   First pack appearance (2 packs)                     |   |
|  |  |         Results: 1 success, 1 failure (50%)                 |   |
|  |  |                                                             |   |
|  |  o Day 7   LLM enrichment classified as "pattern"              |   |
|  |  |         content_type: pattern  confidence: 0.85             |   |
|  |  |                                                             |   |
|  |  * Day 12  PROMOTED TO PRECEDENT                               |   |
|  |  |         confidence: 0.75  by: agent-deploy-01               |   |
|  |  |                                                             |   |
|  |  o Day 15  Appeared in 5 packs -> 4 successes (80%)            |   |
|  |  |                                                             |   |
|  |  o Day 20  Confidence updated: 0.75 -> 0.92                    |   |
|  |  |                                                             |   |
|  |  o Day 25  Signal quality: standard -> high                    |   |
|  |  v                                                             |   |
|  +----------------------------------------------------------------+   |
|                                                                        |
|  PACKS CONTAINING THIS ITEM                                            |
|  +------+----------+--------+---------+------+                         |
|  | Date | Pack ID  | Intent | Outcome | Rank |                         |
|  +------+----------+--------+---------+------+                         |
|  | 3/01 | 01JR...  | deploy | success |  3   |                         |
|  | 3/01 | 01JR...  | deploy | failure |  5   |                         |
|  | 3/08 | 01JS...  | deploy | success |  1   |                         |
|  | 3/08 | 01JS...  | config | success |  2   |                         |
|  | ...  | ...      | ...    | ...     | ...  |                         |
|  +------+----------+--------+---------+------+                         |
+------------------------------------------------------------------------+
```

---

## 3. Pack Comparison View

Side-by-side comparison of the same intent at two different points in time.

```
+------------------------------------------------------------------------+
|  Pack Comparison: "deploy to production"                               |
|  [4 weeks ago] vs [Today]                            [Change intent]   |
+------------------------------------------------------------------------+
|                                                                        |
|  4 WEEKS AGO (March 4)            TODAY (April 1)                      |
|  Success rate: 40%                Success rate: 85%                    |
|  Items: 7  Tokens: 4200          Items: 5  Tokens: 3100               |
|                                                                        |
|  +-------------------------------+  +-------------------------------+  |
|  | #1 Raw trace: failed deploy   |  | #1 Precedent: smoke test     |  |
|  |    score: 0.61  keyword       |  |    score: 0.95  graph        |  |
|  |                               |  |                               |  |
|  | #2 Raw trace: config issue    |  | #2 Precedent: rollback plan  |  |
|  |    score: 0.54  keyword       |  |    score: 0.91  semantic     |  |
|  |                               |  |                               |  |
|  | #3 Keyword: deploy docs       |  | #3 Evidence: deploy checklist|  |
|  |    score: 0.48  keyword       |  |    score: 0.88  keyword      |  |
|  |                               |  |                               |  |
|  | #4 Keyword: old runbook       |  | #4 Entity: service deps map  |  |
|  |    score: 0.35  keyword       |  |    score: 0.82  graph        |  |
|  |                               |  |                               |  |
|  | #5 [NOISE] stale config       |  | #5 Trace: recent success     |  |
|  |    score: 0.22  keyword       |  |    score: 0.75  keyword      |  |
|  |    (now excluded)             |  |                               |  |
|  |                               |  |                               |  |
|  | #6 Trace: unrelated fix       |  |                               |  |
|  |    score: 0.18  keyword       |  |                               |  |
|  |    (now noise-tagged)         |  |                               |  |
|  +-------------------------------+  +-------------------------------+  |
|                                                                        |
|  CHANGES:                                                              |
|  + 2 precedents added (promoted from successful traces)                |
|  + 1 graph entity added (service dependency map)                       |
|  - 2 noise items removed (stale config, unrelated fix)                 |
|  ~ Average relevance score: 0.40 -> 0.86 (+115%)                      |
|  ~ Strategy mix: 100% keyword -> 40% keyword, 40% graph, 20% semantic |
+------------------------------------------------------------------------+
```

---

## 4. Data Flow Diagrams

### 4.1 Evolution Data Collection Flow

```
Agent Work
    |
    v
+-------------------+     +--------------------+     +------------------+
| save_experience() | --> | TRACE_INGESTED     | --> | TraceStore       |
| (MCP/SDK/CLI)     |     | event emitted      |     | (append-only)    |
+-------------------+     +--------------------+     +------------------+
    |
    v
+-------------------+     +--------------------+
| get_context()     | --> | PACK_ASSEMBLED     |
| (PackBuilder)     |     | event emitted      |
|                   |     | payload:           |
|                   |     |   injected_items[] |
|                   |     |   strategies_used  |
|                   |     |   candidates_found |
+-------------------+     +--------------------+
    |                              |
    v                              v
+-------------------+     +--------------------+
| record_feedback() | --> | FEEDBACK_RECORDED  |
| (MCP/SDK/CLI)     |     | event emitted      |
|                   |     | payload:           |
|                   |     |   pack_id          |
|                   |     |   success          |
+-------------------+     +--------------------+
                                   |
                                   v
                          +--------------------+
                          | EventLog           |
                          | (immutable,        |
                          |  append-only)      |
                          +--------------------+
                                   |
                    +--------------+--------------+
                    |              |              |
                    v              v              v
            +------------+  +----------+  +-----------+
            | Evolution  |  | Effectiv.|  | Noise     |
            | Timeline   |  | Analysis |  | Tagging   |
            | Aggregation|  |          |  |           |
            +------------+  +----------+  +-----------+
                    |              |              |
                    v              v              v
            +------------+  +----------+  +-----------+
            | /evolution |  | /effectiv|  | Document  |
            | /timeline  |  | endpoint |  | metadata  |
            | endpoint   |  |          |  | updated   |
            +------------+  +----------+  +-----------+
                    |
                    v
            +--------------------+
            | UI: Evolution View |
            | - Learning curve   |
            | - Composition drift|
            | - Item lifecycle   |
            | - Domain gens      |
            +--------------------+
```

### 4.2 Feedback Loop (The "Natural Selection" Cycle)

```
                    +---> Agent gets context pack
                    |         |
                    |         v
                    |     Agent does work
                    |         |
                    |         v
                    |     Trace ingested + Feedback recorded
                    |         |
                    |         v
                    |     +------------------------------+
                    |     | Effectiveness Analysis       |
                    |     | (per-item success rates)     |
                    |     +------------------------------+
                    |         |                |
                    |         v                v
                    |   Items with          Items with
                    |   high success        low success
                    |   rate (>70%)         rate (<30%)
                    |         |                |
                    |         v                v
                    |   signal_quality     signal_quality
                    |   -> "high"          -> "noise"
                    |         |                |
                    |         v                v
                    |   Prioritized in     EXCLUDED from
                    |   future packs       future packs
                    |         |                |
                    +----<----+                |
                    |                          v
                    |                    Evolution View
                    |                    shows the removal
                    |                    as an annotation
                    |                          |
                    |                          v
                    |                    Operator sees
                    |                    stair-step jump
                    +----<----- motivated to curate more
```

### 4.3 Evolution Snapshot Aggregation Logic

```
Input: EventLog events for time range, bucketed by week

For each bucket (week):
    1. Count PACK_ASSEMBLED events
       -> total_packs

    2. Join PACK_ASSEMBLED with FEEDBACK_RECORDED on pack_id
       -> success_count / total_feedback = success_rate

    3. Parse PACK_ASSEMBLED payloads for injected_items
       -> avg items per pack, avg tokens per pack
       -> per-strategy item counts (source_strategy field)

    4. For each unique item_id across all packs in bucket:
       a. Count appearances (how many packs included it)
       b. Count successes (packs with positive feedback)
       c. success_rate = successes / appearances
       -> per-item effectiveness

    5. Count PRECEDENT_PROMOTED events in bucket
       -> precedents_promoted

    6. Count items where signal_quality changed to "noise"
       -> noise_items_removed

    7. Query graph store for current counts
       -> nodes, edges

Output: EvolutionSnapshot per bucket
```

---

## 5. Backend Schema Additions

### 5.1 EvolutionSnapshot (Pydantic Model)

```python
# src/trellis/schemas/evolution.py (new file)

class StrategyMetrics(TrellisModel):
    """Metrics for a single search strategy."""
    packs_contributed: int = 0
    items_contributed: int = 0
    item_success_rate: float = 0.0

class DomainSnapshot(TrellisModel):
    """Metrics for a single domain in a time period."""
    success_rate: float = 0.0
    item_count: int = 0
    precedent_count: int = 0
    noise_removed: int = 0

class EvolutionSnapshot(TrellisModel):
    """Aggregated metrics for a time period."""
    snapshot_id: str
    period_start: datetime
    period_end: datetime

    # Pack effectiveness
    total_packs: int = 0
    total_feedback: int = 0
    success_rate: float = 0.0
    avg_items_per_pack: float = 0.0
    avg_tokens_per_pack: float = 0.0

    # Strategy attribution
    strategy_breakdown: dict[str, StrategyMetrics] = {}

    # Graph health
    total_nodes: int = 0
    total_edges: int = 0
    noise_items_removed: int = 0
    precedents_promoted: int = 0

    # Per-domain
    domain_metrics: dict[str, DomainSnapshot] = {}

class ItemLifecycleEvent(TrellisModel):
    """A single event in an item's lifecycle."""
    date: datetime
    event_type: str  # "ingested", "pack_appearance", "classified",
                     # "promoted", "noise_tagged", "quality_upgraded"
    description: str
    metadata: dict[str, Any] = {}

class ItemLifecycle(TrellisModel):
    """Complete lifecycle of a knowledge item."""
    item_id: str
    item_type: str
    current_signal_quality: str
    total_appearances: int = 0
    total_successes: int = 0
    success_rate: float = 0.0
    events: list[ItemLifecycleEvent] = []

class EvolutionTimeline(TrellisModel):
    """Response model for /evolution/timeline."""
    snapshots: list[EvolutionSnapshot]
    period_days: int
    bucket_size: str  # "day" | "week" | "month"
```

### 5.2 API Response Models

```python
# Added to src/trellis_api/models.py

class EvolutionCurrentResponse(BaseModel):
    current: EvolutionSnapshot
    previous: EvolutionSnapshot
    deltas: dict[str, float]  # {"success_rate": +0.08, "noise_removed": +3, ...}

class PackComparisonResponse(BaseModel):
    intent: str
    before_date: datetime
    after_date: datetime
    before_items: list[PackItem]
    after_items: list[PackItem]
    before_success_rate: float
    after_success_rate: float
    changes: list[str]  # Human-readable change descriptions
```

---

## 6. File Changes Summary

| File | Action | Description |
|------|--------|-------------|
| `src/trellis/stores/base/event_log.py` | Modify | Add `EVOLUTION_SNAPSHOT` to EventType |
| `src/trellis/schemas/evolution.py` | **Create** | EvolutionSnapshot, ItemLifecycle, EvolutionTimeline models |
| `src/trellis/retrieve/evolution.py` | **Create** | compute_evolution_timeline(), compute_strategy_attribution(), compute_item_lifecycle() |
| `src/trellis/retrieve/pack_builder.py` | Modify | Add `source_strategy` to PACK_ASSEMBLED telemetry payload |
| `src/trellis_api/routes/evolution.py` | **Create** | FastAPI router with 5 endpoints |
| `src/trellis_api/app.py` | Modify | Register evolution router |
| `src/trellis_api/models.py` | Modify | Add evolution response models |
| `src/trellis_workers/learning/evolution_snapshot.py` | **Create** | EvolutionSnapshotWorker |
| `tests/unit/retrieve/test_evolution.py` | **Create** | Tests for aggregation logic |

---

## 7. Demo Data Generator

### Script: `scripts/seed_evolution_demo.py`

Generates 8 weeks of synthetic data:

```python
EVOLUTION_SCENARIO = {
    "week_1": {
        "traces": 6, "success_rate": 0.33,
        "strategies": {"keyword": 1.0},
        "events": ["12 items ingested, all keyword retrieval"]
    },
    "week_2": {
        "traces": 6, "success_rate": 0.45,
        "strategies": {"keyword": 0.9, "graph": 0.1},
        "events": ["Graph search begins contributing"]
    },
    "week_3": {
        "traces": 8, "success_rate": 0.58,
        "strategies": {"keyword": 0.65, "graph": 0.2, "semantic": 0.15},
        "events": [
            "Noise: 'stale-config-doc' tagged (success_rate: 0.15)",
            "Noise: 'unrelated-fix-trace' tagged (success_rate: 0.20)",
        ]
    },
    "week_4": {
        "traces": 10, "success_rate": 0.65,
        "strategies": {"keyword": 0.50, "graph": 0.25, "semantic": 0.15, "precedent": 0.10},
        "events": [
            "Precedent promoted: 'Always run smoke tests after deploy'",
            "Noise: 'old-runbook-v1' tagged (success_rate: 0.22)",
        ]
    },
    "week_5": {
        "traces": 8, "success_rate": 0.72,
        "strategies": {"keyword": 0.35, "graph": 0.25, "semantic": 0.20, "precedent": 0.20},
        "events": [
            "Precedent promoted: 'Rollback plan required for prod deploys'",
        ]
    },
    "week_6": {
        "traces": 10, "success_rate": 0.78,
        "strategies": {"keyword": 0.25, "graph": 0.25, "semantic": 0.25, "precedent": 0.25},
        "events": ["Balanced strategy mix achieved"]
    },
    "week_7": {
        "traces": 12, "success_rate": 0.82,
        "strategies": {"keyword": 0.20, "graph": 0.20, "semantic": 0.25, "precedent": 0.35},
        "events": [
            "Precedents now dominate pack composition",
            "New domain 'data-pipeline' first trace"
        ]
    },
    "week_8": {
        "traces": 12, "success_rate": 0.85,
        "strategies": {"keyword": 0.20, "graph": 0.20, "semantic": 0.25, "precedent": 0.35},
        "events": [
            "Steady state reached",
            "Precedent promoted in data-pipeline domain"
        ]
    },
}
```

Each week generates:
- N traces with appropriate success/failure distribution
- PACK_ASSEMBLED events with item compositions matching strategy breakdown
- FEEDBACK_RECORDED events matching the success rate
- PRECEDENT_PROMOTED events as specified
- Entity/edge creation events for graph growth
- All events backdated with realistic timestamps (Monday-Friday distribution)
