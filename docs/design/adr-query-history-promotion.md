# ADR: Query-history promotion path

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Trellis core
**Resolves:** [#218](https://github.com/ronsse/trellis-ai/issues/218)
**Related:**
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — #217, the enterprise-ontology umbrella this ADR sits under
- [`./adr-ontology-profiles.md`](./adr-ontology-profiles.md) — #219, profile mechanism that scopes which pattern/fact types a deployment admits
- [`./adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md) — #220, the accepted-facts handoff to an external Enterprise Graph Platform
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — existing `Observation` / `Measurement` home this ADR builds on
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — sets the schema.org / PROV-O alignment + open-string contract this ADR inherits

---

## 1. Context

Query history is one of the highest-value inputs for data-platform agents and enterprise-graph enrichment. A warehouse's query log reveals real usage: common joins, frequently co-accessed tables, hot datasets, repeated filters, generated assets, and demand signals. Agents that retrieve "how is this table actually used" before writing a query get materially better context than agents that read only the catalog.

But query history is **behavioral evidence, not canonical semantic truth.** A pair of tables joined together in 4,000 queries is a strong *signal*; it is not, by itself, a curated `relatedTo` edge in the ontology, and it is certainly not an accepted enterprise fact. The query author is not the table owner. A manual SQL run that produced a table is provenance, not a lineage declaration. Co-access is correlation, not a dependency.

Without an explicit promotion policy, query-history enrichment pollutes the graph with behavioral artifacts that *look* like curated truth, and it risks storing raw SQL and raw user identifiers — a privacy and security liability.

Trellis already has the right primitives to model this correctly without inventing a parallel schema:

- `Measurement` ([`src/trellis/schemas/measurement.py`](../../src/trellis/schemas/measurement.py)) — scalar, time-series-shaped metrics with `metric_name` / `metric_value` / `measured_at`, append-only by convention.
- `Observation` ([`src/trellis/schemas/observation.py`](../../src/trellis/schemas/observation.py)) — narrative, evidence-bearing claims with `confidence` and an open `metadata` bag carrying the conventional `window_start` / `window_end` / `sample_size` / `method` keys from [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §2.3.
- `NodeRole` ([`src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py)) — `structural` / `semantic` / `curated`, where `curated` nodes carry a `GenerationSpec`.
- `GenerationSpec` ([`src/trellis/schemas/entity.py`](../../src/trellis/schemas/entity.py)) — `generator_name` / `generator_version` / `generated_at` / `source_node_ids` / `source_trace_ids` / `parameters`, exactly the provenance a derived pattern node needs.
- `EntityDraft` / `EdgeDraft` ([`src/trellis/schemas/extraction.py`](../../src/trellis/schemas/extraction.py)) carry `node_role` and `generation_spec`, so the tiered extractor path can emit curated drafts directly.

This ADR defines the **layered promotion model** that maps each tier of query-history derivation onto those existing primitives, the **hard safety rules** that govern what may and may not be stored or inferred, and the **promotion gates** that separate states safe for Trellis retrieval from states eligible for external EGP publication.

## 2. Decision

Adopt a six-state promotion ladder. Each state is a distinct level of epistemic commitment, and each maps onto an existing Trellis primitive. Promotion between states is **monotonic and gated** — a higher state never silently overwrites the lower-state evidence it was derived from; the lower state remains as provenance.

| # | State | Trellis primitive | `node_role` | Retrieval? | EGP publish? |
|---|---|---|---|---|---|
| 1 | Raw execution evidence | trace / `Evidence` row, short retention | n/a (off-graph) | No (default) | No |
| 2 | Measurement | `Measurement` node + `hasMeasurement` edge | `semantic` | Yes (via `ObservationSearch`) | No |
| 3 | Observation | `Observation` node + `hasObservation` + `wasDerivedFrom` | `semantic` | Yes | No |
| 4 | Curated pattern node | `Entity` (`JoinPattern` / `AccessPattern` / `HotDataset` / `QueryTemplate`) | `curated` + `GenerationSpec` | Yes | No |
| 5 | Candidate enterprise fact | `Entity` / `Edge`, `metadata["fact_state"]="candidate"` | `curated` + `GenerationSpec` | Yes (labeled candidate) | No |
| 6 | Accepted enterprise fact | `Entity` / `Edge`, `metadata["fact_state"]="accepted"` | `semantic` or `curated` | Yes | **Yes** |

This ADR is design only. It does not register new well-known canonicals, does not add fields to any schema, and does not ship an extractor. The pattern-node types in state 4 are **open-string entity types** per the [`adr-graph-ontology.md`](./adr-graph-ontology.md) open-string contract — they live in a data-platform extension package (`trellis_workers.extract`), not in core `well_known.py`. Whether a given deployment admits them is governed by the ontology-profile mechanism in [`adr-ontology-profiles.md`](./adr-ontology-profiles.md) (#219).

### 2.1 State 1 — Raw execution evidence

The query-log row itself: who ran what, when, against which datasets, with what runtime. This is the rawest tier and the most dangerous.

- **Never the SQL body by default** (see §4). At most a redacted/normalized query shape and a content hash for dedup/grouping.
- Actor is **hashed or grouped**, never a raw email modeled as a graph fact (§4).
- Modeled as a trace/`Evidence` row referencing the touched `Dataset` entities via `wasInformedBy` — off-graph, **short retention**, **not retrieved by default**.
- A manual, human-authored SQL run that generated an asset is recorded as PROV-style `Activity` evidence (`wasGeneratedBy` from the produced `Dataset`, `wasAssociatedWith` the hashed actor) — see the worked example in §6.3. This is provenance of *an action*, not a lineage *declaration*.

### 2.2 State 2 — Measurement

Aggregate counts over a window collapse the raw rows into machine-comparable metrics. Use `Measurement` verbatim:

- `metric_name`: `"query_count"`, `"co_access_count"`, `"join_frequency"`, `"filter_count"`, `"p99_runtime_ms"`.
- `metric_value`: the scalar.
- `subject_entity_id` / `subject_entity_type`: the `Dataset` (or column) the metric is about.
- `measured_at` plus `metadata`: `window_start`, `window_end`, `sample_size`, `method` (e.g. `"count_distinct_query_hashes"`), and `generator_version`.
- Attached via the `hasMeasurement` edge ([`well_known.py`](../../src/trellis/schemas/well_known.py)).

Measurements are append-only by convention (per [`measurement.py`](../../src/trellis/schemas/measurement.py) docstring) — a new window is a new node, not a mutation. This keeps high-frequency metric streams from churning SCD-2 versions.

### 2.3 State 3 — Observation

A narrative claim that interprets one or more measurements: *"`fct_orders` is usually joined to `dim_customer` on `customer_id`."* Use `Observation` verbatim:

- `content`: the narrative.
- `subject_entity_id`: the primary `Dataset`.
- `confidence`: producer's confidence in `[0,1]`.
- `evidence_ref`: pointer back to the measurement / trace it summarizes.
- `metadata`: `window_start`, `window_end`, `sample_size`, `method`, `generator_version`, plus redaction metadata (which fields were redacted before any LLM summarization).
- Carries `wasDerivedFrom` back to its source rows (the provenance edge) and `hasObservation` from its subject (the subject-of edge), per [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §2.2.

An Observation derived via an LLM summary of query intent **must** record `method="llm_summary_from_redacted_query_log"` and the `generator_version`; the SQL fed to that LLM must already be redacted (§4).

### 2.4 State 4 — Curated pattern node

When a signal is strong and stable enough to be worth its own retrievable node, promote it to a curated `Entity` of an open-string pattern type:

| Pattern type | Models | Primary metric backing it |
|---|---|---|
| `JoinPattern` | a recurring join between two datasets on a key | `join_frequency` |
| `AccessPattern` | a recurring co-access / sequence of datasets | `co_access_count` |
| `HotDataset` | a dataset with disproportionately high usage | `query_count` |
| `QueryTemplate` | a redacted, parameterized recurring query shape | `query_count` per shape-hash |

These are `node_role="curated"` and **must** carry a `GenerationSpec` (`generator_name="query_history_promoter"`, `generator_version`, `source_node_ids` = the measurements/observations it rolled up, `parameters` = the thresholds that fired). They are retrievable and useful for ranking, but they are explicitly **patterns**, not semantic ontology edges. A `JoinPattern` node says "these tables are joined a lot"; it does **not** create a `relatedTo` edge asserting the tables are semantically related (§4).

### 2.5 State 5 — Candidate enterprise fact

A pattern node *proposes* a semantic fact — e.g., "the `JoinPattern` between `fct_orders` and `dim_customer` suggests a real foreign-key relationship." That proposal is recorded as a candidate: a `curated` `Entity`/`Edge` with `metadata["fact_state"]="candidate"` and a `metadata["candidate_for"]` pointer to the semantic edge it would become. Candidates are retrievable but **always surfaced labeled as candidate/inferred**, never as accepted truth, and never published to EGP.

### 2.6 State 6 — Accepted enterprise fact

A candidate becomes accepted **only** after a promotion gate clears (§5): human/domain-owner review, or corroboration from an authoritative source (a catalog-declared FK, a dbt relationship test). On acceptance the fact carries `metadata["fact_state"]="accepted"`, `metadata["accepted_by"]`, and `metadata["accepted_at"]`. Accepted facts are the **only** state eligible for EGP publication via the bridge in [`adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md) (#220).

## 3. Retrieval vs EGP publication

Two different consumers, two different bars.

- **Trellis retrieval** is advisory context for an agent that will reason about it. States 2–5 are all retrievable, because the consuming agent reconciles signal against truth (the same principle as [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §2.4: empirical observations coexist with structural facts; Trellis does not adjudicate). Candidate facts (state 5) are retrievable **only when labeled** so the agent knows the claim is inferred, not accepted.
- **EGP publication** writes into a shared, governed enterprise ontology that *other* systems treat as truth. Only **accepted** facts (state 6) cross that boundary. Raw evidence, measurements, observations, pattern nodes, and candidates **never** publish to EGP.

State 1 (raw evidence) is not retrieved by default at all — it exists for audit and for re-derivation of higher states, behind classification gating.

## 4. Hard Safety Rules

These are non-negotiable. A producer that violates one is a bug, not a tuning choice.

1. **Never store raw SQL literals by default.** Store a redacted/normalized query shape and a content hash. Storing a raw SQL body requires an explicit, deployment-level opt-in with its own retention and classification policy.
2. **Redact SQL before it reaches an LLM.** Any LLM summarization of query intent operates on redacted SQL only. The Observation that results records which fields were redacted.
3. **Hash or group user identifiers.** Do not model raw emails / usernames as graph facts. Actor identity is a hash or a coarse group (team, role) unless there is an approved business reason recorded in the deployment's profile.
4. **Do not infer ownership from query authorship.** The agent or human who ran a query is **not** the owner of the datasets it touched. Authorship is at most `wasAssociatedWith` evidence on an `Activity`, never `wasAttributedTo` ownership on the `Dataset`.
5. **Do not infer lineage from co-access alone.** Two datasets appearing in the same query is co-access, not a `dependsOn` / lineage edge. Lineage comes from declared sources (dbt manifest, OpenLineage), not from the query log.
6. **Do not infer business semantics from repeated joins without review.** A repeated join stays a `JoinPattern` (state 4). It becomes a semantic `relatedTo` / foreign-key edge only through the state-5 → state-6 review gate.
7. **Always store provenance metadata.** Every measurement, observation, pattern node, and candidate **must** record source window, sample size, method, confidence (where applicable), and generator version — using the `Measurement`/`Observation` `metadata` keys and the `GenerationSpec` fields. A producer that omits a required provenance field raises at draft-time validation (the no-silent-defaults discipline from [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §4.2).

## 5. Promotion gates

| Transition | Gate |
|---|---|
| 1 → 2 | Window closes; aggregation runs. Automatic. Requires `sample_size`, `window_*`, `method`. |
| 2 → 3 | Threshold on the backing metric (e.g. `join_frequency ≥ N` over the window). Automatic. LLM-summarized observations require redacted input. |
| 3 → 4 | Stability threshold: the signal persists across ≥ K consecutive windows, and the deployment's ontology profile ([#219](./adr-ontology-profiles.md)) admits the pattern type. Automatic. Emits `curated` node with `GenerationSpec`. |
| 4 → 5 | A pattern crosses a *semantic-proposal* threshold (configurable, profile-scoped). Recorded as `fact_state="candidate"`. Automatic, but the candidate is **only ever surfaced labeled** and **never** published. |
| 5 → 6 | **Review-gated.** Domain-owner approval **or** corroboration by an authoritative declared source (catalog FK, dbt relationship test). Never automatic. Records `accepted_by` / `accepted_at`. |
| 6 → EGP | Accepted facts only, via [`adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md). |

The only fully manual gate is **5 → 6**; everything below it is automatic but monotonic and fully provenanced, so any automatic promotion can be audited and re-derived from the state beneath it.

## 6. Worked examples

### 6.1 Repeated join stays a `JoinPattern` until reviewed

`fct_orders` and `dim_customer` are joined on `customer_id` in 4,200 queries this quarter.

- State 2: `Measurement(metric_name="join_frequency", metric_value=4200, subject=fct_orders, metadata={window, sample_size, method, generator_version})`.
- State 3: `Observation(content="fct_orders is usually joined to dim_customer on customer_id", confidence=0.9, wasDerivedFrom=...)`.
- State 4: `Entity(entity_type="JoinPattern", node_role="curated", generation_spec=...)` linking the two datasets.
- State 5: candidate `relatedTo` / foreign-key edge, `fact_state="candidate"`, surfaced labeled.
- State 6: **only** after a domain owner confirms (or a dbt `relationships` test corroborates) does it become an accepted semantic edge eligible for EGP. Per Rule 6, the join count alone never makes this leap.

### 6.2 High query count becomes a `HotDataset` ranking signal

`dim_customer` is queried 50× more than the median dataset.

- State 2: `Measurement(metric_name="query_count", metric_value=...)`.
- State 4: `Entity(entity_type="HotDataset", node_role="curated", generation_spec=...)`.
- This is used by retrieval as a **ranking signal** (hot datasets surface earlier in packs) — no semantic claim, no candidate fact, no EGP publication. It never needs to climb past state 4.

### 6.3 Manual SQL becomes PROV-style `Action` evidence

A human runs ad-hoc SQL that creates `tmp_revenue_rollup`.

- Recorded as state 1 `Activity` evidence: the produced `Dataset` carries `wasGeneratedBy` → the `Activity`, and the `Activity` carries `wasAssociatedWith` → the **hashed** actor.
- This is provenance of *an action that happened*. It is **not** a lineage declaration (Rule 5) and the actor is **not** the table's owner (Rule 4).

### 6.4 Query author does **not** become owner

`alice@corp` ran 900 queries against `fct_orders`.

- Actor is hashed/grouped (Rule 3). At most this yields a `wasAssociatedWith` link from the query `Activity` to the hashed actor.
- It produces **no** `wasAttributedTo` ownership edge on `fct_orders` (Rule 4). Heavy usage is a `HotDataset` signal about the dataset, not an ownership claim about the user.

## 7. Acceptance criteria

When this ADR's intent is implemented, the following must hold:

1. Trellis docs (this ADR + the query-history recipe) define all six fact states and their backing primitives.
2. The recipe states explicitly which states are retrievable (2–5, candidates labeled) and which publish to EGP (6 only).
3. The worked examples in §6 are reproduced in the agent-guide recipe: repeated join stays `JoinPattern` until reviewed; high query count → `HotDataset` ranking signal; manual SQL → PROV-style `Activity` evidence; query author does not become owner.
4. The recipe documents redaction, actor hashing, and the no-raw-SQL-by-default rule.
5. The promotion-gate table (§5) and the review requirement before any EGP accepted write are documented.
6. Measurements, observations, pattern nodes, and candidates carry source window, sample size, method, confidence, and generator version — enforced by the no-silent-defaults discipline.

## 8. Non-goals

- **No new schema.** This ADR reuses `Measurement`, `Observation`, `NodeRole`, and `GenerationSpec`. It does not add fields, does not register new well-known canonicals, and does not introduce a `fact_state` enum in core — `fact_state` is a metadata-key convention (`candidate` / `accepted`), consistent with the open `metadata` bag on `Entity`.
- **No extractor implementation.** The `query_history_promoter` generator, its thresholds, and the pattern-type extension package are the consuming plan's work, not this ADR's.
- **No EGP wire format.** How accepted facts serialize onto the bridge is owned by [`adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md) (#220).
- **No profile mechanism.** Which pattern/fact types a deployment admits is owned by [`adr-ontology-profiles.md`](./adr-ontology-profiles.md) (#219).
- **No retention/redaction engine.** This ADR sets the *rules*; the redaction implementation and short-retention enforcement for state-1 evidence are deployment-infrastructure concerns.
- **No automatic semantic acceptance.** State 6 is never reached without the 5 → 6 review gate. There is deliberately no "high enough confidence auto-accepts" path.

## 9. References

- [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) — the `Observation` / `Measurement` home this ADR builds on.
- [`adr-graph-ontology.md`](./adr-graph-ontology.md) — schema.org / PROV-O alignment + open-string contract.
- [`adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) (#217), [`adr-ontology-profiles.md`](./adr-ontology-profiles.md) (#219), [`adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md) (#220) — sibling enterprise-ontology ADRs.
- PROV-O `wasGeneratedBy` / `wasAssociatedWith` / `wasAttributedTo` — https://www.w3.org/TR/prov-o/
