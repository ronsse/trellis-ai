# Plan: Well-known promotion loop

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable
**ADR:** [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 5
**Depends on:** none (the analyzer reads existing EventLog + GraphStore data; no new event-emission contract needed for the input side).
**Unblocks:** Item 7 (coding-agent loop) consumes `WELL_KNOWN_CANDIDATE` events.

## 1. Scope

**In scope:**
- New `WELL_KNOWN_CANDIDATE` event type registered in the operational EventLog.
- `src/trellis/learning/schema_evolution.py::analyze_well_known_candidates()`.
- CLI: `trellis analyze schema-evolution`.
- CLI: `trellis admin draft-promotion-adr <candidate_id>` for human authoring.
- Eval scenario.

**Out of scope:**
- Auto-mutation of `well_known.py` (explicitly forbidden by the ADR).
- Promotion for content tags (the ADR explicitly says the mechanism generalizes; the wiring of `CUSTOM_TAG_USED` is `adr-tag-vocabulary-split.md` Phase 5's work, not this plan's).
- LLM-driven naming heuristics.

## 2. POC directives applied

- Analyzer is **read-only**. It never writes to GraphStore or to `well_known.py`. Only writes are `WELL_KNOWN_CANDIDATE` events to the EventLog.
- Threshold lookup uses the parameter registry (Item 3's wiring). If the registry lacks the key, **raises** — no silent default thresholds.
- The ADR-draft CLI **refuses to overwrite** an existing ADR file with the same candidate_id. Operator must explicitly `--force` if they want to regenerate (and the regeneration emits a WARN event noting the prior file content).
- Naming-collision detection **raises an error** in `draft-promotion-adr` if the suggested name conflicts with an existing canonical — operator must rename or alias explicitly.

## 3. Phases

### Phase 0 — event registration

**Files to touch:**
- `src/trellis/schemas/event.py` — add `WELL_KNOWN_CANDIDATE` to `EventType`.
- `tests/unit/schemas/test_event.py` — 1 test for the new EventType.

**Estimated size:** ~10 LOC + ~15 LOC tests.

### Phase 1 — analyzer

**Files to touch:**
- `src/trellis/learning/schema_evolution.py` — new module.
- `tests/unit/learning/test_schema_evolution.py` — new.

**Analyzer API:**

```python
def analyze_well_known_candidates(
    *,
    graph_store: GraphStore,
    event_log: EventLog,
    registry: ParameterRegistry,
    since: datetime,
    until: datetime | None = None,
    candidate_kinds: tuple[str, ...] = ("entity_type", "edge_kind"),
) -> list[WellKnownCandidate]:
    """Identify open-string types eligible for canonical promotion.

    Emits WELL_KNOWN_CANDIDATE events for newly-eligible candidates,
    respecting cooldown per candidate_id.

    Raises:
        KeyError: if registry lacks any threshold key.
    """
```

Internal flow:

1. Pull all open-string `node_type` values from GraphStore (one COUNT-grouped query per backend, batched).
2. Filter to non-canonical values (`well_known.is_known_entity_type(value) is False`).
3. For each, compute count, distinct_extractors (via EventLog `MUTATION_EXECUTED` events filtered by entity_type), distinct_domains (via attached `ContentTags`), avg_signal_quality.
4. Apply thresholds from registry.
5. Check cooldown via prior `WELL_KNOWN_CANDIDATE` events for this candidate_id.
6. Emit + return for newly-eligible.
7. Same flow for edge_kind.

**Cooldown check:** query EventLog for the most recent `WELL_KNOWN_CANDIDATE` event with this `candidate_id`. If `(now - last_emitted) < cooldown_window` AND count growth < 20%, skip emission. Log at INFO with cooldown remaining.

**Tests (10):**

1. Empty graph → empty candidate list.
2. Open-string type with count=300 → not eligible (below default 500 threshold).
3. Open-string type with count=600, 2 extractors, 2 domains → eligible; event emitted.
4. Same as 3, immediate re-run → not emitted (cooldown).
5. Same as 3, after cooldown_window → re-emitted (recurrence).
6. Same as 3, count grows to 800 within cooldown → re-emitted (growth trigger).
7. Canonical type ("Person") with count=10000 → not in candidate list (already canonical).
8. Single-extractor type → not eligible (distinct_extractors < 2).
9. Missing registry threshold → raises KeyError naming the missing key.
10. Naming collision: open-string "Person" (case mismatch) → suggested_canonical_name flags `naming_collision=True`.

**Estimated size:** ~500 LOC code + ~400 LOC tests.

### Phase 2 — CLI: analyze schema-evolution

**Files to touch:**
- `src/trellis_cli/analyze.py` — add subcommand.
- `tests/unit/cli/test_analyze.py` — add tests.

**CLI:**

```
trellis analyze schema-evolution
    --since 7d
    --until now
    --kinds entity_type,edge_kind   # default both
    --format table|json             # default table
    --no-emit                       # analyze without emitting events (dry-run)
```

Table output columns: `kind`, `open_string`, `count`, `distinct_extractors`, `distinct_domains`, `suggested_canonical`, `candidate_id`. Exit 0 unless `--strict` and any new candidates surfaced.

**Tests:** dry-run mode does not write events; non-dry-run does. Output format matches expected fixture for a synthetic graph.

**Estimated size:** ~150 LOC + ~100 LOC tests.

### Phase 3 — CLI: admin draft-promotion-adr

**Files to touch:**
- `src/trellis_cli/admin.py` — add subcommand.
- `src/trellis/templates/promotion_adr.md.j2` — Jinja2 template (or simple .format() — pick whichever is already in use).
- `tests/unit/cli/test_admin.py` — add tests.

**CLI:**

```
trellis admin draft-promotion-adr <candidate_id>
    --output docs/design/adr-promote-<name>.md   # default
    --canonical-name <override>                  # optional override of suggestion
    --force                                      # overwrite existing file
```

Output: a markdown file pre-populated with:

- The candidate's evidence (count, extractors, domains).
- A proposed `well_known.py` diff.
- A proposed `_ENTITY_SCHEMA_ALIGNMENT` or `_EDGE_SCHEMA_ALIGNMENT` entry if a heuristic alignment URI was suggested.
- A blank "Decision" section for the human to fill in.
- The required guardrail acknowledgments from `adr-graph-ontology.md` §5.

**Tests:** generated ADR has the expected sections, references the candidate_id, refuses to overwrite without `--force`, names-collision detection raises.

**Estimated size:** ~250 LOC + ~150 LOC tests + ~80 LOC template.

### Phase 4 — eval scenario

**File:**
- `eval/scenarios/schema_evolution_candidate_emergence.py` — new.

**Behavior:** synthetic ingest of 1000 entities with `entity_type="metric"`, distributed across 3 extractor IDs and 4 domains, with `signal_quality="standard"` or above. Run `analyze_well_known_candidates()`. Assert:

- One candidate surfaced for `"metric"` with count=1000.
- Suggested canonical name = `"Metric"`.
- No naming collision.
- Subsequent run (same data) does not re-emit (cooldown).
- After advancing test clock past cooldown, re-emit fires.

**Estimated size:** ~300 LOC.

## 4. Total size estimate

| Phase | LOC code | LOC tests |
|---|---|---|
| 0 | 10 | 15 |
| 1 | 500 | 400 |
| 2 | 150 | 100 |
| 3 | 330 | 150 |
| 4 | 300 | 0 (scenario *is* the test) |
| **Total** | **~1290** | **~665** |

Sized for **one swarm unit** if scoped tight, **two** if split into Phases 0+1 / Phases 2+3+4.

## 5. Done when

- All tests pass.
- `trellis analyze schema-evolution` runs against the demo graph and produces a coherent (possibly empty) report.
- `trellis admin draft-promotion-adr <id>` produces a syntactically valid markdown file referencing `adr-graph-ontology.md`.
- Eval scenario passes.
- mypy clean.

## 6. Cleanup considerations

- After landing, the `CUSTOM_TAG_USED` work scoped by `adr-tag-vocabulary-split.md` Phase 5 should be re-evaluated — most of it is now satisfied by this mechanism. Update that ADR with a pointer to this work.
- Existing TODO.md item D.5 ("CUSTOM_TAG_USED telemetry + admin reporting CLI + promotion process") becomes "subsumed by self-improvement program item 5".

## 7. Risks

- **Cold-start with empty EventLog.** A fresh install has no MUTATION_EXECUTED history, so all candidates have count=0. Analyzer returns empty list. Mitigation: documented behavior, not a bug. The CLI exits 0 with "no candidates found" message.
- **Cross-extractor over-counting.** If extractor A and extractor B both extract the same row (one deterministic, one LLM fallback per ExtractionDispatcher), naive counting double-counts. Mitigation: count `distinct entity_ids written with this type`, not raw write events.
- **Schema-evolution feedback loop with Item 6 (dogfooding).** If the analyzer itself writes `Activity` and `Observation` nodes (via Item 6's meta-trace machinery), those become candidates for promotion. Already canonical, so they don't surface — but any future Trellis-emitted open strings would. The analyzer's own writes should not be counted toward promotion criteria: add an explicit filter for `extractor_id startswith "trellis_meta_"`.
