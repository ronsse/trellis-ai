# ADR: EGP interop bridge

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Trellis core
**Resolves:** [#220](https://github.com/ronsse/trellis-ai/issues/220)
**Related:**
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — [#217](https://github.com/ronsse/trellis-ai/issues/217), the umbrella ADR that frames EGP/Trellis as adjacent capabilities; this bridge is one piece under it.
- [`./adr-ontology-profiles.md`](./adr-ontology-profiles.md) — [#219](https://github.com/ronsse/trellis-ai/issues/219). The fact-state vocabulary (§3) and the projection contract (§4) are delivered **as ontology-profile metadata**, not as core schema changes. This ADR defines *what* they mean; the profiles ADR defines *where they live*.
- [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md) — [#218](https://github.com/ronsse/trellis-ai/issues/218). The Trellis→EGP candidate handoff (§2.2) is the consumer of the promotion path; this ADR defines the wire shape of a candidate, that ADR defines the review gate that produces it.
- [`./adr-alias-resolution.md`](./adr-alias-resolution.md) — existing. The `(source_system, raw_id)` alias model is how golden records map into Trellis (§2, Golden record row).
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — existing. schema.org/PROV-O well-known defaults; the mapping table reuses these names.
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — existing. `Observation` / `Measurement` entity types and `hasObservation` edge; candidate inferred facts land here.

---

## 1. Context

EGP (the governed enterprise graph) and Trellis solve adjacent problems. EGP is the system of record for **cross-domain traversal, accountability, provenance, and projections** over accepted enterprise facts. Trellis is the **agent-facing memory and retrieval layer** — traces, context packs, observations, measurements, feedback attribution, all behind a governed mutation pipeline.

The two systems share a graph shape (Node/Edge, provenance, time-travel) but differ on *authority*. EGP holds curated, reviewed, organizationally-blessed facts. Trellis holds a wider, noisier corpus: agent experience, empirical observations, query-history patterns, and machine-inferred candidates that have **not** been blessed. The whole value of keeping them separate is that Trellis can record a candidate without that candidate becoming an enterprise truth.

Today there is **no explicit mapping or flow** between an EGP-style Node/Edge/Event model and Trellis's graph/trace/document/observation model. Without a defined bridge:

- EGP risks duplicating Trellis observations as accepted facts too early.
- Trellis risks ingesting EGP facts without preserving *which system asserted them*.
- Query-history-derived facts risk becoming canonical edges with no review.
- Builders won't know whether a given event is a Trellis trace, an EGP event, an `Observation`, or an EventLog audit row.
- Provenance fields drift between systems.

This ADR defines the bridge: a mapping table, a directionality contract, a fact-state vocabulary expressed as metadata conventions, and a projection contract. It is a **design document** — it commits to no implementation. Everything here is realizable additively on top of the existing schemas (`src/trellis/schemas/entity.py`, `src/trellis/schemas/graph.py`, `src/trellis/schemas/observation.py`) and stores (`src/trellis/stores/base/graph.py`, `src/trellis/stores/base/event_log.py`) without a core schema change; where a core change would eventually help, it is called out and deferred.

### 1.1 What this ADR is *not*

The non-goals from [#220](https://github.com/ronsse/trellis-ai/issues/220) are load-bearing and restated as hard constraints in §6:

- EGP is **not** the Trellis storage backend.
- Trellis is **not** responsible for EGP's production RBAC or graph serving.
- Trellis observations are **not** EGP accepted facts by default.
- Not every Trellis deployment must know about EGP. The bridge is opt-in; a deployment with no EGP is unaffected.

---

## 2. Mapping table

Each EGP concept maps to one Trellis concept, with the **source-authority** rule that governs the crossing. Trellis already accepts any entity type / edge kind as an open string (see [`adr-graph-ontology.md`](./adr-graph-ontology.md) §5.1–5.2), so the mapping needs no new type registrations — it needs *conventions* on `properties` / `metadata`.

| EGP concept | Trellis concept | Crossing rule |
|---|---|---|
| **Node** | `Entity` (`src/trellis/schemas/entity.py`) | Preserve EGP `entity_type`, `domain`, and source refs in `properties`; preserve **source authority** in `metadata` (`source_system`, `fact_state="source_asserted"` or `"accepted"`). `node_role` defaults to `SEMANTIC` — an imported EGP node is a real thing in the world, not Trellis-curated plumbing. |
| **Edge** | `Edge` (`src/trellis/schemas/graph.py`) | Preserve relationship type as `edge_kind` (canonicalized via `well_known.canonicalize_edge_kind` where a PROV-O/schema.org verb fits), and `confidence`, `declared_by`, `source_system`, `valid_from` / `valid_to` in `properties` — the additive provenance keys already sanctioned in [`adr-graph-ontology.md`](./adr-graph-ontology.md) §4.1. |
| **Event** | `Trace` / `Activity` / `Observation` / EventLog entry | Routed by *what kind of event it is* — see §2.3. There is no single target; choosing wrong is the most common builder error this ADR exists to prevent. |
| **Cross-domain edge** | `Edge` + projection metadata | Same as Edge, but **must** carry attribution (`declared_by`, `source_system`) and a `fact_state`; cross-domain edges are exactly the ones a projection (§4) gates on for source authority. |
| **Golden record** | `Entity` + aliases | The canonical entity is one `Entity`; each source identifier (UC, dbt, Workday, SBK, …) is an `EntityAlias` keyed by `(source_system, raw_id)` via `GraphStore.upsert_alias` (`src/trellis/stores/base/graph.py` line 381). The alias model is the golden-record join — see [`adr-alias-resolution.md`](./adr-alias-resolution.md). |
| **Candidate inferred fact** | `Observation` **or** curated node | Lands as an `Observation`/`Measurement` (`src/trellis/schemas/observation.py`) or a `node_role=CURATED` entity with `fact_state="candidate"` / `"inferred"`. It is **not** an accepted semantic edge until reviewed through the [#218](https://github.com/ronsse/trellis-ai/issues/218) promotion gate. |

### 2.1 Why Node→Entity and not Node→a new type

Trellis's `Entity` already carries `properties`, `metadata`, and `node_role`, and its graph nodes are SCD-2 versioned at the `GraphStore` layer (`valid_from`/`valid_to` are storage-layer columns in [`stores/base/graph.py`](../../src/trellis/stores/base/graph.py), not `Entity` model fields). An EGP node is structurally an `Entity` with extra provenance. Inventing an `EGPNode` type would fork the retrieval surface (PackBuilder strategies, classification, alias resolution all key off `Entity`) for no semantic gain. The EGP-ness lives in `metadata`, not in the type.

### 2.2 Why Candidate→Observation and not Candidate→Edge

A candidate inferred fact is, by definition, an empirical or machine-derived claim that has not been blessed. [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §1 names the exact failure of stuffing such a claim onto an entity property or a bare edge: lost provenance, no window, no confidence, SCD-2 churn on the subject. An `Observation` node with `wasDerivedFrom` back to its source trace is the right home. Promotion to an accepted EGP **edge** happens later, through review — never as a side effect of recording the candidate.

### 2.3 Event routing (the four-way fork)

An EGP "Event" is overloaded. Trellis splits it by intent:

| If the event is… | It becomes a… | Rationale |
|---|---|---|
| Agent work (a unit of reasoning/tool use the agent performed) | `Trace` / `Activity` node (`src/trellis/schemas/trace.py`; `Activity` is the PROV-O graph projection of a trace, see [`adr-graph-ontology.md`](./adr-graph-ontology.md) §3.1) | Traces are immutable agent experience. They are retrievable but are **not** accepted facts. |
| A source-system event (a deployment, a schema change, an incident) | `Event` entity (schema.org `Event`) | A real-world business event is a first-class entity, not a Trellis mutation. |
| An empirical observation ("this column is filtered 95% of the time") | `Observation` / `Measurement` | First-class home per [`adr-observation-entity-type.md`](./adr-observation-entity-type.md). |
| A Trellis mutation that happened (audit) | EventLog entry (`EventType`, `src/trellis/stores/base/event_log.py` line 25) | The EventLog is the authoritative audit journal — `mutation.executed`, `entity.created`, `feedback.recorded`, etc. It records *how we got here*, not *the shape of the world*. |

The graph holds the current shape of the world (with SCD-2 history); the EventLog holds the ordered audit of mutations. This is the existing Trellis split ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §4.3) and the bridge does not change it.

---

## 3. Directionality

The bridge is **asymmetric on purpose.** Authority flows one way (EGP→Trellis preserves it); candidacy flows the other way (Trellis→EGP earns it).

### 3.1 EGP → Trellis (seed retrieval, preserve authority)

- **Accepted enterprise facts seed retrieval.** EGP nodes/edges import as `Entity`/`Edge` with `fact_state` ∈ {`source_asserted`, `accepted`} so PackBuilder can surface them to agents.
- **EGP projections can become Trellis context-pack sections.** A projection (§4) is a named retrieval shape; an EGP-side projection result maps onto a Trellis pack section.
- **Source authority is preserved as metadata**, never flattened. An imported node keeps `source_system` and `declared_by`; Trellis does not relabel an EGP-accepted fact as Trellis-authored.

EGP→Trellis is a **read-seeding** crossing. Trellis never asserts authority over what it imported; it carries EGP's authority forward as metadata.

### 3.2 Trellis → EGP (publish only reviewed candidates)

- Agent traces and query-history analysis produce **observations, measurements, and curated candidates** inside Trellis.
- **Only reviewed candidates** cross to EGP as accepted edges/nodes. The review gate is [#218](https://github.com/ronsse/trellis-ai/issues/218)'s promotion path.
- **Raw traces and raw query history are never bulk-copied into EGP** as canonical facts. Trellis's trace corpus and query logs stay in Trellis; only the distilled, reviewed candidate crosses.

Trellis→EGP is a **review-gated publish** crossing. The default state of anything Trellis produces is `candidate` / `inferred`, and crossing to `accepted` requires an explicit human-reviewed promotion — consistent with the non-goal that Trellis observations are not EGP accepted facts by default.

```
EGP  ──accepted facts──▶  Trellis retrieval        (authority preserved as metadata)
EGP  ──projections────▶  Trellis pack sections

Trellis traces/obs/query-history ──distill──▶ candidate
candidate ──[#218 review gate]──▶ accepted ──publish──▶ EGP
            (raw traces & query history never cross)
```

---

## 4. Fact states (metadata conventions, not schema changes)

Trellis distinguishes seven fact states. **These start as metadata conventions carried in an ontology profile** ([#219](https://github.com/ronsse/trellis-ai/issues/219)) — a `fact_state` key on `Entity.metadata` / `Edge.properties` — **before any core schema change.** Nothing in this ADR adds a column, an enum, or a validator. The open-string contract ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §5.1) means a profile can introduce `fact_state` today with zero migration.

| `fact_state` | Meaning | Typical origin | Crosses to EGP as accepted? |
|---|---|---|---|
| `source_asserted` | A source system asserted it; not yet enterprise-blessed | UC/dbt/Workday/SBK ingest | Only after EGP acceptance |
| `accepted` | Enterprise-blessed fact | EGP, or post-review in Trellis | Yes — it already is |
| `candidate` | A proposed fact awaiting review | Trellis curation, query-history promotion | No — pending review |
| `inferred` | Machine-derived, not yet evidenced enough to be a candidate | Extractors, LLM analysis | No |
| `behavioral_evidence` | Observed behavior (query patterns, access patterns) backing a claim | Query-history analysis, `Observation` nodes | No — evidence, not the claim |
| `curated` | Synthesized inside Trellis from the graph itself | `node_role=CURATED` entities, precedents | No — internal synthesis |
| `deprecated` | Superseded or retired; retained for history | Any, after supersession | No — excluded from acceptance |

Notes:

- `behavioral_evidence` aligns with the `Observation` / `Measurement` distinction in [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §2.1 — empirical, windowed, evidence-bearing — and with `NodeRole` (`src/trellis/schemas/enums.py`): `curated` fact-state entities are `node_role=CURATED`.
- Promotion `candidate → accepted` is **always** review-gated (§3.2). No fact-state transition happens implicitly inside core code.
- The profile is the only place these strings are *defined*; core treats them as opaque metadata. If a future policy consumer needs to gate on `fact_state`, that is the promotion-to-column path described in [`adr-graph-ontology.md`](./adr-graph-ontology.md) §4.1 — a later ADR, not this one.

---

## 5. Projection contract

EGP-style projections become **first-class named retrieval shapes**. A projection is a declarative contract — it does not execute traversals here; it constrains what a traversal *may* do. Like fact states, projections live in the ontology profile ([#219](https://github.com/ronsse/trellis-ai/issues/219)), not in core.

Each projection declares:

- **allowed node roles** — `structural` / `semantic` / `curated` (`src/trellis/schemas/enums.py`)
- **allowed node types** — open strings; well-known defaults from [`adr-graph-ontology.md`](./adr-graph-ontology.md)
- **allowed edge kinds** — open strings; PROV-O / schema.org verbs where they fit
- **max traversal depth** — bound on hops, so a projection can't degrade into a full-graph scan
- **source-authority rules** — which `fact_state` / `source_system` values are admissible in the result

| Projection | Allowed node roles | Allowed node types (illustrative) | Allowed edge kinds | Max depth | Source-authority rule |
|---|---|---|---|---|---|
| `metric_accountability` | semantic, curated | `Dataset`, `Person`, `Organization`, `Team` | `wasAttributedTo`, `dependsOn` | 2 | `fact_state` ∈ {`accepted`, `source_asserted`}; declared_by required |
| `lineage_impact` | structural, semantic | `Dataset`, `SoftwareApplication`, `File` | `wasDerivedFrom`, `dependsOn`, `used` | 4 | `fact_state` ∈ {`accepted`, `source_asserted`} from lineage source systems (dbt, OpenLineage) |
| `ownership` | semantic | `Person`, `Team`, `Organization`, `Dataset` | `wasAttributedTo`, `partOf` | 2 | `fact_state=accepted`; HR/IdP source authority (Workday) preferred |
| `business_domain` | semantic, curated | `Organization`, `Concept`, `Dataset` | `partOf`, `relatedTo` | 3 | `fact_state` ∈ {`accepted`, `curated`} |
| `agent_memory` | semantic, curated | `Activity`, `Observation`, `CreativeWork`, `Concept` | `wasDerivedFrom`, `hasObservation`, `wasInformedBy` | 3 | Trellis-internal; `fact_state` ∈ {`curated`, `behavioral_evidence`, `inferred`} — explicitly **not** `accepted`-only |
| `query_history_patterns` | semantic, curated | `Observation`, `Measurement`, `Dataset` | `hasObservation`, `wasDerivedFrom` | 2 | `fact_state=behavioral_evidence`; never crosses to EGP without review |

The two Trellis-native projections (`agent_memory`, `query_history_patterns`) deliberately admit non-accepted fact states — that is the point of an agent memory layer. The four enterprise projections admit only authoritative states. A projection's source-authority rule is the enforcement point for the directionality contract in §3.

---

## 6. Non-goals

These are constraints, not aspirations. A change that violates one is out of scope for this ADR and the bridge it defines.

1. **EGP is not the Trellis storage backend.** Trellis keeps its own stores (`StoreRegistry`, `src/trellis/stores/`). The bridge is an import/export mapping, not a backend substitution. Trellis does not query EGP at retrieval time as if it were a `GraphStore`.
2. **Trellis does not own EGP RBAC or graph serving.** EGP's production access control and serving stay in EGP. Trellis's policy layer governs Trellis mutations only; it makes no claim over EGP's RBAC.
3. **Trellis observations are not EGP accepted facts by default.** The default `fact_state` for anything Trellis produces is `candidate` / `inferred` / `behavioral_evidence` / `curated` — never `accepted`. Crossing to `accepted` is review-gated ([#218](https://github.com/ronsse/trellis-ai/issues/218)).
4. **Not every deployment must know about EGP.** The bridge, fact states, and projections all live in an opt-in ontology profile ([#219](https://github.com/ronsse/trellis-ai/issues/219)). A Trellis instance with no EGP carries none of this and behaves exactly as it does today.

---

## 7. Worked examples

The examples from [#220](https://github.com/ronsse/trellis-ai/issues/220), each showing the crossing rule in effect.

### 7.1 Accepted EGP ownership edge imported into Trellis retrieval

EGP holds an accepted edge: *Team `data-platform` owns Dataset `fct_orders`.* Imported into Trellis:

```python
# Entity (golden record for the dataset), already aliased to its UC identifier
Entity(node_type="Dataset", node_role=NodeRole.SEMANTIC,
       metadata={"source_system": "egp", "fact_state": "accepted"})

# Edge: Dataset wasAttributedTo Team
Edge(edge_kind="wasAttributedTo",
     source_id=<fct_orders entity id>, target_id=<data-platform team id>,
     properties={"fact_state": "accepted", "source_system": "egp",
                 "declared_by": "egp-curation", "confidence": 1.0})
```

The `ownership` projection (§5) admits this edge: `fact_state=accepted`, edge kind `wasAttributedTo`, depth 1. It surfaces in an agent's pack with EGP authority intact. Trellis never relabels it as Trellis-authored.

### 7.2 Trellis query-history `JoinPattern` exported as an EGP candidate

A Trellis query-history analysis observes that `fct_orders` and `dim_customer` are joined in 95% of queries over a 30-day window. Recorded in Trellis as:

```python
Observation(node_type="Observation", node_role=NodeRole.SEMANTIC,
            properties={"kind": "join_pattern", "value": 0.95, "unit": "per_query",
                        "window_start": ..., "window_end": ..., "sample_size": 1840,
                        "method": "count_distinct_query_hashes"},
            metadata={"fact_state": "behavioral_evidence", "source_system": "trellis"})
# + wasDerivedFrom edge back to the source trace
```

This is `behavioral_evidence`, admitted by `query_history_patterns` (§5) but **not** by any enterprise projection. To become an EGP candidate edge (`fct_orders relatedTo dim_customer`), it goes through the [#218](https://github.com/ronsse/trellis-ai/issues/218) review gate — transitioning `behavioral_evidence → candidate → accepted`. It does **not** become a canonical EGP edge by being observed.

### 7.3 Trellis agent trace retained as trace/Activity, not an accepted EGP edge

An agent reasons through a debugging task touching three datasets. The trace is ingested (immutable) and projects into the graph as an `Activity` node with `used` / `wasInformedBy` edges to the datasets it touched. This is agent experience: retrievable via the `agent_memory` projection, never crossing to EGP as an accepted fact. The `Trace` is *not* an EGP event and *not* an accepted edge — it stays Trellis-side per §2.3 and §3.2.

### 7.4 Source-authority preservation for UC / dbt / Workday / SBK

A single logical dataset is known by four source identifiers. The golden record is one `Entity`; each identifier is an alias keyed by `(source_system, raw_id)` (see [`adr-alias-resolution.md`](./adr-alias-resolution.md)):

```python
graph.upsert_alias(entity_id=E, source_system="unity_catalog", raw_id="main.gold.fct_orders")
graph.upsert_alias(entity_id=E, source_system="dbt",           raw_id="model.analytics.fct_orders")
graph.upsert_alias(entity_id=E, source_system="workday",       raw_id="WD-DS-00412")
graph.upsert_alias(entity_id=E, source_system="sbk",           raw_id="sbk://orders/fct")
```

Each alias preserves *which system asserted the identifier*. Facts imported from each source carry that `source_system` in `metadata`, so a projection's source-authority rule (§5) can prefer, e.g., Workday for `ownership` and dbt/OpenLineage for `lineage_impact`. Authority is never collapsed into a single anonymous "imported" tag.

---

## 8. Acceptance criteria

From [#220](https://github.com/ronsse/trellis-ai/issues/220), with where each is satisfied:

- [x] A design doc defines the EGP ↔ Trellis mapping — §2.
- [x] Examples for: accepted EGP ownership edge imported into retrieval (§7.1); query-history `JoinPattern` exported as a candidate (§7.2); agent trace retained as trace/Activity, not an accepted edge (§7.3); source-authority preservation for UC/dbt/Workday/SBK (§7.4).
- [x] Recommended metadata fields for fact state, source authority, `declared_by`, `confidence`, and review state — §2 (crossing rules), §4 (fact states). Review state is the `candidate`/`accepted` axis of `fact_state` plus the [#218](https://github.com/ronsse/trellis-ai/issues/218) gate.
- [x] How aliases / golden records are represented — §2 (Golden record row), §7.4, via [`adr-alias-resolution.md`](./adr-alias-resolution.md).
- [x] Review/promotion is explicit before Trellis-generated candidates become accepted EGP facts — §3.2, §4 (no implicit transition), deferred to [#218](https://github.com/ronsse/trellis-ai/issues/218).

This ADR is **Proposed**. It commits to no code. Realization is scoped by the sibling ADRs: profiles ([#219](https://github.com/ronsse/trellis-ai/issues/219)) host the fact-state and projection metadata; promotion ([#218](https://github.com/ronsse/trellis-ai/issues/218)) hosts the review gate; the umbrella ([#217](https://github.com/ronsse/trellis-ai/issues/217)) sequences them.

---

## 9. Consequences

### 9.1 What this preserves

- The Knowledge Plane / Operational Plane split and the immutability of traces are untouched.
- Open-string types stay open; the bridge adds conventions, not enums.
- A no-EGP deployment is unaffected (non-goal 4).

### 9.2 What this costs

- A new convention (`fact_state`, `declared_by`, `source_system` on metadata) that profiles and any EGP bridge code must agree on. Documented here; enforced nowhere in core until a policy consumer demands it.
- Two Trellis-native projections (`agent_memory`, `query_history_patterns`) that intentionally diverge from enterprise authority rules — a reviewer must understand *why* they admit non-accepted facts (§5).

### 9.3 What this forecloses

- Treating any Trellis-produced fact as enterprise-accepted without review. The directionality contract (§3) is asymmetric by design and a later ADR would have to explicitly overturn it.
- A `fact_state` core column lands only via the promotion path in [`adr-graph-ontology.md`](./adr-graph-ontology.md) §4.1, gated on a real policy consumer — not as a side effect of this bridge.
