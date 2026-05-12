# Plan: Observation / Measurement entity vocabulary

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable
**ADR:** [`adr-observation-entity-type.md`](./adr-observation-entity-type.md)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 1
**Depends on:** none
**Unblocks:** Item 2 (provenance columns gain a consumer), Item 6 (dogfooding meta-traces have a place to write findings)

## 1. Scope

Add `Observation` and `Measurement` canonical entity types and `hasObservation` canonical edge kind. Land an SDK / MCP surface. Land a retrieval strategy. Ship one sample extractor and one eval scenario.

**In scope:**
- `src/trellis/schemas/well_known.py` additions + tests + docs.
- `src/trellis_sdk` `record_observation()` + `query_observations()`.
- MCP tool exposures.
- `src/trellis/retrieve/strategies.py::ObservationSearch`.
- `src/trellis_workers/extract/query_pattern_observer.py` (sample extractor).
- One scenario in `eval/scenarios/`.

**Out of scope:**
- Promoting `kind` / `window_start` / etc. from properties to columns (that's a follow-on Phase 3-shaped item — defer until 100+ observations of measurable retrieval cost).
- Cross-entity observation joins (e.g., "find columns whose null_rate exceeds 0.1 and are referenced by mart-layer models"). PackBuilder retrieval through `ObservationSearch` is single-seed.
- LLM-driven observation production. The sample extractor is deterministic; LLM observation producers ship in trellis_workers behind opt-in flag in a follow-on.

## 2. POC directives applied

Per [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §2:

- `record_observation()` **raises** on missing `kind` / `window_start` / `window_end` / `method`. No defaults.
- `ObservationSearch.search()` **raises** if asked to retrieve from a backend that has not seen `Observation` writes — surfaces "no observations exist for this entity" loudly to the caller, never returns empty silently when a `kind` filter was specified.
- The sample extractor **raises** on malformed input log; does not skip rows.
- No alias added for misspellings (e.g., `observation` lowercase). Producers must use the canonical `Observation`. Open-string callers can spell it however they want; they just won't bucket with the canonical retrieval path.

## 3. Phase 0 — registry additions

### Files to touch

- `src/trellis/schemas/well_known.py`
- `tests/unit/schemas/test_well_known.py`
- `docs/agent-guide/schemas.md`

### Changes

In `well_known.py`:

```python
# New canonical entity types
OBSERVATION = "Observation"
MEASUREMENT = "Measurement"

# Extend ENTITY_TYPE_CANONICALS frozenset to include them.
# Extend _ENTITY_SCHEMA_ALIGNMENT:
_ENTITY_SCHEMA_ALIGNMENT[OBSERVATION] = "schema.org/Observation"
_ENTITY_SCHEMA_ALIGNMENT[MEASUREMENT] = "schema.org/PropertyValue"

# New canonical edge kind
HAS_OBSERVATION = "hasObservation"

# Extend EDGE_KIND_CANONICALS frozenset.
# No schema_alignment URI (Trellis-specific verb; intentionally None).

# Bump:
WELL_KNOWN_VERSION = "1.1.0"
```

### Tests

Six new tests in `test_well_known.py`:

1. `test_observation_is_canonical_entity` — `is_canonical_entity_type("Observation")` returns True.
2. `test_measurement_is_canonical_entity` — same for `"Measurement"`.
3. `test_has_observation_is_canonical_edge` — `is_canonical_edge_kind("hasObservation")` returns True.
4. `test_observation_alignment_uri` — `schema_alignment_for_entity_type("Observation") == "schema.org/Observation"`.
5. `test_has_observation_has_no_alignment` — `schema_alignment_for_edge_kind("hasObservation") is None`.
6. `test_well_known_version_bumped` — assert `WELL_KNOWN_VERSION == "1.1.0"`.

### Docs

`docs/agent-guide/schemas.md` — add rows to the EntityType + EdgeKind canonical tables. One paragraph describing the Observation / Measurement distinction with the "filter_rate" example.

**Done when:** 6 tests pass; mypy clean; `docs/agent-guide/schemas.md` cross-references this ADR.
**Estimated size:** ~60 LOC code + ~50 LOC tests + ~30 LOC docs.

## 4. Phase 1 — SDK + MCP surface

### Files to touch

- `src/trellis_sdk/observations.py` (new)
- `src/trellis_sdk/__init__.py` (export)
- `src/trellis/mcp/tools.py` (MCP tool registration — locate current home via `grep "@tool" src/trellis/mcp/`)
- `tests/unit/sdk/test_observations.py` (new)

### API

```python
# trellis_sdk
def record_observation(
    *,
    subject_entity_id: str,
    kind: str,
    value: float | bool | str | list | dict | None,
    window_start: datetime,
    window_end: datetime,
    method: str,
    sample_size: int | None = None,
    unit: str | None = None,
    confidence: float | None = None,
    evidence_ref: str | None = None,
    agent_id: str,
    is_measurement: bool = False,  # toggles entity_type between Observation and Measurement
) -> str:
    """Record an observation about an entity. Returns the new observation entity_id.

    Raises:
        ValueError: if any required field is missing or malformed.
        PolicyError: if the subject entity's DataClassification forbids observation writes.
    """
```

Under the hood, `record_observation` constructs an `EntityDraft(entity_type="Observation" | "Measurement", properties=...)` plus an `EdgeDraft(edge_kind="hasObservation", source=subject_entity_id, target=<new_id>)`, batches both, and submits through MutationExecutor.

`query_observations(subject_entity_id, kind=None, since=None, limit=20) -> list[ObservationRecord]` mirrors on read.

### MCP

Same two operations exposed as MCP tools `record_observation` and `query_observations`. JSON-schema enforced server-side; same loud-on-missing-required-field discipline.

### Tests

- Round-trip: record + query returns the same observation.
- Missing `method` raises ValueError; no observation written.
- Local mode + remote mode (httpx mock) both work — `trellis_sdk` is dual-mode per CLAUDE.md.

**Done when:** SDK tests green; MCP tool surfaces in `trellis-mcp tools list`; PolicyError path tested with a synthetic gate.
**Estimated size:** ~250 LOC code + ~200 LOC tests.

## 5. Phase 2 — retrieval strategy

### Files to touch

- `src/trellis/retrieve/strategies.py` — add `ObservationSearch(SearchStrategy)`.
- `tests/unit/retrieve/test_strategies.py` — add `TestObservationSearch` class.

### Behavior

`ObservationSearch.search(query=ObservationQuery(seed_entity_ids=[...], kind=None, since=None, limit=20))`:

1. For each seed entity_id, traverse outbound `hasObservation` edges.
2. For each Observation/Measurement node found, optionally filter by `properties.kind` and `properties.window_end >= since`.
3. Return as `PackItem(item_type="observation", source_strategy="observation")`.
4. Tier mapping: observations land in the "semantic" tier by default; `Measurement` rows go to a new "metric" sub-tier (see `src/trellis/retrieve/tier_mapping.py` — extend).

### Integration with PackBuilder

When a seed entity is in the pack and `ObservationSearch` is registered, observations attached to that seed are *additively* injected. They do not displace structural neighbors; they extend the pack within the `max_items` / `max_tokens` budget. Importance scoring uses observation `confidence` if present, else 0.5 default — `confidence=None` does NOT trigger a fallback to 0.5 silently; **the strategy emits a `DEBUG`-level event for missing-confidence cases** so the operator can see prevalence in real workloads.

### Tests

- 3 tests in `TestObservationSearch`: round-trip, kind filter, since filter.
- 2 tests in `TestPackBuilderObservations`: observations augment a pack within budget; observations are *not* preferred over structural seeds.

**Done when:** 5 new tests green; PackBuilder integration works in a sample scenario.
**Estimated size:** ~200 LOC code + ~150 LOC tests.

## 6. Phase 3 — sample extractor

### Files to touch

- `src/trellis_workers/extract/query_pattern_observer.py` (new)
- `tests/unit/workers/extract/test_query_pattern_observer.py` (new)
- `eval/fixtures/query_log_sample.jsonl` (new, ~100 synthetic rows)

### Behavior

`QueryPatternObserver` takes a path to a JSONL query log (one record per query: `{timestamp, query_text, tables_referenced, columns_filtered, columns_projected, ...}`) and a target entity (a Dataset node representing a table). Outputs `EntityDraft`s for:

- One `Observation` node per (column, kind) pair where kind ∈ `{filter_rate, projection_rate, null_rate_inferred}`.
- One `Measurement` node per column with `kind="query_count"`, `value=int`.

For each, an `EdgeDraft` `hasObservation` from the column to the new observation, plus a `wasDerivedFrom` edge from the new observation to the source query-log file (or a synthetic ID representing the analysis run).

**Deterministic, no LLM.** This is the simplest end-to-end demonstration; LLM-based observation producers ship later.

### Tests

- Fixture round-trip: load 100-row log, run extractor, assert N expected observations.
- Schema validity: every emitted draft passes `EntityDraft` validation.
- Loud failure: malformed row raises; the extractor does **not** skip.

**Done when:** 4 tests green; extractor runs against the fixture and produces a stable count.
**Estimated size:** ~300 LOC code + ~200 LOC tests + ~150 LOC fixture.

## 7. Phase 4 — eval scenario

### Files to touch

- `eval/scenarios/observation_retrieval.py` (new)
- `eval/scenarios/__init__.py` (register)

### Behavior

Synthetic scenario: generate a graph with 10 tables × 10 columns each. Inject observations on half the columns. Run a PackBuilder query for "what do we know about table X?" against the populated graph. Assert:

- Pack contains the table node + all 10 columns + observations on the 5 that have them.
- Without `ObservationSearch` registered, pack contains structural neighbors only.
- Observations carry `wasDerivedFrom` provenance.

### Done when

- Scenario runs against SQLite (default), Postgres, Neo4j, ArcadeDB.
- Recall@10 for observations ≥ 0.9 across backends.
- Pack token budget honored.

**Estimated size:** ~400 LOC scenario + ~100 LOC for the synthetic generator.

## 8. Total size estimate

~1400 LOC of code + ~700 LOC of tests + ~250 LOC of docs/fixtures. Sized for **two consecutive swarm units** (Phase 0+1 first, Phases 2+3+4 second). Phases 0+1 are mergeable without 2-4 in place — the new types exist, the SDK exists, retrieval just doesn't yet use them.

## 9. Cleanup considerations

- After Phase 3, audit existing code paths that stuff statistics into `entity.properties` JSON. Surface candidates as part of the [`plan-cleanup-dead-code.md`](./plan-cleanup-dead-code.md) follow-up.
- After Phase 2, verify `src/trellis/retrieve/tier_mapping.py:30,42` (which expects `{"precedent","owner","team"}` per existing TODO) is brought into the same audit — the new "metric" tier sub-mapping is a chance to consolidate.

## 10. Risks

- **Pack budget displacement.** Observations are additive; on a high-observation entity, they could crowd out structural neighbors. Mitigation: PackBuilder applies the budget per source_strategy as well as globally — extend `BudgetConfig` to support per-strategy caps if Phase 4 surfaces displacement.
- **Observation explosion.** Every CLI analyze command (Item 6) writes observations. Without sampling, the graph balloons. Mitigation: Item 6's plan owns the sampling logic; this plan does not.
- **SCD-2 cost.** Updating an observation creates a new version of the observation node. For high-frequency measurement streams this is expensive. Mitigation: `Measurement` nodes are *append-only* by convention — a new measurement is a new node with new `window_*`, never a mutation of an existing node. The plan documents this; PackBuilder retrieval applies `since` filters to keep historical measurements out unless requested.
