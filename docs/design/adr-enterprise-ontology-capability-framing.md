# ADR: Enterprise ontology capability framing

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Trellis core
**Resolves:** [#217](https://github.com/ronsse/trellis-ai/issues/217)
**Related:**
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — schema.org/PROV-O alignment and the open-string contract this ADR sits on top of
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — the `Observation` / `Measurement` vocabulary that gives behavioral evidence a first-class home
- [`./adr-terminology.md`](./adr-terminology.md) — canonical term map (Knowledge Plane / Operational Plane / Substrate / Backend)
- [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md) — node vs property vs document decisions (the four-question test); this ADR frames *which layer owns the fact* before that guide decides *which store holds it*
- [`../agent-guide/source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) — per-source recipes the decision tree here routes into
- **Sibling ADRs (written in parallel, this is their umbrella):**
  - [`./adr-ontology-profiles.md`](./adr-ontology-profiles.md) ([#219](https://github.com/ronsse/trellis-ai/issues/219)) — optional governed ontology profiles for enterprise builders
  - [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md) ([#218](https://github.com/ronsse/trellis-ai/issues/218)) — promoting query-history-derived evidence to accepted graph facts through review
  - [`./adr-enterprise-graph-interop-bridge.md`](./adr-enterprise-graph-interop-bridge.md) ([#220](https://github.com/ronsse/trellis-ai/issues/220)) — the enterprise graph (EG) as one governed projection/integration layer that interoperates with Trellis
  - [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) ([#221](https://github.com/ronsse/trellis-ai/issues/221)) — guardrails against modeling table columns / hierarchy leaves as graph nodes

---

## 1. Context

Recent enterprise graph/ontology work surfaced a framing problem. When several graph-shaped proposals land in front of a builder at once — an enterprise graph platform (EG), a canonical entity store, a curated domain ontology, a PROV-style practical-lineage spine, and Trellis / an agent context graph (the consumer knowledge graph) — the instinct is to ask "which one wins?" That question is wrong. None of them is a strict superset of the others. They differ on **capability fit, source authority, and operating layer**, and a healthy enterprise stack runs several of them at once, each owning the facts it is authoritative for.

Trellis already has strong, scattered pieces of guidance:

- [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md) decides node vs property vs document (the four-question test, the four-store split).
- [`../agent-guide/source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) gives per-source recipes.
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) sets the schema.org-for-entities / PROV-O-for-provenance naming policy and the open-string extensibility contract.
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) gives `Observation` / `Measurement` a first-class home for derived empirical claims.

What is missing is **one enterprise-facing framing doc** that says, before any of those guides apply: which ontology layer *owns* which facts, which layer is *system of record* vs *serving index*, when something is *behavioral evidence* rather than an *accepted fact*, and how an EG-style governed taxonomy fits **without closing Trellis core**.

### The two failure modes this prevents

Without that framing, builders overgeneralize from whichever graph proposal they saw last:

1. **EG-as-the-one-ontology.** A builder copies the EG's broad Node/Edge/Event shape into Trellis and treats it as *the* canonical model — closing the open-string type system, duplicating source-system payloads into the graph, and overwriting authoritative domain systems.
2. **Open strings with no contract.** A builder ignores the EG's governance strengths entirely and emits open strings with no profile, no projection, and no source-authority contract — so cross-domain questions have nothing governed to traverse.

This ADR is the umbrella. It establishes the capability map, the layer-ownership model, the comparison matrix, and the builder decision tree. The sibling ADRs go deep on individual pieces: optional governed **profiles** ([`./adr-ontology-profiles.md`](./adr-ontology-profiles.md)), the **promotion** path for query-history evidence ([`./adr-query-history-promotion.md`](./adr-query-history-promotion.md)), the **EG interop bridge** ([`./adr-enterprise-graph-interop-bridge.md`](./adr-enterprise-graph-interop-bridge.md)), and **column/leaf guardrails** ([`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md)).

### What this ADR is *not*

- It does not make Trellis a full RDF/OWL runtime. [`./adr-graph-ontology.md`](./adr-graph-ontology.md) §2.3 already rejected that, and this ADR inherits the rejection.
- It does not declare a winner among the layers. The comparison is capability-fit, not a ranking.
- It does not close Trellis core's entity/edge types. The enums in [`../../src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py) are **well-known defaults, not a closed set** — the storage and API layers accept any string. This is preserved emphatically.
- It does not specify the EG's internals, the profile schema, or the promotion-review workflow — those are the sibling ADRs.

## 2. Decision

Adopt a **capability-fit framing** for enterprise ontology work, expressed as four artifacts that builders consult in order:

1. A **capability map** (§3) — which kind of fact each layer is best at.
2. A **layer-ownership model** (§4) — five ownership stances (system of record, governed assertion, serving index, curated/inferred candidate, behavioral evidence) and which layer holds which.
3. A **comparison matrix** (§5) — the EG / the canonical entity store / domain ontology / practical lineage / Trellis (the consumer knowledge graph) across eight dimensions.
4. A **builder decision tree** (§6) — given a fact in hand, where does it go: graph node, property, document, blob pointer, observation/measurement, curated node, or external source pointer.

The governing principles (§7) are: **Trellis core stays open-string and domain-neutral**; **the EG is one governed projection/integration layer among several**, not the canonical model everything else defers to; and **inferred / query-history facts are promoted through review before they become accepted graph facts** (the mechanism is [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md)).

This ADR commits only to the framing and to linking it from the existing guides. It does not commit Trellis to building an EG, the profiles, or the promotion workflow — those are separately decided in the sibling ADRs.

## 3. Capability map

Each layer is *best at* a different kind of fact. The map below is the "use the right tool" table — read it as "if your fact is shaped like X, layer Y is its natural owner," not "layer Y is better."

| Capability | Best-fit layer | Why |
|---|---|---|
| **Canonical identity & provider crosswalks** | Canonical entity store | Owns the blessed identifier for a real-world thing and the crosswalk between provider IDs. The authority that says "these three provider rows are the same customer." |
| **Curated business semantics & journeys** | Domain ontology | Human-curated conceptual relationships, business definitions, journey models. Slow-moving, reviewed, meaning-bearing. |
| **Technical lineage & organizational accountability** | Practical lineage (PROV-shaped) | The data-movement and responsibility spine: what produced what, who owns it, what it derived from. Maps onto PROV-O's `Activity` / `Entity` / `Agent` triad (see [`./adr-graph-ontology.md`](./adr-graph-ontology.md) §2.2). |
| **Agent memory, retrieval feedback, observations** | Trellis / the consumer knowledge graph | Traces, context packs, behavioral evidence (`Observation` / `Measurement`), and the feedback loop. The graph of agent experience. |
| **Cross-domain enterprise graph projections** | The EG | Governed traversal *across* the domains the other layers own. A projection/integration layer, not a system of record. |
| **Documents, vectors, blobs** | Trellis Knowledge Plane stores | Free-form prose (document store), similarity (vector store), large/binary content (blob store) — the non-node homes from [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md). |
| **Derived empirical claims (stats, rates, patterns)** | Trellis `Observation` / `Measurement` | Time-windowed, provenance-bearing, evidence-shaped claims about an entity. See [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md). |

The map respects the Knowledge Plane / Operational Plane split from [`./adr-terminology.md`](./adr-terminology.md): the agent-facing stores (graph, vector, document, blob) hold the modeled facts; the Operational Plane (trace store, EventLog) records *how Trellis got there* and is not a domain-modeling target.

## 4. Layer ownership

The single most important question before modeling anything is **"who is authoritative for this fact?"** A fact has exactly one *system of record*; everything else is a projection, an index, a candidate, or evidence. Conflating these is the root of the two failure modes in §1.

| Ownership stance | What it means | Authoritative? | Typical layer | Mutation discipline |
|---|---|---|---|---|
| **System of record** | The blessed source of truth for this fact. Other layers copy or reference it; none overwrite it. | Yes — sole authority | Canonical entity store (identity); the source platform (a dbt project, Unity Catalog, git) for structural facts | Changes originate here; downstream layers re-extract, never edit. |
| **Governed assertion** | A reviewed, cross-domain claim asserted into a governed graph for traversal. Derived from systems of record under a governance contract. | No — a governed *projection* | The EG | Mutations gated by the EG's review/governance model. |
| **Serving index** | A retrieval-optimized copy/projection of facts owned elsewhere. Exists to make reads fast and context-shaped, not to be authoritative. | No — a copy | Trellis Knowledge Plane (graph/vector/document) when it mirrors an upstream source | Refreshed from the source; staleness is signalled, not edited away. See [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md) "Freshness signals." |
| **Curated / inferred candidate** | Synthesized from other facts (clustering, rollups, LLM synthesis). Regeneratable, human-refinable, explicitly *not* ground truth. | No — derived | Trellis `node_role="curated"` nodes; domain-ontology curated concepts | Regenerated from a `generation_spec`; human edits tracked separately. |
| **Behavioral evidence** | Empirical claims derived from observed agent/query behavior. Carries a time window and provenance; describes what *was observed*, not what is *canonically true*. | No — evidence, pending promotion | Trellis `Observation` / `Measurement` nodes; query-history-derived patterns | Append-only evidence; promotion to an accepted fact goes through review per [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md). |

**The ownership rule:** a downstream layer may *reference*, *index*, or *project* a fact it does not own, but it must never silently overwrite the system of record. Treat domain-specific canonical systems as **authorities**, not as data sources to clobber. Where Trellis mirrors an upstream source, it is a *serving index* — and it signals staleness (via `valid_from`/`valid_to`, `importance_scored_at`, and `TAGS_REFRESHED`; see [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md)) rather than pretending its snapshot is canonical.

## 5. Comparison matrix

The five layers across eight dimensions. **This is deliberately not "the EG vs everything else."** The EG appears as one column — a governed projection/integration layer — alongside the others. Each layer is the natural owner of the rows where it scores "authoritative."

| Dimension | The EG | Canonical entity store | Domain ontology | Practical lineage | Trellis / consumer KG |
|---|---|---|---|---|---|
| **Node universe** | Cross-domain: whatever the governed taxonomy projects | Real-world canonical entities + provider crosswalks | Curated business concepts & journeys | `Activity` / `Entity` / `Agent` (data assets, jobs, runs, owners) | Open: traces, entities, documents, observations, curated rollups |
| **Formalism** | Governed property-graph taxonomy | Closed canonical schema | Curated, often OWL/SKOS-leaning concept model | PROV-O / OpenLineage-shaped | **Open-string, well-known defaults** (schema.org + PROV-O), no closed set ([`./adr-graph-ontology.md`](./adr-graph-ontology.md)) |
| **System-of-record stance** | Governed *assertion* / projection — not SoR | **SoR for identity** | **SoR for business semantics** | **SoR for lineage & accountability** | **Serving index** for mirrored facts; **SoR for agent experience** (traces, observations) |
| **Storage / serving** | Enterprise graph backend | Canonical store + crosswalk tables | Ontology store / triplestore | Lineage store (PROV/OpenLineage) | Knowledge Plane (graph + vector + document + blob); Substrate = blessed default backend per store (see [`./adr-terminology.md`](./adr-terminology.md); ArcadeDB is the blessed graph+vector substrate) |
| **Temporal / provenance** | Governed assertion history | Crosswalk validity periods | Concept version history | **Provenance is the point** (`wasDerivedFrom`, runs) | SCD Type 2 on every node; EventLog as the audit journal; `Observation` windows |
| **Identity** | Projected IDs mapped from sources | **Canonical blessed IDs + aliases** | Concept URIs | Dataset/job/run URIs (OpenLineage namespaces) | Deterministic `(source_system, raw_id) → entity_id` aliasing; references canonical IDs, does not mint competing ones |
| **Mutation / review model** | Governed review gate | Stewarded canonical change process | Curator review | Emitted by pipelines, append-mostly | Governed `MutationExecutor` pipeline (validate → policy → idempotency → execute → emit); promotion-through-review for behavioral evidence |
| **Retrieval impact** | Cross-domain traversal answers | Resolves "which entity is this, really?" | Answers "what does this business concept mean?" | Answers "what feeds/owns this?" | **Assembles context packs** for agents; boosts curated, filters structural, surfaces observations alongside structural facts |

Reading the matrix: no column dominates. The EG's strength is the **cross-domain traversal** row and the **governed assertion** stance; its weakness is that it is *not* a system of record for anything — it projects from the layers that are. Trellis's strength is the **agent-experience / retrieval** rows and the open-string formalism; its weakness is that it is a *serving index* for facts other layers own, so it must defer to them on authority.

## 6. Builder decision tree

Given a fact in hand, route it. This sits *above* the four-question test in [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md): first decide **which layer owns it and which Trellis home it gets**; then, if it's a Trellis graph node, the four-question test and the column/leaf guardrails ([`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md)) decide node vs property.

```
Is this fact CANONICAL IDENTITY (which real-world thing is this, and what are its provider IDs)?
   └─ yes → Source it from the canonical entity store. In Trellis: reference the canonical ID via
            the alias system; do NOT mint a competing identity. (SoR = canonical store.)

Is this a CURATED BUSINESS CONCEPT or journey (a reviewed, meaning-bearing definition)?
   └─ yes → Domain ontology owns it. In Trellis: a curated semantic node (node_role="curated")
            that references the concept, OR a Concept node — never an ingested ground-truth node.

Is this DATA MOVEMENT or ORG ACCOUNTABILITY (what produced/owns/derives this)?
   └─ yes → PROV / OpenLineage-shaped lineage. In Trellis: graph edges using PROV-O verbs
            (wasDerivedFrom, wasGeneratedBy, wasAttributedTo) per adr-graph-ontology.md.

Is this AGENT BEHAVIOR or QUERY-HISTORY EVIDENCE (a stat, rate, join pattern, co-access pattern)?
   └─ yes → It is BEHAVIORAL EVIDENCE, not a canonical fact.
            • Scalar/numeric/boolean, time-series-shaped → Measurement node.
            • Narrative/compound claim → Observation node (adr-observation-entity-type.md).
            • Recurring derived pattern (JoinPattern, AccessPattern) → curated node.
            Promote to an accepted graph fact only through review (adr-query-history-promotion.md).

Is this FREE-FORM EXPLANATION (prose: README, runbook, description)?
   └─ yes → Document store, linked to the owning node via a described_by / mentions edge.

Is this LARGE or VOLATILE source content (a 50MB PDF, a mutating wiki page, raw payloads)?
   └─ yes → Pointer + summary. Blob URL or source URL as a property on the owning node;
            a short digest in a document if it's worth retrieving. Do NOT copy the body into the graph.

Otherwise — it is a STRUCTURAL FACT about a thing Trellis indexes:
   └─ Apply the four-question test (modeling-guide.md). If it earns node status, it's a graph NODE
      (default node_role="semantic"; structural if plumbing). If not, it's a PROPERTY on the parent.
      Hierarchy leaves (columns, parameters) almost always fail the test → property, not node
      (adr-column-leaf-modeling-guardrails.md).
```

The seven destinations, named: **graph node**, **graph property**, **document**, **blob pointer**, **observation/measurement node**, **curated node**, **external source pointer**. Every fact lands in exactly one primary home (a single real-world artifact often has secondary homes too — a dbt table is a node *plus* a description document *plus* an embedding, anchored on the node).

## 7. Worked examples

### 7.1 "Who do we call when this metric breaks?"

A metric — say a revenue figure on a dashboard — is failing, and an agent needs the on-call owner.

- **Identity** of the metric and its underlying datasets is owned by the **canonical store** (and the source platform). Trellis references those IDs, it does not redefine the metric.
- **Accountability** — "who owns this dataset / job?" — is **practical lineage**. In Trellis this is a `wasAttributedTo` edge from the `Dataset` / `Activity` to an `Agent` (the responsible owner), per [`./adr-graph-ontology.md`](./adr-graph-ontology.md) §3.2, plus an `owned_by`-style edge to a `Team`. The lineage spine answers "what feeds this metric and who owns each hop."
- **The answer** assembles by traversing lineage edges from the metric's dataset up to the owning `Agent`/`Team`, then reading the contact/escalation properties on that owner node (or a runbook `Document` linked via `described_by`).
- **Behavioral evidence** sharpens it: an `Observation` that "this dataset's freshness SLA was missed 3× this month" (a `Measurement` with a window) tells the agent *which* upstream hop is the likely culprit — but that observation is evidence, not the canonical owner. Trellis returns both; the agent reconciles (per [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) §2.4).

**Where it does *not* go:** the owner is not a property invented on the metric node ("envy" of the lineage layer). It is a lineage edge to an `Agent`/`Team` node, because ownership traverses and has its own lifecycle.

### 7.2 "Should query-history join patterns become graph edges?"

An analyzer notices `fct_orders` and `dim_customers` are joined on `customer_id` in 1,847 queries last month. Should that become a `joins_with` edge between the two `Dataset` nodes?

- **No — not as a raw canonical edge.** A join *pattern* observed in query history is **behavioral evidence**, not an accepted structural fact. Minting a canonical edge from it asserts "these tables are related" with the same authority as a declared foreign key, which it is not.
- **Yes — as a curated node.** Model it as a `JoinPattern` curated node (`node_role="curated"`) with a `generation_spec` recording the analyzer, window, and `source_node_ids`, plus `involves_dataset` edges to each `Dataset` — exactly the shape in [`../agent-guide/source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) Recipe 4. It carries `occurrence_count`, `join_keys`, and a curator-editable `description`. It is regeneratable and clearly *derived*, so no one mistakes it for a declared relationship.
- **Promotion to a real edge** — if the pattern is so stable and high-value that it *should* become an accepted `relatedTo` / `dependsOn` edge in the governed graph (or projected into the EG for cross-domain traversal) — goes **through review**, per [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md). Evidence earns its way to fact; it is not auto-promoted.

**The principle:** observed ≠ canonical. Query history is one of Trellis's highest-value signals precisely *because* it is kept distinct from declared structure until reviewed.

### 7.3 "Should table columns become graph nodes?"

A Unity Catalog ingestion has 10K tables and 500K columns. Should each column be a node?

- **Almost always no.** Columns fail all four questions in [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md): a column traverses only to its parent table, is rarely queried cross-parent (a JSON-indexed search covers the occasional case), accumulates no independent evidence, and has no lifecycle separate from its table. They are **properties** — a `columns` JSON array on the `Dataset` node. The table's SCD Type 2 history captures a schema migration as one property-diff event, which is the granularity you actually query. This is the *Schema explosion* / *Cardinality explosion* anti-pattern; the guardrails are spelled out in [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md).
- **The narrow exception:** columns that participate in a *real* cross-parent relationship with their *own* lifecycle — e.g., regulated PII columns with per-column masking rules, consent basis, and a governance audit trail. Model *only those* columns as `node_role="structural"` nodes (keeping them out of default retrieval), with `belongs_to` to the table and `governed_by` to a policy node. That is ~100 nodes, not 500K — each one earns it via the four-question test (independent evidence, independent lifecycle, the cross-parent query "find all columns governed by GDPR Article 6").
- **Layer note:** the *authoritative* column schema is owned by the source platform (Unity Catalog). Trellis is a **serving index** for it — so it mirrors and signals staleness rather than treating its snapshot as the system of record. If a governed cross-domain column taxonomy is needed, that is an EG **projection**, not a reason to explode the Trellis graph.

## 8. Recommendations

1. **Keep Trellis core open-string and domain-neutral.** The enums in [`../../src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py) are well-known defaults; storage and API accept any string. This ADR does not close them, and [`./adr-graph-ontology.md`](./adr-graph-ontology.md) §5 (and CLAUDE.md's extension-point policy) hold: domain-specific types live in their own packages, not in core.
2. **Add optional governed ontology profiles for enterprise builders** — an *opt-in* layer for those who want a contract, leaving core open. Specified in [`./adr-ontology-profiles.md`](./adr-ontology-profiles.md).
3. **Use EG-style projections for cross-domain questions** — described as one governed projection/integration layer that *interoperates with* the others, never as the canonical model. The bridge is [`./adr-enterprise-graph-interop-bridge.md`](./adr-enterprise-graph-interop-bridge.md).
4. **Treat domain-specific canonical systems as authorities, not data sources to overwrite.** Reference their IDs through the alias system; never mint competing identities or clobber their records.
5. **Promote inferred / query-history facts through review** before they become accepted graph facts. The mechanism is [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md); the evidence homes are `Observation` / `Measurement` / curated nodes per [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md).

## 9. Acceptance criteria

- This ADR exists at `docs/design/adr-enterprise-ontology-capability-framing.md` and is cross-linked from [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md), [`../agent-guide/source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md), and [`./adr-graph-ontology.md`](./adr-graph-ontology.md). *(Adding the inbound links to those three docs is a follow-up edit, tracked under [#217](https://github.com/ronsse/trellis-ai/issues/217); this ADR provides the outbound links now.)*
- It includes a capability comparison table across the five layers (§5).
- It includes a builder decision tree whose destinations are: graph node, graph property, document, blob pointer, observation/measurement, curated node, and external source pointer (§6).
- It states explicitly that the EG is **not** "better than everything else" — it is one governed projection/integration layer that interoperates with the other layers (§2, §4, §5, §8.3).
- It contains at least three worked examples, including the three named in [#217](https://github.com/ronsse/trellis-ai/issues/217): "who do we call when this metric breaks?", "should query-history join patterns become graph edges?", and "should table columns become graph nodes?" (§7).

## 10. Non-goals

- **Not an RDF/OWL runtime.** Inherited from [`./adr-graph-ontology.md`](./adr-graph-ontology.md) §2.3 — schema.org/PROV-O *names*, no IRIs, no triplestore, no reasoning engine.
- **Not making the EG the only ontology.** The EG is one layer; the framing deliberately refuses to crown it.
- **Not closing Trellis entity/edge types globally.** The open-string contract is preserved.
- **Not duplicating full source-system payloads in the graph.** Large/volatile content is pointer + summary, never an inlined copy (§6, §7.3).
- **Not specifying the profile schema, the promotion workflow, the EG bridge protocol, or the column-guardrail thresholds.** Those are the four sibling ADRs (§1).
- **Nothing in this ADR is implemented.** It is a framing/design document; the artifacts it points to (profiles, promotion, EG bridge) are separately proposed and not built.
