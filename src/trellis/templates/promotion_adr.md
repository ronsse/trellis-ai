# ADR: Promote `{open_string_value}` to well-known {candidate_kind_label}

**Status:** Proposed
**Date:** {drafted_date}
**Deciders:** TBD
**Related:**

- [`adr-graph-ontology.md`](./adr-graph-ontology.md) — canonical registry contract (§5.2 open-string extensibility, §5.4 one-way commitment)
- [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) — promotion-loop mechanism producing this draft
- [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program scope (item 5)

> **Auto-drafted by `trellis admin draft-promotion-adr {candidate_id}` on {drafted_date}.**
> This is a scaffolding artifact — fill in the *Decision* section before requesting review.

---

## 1. Context

The open-string {candidate_kind_label} `{open_string_value}` has accumulated promotion-worthy usage across the graph:

| Dimension | Observed value | Threshold |
|---|---|---|
| Total writes (current nodes) | **{count}** | ≥ {count_threshold} |
| Distinct extractors | **{distinct_extractors_count}** | ≥ {distinct_extractors_threshold} |
| Distinct ContentTags.domain values | **{distinct_domains_count}** | ≥ {distinct_domains_threshold} |
| Average signal_quality | **{avg_signal_quality}** | ≥ {min_signal_quality_threshold} |
| Evidence window span | **{evidence_window_days_observed} day(s)** | ≥ {window_days_threshold} day(s) |

### 1.1 Evidence summary

- **First seen:** {first_seen}
- **Last seen:** {last_seen}
- **Recurrence count (prior surfaces of this candidate):** {recurrence_count}
- **candidate_id:** `{candidate_id}`

### 1.2 Extractors emitting `{open_string_value}`

{distinct_extractors_block}

### 1.3 Domains represented

{distinct_domains_block}

## 2. Proposed canonical name

**Suggested:** `{suggested_canonical_name}`
{naming_collision_block}
**Suggested schema_alignment URI:** {alignment_uri_label}

The naming heuristic is advisory only. The ADR author is the source of truth — rename freely if the suggestion is wrong.

### 2.1 Proposed `well_known.py` diff (sketch)

```python
# In src/trellis/schemas/well_known.py:

{well_known_constant_name}: Final = "{suggested_canonical_name}"

CANONICAL_{candidate_kind_upper}S: Final[frozenset[str]] = frozenset(
    {{
        # ... existing canonicals ...
        {well_known_constant_name},
    }}
)
```

{alignment_diff_block}

## 3. Decision

> **Fill this in.** Options:
>
> 1. **Accept** the suggested name and ship the diff above.
> 2. **Rename** before accepting — choose a different canonical, justify here.
> 3. **Alias-only** — register `{open_string_value}` as a legacy alias of an existing canonical; explain which one.
> 4. **Reject** — the candidate is real but belongs in a domain-specific extension, not the core registry; close this draft.

**Chosen option:** _TBD_

**Rationale:** _TBD_

## 4. Guardrail acknowledgments

Per [`adr-graph-ontology.md`](./adr-graph-ontology.md) §5, every promotion carries these commitments. Acknowledge by ticking the boxes (or strike through with justification if non-applicable):

- [ ] **One-way commitment (§5.4).** The canonical name added here will work forever; it cannot be removed or repurposed.
- [ ] **Open-string compatibility preserved.** Existing rows with `{open_string_value}` as an open string continue to work — there is no migration; the canonical and the open string coexist by design.
- [ ] **Alias map (if applicable) updated.** If the old open-string form needs to alias to the new canonical, both the legacy-set and the canonical inverse map are updated together.
- [ ] **schema_alignment URI verified.** The URI in `_ENTITY_SCHEMA_ALIGNMENT` / `_EDGE_SCHEMA_ALIGNMENT` resolves to a real published schema (schema.org, PROV-O, ...) — not invented.
- [ ] **MCP / SDK ergonomics.** Downstream MCP tool schemas (if any) referencing the canonical name are updated in lockstep with this ADR landing.

## 5. Consequences

### 5.1 What this enables

- TBD (e.g., "PackBuilder can now bucket `{open_string_value}` entities under retrieval section X").

### 5.2 What this does not change

- Open-string types remain valid throughout the system — this ADR is *additive*, not exclusive.
- Existing data is unmodified.

## 6. References

- `adr-graph-ontology.md` (canonical registry contract)
- `adr-well-known-promotion-loop.md` §2.4 (naming heuristics)
- `learning/schema_evolution.py` (analyzer source)
- Cooldown / recurrence telemetry: query EventLog for `WELL_KNOWN_CANDIDATE` events with `candidate_id={candidate_id}`.
