# ADR: Observation / Measurement entity vocabulary

**Status:** Accepted — Phase 0 landed 2026-05-12 (Pydantic schemas + well-known registration; SDK/MCP/retrieval are Phase 1+).
**Date:** 2026-05-11
**Deciders:** Trellis core
**Amends:** [`adr-graph-ontology.md`](./adr-graph-ontology.md) — adds canonical entity types and edge kinds to the well-known registry
**Implementation (Phase 0):** [`src/trellis/schemas/observation.py`](../../src/trellis/schemas/observation.py), [`src/trellis/schemas/measurement.py`](../../src/trellis/schemas/measurement.py), [`src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) (`OBSERVATION` / `MEASUREMENT` / `HAS_OBSERVATION` constants, `WELL_KNOWN_VERSION = "1.1.0"`).
**Related:**
- [`./plan-observation-entity-type.md`](./plan-observation-entity-type.md) — implementation plan for this ADR
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — sets the schema.org / PROV-O alignment policy this ADR extends
- [`./adr-importance-score-freshness.md`](./adr-importance-score-freshness.md) — precedent for freshness decay; cited in §5
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program this ADR sits inside

---

## 1. Context

Today an agent that scans 1000 historical queries against a column and learns "this column is filtered 95% of the time" has **three bad options** for where to record that insight in the graph:

| Option | Shape | Why it fails |
|---|---|---|
| Property on the entity node | `properties={"filter_rate": 0.95, "common_joins": [...]}` | Opaque JSON. No provenance for *when measured*, *what window*, *which agent*. Updating bumps an SCD-2 version on the whole node — heavy for a derived stat. |
| Edge with properties | `(column)-[hasStatistic {window, source_trace_id}]->(metric_concept)` | Stats are mutable; edges don't version independently of their endpoints. No retrieval strategy looks for "statistic" edges. |
| Evidence row attached via `attachedTo` | `Evidence(content="...", source_origin="trace")` | Evidence is **off-graph** — separate store. PackBuilder has no strategy that crosses planes to fetch it. |

The root cause: **empirical observations have no first-class home in the canonical ontology.** Schema.org has no good fit (`Observation` exists in `schema.org/Observation` but is shallow; PROV-O has `Entity` as the supertype). The graph-ontology ADR ([`adr-graph-ontology.md`](./adr-graph-ontology.md)) deliberately defers domain-specific types to extension packages, but `Observation` is a *general-purpose* concern across every domain Trellis serves — query logs against tables, error rates against services, click-through against documents, etc. It belongs in core.

### What this ADR does *not* try to do

- Does not define how observations are *produced* (extractors are out of scope; that's the consuming plan).
- Does not define a domain-specific schema for query-log observations vs error-rate observations — keeps the value bag open.
- Does not introduce typed `value` columns. The structured payload stays in `properties` JSON; promotion to columns waits on the same signal threshold as Phase 3 of `adr-graph-ontology.md`.
- Does not introduce automatic decay of observation importance — that's a separate ADR amendment if it matters.

## 2. Decision

Add two canonical entity types and one canonical edge kind to `src/trellis/schemas/well_known.py`:

### 2.1 Entity types

| Canonical | Schema.org alignment | Use for |
|---|---|---|
| `Observation` | `schema.org/Observation` | A qualitative or compound claim about an entity, derived from a trace, log, or analysis. E.g., "this column is rarely projected", "this service shows error spikes on Mondays". |
| `Measurement` | `schema.org/PropertyValue` | A scalar, numeric, or boolean measurement attached to an entity. E.g., `filter_rate=0.95`, `null_rate=0.03`, `p99_latency_ms=412`. |

The distinction is deliberate: `Measurement` is the **machine-comparable, time-series-shaped** observation; `Observation` is the **narrative, evidence-bearing** observation. A query-log analysis might produce one `Observation` ("column X exhibits a strong filter-projection asymmetry") plus several `Measurement`s (`filter_count=950`, `project_count=23`).

Both are `node_role="semantic"` by default (curated extractor output, not raw ingest).

### 2.2 Edge kinds

| Canonical | PROV-O alignment | Source / Target | Semantics |
|---|---|---|---|
| `hasObservation` | (no direct PROV-O verb; closest: `prov:specializationOf` inverse, but the semantics here are looser) | Subject entity → Observation/Measurement | Subject is observed by this observation. Many-to-many: an entity can have many observations; an observation can describe one or more entities. |

`hasObservation` is **Trellis-specific** (no schema.org or PROV-O verb fits cleanly — `schema.org/observationAbout` exists but goes the wrong direction). We accept the local invention because the semantics are precise and the alternative is misuse of `attachedTo` (which today is reserved for off-graph Evidence rows).

`wasDerivedFrom` (already canonical, PROV-O) is the **other** edge every Observation should carry — pointing back to the trace, log row, or upstream entity the observation was derived from. This is the provenance edge; `hasObservation` is the *subject-of* edge. Both exist for every observation.

### 2.3 Required properties on Observation / Measurement nodes

The `properties` bag stays open, but the following keys are **conventional and expected by retrieval / display surfaces**:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `kind` | `str` | yes | The semantic kind of observation (`"filter_rate"`, `"query_frequency"`, `"error_rate"`). Open string; analytics group by this. |
| `value` | `float \| bool \| str \| list \| dict` | yes for `Measurement`, optional for `Observation` | The measured value. Type depends on kind. |
| `unit` | `str` | optional | Unit for scalar values (`"per_query"`, `"ms"`, `"per_day"`). |
| `window_start` | `datetime` (ISO 8601) | yes | Start of the observation window. |
| `window_end` | `datetime` (ISO 8601) | yes | End of the observation window. |
| `sample_size` | `int` | optional but recommended | How many underlying events / rows informed this observation. |
| `method` | `str` | yes | Free-form description of how it was derived (`"count_distinct_query_hashes"`, `"llm_summary_from_query_log"`). |
| `confidence` | `float [0,1]` | optional | How confident the producer is in the observation. |

A producer that omits a `yes`-marked field **must raise**, not silently default — per the POC directive in [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §2.

### 2.4 Conflict resolution: Observation vs stored property

When an Observation's `kind` overlaps with a stored property on the subject entity (e.g., `column.nullable=False` structural; `Observation(kind="null_rate", value=0.03)` empirical), the precedence rule for PackBuilder display is:

1. Both are returned in the pack — neither suppresses the other.
2. The stored property is labeled `structural`; the observation is labeled `empirical`.
3. The consuming agent reconciles. Trellis does not adjudicate.

Rationale: empirical observations may *contradict* structural facts intentionally (e.g., the schema says nullable=false but the data shows nulls; that's a *finding*, not a conflict to resolve silently).

## 3. Why this shape and not the alternatives

| Alternative | Reason rejected |
|---|---|
| **Store as properties on the entity node** | Loses provenance, conflates structural and empirical, every update bumps SCD-2 on the entity, no time-series shape. |
| **Store as edges with rich properties** | Edges don't version independently. No retrieval strategy fetches "statistic edges". Stuffing time-series data into edge properties scales poorly. |
| **Reuse `Evidence`** | Evidence is off-graph and pre-allocated for trace-evidence linkage. Repurposing it for derived measurements muddles the audit story. |
| **Adopt OpenLineage `DatasetFacet`** | Excellent wire format for ingestion; not the right *graph node type*. Extractors can read OpenLineage facets and emit Observation nodes — the two coexist. |
| **Single `Observation` type, no `Measurement`** | Loses the time-series-comparable shape. Tooling that wants "give me all `null_rate` measurements for this column over time" has no clean predicate. The distinction costs one row in the registry; preserves a real ergonomic. |
| **Make `Observation` a node_role** (alongside structural/semantic/curated) | NodeRole is orthogonal to entity type and used for retrieval gating. Adding a fourth role conflates the axes. Keep them separate. |

## 4. Guardrails

### 4.1 The open-string contract stays

Adopters defining domain-specific observation types (e.g., `query_pattern_observation`) can use any string for `node_type`. The canonical `Observation` / `Measurement` types are recommended, not required.

### 4.2 No silent default values

A producer that emits an Observation without `window_start`, `window_end`, `kind`, or `method` **raises at draft-time validation**, not at insert time. Per the POC directive: loud failures, never silent defaults.

### 4.3 No retrieval bias

PackBuilder must not preferentially weight observations over structural facts. The relative weight is a tunable retrieval parameter (Item 3's parameter-registry wiring is the appropriate place to control it), not a hard-coded preference in core.

### 4.4 No automatic decay

Observation `freshness` is a property the consumer interprets. This ADR does *not* introduce automatic suppression of stale observations. If a follow-on ADR wants that (along the lines of [`adr-importance-score-freshness.md`](./adr-importance-score-freshness.md)), it amends — does not retrofit silently.

### 4.5 Privacy

Observations attached to an entity inherit the `DataClassification` of that entity. The retrieval layer honors this via existing classification gating (or, where ungated today, will honor it when [`adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) Phase 4 lands). **No new classification logic in this ADR.**

## 5. Cost

* `WELL_KNOWN_VERSION` bumps minor (e.g., 1.0.0 → 1.1.0). One-way commitment — these names are now reserved forever per `adr-graph-ontology.md` §5.4.
* `tests/unit/schemas/test_well_known.py` adds ~6 tests for the new canonicals + alias-map identity.
* Contract test suite (`tests/unit/stores/contracts/graph_store_contract.py`) gets one new test asserting `hasObservation` edges round-trip on every backend.
* Existing data is unaffected. Greenfield: any extractor that wants to emit observations does so against the new types.

## 6. Phases

| Phase | Scope | Status |
|---|---|---|
| 0 | This ADR + well_known.py additions + Pydantic schemas + tests + docs | **Landed 2026-05-12** |
| 1 | SDK helper `record_observation(...)` + MCP tool | Proposed (see plan) |
| 2 | `ObservationSearch` retrieval strategy | Proposed (see plan) |
| 3 | Sample extractor in `trellis_workers` producing query-pattern observations | Proposed (see plan) |
| 4 | Eval scenario demonstrating retrieval of observations alongside structural neighbors | Proposed (see plan) |

Implementation details for each phase live in [`plan-observation-entity-type.md`](./plan-observation-entity-type.md).

## 7. References

- schema.org/Observation — https://schema.org/Observation
- schema.org/PropertyValue — https://schema.org/PropertyValue
- PROV-O `wasDerivedFrom` — https://www.w3.org/TR/prov-o/#wasDerivedFrom
- OpenLineage DatasetFacet spec — https://openlineage.io/ (informational; not adopted)
