# ADR: Column and leaf metadata modeling guardrails

**Status:** Proposed
**Date:** 2026-06-03 (amended 2026-06-11: domain scope + empirical promotion path)
**Deciders:** Trellis core
**Resolves:** [#221](https://github.com/ronsse/trellis-ai/issues/221)
**Related:**
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — [#217](https://github.com/ronsse/trellis-ai/issues/217), the umbrella framing for the enterprise-ontology issue set this ADR sits inside
- [`./adr-ontology-profiles.md`](./adr-ontology-profiles.md) — [#219](https://github.com/ronsse/trellis-ai/issues/219), ontology profiles; a profile may set `Column.node_role_default=structural` (§6.2)
- [`./adr-graph-shape-constraints.md`](./adr-graph-shape-constraints.md) — the declarative shape layer a future linter (§6.3) can build on
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — open-string types + `NodeRole`; this ADR adds no new vocabulary, only guidance
- [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md) — the guide this ADR strengthens (the "Column and leaf metadata policy" section)

---

## 1. Context

Trellis's storage and API layers accept **any string** as an entity type or edge kind, and the graph store applies SCD Type 2 versioning to every node ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §5). That flexibility is a strength, but it has a predictable failure mode at the leaf level: builders model *every* fine-grained leaf of a hierarchical source — database columns, nested struct fields, function parameters, file lines, config keys — as a first-class graph node with a containment edge back to its parent.

The temptation is sharpest for builders coming from enterprise graph (EG) platforms. One EG reference implementation the authors studied mints **one `Column` graph node per physical column** and reports **195K column nodes today, projected toward O(1M)** at full scale; its own UI hides columns by default because they swamp the graph (its demo playbook documents this). Column-level lineage is then modeled as `derived_from` edges *between* column nodes, which compounds the node and edge counts further. A builder porting that schema into Trellis verbatim inherits the same explosion — and, because Trellis versions every node forever, a far heavier long-term obligation.

The [`modeling-guide.md`](../agent-guide/modeling-guide.md) already names this anti-pattern ("Schema explosion", "Cardinality explosion from implicit joins"), already ships the four-question node test, and already documents `node_role=structural` as the escape valve. The [`source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) Unity Catalog recipe already says "**Do NOT emit a node per column.**" This ADR does not invent that guidance — it **elevates it to a decision**, ties it explicitly to EG-style ontology construction, and scopes the follow-up tooling that would make the guidance enforceable.

### What goes wrong if columns become nodes by default

| Failure | Mechanism |
|---|---|
| Graph size grows 10–100× | One node + one containment edge per leaf; column counts dwarf table counts (the EG reference implementation: 195K → O(1M)). |
| Retrieval pollution | Structural leaf nodes compete with semantic parents for token budget unless filtered. |
| SCD-2 churn | Every column add/drop/type/comment change becomes a new versioned row to maintain forever. |
| Source-of-truth drift | The leaf's authoritative history already lives in the source system (catalog, git); the graph duplicates it. |
| Slower rebuilds/checkpoints | Compaction and graph health passes walk 50× more rows. |
| Harder-to-interpret queries | Traversals fan out through leaf sinks that go nowhere. |
| Standing maintenance burden | Low-traversal metadata that nobody queries still has to be versioned, indexed, compacted, and retrieved correctly. |

Column-level graph nodes are sometimes the right call. They should be an **explicit, justified exception** — never the path of least resistance.

## 2. Decision

**Columns, nested fields, function parameters, file lines, and other leaf metadata default to `Dataset.properties.columns` (or the analogous property bag on the parent) or to rendered schema documentation — NOT first-class graph nodes.**

This is the *default rule*. It does not close the type system: column nodes remain a fully supported shape, gated behind the exception criteria in §4. The open-string contract from [`adr-graph-ontology.md`](./adr-graph-ontology.md) is preserved — this ADR adds **no** new canonical entity types, edge kinds, or validation. It is a guidance + guardrail decision, not a schema change.

### 2.1 Preferred placement

| Information | Default placement |
|---|---|
| Column name / type / nullability / comment / tags | `Dataset.properties.columns` (JSON array) or a linked schema document via `described_by`. |
| Full schema dump | Document (rendered markdown) for human-readable schemas; blob pointer when large/binary. |
| Column-level PII / governance tag | Property on the parent — *unless* it carries independent lifecycle, policy, or evidence (then see §4). |
| Query-history column-usage statistics | An `Observation` / `Measurement` ([`adr-observation-entity-type.md`](./adr-observation-entity-type.md)) attached to the parent `Dataset`, or to a column-like subject reference — not a node per referenced column. |
| Column-level lineage | Structural `Column` nodes **only when traversal is genuinely required** (§4.1). |
| Frequent schema diffs | Source-system pointer + summarized document; let `TAGS_REFRESHED` carry the structured diff, not per-node churn. |

The placement convention reuses the `Dataset.properties.columns` shape already documented in [`modeling-guide.md`](../agent-guide/modeling-guide.md) ("Cross-database routing properties for queryable datasets" and worked example #1). Nothing new to learn.

### 2.2 Domain scope (amended 2026-06-11)

This ADR's default rule and exception criteria are written for **data-platform-shaped domains** — datasets/tables/columns, pipelines, BI assets — where leaf metadata is high-cardinality, source-system-versioned, and rarely traversed. It is the template, not the law, for other domains: a domain whose leaves carry materially different economics (code symbols with compiler-derived edges, infrastructure resources with per-resource policy attachments, etc.) defines its own leaf-modeling convention in its own ADR, using this one as the starting point. Absent such a domain ADR, this one's default applies.

## 3. Why this shape, not the alternatives

| Alternative | Reason rejected |
|---|---|
| **Model every column as a node (EG-style default)** | The whole failure mode in §1. Pays the maintenance cost for metadata that almost never traverses. |
| **Close the type system to forbid column nodes** | Breaks the open-string contract and the legitimate exceptions (regulated columns, column-level lineage). Trellis does not close vocabularies ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §8.5). |
| **Write-time enforcement (reject column nodes at the mutation pipeline)** | Too blunt — there is no source-neutral signal that distinguishes "a justified structural column node" from "schema explosion" at write time. Enforcement belongs in advisory tooling (§6), not the hot path. |
| **Say nothing; rely on the existing anti-pattern note** | The existing note is correct but buried among five anti-patterns. EG-style builders need the default stated as a *rule* and reconciled with the schema they're porting from. |

## 4. Exception criteria — when column nodes ARE justified

Model a leaf as a graph node only when **at least one** of these is true. These are the four-question node test from [`modeling-guide.md`](../agent-guide/modeling-guide.md), specialized to leaf metadata, plus a fifth criterion for regulated fields:

1. **Traversal requirement** — users need to traverse *from* the column to other columns, policies, metrics, or downstream assets (not merely *from its parent table to it*).
2. **Cross-parent query requirement** — questions like "which regulated columns feed this dashboard?" are run often enough that a property scan over `Dataset.properties.columns` is not sufficient.
3. **Independent evidence or policy** — evidence, classification, approval, or policy attaches to the column *independently* of the table (the column and its metadata do not always move as a unit).
4. **Independent lifecycle** — the column has its own lifecycle: ownership, governance state, SLA, deprecation, or review workflow distinct from its parent.
5. **Regulated / high-risk field** — PII, payment, or regulated-vertical compliance fields where explicit graph traversal has concrete operational value (e.g., "audit every consent-gated column").

If none hold, it is a property or a document. Model **only the specific leaves that earn it** — not the whole schema. The worked example in [`modeling-guide.md`](../agent-guide/modeling-guide.md) (~100 PII column nodes, not 500K) is the canonical "right size" for an exception.

### Empirical promotion path (amended 2026-06-11)

The five criteria above are *prospective* — a builder judges them up front. This amendment adds the *retrospective* path: a leaf that did not justify a node at ingest time can **earn** one from observed usage. A key column that is joined or referenced frequently across a domain is exactly the leaf the criteria intend to admit; the promotion path notices it from telemetry instead of relying on the builder to predict it.

- **Signal.** Usage telemetry accumulates as `Observation`/`Measurement` on the **parent** (per §2.1) — retrieval demand (pack items referencing the leaf), cross-parent reference frequency (the same key appearing across N parents in a domain), query-evidence references. No node is minted to record the signal.
- **Candidate surfacing.** An analyzer (§8 option 5) flags leaves whose telemetry crosses thresholds (occurrence count + parent-diversity + observation window), mirroring the criteria discipline of [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md): candidates are *surfaced*, never auto-applied.
- **Human-gated minting.** An operator approves; the promotion process mints the node satisfying all §5 requirements and stamps the two-signal opt-in (`allow_structural_leaf=True`, `node_role=STRUCTURAL`) from [`adr-source-modeling-discipline.md`](./adr-source-modeling-discipline.md) §2.5 — the extraction validator acknowledges rather than warns.
- **Demotion on decay.** Promotion is not a ratchet — a one-way path would just recreate schema explosion slowly. If the telemetry that justified the node stays below threshold for a full observation window, the node is end-dated (SCD-2 `valid_to`) and the leaf folds back to the parent property. History is preserved; the standing maintenance commitment (§6) stops.

**Status:** mechanism contract only. The analyzer is gated on a production deployment generating real usage telemetry; until then this section defines the contract a future implementation must honour.

## 5. When column nodes ARE used — requirements

A column node admitted under §4 must satisfy all of:

- **`node_role="structural"`** — `NodeRole.STRUCTURAL` (`src/trellis/schemas/enums.py`). Never `semantic`; these are plumbing, not standalone discoverables.
- **Excluded from retrieval by default** — structural nodes are already filtered out of `PackBuilder` retrieval unless a caller passes `include_structural=True` or sets a per-section `node_role_filter` ([`modeling-guide.md`](../agent-guide/modeling-guide.md), "Why `structural` defaults to filtered-out of retrieval"). Do not promote column nodes into default retrieval.
- **Source identifiers + freshness** — carry the source-system pointer (`source_system`, and where applicable `physical_uri` from `DATASET_ROUTING_PROPERTIES` in `src/trellis/schemas/well_known.py`) and a freshness marker (`last_seen_at` / participation in `importance_scored_at` / `TAGS_REFRESHED`) so consumers can tell how stale the structural fact is.
- **Retention / compaction strategy** — a deliberate answer for how the structural subgraph is bounded over time, rather than unbounded SCD-2 accumulation. Column nodes must not be used as broad semantic context-pack items unless a caller explicitly requests them.

## 6. The maintenance-cost argument

The decisive argument is not graph aesthetics — it is the **standing maintenance obligation** every node imposes. Trellis applies SCD Type 2 to every node: "**Every node you create is a commitment to track its history forever**" ([`modeling-guide.md`](../agent-guide/modeling-guide.md), Temporal considerations).

For a column modeled as a node, that means:

- every column **add / drop / type-change / comment-edit / tag-change** mints a new SCD-2 version row,
- that history is almost always **redundant** with the source system's own history (catalog versioning, git, Unity Catalog), and
- it is almost never **queried by time at column granularity** — the question operators actually ask is "the table's schema changed on 2026-03-15", which a single property-diff on the parent answers.

Modeling columns as `Dataset.properties.columns` turns N column changes into **one** property-diff event on the parent's history — the granularity that matches how the data is actually queried — while still preserving full auditability. The SCD-2 churn argument is the reason the default is "property", and it is the line the strengthened guide must call out explicitly (Acceptance Criterion AC-2).

## 7. Reconciliation with the EG's column-node examples

The EG and Trellis are **not in conflict** — they make different defaults for different jobs:

- **An EG may legitimately create `Column` nodes** for demo storytelling, UI drilldown, and column-level lineage. Its column-lineage product *needs* column-to-column `derived_from` edges; that is a genuine traversal requirement (§4.1) for the EG's use case, and the EG's UI already hides columns by default to manage the volume.
- **Trellis builders default to properties / docs.** A builder porting an EG-style schema into Trellis should **not** carry the per-column-node default across. They adopt `Dataset.properties.columns` unless their *own* workload meets one of the §4 exception criteria — most commonly genuine column traversal or an independent column lifecycle.

The reconciliation rule, stated for a builder: *"The EG mints column nodes because its column-lineage UI traverses them. Unless your Trellis workload also traverses columns (or attaches independent evidence/lifecycle to them), keep columns as properties. If it does, model only the columns that traverse, as `node_role=structural`, excluded from retrieval."* This keeps all guidance source-neutral — no source-platform specifics leak into Trellis core; the EG is referenced only as the motivating example.

## 8. Implementation options (follow-up plan — not committed here)

This ADR commits to the **docs-only guardrail (option 1)**. Options 2–4 are a follow-up plan; none are claimed as implemented, and each is gated on its own decision (and on [`adr-ontology-profiles.md`](./adr-ontology-profiles.md) / [`adr-graph-shape-constraints.md`](./adr-graph-shape-constraints.md) landing where noted).

1. **Docs-only guardrail (this ADR's deliverable).** Add a strengthened "Column and leaf metadata policy" section to [`modeling-guide.md`](../agent-guide/modeling-guide.md) stating the default rule, the exception criteria, and the SCD-2 maintenance cost. Lowest cost; should happen regardless. **In scope here.**
2. **Ontology-profile defaults.** A profile ([`adr-ontology-profiles.md`](./adr-ontology-profiles.md)) may declare `Column.node_role_default=structural` and recommend `Dataset.properties.columns` as the default placement — so a data-platform profile bakes the guardrail into its defaults. *Gated on the profiles ADR landing.*
3. **CLI linter (advisory, opt-in, no write-time enforcement).** A `trellis admin graph-health`–style check that warns on: (a) a high structural-node ratio, (b) `Column`/leaf nodes with no outbound edges beyond a containment edge, and (c) structural nodes included in default retrieval. This can build on the declarative shape layer in [`adr-graph-shape-constraints.md`](./adr-graph-shape-constraints.md) and **overlaps the linter scoped for [#219](https://github.com/ronsse/trellis-ai/issues/219) — build it once.**
4. **Bulk-ingest advisory warnings.** When a bulk ingest contains many `Column`/leaf nodes, return advisory warnings unless the request/profile marks them as intentional structural nodes. Advisory only — never a rejection.
5. **Leaf-promotion analyzer (amended 2026-06-11).** Reads leaf-usage `Observation`/`Measurement` rows and surfaces promotion candidates per the "Empirical promotion path" in §4 — human-gated like the well-known promotion loop, never auto-mutating, with the demotion-on-decay check in the same pass. *Gated on a production deployment generating real usage telemetry.*

> **Follow-up decision (2026-06-11):** the advisory-warning path is now decided and specified in [`adr-source-modeling-discipline.md`](./adr-source-modeling-discipline.md), which authorizes a warn-only, opt-in-flagged validator at extraction time (Track G G1) plus the `column_names` searchability recipe and the no-name-match lineage rule. That ADR is the implementation companion to this one; this ADR remains the policy authority.

## 9. Consequences

### 9.1 What this enables
- A clear, citable default for builders porting EG-style schemas: columns are properties, not nodes.
- A bounded, documented exception path that keeps the open-string contract intact.
- A scoped follow-up plan (profile defaults, advisory linter) that can be built once and shared with [#219](https://github.com/ronsse/trellis-ai/issues/219).

### 9.2 What this does *not* do
- Does not add canonical entity types, edge kinds, or validation.
- Does not enforce anything at write time — column nodes remain creatable.
- Does not implement the linter, profile defaults, or bulk-ingest warnings (§8 options 2–4 are a plan).
- Does not change the EG — the EG keeps its column-node model; only the *Trellis default* is asserted.

### 9.3 What this costs
- One strengthened guide section + this ADR.
- A standing reconciliation note that must be revisited if the EG's column model and Trellis's defaults are ever expected to converge.

## 10. Acceptance criteria

- **AC-1.** [`modeling-guide.md`](../agent-guide/modeling-guide.md) contains a strengthened "Column and leaf metadata policy" section stating the default rule (§2) and the five exception criteria (§4).
- **AC-2.** That section explicitly calls out the maintenance cost of column-level change tracking — specifically SCD-2 churn (§6).
- **AC-3.** The guidance reconciles the EG's column-node examples with the Trellis default (§7): the EG may create column nodes for demo/UI/column-lineage; Trellis builders default to properties/docs unless they need column traversal or independent lifecycle.
- **AC-4.** The "when column nodes ARE used" requirements (§5) are documented: `node_role=structural`, excluded from default retrieval, source identifiers + freshness, retention/compaction strategy.
- **AC-5.** A follow-up implementation plan exists for a linter / profile-based warning (§8), explicitly *not* claimed as implemented and noted as overlapping [#219](https://github.com/ronsse/trellis-ai/issues/219).
- **AC-6.** The guidance does not imply that every referenced column should become a node — the query-history column-usage path routes to an `Observation`/`Measurement` on the parent, not a node per column (§2.1). The existing query-log recipe in [`source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) already conforms (no per-column nodes).

## 11. References

- [`adr-graph-ontology.md`](./adr-graph-ontology.md) — open-string types, `NodeRole`, no-new-vocabulary commitment.
- [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) — where column-usage statistics belong (Observation/Measurement on the parent).
- [`adr-graph-shape-constraints.md`](./adr-graph-shape-constraints.md) — the declarative shape layer a future linter can build on.
- [`modeling-guide.md`](../agent-guide/modeling-guide.md) — four-question node test, three node roles, Schema-explosion / Cardinality-explosion anti-patterns, temporal "commitment to track history forever".
- [`source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) — Unity Catalog recipe ("Do NOT emit a node per column") and SQL query-log recipe.
- `src/trellis/schemas/enums.py` — `NodeRole.STRUCTURAL`.
- `src/trellis/schemas/well_known.py` — `DATASET_ROUTING_PROPERTIES`.
- EG reference implementation (195K → O(1M) column nodes; UI hides columns by default). Motivating example only; no EG specifics in Trellis core.
