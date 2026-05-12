# ADR: Well-known promotion loop

**Status:** Proposed
**Date:** 2026-05-11
**Deciders:** Trellis core
**Related:**
- [`./plan-well-known-promotion-loop.md`](./plan-well-known-promotion-loop.md) — implementation plan
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — defines the canonical registry being promoted into
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — proposes a parallel CUSTOM_TAG_USED telemetry for content tags; this ADR generalizes the pattern
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program

---

## 1. Context

The graph-ontology ADR commits to **open-string extensibility** ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §5.2). Adopters define domain-specific entity types (`"dbt_model"`, `"oncall_shift"`, `"metric"`) and edge kinds (`"emits_metric"`, `"escalates_to"`) without core changes. This is correct.

The cost of correctness: **the canonical `well_known.py` registry never grows.** A type like `"metric"` used by 12 extractors across 4 domains stays an open string forever, with no schema_alignment URI, no PackBuilder retrieval bucketing, no SDK ergonomics, no MCP tool schema for it. The ADR explicitly contemplates this gap ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §6.4 Phase N) and defers a promotion path until a design partner asks.

Two things have changed since Phase 0 of the graph-ontology ADR landed:

1. **Item 6 (dogfooding) makes Trellis its own first user.** Without a promotion loop, Trellis-emitted open-string types pile up uncontrolled.
2. **The `CUSTOM_TAG_USED` telemetry proposed by [`adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) §5** is the same shape for content tags. Building one mechanism instead of two is straightforward.

The decision: build the generic promotion-candidate loop *now*, before the registry has time to drift further.

## 2. Decision

Introduce a `WELL_KNOWN_CANDIDATE` event type and a `src/trellis/learning/schema_evolution.py` analyzer. The loop is **surface-only** — it never auto-mutates `well_known.py`. Candidates surface in a CLI report; an ADR amendment is the only path to formal promotion.

### 2.1 Candidate sources

The analyzer reads from the EventLog and the GraphStore to identify open-string `node_type` and `edge_kind` values that meet promotion criteria. The criteria, as a starting threshold (operator-tunable via parameter registry):

| Dimension | Default threshold |
|---|---|
| Total count of writes with this open-string type | ≥ 500 |
| Distinct extractors emitting this type | ≥ 2 |
| Distinct domains (per ContentTags.domain) | ≥ 2 |
| Average `signal_quality` (if classified) | ≥ "standard" |
| Time window the data is drawn from | ≥ 7 days |

A candidate that passes all five dimensions is *surfaced*. The thresholds are deliberately high — false positives in the candidate list are cheaper than false negatives that pollute discussion.

### 2.2 Candidate output

```python
@dataclass
class WellKnownCandidate:
    candidate_kind: Literal["entity_type", "edge_kind"]
    open_string_value: str
    count: int
    distinct_extractors: list[str]
    distinct_domains: list[str]
    avg_signal_quality: str
    first_seen: datetime
    last_seen: datetime
    suggested_canonical_name: str       # PascalCase for entity, camelCase for edge — see §2.4
    suggested_alignment_uri: str | None # heuristic suggestion; never authoritative
    candidate_id: str                   # stable hash of open_string_value + candidate_kind
    cooldown_until: datetime | None     # if previously surfaced, when it's eligible to re-surface
```

### 2.3 Idempotency and cooldown

The analyzer emits `WELL_KNOWN_CANDIDATE` events to the operational EventLog. The `candidate_id` is a stable hash; a candidate that was surfaced last week and has not changed materially does *not* re-emit. The cooldown is 7 days by default, reset to 0 if either:

- The candidate's count grew by ≥ 20% since last emission, or
- The candidate now meets a threshold it previously missed (e.g., it crossed the domain-count bar).

Per [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.4, this is the idempotency contract for self-modifying loops.

### 2.4 Naming heuristics for `suggested_canonical_name`

The suggestion is a starting point for the human author, not authoritative:

- For entity types: PascalCase, underscore→camel (`dbt_model` → `DbtModel`).
- For edge kinds: camelCase, underscore→camel (`emits_metric` → `emitsMetric`).
- If the open string is already in canonical form, no suggestion is made.
- If the suggested name collides with an existing canonical name, the analyzer flags `naming_collision=True` — the human authoring the ADR amendment decides whether to alias, rename, or reject.

### 2.5 The promotion path (no auto-mutation)

The system never writes to `well_known.py`. The promotion path is:

1. `trellis analyze schema-evolution` reports candidates.
2. A human (or, eventually, the Item 7 coding-agent loop) reads the report and decides whether to promote.
3. A new ADR amendment to `adr-graph-ontology.md` proposes the addition with the candidate evidence inline.
4. The ADR's "Accepted" status is the prerequisite for editing `well_known.py`.

The CLI provides a template generator (`trellis admin draft-promotion-adr <candidate_id>`) that produces the ADR amendment file pre-populated with the candidate evidence. This makes the formal step low-friction without short-circuiting it.

## 3. Why this shape

### 3.1 Why not auto-mutate

The canonical registry is a **one-way commitment** ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §5.4) — a name added today must work forever. Auto-mutation by a statistical heuristic is the wrong shape: short-lived spikes (an extractor bug emitting `dbt_modle` 800 times in a day) would pollute the registry forever. A human gate at the ADR-amendment step is correct.

### 3.2 Why generalize entity + edge + (future) content tag in one mechanism

The `CUSTOM_TAG_USED` telemetry proposed by `adr-tag-vocabulary-split.md` §5 is the same shape: count occurrences of an open-string value, surface when it crosses a threshold, route through a human-gated ADR. Building three independent mechanisms — one for entity types, one for edge kinds, one for content tags — would mean three code paths to maintain. Generalize once.

### 3.3 Why threshold-based and not anomaly-based

Threshold criteria are interpretable and operator-tunable. Anomaly detection (e.g., "this type's growth rate is 5σ above its baseline") is sexier but harder to explain when it fires unexpectedly. POC stage prefers boring.

## 4. Guardrails

### 4.1 No silent suppression of candidates

If the analyzer finds a candidate but emits no event because of the cooldown, it logs at INFO level naming the candidate and the cooldown remaining. An operator reading logs can always reconstruct the full candidate set.

### 4.2 No "dead candidate" rot

A candidate that was surfaced 6 months ago and never resulted in an ADR amendment, but whose underlying count keeps growing, **re-surfaces with `recurrence_count` incremented**. Persistent candidates are persistent signals; the loop doesn't let them age out silently.

### 4.3 No domain-specific promotion

Domain-specific types that are correct to stay open-string (e.g., `unity_catalog_table`, used only by the Unity Catalog integration) will hit the count threshold. The criteria of `distinct_domains ≥ 2` filters most of these — single-domain types don't promote. The human review step rejects the rest.

## 5. Consequences

### 5.1 What this enables

- The well_known registry can grow with evidence-driven additions.
- Item 7 (coding-agent loop) has its second signal source (`WELL_KNOWN_CANDIDATE` events).
- `adr-tag-vocabulary-split.md` Phase 5 (`CUSTOM_TAG_USED` telemetry) is satisfied by this mechanism — that Phase becomes a thin specialization rather than a parallel build.

### 5.2 What this does not do

- Does not introduce auto-promotion.
- Does not change `well_known.py` write semantics.
- Does not change how open-string types are stored or retrieved (open-string contract preserved).

## 6. References

- `adr-graph-ontology.md` §5.4 (one-way commitment), §6.4 (Phase N gap)
- `adr-tag-vocabulary-split.md` §5 (CUSTOM_TAG_USED telemetry, parallel concern)
- `learning/scoring.py` (existing recommendation pattern; not directly reused but spiritually similar)
