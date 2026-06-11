# Graph Modeling Guide

> **Who this is for:** anyone designing an ingestion runner, a knowledge import pipeline, or a domain-specific schema for Trellis. Read this **before** you write the first `upsert_node` call. The decisions you make at modeling time are hard to undo later.

> **What this covers:** how to decide what becomes a node, what becomes a property, and what becomes a document. How the three node roles (`structural`, `semantic`, `curated`) shape that decision. The anti-patterns we've already seen in the wild and how to avoid them. Two worked examples in different domains.

> **What this is not:** an API reference. For schema fields and method signatures see [schemas.md](schemas.md). For operational commands see [operations.md](operations.md).

> **Designing an enterprise/EGP-style graph?** Read the capability-framing ADR [`adr-enterprise-ontology-capability-framing.md`](../design/adr-enterprise-ontology-capability-framing.md) first — it places this guide's node/property/document decision inside the wider stack (canonical identity, lineage, governed profiles, agent memory) and links the column-guardrails ([`adr-column-leaf-modeling-guardrails.md`](../design/adr-column-leaf-modeling-guardrails.md)) and ontology-profiles ([`adr-ontology-profiles.md`](../design/adr-ontology-profiles.md)) ADRs.

---

## The core tension

Trellis is deliberately domain-neutral. The core library ships a handful of well-known entity types (`PERSON`, `SYSTEM`, `SERVICE`, `TEAM`, `DOCUMENT`, `CONCEPT`, `DOMAIN`, `FILE`, `PROJECT`, `TOOL`) but the storage and API layers accept **any string** as an entity type or edge kind. This is intentional — a pharma deployment needs molecule→protein relationships, a code-search deployment needs function→parameter, an infrastructure deployment needs host→service. The core library cannot pre-judge which of these should exist.

The cost of this flexibility is that **every consumer has to make modeling decisions**, and those decisions are not obvious. The most common failure mode we've seen is over-modeling: treating every leaf of a hierarchical structure (database columns, function parameters, file lines, config keys) as its own graph node with a `belongs_to` edge back to its parent. This inflates the graph by 10-100×, pollutes retrieval, fragments what should be cohesive context, and produces no traversal payoff — because the graph queries you actually run are at the parent level, not the leaf level.

This guide exists to make the right decision the obvious one.

---

## Where data lives: the four stores

Before deciding what becomes a node, you have to decide whether it belongs in the graph at all. Trellis splits storage into four agent-facing stores (the Knowledge Plane) plus two Trellis-internal stores (the Operational Plane). The right question is not "what should this look like as a graph?" but "which of the four stores does this piece of information actually belong to?"

| Store | What it holds | What it's optimized for | Anti-pattern |
|---|---|---|---|
| **Graph** | Identity-bearing structural facts: entities, relationships, versioning, ownership, lineage. | Cross-parent traversal, temporal queries via SCD Type 2, structured filters. | Storing free-form text as node properties. Storing leaf-only data that never traverses. |
| **Document** | Free-form text content: descriptions, READMEs, runbooks, wiki pages, schema docs, ADRs. Always linked to one or more graph nodes via `described_by` or similar edges. | Full-text search, semantic search via vector embeddings (when the document store also serves vectors), token-budget-aware pack assembly. | Stuffing structured key-value data into document text so the structured query becomes a regex. See *Property-envy documents* below. |
| **Blob** | Binary artifacts: PDFs, parquet files, model checkpoints, screenshots, raw payloads larger than the document store cares to inline. Referenced by URL stored as a property on the owning graph node. | High-volume, infrequently-accessed bytes. Lifecycle-managed (TTL) without polluting the graph. | Inlining 50MB PDFs as base64 in document content. |
| **Vector** | Embeddings for similarity search. Two shapes are supported: an *independent* vector store (default) or *attached to graph nodes* as an optional property (the Neo4j shape #2 — same nodes, same database). | Approximate-nearest-neighbour retrieval, semantic-seed extraction (see swarm 3 SEM-1), tag-filtered similarity. | Embedding every structural node "just in case." Embeddings should follow retrievable content, not plumbing. |

The Operational Plane stores — `TraceStore` (immutable agent execution records) and `EventLog` (governance audit) — are not addressed by domain modeling decisions. They're populated automatically by the mutation pipeline; you don't choose to put data there.

**The decision question:** *"Where will an agent or operator want to find this six months from now?"*

- If they'll filter or traverse by it: graph property or node.
- If they'll read it as prose: document.
- If they'll fetch it as a file: blob.
- If they'll do similarity search on it: vector (often alongside document or graph).

A single real-world artifact often lives in three of the four. A dbt source table becomes: a graph node (the structural fact), a document holding its description (the prose), and a vector embedding of that description (for semantic retrieval). The graph node is the anchor; the others hang off it via edges or properties.

---

## The four-question test

Before adding anything to the graph, answer these four questions about it. A thing should be a **node** only if **at least one** of them is true:

1. **Traversal** — *Do you traverse from it to other things, not just from its parent?*
   A database table is traversed: you follow its `DEPENDS_ON` edges to upstream tables, its `OWNED_BY` edge to a team, its `DESCRIBED_BY` edge to documentation. A column, in contrast, only ever has one inbound edge from its parent table and no outbound edges. The table traverses; the column doesn't.

2. **Cross-parent query** — *Do you query for it directly across parents?*
   "Find all services owned by the Platform team" is a cross-parent query — the service type earns node status. "Find all columns named `user_id` across all tables" sounds like a cross-parent query, but in practice it's better served by a search index over a JSON property. Ask: do you run this query *often enough* that graph-native support matters? If you run it twice a year for auditing, a scan is fine.

3. **Independent evidence** — *Do you attach evidence, observations, policies, or feedback to it independently of its parent?*
   A precedent accumulates feedback over many traces — it earns node status because the evidence attaches to it, not to whatever spawned it. A column's PII classification attaches... to the column, which sounds like it earns node status — but almost always, PII classification is *one property among many* on the column, and all those properties travel together as a unit. If the column and its properties always move as a unit, the unit is the atom; the column is not.

4. **Independent lifecycle** — *Does it have its own lifecycle — versioning, deprecation, refresh cadence — separate from its parent?*
   A dbt model has its own git history, its own deprecation status, its own refresh schedule. A column in that model does not — when the model changes, the columns change with it. The model earns node status; the columns don't.

**If none of the four are true, it's not a node.** It's a property on the parent node (if small and structured), or it's a document linked to the parent via an edge (if large and text-shaped).

Write the four questions down as comments above your ingestion runner's upsert logic. If you can't answer "yes" to at least one for every entity type you create, you're over-modeling.

---

## The three node roles

Some things genuinely need to be nodes but play very different structural roles. Trellis distinguishes three:

| Role | What it means | Examples | Retrieval default | Temporal profile |
|---|---|---|---|---|
| **`structural`** | Fine-grained plumbing, machine-generated, regenerated from source. Exists to support its parent, not to be retrieved independently. | Columns *when* column-level lineage is a real requirement; function parameters when call-graph analysis is needed; file lines when diff-based retrieval matters. | **Filtered out** of retrieval by default. Consumers must explicitly opt in. | Still SCD Type 2, but property histories are usually redundant with the source system's history. |
| **`semantic`** (default) | Represents a real thing in the world, ingested from a source-of-truth. The core of the graph. | Tables, dbt models, services, people, teams, documents, precedents, concepts, projects, workflows. | **Normal ranking**, standalone discoverable, all retrieval strategies apply. | SCD Type 2 captures meaningful property changes over time. `get_node_history()` is a first-class query. |
| **`curated`** | Synthesized or derived from the graph itself. Regeneratable from a generator spec. Human-editable. Meant for iteration. | Domain rollups, community cluster summaries, promoted precedents, "popular entities" indexes, LLM-generated explanations of subgraphs. | **Boosted** for broad, strategic, objective-tier queries. Lower density, higher information value. | Two histories: standard SCD Type 2 (for edits) *and* `generation_spec` (for regenerations). |

### Why three roles, not two

The binary distinction (structural vs semantic) is not enough because **curated nodes have fundamentally different semantics from ingested entities**. An ingested table cannot be edited — doing so would drift from the source-of-truth and corrupt downstream lineage. A curated domain summary, by contrast, is *meant* to be edited: it's a human-readable synthesis that a domain expert should be able to refine. Lumping both under `semantic` makes it impossible to build curation tooling without risking accidental corruption of ingested data.

Separating them in the schema means:
- `trellis curate edit` can operate safely on curated nodes
- Regeneration workflows know which nodes they're allowed to recompute
- Retrieval can boost curated summaries (high information density) without boosting every semantic node
- Effectiveness analysis can measure whether curation is actually adding value

### Why `structural` defaults to filtered-out of retrieval

The whole point of structural nodes is that they exist for graph traversal, not for retrieval. If a `PackBuilder` query turns up 500 column nodes alongside the 5 table nodes you actually wanted, you've wasted token budget on plumbing. By default, structural nodes are skipped in retrieval; they surface only when a query explicitly requests them (e.g., "show me the schema for this table" will walk the column nodes via the parent).

This is a soft guarantee, not a hard one — consumers with legitimate need can pass `include_structural=True` to any retrieval call, or set a per-section `node_role_filter` in `SectionRequest`. The default just optimizes for the common case.

---

## Node vs property vs document: the decision flow

```
┌─────────────────────────────────────────────────────────────────────┐
│  I want to add information to the graph. Where does it go?          │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
            ┌───────────────────────────────────┐
            │ Is it free-form text longer than  │
            │ ~1 paragraph? (description,       │
            │ readme, comment, specification)   │
            └───────────────────────────────────┘
                    │                     │
                   yes                    no
                    │                     │
                    ▼                     ▼
    ┌───────────────────────┐   ┌───────────────────────┐
    │ DOCUMENT linked to    │   │ Does it pass at least │
    │ a parent node via     │   │ one of the four       │
    │ DESCRIBED_BY edge.    │   │ questions?            │
    │ Stored in the         │   │ (traversal, cross-    │
    │ DocumentStore, full-  │   │  parent query,        │
    │ text indexed, can be  │   │  independent evidence,│
    │ embedded for semantic │   │  independent          │
    │ search.               │   │  lifecycle)           │
    └───────────────────────┘   └───────────────────────┘
                                        │            │
                                       yes           no
                                        │            │
                                        ▼            ▼
                        ┌───────────────────┐  ┌───────────────────┐
                        │ NODE. Determine   │  │ PROPERTY on the   │
                        │ node_role:        │  │ parent node's     │
                        │                   │  │ JSON properties   │
                        │ STRUCTURAL:       │  │ field. Travels    │
                        │   fine-grained,   │  │ with the parent,  │
                        │   opt-in-only     │  │ versioned with    │
                        │   retrieval       │  │ the parent's SCD  │
                        │                   │  │ Type 2 history.   │
                        │ SEMANTIC:         │  │                   │
                        │   default, real-  │  │                   │
                        │   world thing,    │  │                   │
                        │   source of truth │  │                   │
                        │                   │  │                   │
                        │ CURATED:          │  │                   │
                        │   synthesized,    │  │                   │
                        │   regeneratable,  │  │                   │
                        │   human-editable  │  │                   │
                        └───────────────────┘  └───────────────────┘
```

---

## Reference vs summary: when to inline, when to point

The document-vs-property axis covered above answers *whether* free-form content goes in the document store. A second decision follows: when free-form content does go in the document store, how much of the source should be inlined versus referenced via URL?

This matters most for sources that already have their own canonical storage — Confluence pages, Jira tickets, git files, Notion docs, S3 objects. The temptation is to inline the entire body so retrieval is self-contained. The cost is two-fold: bloated document store, and drift between Trellis's snapshot and the source's current state.

The decision rule:

| Source content shape | What to store in document | What to store as property/edge |
|---|---|---|
| Small, stable, frequently retrieved | **Inline.** Full body in document content. | URL in node properties for the audit trail. |
| Small, mutates frequently | **Summary + reference.** A 1-3 sentence digest in the document; full body fetched from source. | Source URL as a node property; `last_seen_revision` as a timestamp. |
| Large, infrequently retrieved | **Reference only.** No document. | Source URL on the node; a one-line description on the node itself. |
| Large, contains pockets of high-value text | **Selective inline.** Extract the high-signal sections as their own documents; link the rest by reference. | Source URL on the parent node; `derived_from` edge from each extracted document. |
| Binary | **Never inline.** | Blob URL as a property; the node carries metadata only. |

Concrete thresholds we've found useful (your mileage may vary):

- **Inline when** content < 4KB *and* mutates less than weekly *and* is queried/embedded/searched. Inline-able items dominate retrieval cost; spending storage on copies pays off.
- **Reference when** content > 50KB *or* mutates daily *or* is rarely retrieved.
- **Summary + reference when** size is somewhere in between *or* mutation rate is moderate. The summary captures intent ("Q3 OKR — improve retrieval p95 latency to under 200ms") so an agent can decide whether to fetch the full body.

Common shapes by source:

- **Confluence / wiki pages**: usually small enough to inline; deduplicate against the parent-page revision tree so each page version doesn't become a new document.
- **dbt model descriptions**: short, stable, inline.
- **PDF runbooks**: store as blob, summary as document, both linked to the same graph node.
- **Jira tickets**: title + description as document; comments as reference (re-fetch on access) — comments grow unboundedly and rarely matter to retrieval.
- **GitHub issues / PRs**: similar to Jira. Reviews, comments, and CI output stay external.

If you can't decide, default to **reference + summary**. It's the safest position: the source stays authoritative, your store stays small, and you can promote to fully-inlined later if retrieval signal pushes you there. The reverse (un-inlining after the fact) is harder.

---

## Cross-database routing properties for queryable datasets

Entities representing queryable datasets — canonical type `Dataset` (plus the lowercase `dataset` alias and extractor-specific shapes like `dbt_model` / `dbt_source` / `UC_TABLE`) — should carry routing properties so query-engine agents can dispatch queries without consulting the prompt or out-of-band config. The convention is defined in [`src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) under `DATASET_ROUTING_PROPERTIES`:

| Property | Type | Required? | Meaning |
|---|---|---|---|
| `source_system` | string | recommended | Short identifier of the data platform: `"snowflake"`, `"postgres"`, `"bigquery"`, `"databricks"`, `"duckdb"`. Maps to dbt's `metadata.adapter_type` and to the URI scheme of OpenLineage namespaces. |
| `connection_ref` | string | optional | Env-var name (or secrets-manager key) resolving to a connection string or client config. **Never inline a credential.** Optional because many entities are read-only metadata records that don't need an active connection. |
| `database_name` | string | recommended | Physical database / catalog name. |
| `schema_name` | string | recommended | Physical schema / namespace within the database. Distinct from the dbt `schema` property, which historically encodes both physical schema and logical layer convention; both keys coexist on dbt-extracted entities. |
| `physical_uri` | string | optional | Fully-qualified locator: `snowflake://account/db/schema/table` or `postgres://host:port/db.schema.table`. Extractors construct this only when the upstream source supplies enough information; agents prefer this over recomposing from parts. |

These are *recommended convention*, not enforced schema — Trellis entity properties are open bags by design. Extractors populate what the upstream system supplies; consumers read with `.get(...)` and fall back gracefully when a property is absent.

**Worked example.** A dbt model `analytics.marts.fct_orders` extracted from a Snowflake-adapter dbt project lands with:

```json
{
  "entity_id": "model.my_project.fct_orders",
  "entity_type": "dbt_model",
  "name": "fct_orders",
  "properties": {
    "unique_id": "model.my_project.fct_orders",
    "schema": "marts",
    "database": "analytics",
    "source_system": "snowflake",
    "schema_name": "marts",
    "database_name": "analytics",
    "physical_uri": "snowflake://analytics/marts/fct_orders",
    "materialized": "table"
  }
}
```

A query-engine agent gets this entity in a pack, reads `physical_uri`, looks up the active Snowflake connection via `connection_ref` (when present), and dispatches a query against the right warehouse — without the orchestrating prompt needing to know which database holds what.

**What extractors are responsible for**: populating the properties they can derive from the source. dbt manifests provide `adapter_type`, `database`, `schema`, `name` — everything except `connection_ref`. OpenLineage namespaces provide a URI scheme that becomes `source_system`. Unity Catalog provides all five parts directly. Markdown docs and git repos provide nothing — they're not queryable datasets and shouldn't claim the convention.

**What curators are responsible for**: filling `connection_ref` when an entity is meant to be queried interactively. This is the only field that requires deployment-specific knowledge (which env var holds the connection string), so it's the one a curator typically adds rather than an extractor.

---

## Column and leaf metadata policy

This is the single decision most likely to wreck a graph at scale, so it gets its own rule. It specializes the [four-question test](#the-four-question-test) to the leaf level and is the subject of [`adr-column-leaf-modeling-guardrails.md`](../design/adr-column-leaf-modeling-guardrails.md).

> **Default rule:** columns, nested struct fields, function parameters, file lines, config keys, and other leaf metadata live as **properties on the parent** (`Dataset.properties.columns` and friends) or as **rendered schema documentation** — **not** as first-class graph nodes.

EGP-style enterprise graph platforms mint one `Column` node per physical column (one such reference platform reports 195K column nodes today, projected toward ~1M, and hides columns in its UI by default because they swamp the graph). That is a legitimate choice *for a column-lineage UI that traverses columns*. It is the wrong default to carry into Trellis. A builder porting that schema should collapse columns to properties unless their own workload meets an exception below.

### Preferred placement

| Information | Default placement |
|---|---|
| Column name / type / nullability / comment / tags | `Dataset.properties.columns` (JSON array) or a linked schema document via `described_by`. |
| Full schema dump | Document (rendered markdown), or blob pointer when large/binary. |
| Column-level PII / governance tag | Property on the parent — unless it has independent lifecycle, policy, or evidence (then see below). |
| Query-history column-usage statistics | An `Observation` / `Measurement` attached to the parent `Dataset` — **not** a node per referenced column. |
| Column-level lineage | Structural `Column` nodes only when column traversal is genuinely required. |
| Frequent schema diffs | Source-system pointer + summarized document; let `TAGS_REFRESHED` carry the structured diff, not per-node churn. |

### When a column *does* earn node status

Model a leaf as a node only when **at least one** of these holds — and then model **only the specific leaves that earn it**, not the whole schema:

1. **Traversal requirement** — you traverse *from* the column to other columns, policies, metrics, or downstream assets (not merely from its parent to it).
2. **Cross-parent query** — "which regulated columns feed this dashboard?" is run often enough that a scan over `Dataset.properties.columns` is not enough.
3. **Independent evidence or policy** — classification, approval, or policy attaches to the column independently of the table (the column and its metadata do not always move as a unit).
4. **Independent lifecycle** — the column has its own ownership, governance state, SLA, deprecation, or review workflow.
5. **Regulated / high-risk field** — PII, payment, responsible-gaming, or compliance-sensitive fields where explicit graph traversal has concrete operational value.

When a column node is justified it must be `node_role="structural"` (excluded from default retrieval), carry source identifiers (`source_system`, `physical_uri`) and a freshness marker, and have a retention/compaction strategy. See worked example #1 above for the right-sized exception (~100 PII column nodes, not 500K).

### Why the default is "property": the SCD-2 maintenance cost

The decisive reason is not graph size — it is the standing maintenance obligation. Trellis applies SCD Type 2 to **every** node, so every node you create is a commitment to track its history forever. Model a column as a node and *every* column add / drop / type-change / comment-edit / tag-change mints a new versioned row — history that is almost always redundant with the source system's own (catalog, git, Unity Catalog) and almost never queried by time at column granularity.

Model the same columns as `Dataset.properties.columns` and those N changes collapse into **one** property-diff event on the parent's history — the granularity operators actually query ("the table's schema changed on 2026-03-15") — while preserving full auditability. Column-level change tracking as nodes is SCD-2 churn you pay for forever and rarely read; that is the line that makes "property" the default and "node" the justified exception.

---

## Temporal considerations

### Every node is a temporal entity

Trellis's graph store uses [SCD Type 2 versioning](https://en.wikipedia.org/wiki/Slowly_changing_dimension#Type_2:_add_new_row) on nodes: each node carries `valid_from` and `valid_to` fields, and `get_node_history(node_id)` returns the full audit trail of every version. Time-travel queries via `as_of` let you see the graph exactly as it looked at an arbitrary past moment.

**Every node you create is a commitment to track its history forever.** This is a feature, not a bug — auditability is one of Trellis's core value propositions. But it has a direct consequence for modeling: if you create 500K column nodes, you now have 500K independent temporal histories, almost none of which you will ever query by time. The storage cost is real, the query cost is real, and the retrieval noise cost is real.

Contrast with modeling columns as a JSON property on the parent table node: a schema migration shows up as **one** property-diff event on the table's history, which is usually what you actually want to see anyway ("the table's schema changed on 2026-03-15"). You still get the full history, at a granularity that matches how you query it.

**The temporal rule of thumb:** when deciding node-vs-property, ask *"do I want time-travel queries at this granularity?"* If no, it's a property.

### Curated nodes have two histories

Curated nodes carry two distinct kinds of history, and understanding the distinction matters for curation tooling:

1. **Property history (SCD Type 2).** Edits to a curated node's summary, title, or other properties create new SCD Type 2 versions. The `human_edited` metadata flag distinguishes human edits from other kinds of updates.

2. **Generation history (`generation_spec`).** When the node is regenerated by its generator (e.g., re-running the community detection algorithm after new entities arrive), the `generation_spec` is updated: `generator_version`, `generated_at`, `source_node_ids`, and `parameters` all change. A regeneration *also* creates a new SCD Type 2 version (because properties changed), but additionally carries the regeneration marker.

This separation means `trellis curate diff` can show you two meaningfully different things: "how has the human-edited summary changed over time?" versus "how has each regeneration differed from the last?" These answer different questions and enable different quality gates.

**The curated-node temporal rule:** edits are property changes; regenerations are generation-spec changes; both are SCD Type 2 versions.

### Structural nodes: SCD is still on

You might reasonably ask whether structural nodes need SCD Type 2 at all. Their source system already tracks history (git, Unity Catalog, etc.), and 500K column histories feel redundant.

The answer is **yes, keep SCD on for structural nodes too**, for two reasons:
1. Uniformity matters more than marginal storage savings. Mixed-history storage models are a debugging nightmare.
2. Structural nodes should be rare. If you have so many that SCD cost becomes a problem, the real answer is "model fewer things as nodes," not "turn off versioning for the ones you have."

If storage cost ever becomes a pain point for a legitimately large structural graph, we'll add a `skip_history: bool` flag on `NodeRole` configuration — but that's a future problem, not a current one.

---

## Freshness signals: how staleness propagates

A graph that mirrors moving production systems is wrong the moment it stops being refreshed. Trellis exposes three signals so agents can reason about staleness rather than treating every read as ground truth.

### Signal 1: `valid_from` / `valid_to` (SCD Type 2)

Every node carries `valid_from` and `valid_to` timestamps. The current version has `valid_to=NULL`; superseded versions carry the timestamp when the next version landed. `get_node_history(node_id)` returns the chain; `as_of=<ts>` on graph queries time-travels the entire read.

This is the *fact* axis: "what did the graph say at time T?" It does not directly answer "is the current version stale?" — that requires the next two signals.

### Signal 2: `importance_scored_at`

Set on nodes that participate in retrieval ranking. Records the timestamp when the node's importance score was last computed. A refresh hook (`recompute_importance(...)`) updates the score and stamps the field. The read-path guardrail in `PackBuilder` flags nodes whose `importance_scored_at` is older than a configurable threshold so retrieval can downrank them or trigger background refresh.

Practical use: a dbt model that hasn't been re-extracted in 30 days but is still being retrieved is a candidate for either re-extraction or demotion. Agents read `importance_scored_at` from the pack metadata; the EventLog emits `IMPORTANCE_REFRESHED` when the score is recomputed.

### Signal 3: `TAGS_REFRESHED` events + `Lifecycle.state`

When an extractor re-runs and produces a structural diff vs the prior extraction (e.g., a column was added to a table, a description changed, a depends-on edge appeared), the dispatcher emits a `TAGS_REFRESHED` event into the `EventLog` with the structured diff payload. Agents watching the event log know that cached pack content referencing the affected node is stale.

`Lifecycle.state` is the human-readable summary of that signal: `active` (current), `superseded` (a newer version exists), `deprecated` (marked by a curator), `archived` (removed from default retrieval), `noise` (demoted by feedback). State transitions are driven by:

- *Extractor re-runs* (via `trellis extract refresh`) → may set `superseded` or `archived` based on diff
- *Curator action* → can set `deprecated`, `archived`, or restore to `active`
- *Feedback loop* → `apply_noise_tags()` demotes consistently-irrelevant items to `noise`

### Three signals, three uses

| Question an agent asks | Signal to read |
|---|---|
| "What did this entity look like on 2026-04-01?" | `as_of` time-travel via `valid_from` / `valid_to`. |
| "How confident should I be that this entity's importance is current?" | `importance_scored_at` age. |
| "Has this entity been touched recently, and what changed?" | `TAGS_REFRESHED` events on the EventLog; `Lifecycle.state` for the rolled-up answer. |

See [`freshness-and-curation.md`](freshness-and-curation.md) for operational details on triggering refresh, reading drift events, and the two refresh modes (periodic re-run vs pushed events).

---

## Anti-patterns: named failure modes

These are the over-modeling patterns we've actually seen in the wild. Each has a name and a concrete fix.

### Schema explosion

**Symptom:** every leaf of a hierarchical structure becomes its own node. Database columns (the canonical example), function parameters, file lines, config keys, TOML sub-sections. A graph that should have 10K table nodes has 500K column nodes plus 500K `belongs_to` edges.

**Why it happens:** "the data has structure, so the graph should have structure." The ingestion runner walks the source tree recursively and calls `upsert_node` at every level. It feels natural but it's wrong.

**Diagnosis:** run `trellis admin graph-health` (when available) and look at the "leaf-node analysis by entity type" report. A semantic entity type with >90% leaves is almost always schema explosion.

**Fix:** collapse the leaves into a JSON property on the parent. The parent's SCD Type 2 history captures leaf changes as property diffs. Retrieval returns cohesive parent units instead of fragmented leaves.

**When the anti-pattern is *not* an anti-pattern:** when column-level lineage (or parameter-level call graph, or line-level diff traversal) is a genuine requirement — for instance, regulated data products with per-column provenance audits, or advanced compiler analysis tooling. In those cases, model *only the leaves that actually participate in meaningful cross-parent relationships* as `node_role=structural`. Not all of them — just the ones that earn it via the four-question test.

### Leaf-only nodes

**Symptom:** a semantic entity type whose nodes almost never appear as the source of any edge, only as the target. They're sinks: things come in, nothing goes out.

**Why it happens:** the type was introduced during ingestion to "make the data searchable," but nobody ever built the reverse queries or the outbound relationships. The node exists because it was easy to create, not because it does graph work.

**Diagnosis:** `trellis admin graph-health` reports "edge fan-out distribution per edge kind" and flags edge kinds where the target is ≥90% leaves. If the type's only purpose is to be a search target, ask yourself whether a search index over a JSON property would serve the same purpose at one-tenth the graph cost.

**Fix:** two options, depending on whether the type has any traversal value at all.
- If it has *some* traversal value (e.g., you occasionally follow the edge), demote to `node_role=structural` so it stops polluting default retrieval while still being queryable via explicit opt-in.
- If it has *no* traversal value, collapse into the parent as a JSON property and delete the type from the schema entirely.

### Ingested-looking curated nodes

**Symptom:** synthetic summaries, LLM-generated descriptions, or derived rollups stored without `node_role=curated`, making them indistinguishable from ingested ground truth. A human looking at the graph can't tell which nodes are "what actually exists" and which are "what we computed about what exists."

**Why it happens:** the first curator gets built without knowledge of the curated-node framework. It just calls `upsert_node` with `node_role=semantic` (the default) because that's what semi-structured data looks like.

**Consequences:**
- Regeneration becomes impossible without risking corruption of ingested data (because you can't tell the two apart)
- Humans cannot safely edit curated summaries (they might edit an ingested node by mistake)
- Retrieval treats both alike instead of boosting curated content for strategic queries
- Effectiveness analysis can't measure curation quality vs ingestion quality separately

**Fix:** always set `node_role=CURATED` when the node is derived rather than ingested. Populate `generation_spec` with the generator name, version, source node IDs, and parameters. If you can't populate `generation_spec` meaningfully, the node probably isn't actually curated — it's semantic, and you should accept it as ground truth and stop trying to regenerate it.

### Property-envy documents

**Symptom:** structured information (key-value pairs, numeric metrics, timestamps) shoved into document text because "the document store is easier to ingest." The graph is missing first-class facts; the document store is full of strings that look like JSON.

**Why it happens:** the ingestion runner is simpler if it just dumps everything as text. Parsing the structure and turning it into typed fields is annoying, especially when the source format is heterogeneous.

**Consequences:**
- Structured queries become string queries ("find tables where row_count > 1M" becomes a regex over document text)
- Typed aggregations become impossible
- The graph's structural queries return nothing useful because the data lives in unstructured blobs

**Fix:** invest in the parser. Extract structured fields into node properties; keep only genuinely free-form content (descriptions, comments, user notes) in documents. If the source is messy, use a normalization step in the ingestion runner — not the document store — as the catch-all.

### Cardinality explosion from implicit joins

**Symptom:** the ingestion runner creates a node for every `(X, Y)` pair in a join — e.g., every `(table, column)` pair, every `(file, import)` pair, every `(service, endpoint)` pair. The result is a graph with N*M nodes where N+M would suffice.

**Why it happens:** the join exists in the source data (e.g., dbt's manifest lists `(model, column)` rows) and the ingestion runner walks it literally.

**Diagnosis:** a graph where two entity types have roughly equal counts and one edge kind exactly matches the product of their counts is a smoking gun.

**Fix:** collapse the join into properties on whichever side is the "strong" entity. For `(table, column)`, columns go as JSON on tables. For `(file, import)`, imports go as a JSON array on files. For `(service, endpoint)`, endpoints go as a JSON array on services (unless endpoints have their own lifecycle — rate limits, deprecation, per-endpoint auth policies — in which case endpoints earn their own node status via the four-question test).

### Synthetic-identity collision

**Symptom:** the ingestion runner generates node IDs from a naming scheme (e.g., `table:catalog.schema.name`) but the scheme isn't unique (e.g., temp tables from different runs collide). Upserts clobber each other's histories; SCD Type 2 tracks nonsensical version chains.

**Why it happens:** the naming scheme was designed before the full scope of the data was understood. Debug data, test data, or cross-environment data produces collisions that nobody anticipated.

**Fix:** design the alias system up front. Use the `(source_system, raw_id)` → `entity_id` pattern so that ambiguous names get disambiguated at the alias layer, and the internal `entity_id` stays unique. This is what the deterministic alias system in Trellis is for — don't bypass it by generating your own IDs.

---

## Worked example 1: Database catalog ingestion

This is the anti-pattern that motivated this guide. A data platform team was ingesting a Unity Catalog with ~500K columns across ~10K tables and modeling every column as a graph node.

### The wrong shape

```python
# ❌ DON'T DO THIS
for table in catalog.tables():
    upsert_node(
        entity_id=f"table:{table.fqn}",
        entity_type="UC_TABLE",
        properties={"row_count": table.row_count, ...},
    )
    for column in table.columns:
        upsert_node(
            entity_id=f"column:{table.fqn}.{column.name}",
            entity_type="UC_COLUMN",
            properties={"type": column.type, "nullable": column.nullable, ...},
        )
        upsert_edge(
            source=f"column:{table.fqn}.{column.name}",
            target=f"table:{table.fqn}",
            kind="belongs_to",
        )
```

**What this produces:**
- 10K `UC_TABLE` nodes
- 500K `UC_COLUMN` nodes (50× the tables)
- 500K `belongs_to` edges
- 510K SCD Type 2 histories being maintained indefinitely
- Every retrieval query competing column nodes against table descriptions for token budget
- Every graph traversal fanning out through leaf column nodes that go nowhere

**Apply the four-question test to `UC_COLUMN`:**
1. Traversal — does a column traverse to other things? *No, only its parent table.*
2. Cross-parent query — do we query for columns across tables? *Rarely; when we do, a JSON-indexed search handles it.*
3. Independent evidence — do we attach evidence to columns independently of tables? *No, column metadata always travels with the table.*
4. Independent lifecycle — does a column have its own version history? *No, it changes when the table changes.*

**Zero yesses. It's not a node.**

### The right shape

```python
# ✅ DO THIS
for table in catalog.tables():
    upsert_node(
        entity_id=f"table:{table.fqn}",
        entity_type="UC_TABLE",
        node_role="semantic",  # default, shown for clarity
        properties={
            "row_count": table.row_count,
            "partition_keys": table.partition_keys,
            "columns": [
                {
                    "name": col.name,
                    "type": col.type,
                    "nullable": col.nullable,
                    "comment": col.comment,
                    "tags": col.tags,
                }
                for col in table.columns
            ],
            ...
        },
    )

    # Rich schema documentation as a document, linked via edge
    upsert_document(
        doc_id=f"doc:schema:{table.fqn}",
        content=render_schema_markdown(table),
    )
    upsert_edge(
        source=f"table:{table.fqn}",
        target=f"doc:schema:{table.fqn}",
        kind="described_by",
    )

    # Table-level lineage (this is where lineage actually lives)
    for upstream in table.upstream_tables:
        upsert_edge(
            source=f"table:{table.fqn}",
            target=f"table:{upstream.fqn}",
            kind="depends_on",
        )

    # Ownership
    upsert_edge(
        source=f"table:{table.fqn}",
        target=f"team:{table.owner}",
        kind="owned_by",
    )
```

**What this produces:**
- 10K `UC_TABLE` nodes (50× fewer)
- 10K schema description documents (full-text indexed for search, optionally embedded for semantic retrieval)
- Table-level lineage edges (where lineage actually lives)
- Team ownership edges
- 10K SCD Type 2 histories — each capturing schema migrations as property-diff events ("on 2026-03-15, the columns array changed from [...] to [...]")
- Retrieval returns cohesive table-with-schema units
- Traversal follows meaningful edges, not structural plumbing

### The legitimate exception

Some data products have real column-level requirements: regulated data, dbt exposures with per-column grants, privacy tooling that needs per-column PII tags with their own audit history. For these, model **only the columns that actually participate in meaningful cross-parent relationships** as `node_role=structural`:

```python
# Legitimate: column-level PII tracking for a regulated dataset
for column in table.columns:
    if column.pii_classification:  # only PII columns, not all columns
        upsert_node(
            entity_id=f"column:{table.fqn}.{column.name}",
            entity_type="UC_COLUMN",
            node_role="structural",  # explicit: plumbing, not retrieval target
            properties={
                "pii_type": column.pii_classification,
                "masking_rule": column.masking_rule,
                "consent_basis": column.consent_basis,
            },
        )
        upsert_edge(
            source=f"column:{table.fqn}.{column.name}",
            target=f"table:{table.fqn}",
            kind="belongs_to",
        )
        upsert_edge(
            source=f"column:{table.fqn}.{column.name}",
            target=f"policy:{column.consent_basis}",
            kind="governed_by",
        )
```

This is *100 column nodes*, not *500K*. Each one passes the four-question test (independent evidence via PII classification history; independent lifecycle via policy updates; cross-parent query via "find all columns governed by GDPR Article 6"). The `node_role=structural` marker keeps them out of default retrieval so they don't pollute agent context packs when someone asks "what's in the `fact_orders` table."

---

## Worked example 2: Code repository ingestion

To show that this is not database-specific, here's the same three-role split applied to a code-search deployment ingesting a Python repository.

### The wrong shape

Treating every file, function, parameter, and import as its own node:

```python
# ❌ DON'T DO THIS
for file in repo.files():
    upsert_node(entity_type="FILE", ...)
    for func in file.functions:
        upsert_node(entity_type="FUNCTION", ...)
        upsert_edge(source=func_id, target=file_id, kind="defined_in")
        for param in func.parameters:
            upsert_node(entity_type="PARAMETER", ...)
            upsert_edge(source=param_id, target=func_id, kind="parameter_of")
        for imp in func.imports:
            upsert_node(entity_type="IMPORT", ...)
            upsert_edge(source=func_id, target=imp_id, kind="imports")
```

**Result:** a repository with 1K files, 20K functions, 80K parameters, and 100K imports becomes a graph with 201K nodes and 200K edges. Almost all of them structural plumbing.

### The right shape

```python
# ✅ DO THIS
for file in repo.files():
    upsert_node(
        entity_id=f"file:{file.path}",
        entity_type="FILE",
        node_role="semantic",
        properties={
            "path": file.path,
            "language": file.language,
            "functions": [
                {
                    "name": f.name,
                    "line": f.line,
                    "parameters": [
                        {"name": p.name, "type": p.type} for p in f.parameters
                    ],
                    "signature": f.signature,
                    "docstring": f.docstring,
                }
                for f in file.functions
            ],
            "imports": [imp.module for imp in file.imports],
        },
    )

    # File-level call graph (where traversal actually happens)
    for imported_module in file.local_imports:
        upsert_edge(
            source=f"file:{file.path}",
            target=f"file:{imported_module.path}",
            kind="imports",
        )
```

Result: 1K file nodes, 1-5K file-to-file import edges, zero structural plumbing. Retrieval returns cohesive file units with embedded function metadata. The call graph lives at the file level where it's actionable.

### The legitimate exception

If you're building a static analyzer that needs to reason about individual function calls (e.g., "which functions transitively call `deprecated_api()`?"), functions earn structural node status:

```python
# Legitimate: call-graph analysis
for func in file.functions:
    if func.is_public_api:  # only public functions, not every helper
        upsert_node(
            entity_id=f"func:{file.path}:{func.name}",
            entity_type="FUNCTION",
            node_role="structural",
            properties={"signature": func.signature, "is_deprecated": func.is_deprecated},
        )
        for called in func.calls:
            if called.is_public_api:
                upsert_edge(
                    source=f"func:{file.path}:{func.name}",
                    target=f"func:{called.file}:{called.name}",
                    kind="calls",
                )
```

Public API functions only — not every helper function, not every parameter. The four-question test earns node status via cross-parent traversal (the call graph) and independent lifecycle (deprecation tracking). Everything else stays as JSON on the file node.

---

## Worked example 3: Curated knowledge from SQL query logs

This is the canonical curated-derivation example because SQL query logs are simultaneously high-volume, low-signal-per-row, and high-signal-in-aggregate. They illustrate why curated nodes exist as a distinct role.

### The raw shape

A warehouse query log emits one row per executed query: text, runtime, user, timestamp, referenced tables. Ingesting this directly gives you a stream of `QueryExecution` entities with `reads_from` / `writes_to` edges to `Dataset` entities:

```python
# ingest one event per query execution
upsert_node(
    entity_id=f"query:{run_id}",
    entity_type="QueryExecution",
    node_role="semantic",
    properties={
        "query_text": event.sql,
        "runtime_ms": event.duration_ms,
        "user": event.user,
        "started_at": event.timestamp,
        "warehouse": event.compute,
    },
)
for table_fqn in event.read_tables:
    upsert_edge(
        source=f"query:{run_id}",
        target=f"dataset:{table_fqn}",
        kind="reads_from",
    )
for table_fqn in event.write_tables:
    upsert_edge(
        source=f"query:{run_id}",
        target=f"dataset:{table_fqn}",
        kind="writes_to",
    )
```

A million queries a day produces a million `QueryExecution` nodes a day. Most of them never get retrieved individually — and that's fine, because the *value is in the aggregate*, not in any single execution.

### The derived shape

A nightly analyzer reads the raw `QueryExecution` history and produces curated entities representing patterns:

```python
# Curated entity for a popular join pattern
upsert_node(
    entity_id="join_pattern:orders_joined_with_customers",
    entity_type="JoinPattern",
    node_role="curated",
    generation_spec={
        "generator_name": "sql_log_analyzer",
        "generator_version": "1.2",
        "generated_at": "2026-05-11T03:00:00Z",
        "source_node_ids": [...],  # the QueryExecution ids that contributed
        "parameters": {
            "lookback_days": 30,
            "min_query_count": 50,
        },
    },
    properties={
        "left_dataset": "snowflake://analytics/marts/fct_orders",
        "right_dataset": "snowflake://analytics/marts/dim_customers",
        "join_keys": ["customer_id"],
        "occurrence_count": 1847,
        "median_runtime_ms": 1250,
        "common_filters": ["order_date > current_date - 30"],
        "example_query": "SELECT ... FROM fct_orders o JOIN dim_customers c ON o.customer_id = c.customer_id WHERE ...",
        "description": (
            "Highly recurring join on customer_id, appearing in 1847 queries "
            "over the last 30 days. Typical filter narrows to recent orders. "
            "Consider materializing as a wide table if runtime becomes a "
            "bottleneck."
        ),
    },
)
upsert_edge(
    source="join_pattern:orders_joined_with_customers",
    target="dataset:snowflake://analytics/marts/fct_orders",
    kind="involves_dataset",
)
upsert_edge(
    source="join_pattern:orders_joined_with_customers",
    target="dataset:snowflake://analytics/marts/dim_customers",
    kind="involves_dataset",
)
```

The `JoinPattern` node is curated because:

1. It's **synthesized** — derived from many `QueryExecution` rows, not extracted from any single source.
2. It's **regeneratable** — running the analyzer again next month produces an updated `JoinPattern` reflecting the new month's data.
3. It's **valuable for strategic retrieval** — when an agent is asked "how do orders and customers join?", this pattern is exactly the kind of high-density content that should outrank an arbitrary `QueryExecution` from last Tuesday.
4. It's **meant to be refined** — a curator can edit `description` to add domain context ("customer_id is the only join key — do not use email even though it appears unique") without breaking the regenerator.

Other curated entities the same analyzer might produce:

- **`AccessPattern`** — "Dataset X is co-accessed with Dataset Y in 73% of queries that read X." Useful for retrieval expansion and for warehouse layout decisions.
- **`HotDataset`** — top-N most-queried datasets per domain. Boosts these in pack retrieval when the agent's objective is broad.
- **`QueryTemplate`** — parameterized query body extracted by clustering similar `QueryExecution.query_text` values. Useful as a starting point for new SQL-generation tasks.

### What goes into the graph vs what doesn't

| Thing | Where it lives | Why |
|---|---|---|
| Individual `QueryExecution` rows | Graph as semantic nodes | Cross-parent traversal: `reads_from` / `writes_to` edges to Datasets carry lineage. SCD Type 2 captures execution history. |
| `query_text` (raw SQL) | Graph property on `QueryExecution` (small) **or** Document linked via `described_by` (long queries) | The 4KB threshold applies — most queries inline, occasional 50KB analytics queries get documented. |
| Execution plans, profiling output | Blob, referenced from `QueryExecution.profile_uri` | Large, binary, infrequently retrieved. |
| `JoinPattern` / `AccessPattern` / `HotDataset` | Graph as curated nodes | Synthesized, regeneratable, retrieval-boosted. |
| `JoinPattern.description` (human-edited narrative) | Graph property on the curated node | Small, mutates only on curator edit, queried alongside the node. |
| The analyzer script itself | Source repo (not in graph) | Code, not data. Versioned via `generation_spec.generator_version`. |

### The freshness contract for derived nodes

Curated nodes inherit the freshness signals from [the freshness section](#freshness-signals-how-staleness-propagates) but with one twist: `generation_spec.generated_at` is the authoritative "when was this computed" timestamp, distinct from `valid_from` (when this version landed). An agent reading a `JoinPattern` with `generated_at` two months old should treat it as a soft signal that the underlying query mix may have shifted. The next analyzer run will produce a new version; both old and new live in the SCD Type 2 history.

When a curator edits `description` between analyzer runs, the SCD Type 2 history shows the edit but `generation_spec` is unchanged — the substantive content is human-authored, not regenerated. The next analyzer run preserves human edits or notifies the curator that a regenerated version conflicts; see `freshness-and-curation.md` for the policy.

---

## When curated nodes earn their role

Curated nodes are the least obvious of the three roles. A thing should be `node_role=curated` (rather than `semantic`) when **all** of these are true:

1. **It's synthesized, not ingested.** The node's content is derived from other nodes in the graph, from LLM output, from clustering analysis, or from human synthesis — not pulled from an external source-of-truth.
2. **It's regeneratable.** You could run the generator again and produce a new version with the same structure (possibly different content). If the node is a one-shot artifact that can't be recreated, it's probably semantic with metadata noting its origin.
3. **It's meant to be edited or refined.** Humans should be able to improve the content without breaking the system's assumptions about ingested data.
4. **It's valuable for broad/strategic retrieval.** Curated nodes exist to answer questions that ingested nodes can't answer directly — "what's the Sportsbook domain about?", "what patterns succeed most often in SQL generation?", "what are the most frequently co-accessed tables?"

Common examples:
- **Domain rollups** — a `CURATED` node named `domain:sportsbook` with a summary synthesized from all entities tagged with that domain
- **Community cluster summaries** — a `CURATED` node per graph community produced by label propagation, with an LLM-generated description
- **Promoted precedents** — a `CURATED` node created by the precedent promotion worker from successful trace patterns
- **Popular-entity indexes** — a `CURATED` node listing the most-queried or most-touched entities in a domain, regenerated daily

Not curated:
- **A trace** — it's an immutable record of something that happened. Semantic.
- **A document imported from a wiki** — it's ingested from a source. Semantic.
- **A file's README** — it exists in the source repo. Semantic.
- **A workflow run** — it's a real execution that happened. Semantic. (This is a common confusion — workflow runs feel "synthesized" because they're about the system's own behavior, but they're ingested from the trace stream, not derived from graph analysis.)

See the `trellis curate` CLI namespace in [operations.md](operations.md) (when implemented) for how to create, regenerate, edit, and diff curated nodes.

---

## Decision flowchart

```
                          ┌──────────────────┐
                          │ New information  │
                          │ to add to graph  │
                          └────────┬─────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │ Free-form text > 1 para? │
                    └──────────┬───────────────┘
                       yes ────┤──── no
                               │
                ┌──────────────┘
                ▼
      ┌──────────────────┐                   ┌─────────────────────┐
      │ DOCUMENT linked  │                   │ Passes ≥1 of the    │
      │ via edge         │                   │ four-question test? │
      └──────────────────┘                   └──────────┬──────────┘
                                                yes ────┤──── no
                                                        │      │
                                              ┌─────────┘      └─────────┐
                                              ▼                          ▼
                                    ┌────────────────┐         ┌──────────────────┐
                                    │ Is it derived  │         │ PROPERTY on      │
                                    │ from other     │         │ parent node's    │
                                    │ graph content? │         │ JSON field       │
                                    └───────┬────────┘         └──────────────────┘
                                   yes ─────┤────── no
                                            │        │
                                     ┌──────┘        └──────┐
                                     ▼                      ▼
                          ┌──────────────────┐   ┌─────────────────────┐
                          │ CURATED node     │   │ Does it earn cross- │
                          │ + generation_spec│   │ parent queries or   │
                          │                  │   │ indep. lifecycle?   │
                          └──────────────────┘   └──────────┬──────────┘
                                                    yes ────┤──── no
                                                            │      │
                                                   ┌────────┘      └────────┐
                                                   ▼                        ▼
                                          ┌──────────────┐        ┌──────────────┐
                                          │ SEMANTIC node│        │ STRUCTURAL   │
                                          │ (default)    │        │ node (opt-in │
                                          │              │        │ retrieval    │
                                          │              │        │ only)        │
                                          └──────────────┘        └──────────────┘
```

---

## Further reading

- [schemas.md](schemas.md) — field-by-field reference for `Node`, `Edge`, `NodeRole`, `GenerationSpec`
- [operations.md](operations.md) — CLI commands including `trellis admin graph-health` and `trellis curate`
- [playbooks.md](playbooks.md) — step-by-step procedures for common ingestion patterns
- [tagging-for-retrieval.md](tagging-for-retrieval.md) — how `ContentTags` and `retrieval_affinity` interact with `node_role`
- [tiered-context-retrieval.md](tiered-context-retrieval.md) — how sectioned pack assembly uses `node_role` to filter and boost content
- [extractor-authoring.md](extractor-authoring.md) — the `Extractor` Protocol contract, tier semantics, and how to ship one as a plugin
- [source-modeling-cookbook.md](source-modeling-cookbook.md) — per-source recipes for documentation, Jira, Confluence, SQL queries, Unity Catalog, and git repos
- [freshness-and-curation.md](freshness-and-curation.md) — how to keep extracted data fresh, the two refresh modes, and curator workflows

---

## Summary: the five rules

1. **Ask the four questions before creating any node.** If none answer "yes," it's a property or a document.
2. **Default to `semantic`.** Only mark something `structural` when you have a concrete reason to want it in the graph but not in default retrieval. Only mark something `curated` when it's synthesized, regeneratable, and meant to be iterated on.
3. **Don't model what the source system already tracks.** Columns have history in the catalog. Function parameters have history in git. Don't duplicate that history in the graph unless you're adding value beyond what the source has.
4. **Make curation visible.** If the graph contains synthesized content, mark it `curated` so curation tooling can find it and humans can trust what's ground truth.
5. **When in doubt, fewer nodes beats more nodes.** You can always promote a property to a node later if a real cross-parent query emerges. You cannot easily demote a 500K-node mistake without breaking every consumer who built on it.
