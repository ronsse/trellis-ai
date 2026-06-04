# Source Modeling Cookbook

> **Who this is for:** anyone about to write a new extractor or design the modeling for a domain Trellis doesn't already cover. The six sources here are the most-requested shapes. Each section is a recipe — entity types, edges, reference vs summary decisions, suggested curated derivations, refresh cadence.

> **What this is not:** built extractors. The recipes describe *what* to emit; [extractor-authoring.md](extractor-authoring.md) covers *how* to emit it. Trellis ships built extractors only for dbt manifests and OpenLineage events; everything else is on you (and shouldn't be — that's the whole point of the extractor plugin contract).

> **Before you read this:** make sure you've internalized [modeling-guide.md](modeling-guide.md). Especially the four-question test, the four-store split, and the cross-database routing convention. The recipes below assume you understand those.

> **Building a data-platform / enterprise graph?** The query-history recipes here are intentionally conservative about promotion — see [`adr-query-history-promotion.md`](../design/adr-query-history-promotion.md) (#218) for the behavioral-evidence → accepted-fact ladder and [`adr-enterprise-ontology-capability-framing.md`](../design/adr-enterprise-ontology-capability-framing.md) (#217) for how these recipes fit the wider ontology stack.

---

## How to use this cookbook

Each recipe answers four questions:

1. **What entity types and edges?** The minimum viable graph shape.
2. **What goes in the graph vs document vs blob vs reference?** Concrete inline-vs-pointer decisions per field.
3. **What curated derivations are high-value?** Optional, but usually the biggest retrieval win.
4. **What's the refresh cadence?** When to re-extract and how the system knows.

Recipes are opinionated defaults. Your real deployment will diverge — that's expected. Use the four-question test to justify divergences; don't add types just because the source has them.

---

## Recipe 1: Documentation (Markdown / wiki pages from disk)

The simplest source. A directory of `.md` files with optional frontmatter. Best for project docs, internal handbooks, ADRs that live in a repo.

### The graph shape

```
                ┌────────────────────┐
                │ Document           │  ← one per .md file
                │ - source_uri       │
                │ - title            │
                │ - tags             │
                │ - frontmatter      │
                └─────────┬──────────┘
                          │ mentions
                          ▼
                ┌────────────────────┐
                │ Concept / Person / │  ← entities mentioned in the prose
                │ Project / etc.     │
                └────────────────────┘

                ┌────────────────────┐  defines  ┌──────────────────┐
                │ Document           │──────────►│ Concept          │
                │ (glossary entry)   │           │ (curated)        │
                └────────────────────┘           └──────────────────┘

                ┌────────────────────┐ supersedes ┌──────────────────┐
                │ Document (v2)      │───────────►│ Document (v1)    │
                └────────────────────┘            └──────────────────┘
```

### Entity types

| Type | When to emit | Anchored at |
|---|---|---|
| `Document` (or `CreativeWork`) | One per `.md` file. | `source_uri = file://<repo>/<path>` |
| `Concept` *(curated, optional)* | One per glossary term distilled by a curator from prose. | `source_node_ids = [<list of Documents>]` |

A `Document` here is *both* a graph node *and* a document-store entry — the `Document` node carries metadata, the document store holds content. The two are linked by `document_id` matching on both sides.

### Edges

- `mentions`: from `Document` to any entity referenced in the body (other documents via `[[wikilinks]]` or `[text](path/to/other.md)`; people; projects; concepts). The mentioned entity must already exist as a node — `allow_dangling=True` covers cross-batch cases.
- `defines`: from a `Document` that is a glossary entry to the `Concept` it defines.
- `supersedes`: from a newer `Document` to an older one when the file's frontmatter declares `supersedes: <other-doc-id>`.
- `authored_by`: from `Document` to a `Person` (when frontmatter declares `author`).

### Content placement

| Field | Where |
|---|---|
| Body text (< 4KB) | Document store, inlined. Embeddable for semantic search. |
| Body text (> 50KB) | Document store, but consider chunking. A 200KB design doc as one document hurts retrieval ranking; split on `##` headings. |
| Frontmatter (yaml/toml header) | Parsed into node properties on the `Document` node. Don't keep the raw frontmatter text. |
| Image attachments | Blob store. `Document.properties.attachments = ["<blob-uri>", ...]`. |
| Tags from frontmatter | `Document.properties.tags` (list of strings). Also propagate to `ContentTags` via the classification pipeline. |

### Stable identity

```python
entity_id = f"doc:{path.relative_to(repo_root).as_posix()}"
# e.g. "doc:handbook/onboarding.md"
```

Path-based IDs survive file moves only if you maintain a `supersedes` edge from the new path to the old. For documentation that moves frequently, prefer a frontmatter-declared `id:` field as the canonical identity, falling back to path.

### Curated derivations

The biggest retrieval win is a **glossary**: a curator (human or LLM) reads the corpus and produces a `Concept` per important term, linking it to the `Document`s that define or substantively discuss it. Glossary `Concept`s are `node_role=curated` with `generation_spec` pointing at the source documents.

Other high-value curated derivations:

- **Topic clusters**: group documents into N curated `Topic` nodes via embedding-based clustering. Each `Topic` carries an LLM-generated summary.
- **Reading paths**: ordered sequences of documents that form a logical learning order ("if you want to understand X, read these in this order"). Useful for agent onboarding queries.

### Refresh cadence

Cheap to re-extract (filesystem walk + parse), so nightly is reasonable. Tracked by content hash: if the file hash hasn't changed, the extractor skips emission entirely. The dispatcher's `EXTRACTOR_USED` event still fires for the run, with `entities_emitted=0`, so the EventLog records that the refresh happened.

Triggered on git commit (via GHA / pre-commit hook) for repos that prioritize freshness over batch latency.

---

## Recipe 2: Jira

Tickets, projects, sprints. Hierarchical and high-mutation. Most of the field set stays as Jira's; the graph captures only the relationships that traverse.

### The graph shape

```
        ┌──────────────────┐
        │ Project          │
        └────────┬─────────┘
                 │ has_issue
                 ▼
        ┌──────────────────┐  blocks    ┌──────────────────┐
        │ Issue            │───────────►│ Issue (other)    │
        │ - status         │            └──────────────────┘
        │ - priority       │
        │ - labels         │            ┌──────────────────┐
        └─┬──┬─────────────┘ relates_to │ Issue            │
          │  │                ─────────►└──────────────────┘
          │  │ assigned_to
          │  ▼
          │ ┌──────────────────┐
          │ │ Person           │
          │ └──────────────────┘
          │
          │ mentions
          ▼
        ┌──────────────────┐
        │ Dataset / File / │  ← graph entities the ticket refers to
        │ Repository       │
        └──────────────────┘
```

### Entity types

| Type | When to emit |
|---|---|
| `Issue` | One per Jira ticket. |
| `Project` | One per Jira project. |
| `Person` | One per Jira user who appears as assignee, reporter, or commenter. |
| `Sprint` *(optional)* | Only if your queries traverse sprints — many deployments find this unnecessary. |

Note what's *not* a node: comments (they live as document-store entries linked via `commented_on`), labels (properties on `Issue`), workflow status (property), fix versions (property unless your queries cross versions).

### Edges

- `has_issue`: from `Project` to `Issue`.
- `blocks` / `relates_to` / `caused_by` / `duplicates`: from `Issue` to `Issue` (Jira's "issue links").
- `assigned_to`: from `Issue` to current assignee `Person`. Historical assignees are captured in SCD Type 2 history of the property.
- `reported_by`: from `Issue` to `Person`.
- `mentions`: from `Issue` to any external entity (Dataset, Repository, File) parsed out of the ticket body. The mentioning is high-value for cross-domain retrieval — "find tickets that touch `fct_orders`" should work.
- `commented_on`: from a comment `Document` to its parent `Issue`.

### Content placement

| Field | Where |
|---|---|
| Issue title | `Issue.properties.title` |
| Issue body | Document store, linked via `described_by`. Body < 4KB inline; longer bodies still inline unless you have thousands of long-form tickets. |
| Comments | Document store, one per comment. `commented_on` edge. Comments accumulate unboundedly; consider TTL via blob/document retention policy. |
| Status, priority, labels, fix_version, components, custom fields | Properties on `Issue`. |
| Attachments | Blob store. `Issue.properties.attachments = [{"name": ..., "uri": ...}, ...]`. |
| Transition history (status changes) | Property on `Issue` (`status_history: [...]`); the property's SCD-2 history captures changes at node granularity. |

### Stable identity

```python
entity_id = f"jira:{issue_key}"  # e.g. "jira:ENG-1234"
```

Jira keys are stable across moves between projects (the key tracks the issue, not the project), so this survives reorgs.

### Curated derivations

- **`Incident` curated nodes**: a curator reviewing tickets tagged `incident` or in a specific project produces curated `Incident` nodes that aggregate timeline, root cause, affected services. Often more useful for retrieval than the raw tickets.
- **Service health rollups**: count incidents per Dataset / Service over the last N days; emit as a curated `ServiceHealth` node refreshed weekly.
- **Recurring-problem patterns**: cluster `Issue` bodies via embedding, emit curated `ProblemPattern` nodes for clusters above threshold.

### Refresh cadence

Jira's API supports webhook-driven push (Atlassian Connect, Forge apps). Recommended pattern: app pushes change events to `POST /api/v1/extract/drafts`, which routes through the same extractor as the periodic re-extract. No polling.

Periodic re-extract still useful for nightly catch-up. Refresh per-project rather than full-corpus — extract scope is naturally projectful.

---

## Recipe 3: Confluence

Closest cousin to the Markdown recipe but with auth, hierarchy, and edit history baked in.

### The graph shape

```
        ┌──────────────────┐
        │ Space            │
        └────────┬─────────┘
                 │ contains
                 ▼
        ┌──────────────────┐  parent_of   ┌──────────────────┐
        │ Document         │─────────────►│ Document (child) │
        │ (page)           │              └──────────────────┘
        └────────┬─────────┘
                 │ mentions
                 ▼
        ┌──────────────────┐
        │ Issue / Person / │
        │ Dataset / etc.   │
        └──────────────────┘

        ┌──────────────────┐ supersedes  ┌──────────────────┐
        │ Document (v3)    │────────────►│ Document (v2)    │
        └──────────────────┘             └──────────────────┘
```

### Entity types

| Type | When to emit |
|---|---|
| `Space` | One per Confluence space. |
| `Document` | One per page, in the *current* revision. Old revisions live as SCD-2 history *on the same node*, not as separate nodes — Confluence already keeps revision history, Trellis just snapshots the active version. |
| `Person` | Page author + editors. |

What's *not* a node: comments (document-store entries), inline labels (properties on `Document`), individual revisions (use SCD-2 history).

### Edges

- `contains`: from `Space` to `Document` (top-level pages).
- `parent_of`: from `Document` to child `Document`. Confluence pages have hierarchy; this captures it.
- `authored_by`, `last_edited_by`: from `Document` to `Person`.
- `mentions`: from `Document` to any entity referenced in the body. Especially valuable for cross-system retrieval — "find Confluence pages about `fct_orders`."
- `supersedes`: from a `Document` to an older `Document` it explicitly replaces (via `supersedes` macro or a curator-applied edge). Not for normal page revisions — those are SCD-2 history.

### Content placement

| Field | Where |
|---|---|
| Page body (Confluence storage format) | Document store. Convert to Markdown for inline content; keep the storage-format-as-blob for fidelity if needed. |
| Page metadata (labels, restrictions, creator, last-edited timestamps) | Properties on `Document`. |
| Inline images | Blob store. References from document content rewritten to blob URIs. |
| Comments | Document store, one per comment thread. `commented_on` edge. |
| Page restrictions (who can view) | Properties on `Document`; consider modeling as a `DataClassification` if your deployment policy-gates retrieval by access list. |

### Stable identity

```python
entity_id = f"confluence:{space_key}:{page_id}"
# e.g. "confluence:ENG:1234567"
```

Confluence page IDs are stable across moves between spaces; the space key changes but the page ID does not. The composed ID surfaces space context but the canonical lookup is by page ID — consider an alias `confluence:{page_id}` that resolves to the current `(space_key, page_id)` pair.

### Curated derivations

- **`RunbookIndex`**: curator-maintained index of operational runbooks, with quick links and tag-based filtering. Refreshed when new pages tagged `runbook` appear.
- **`Glossary`**: same as the Markdown recipe — curated `Concept` nodes per distilled term.
- **Topic clusters**: especially valuable for large Confluence spaces; clusters surface unexpected adjacencies.

### Refresh cadence

Atlassian Connect / Forge apps can push change events. Same pattern as Jira: webhook → `POST /api/v1/extract/drafts`. For deployments without app installation, periodic re-extract per-space (queue by `last_updated > <last_refresh>`).

---

## Recipe 4: SQL query logs

The high-leverage source. Raw query logs are individually low-value; their aggregate is *the most useful retrieval signal in the data platform*. This recipe is detailed because the curated derivation layer is where most of the value lives.

### The graph shape

```
        ┌──────────────────┐  reads_from  ┌──────────────────┐
        │ QueryExecution   │─────────────►│ Dataset          │
        │ - text           │              └──────────────────┘
        │ - runtime_ms     │
        │ - user           │  writes_to   ┌──────────────────┐
        │ - started_at     │─────────────►│ Dataset          │
        └──────────────────┘              └──────────────────┘

        ┌──────────────────┐ involves     ┌──────────────────┐
        │ JoinPattern      │─────────────►│ Dataset (left)   │
        │ (curated)        │              └──────────────────┘
        │ - join_keys      │ involves     ┌──────────────────┐
        │ - occurrence ct  │─────────────►│ Dataset (right)  │
        │ - example_query  │              └──────────────────┘
        │ - description    │
        └──────────────────┘

        ┌──────────────────┐ co_accesses  ┌──────────────────┐
        │ AccessPattern    │─────────────►│ Dataset          │
        │ (curated)        │              └──────────────────┘
        └──────────────────┘
```

### Raw entity types

| Type | When to emit |
|---|---|
| `QueryExecution` | One per executed query. High volume — millions per day is normal. |

### Raw edges

- `reads_from`: from `QueryExecution` to the `Dataset`(s) it read.
- `writes_to`: from `QueryExecution` to the `Dataset`(s) it wrote.
- `executed_by`: from `QueryExecution` to a `Person` (or `Service` for automated jobs).

`reads_from` and `writes_to` use `allow_dangling=True` — datasets are often emitted by a different extractor (dbt, Unity Catalog) in a separate batch.

### Content placement

| Field | Where |
|---|---|
| `query_text` (short, < 4KB) | Inline as property on `QueryExecution`. |
| `query_text` (long, > 4KB) | Document store, linked via `described_by`. Skip embedding unless your retrieval includes "find queries similar to this one" — embeddings on noisy individual queries are not high-signal. |
| Execution plan / profile | Blob store. `QueryExecution.properties.profile_uri = "..."`. |
| Result row count, scanned bytes, cost estimate | Properties on `QueryExecution`. |

### Stable identity

```python
entity_id = f"query:{warehouse}:{run_id}"
# e.g. "query:snowflake:01abc-2026-05-11-..."
```

Warehouse-supplied run IDs are stable. If your warehouse doesn't provide one, hash `(query_text, started_at_epoch, user)` — collisions are tolerable because aggregate value comes from the derivations, not from individual rows.

### Curated derivations — the high-value layer

A nightly (or hourly, depending on volume) analyzer reads the raw `QueryExecution` history and produces curated entities. This is where SQL log ingestion earns its place.

**`JoinPattern`** — emitted when N or more `QueryExecution`s in a window join the same pair of datasets on the same keys:

```python
{
  "entity_id": "join_pattern:snowflake:fct_orders⨯dim_customers:customer_id",
  "entity_type": "JoinPattern",
  "node_role": "curated",
  "generation_spec": {
    "generator_name": "sql_log_analyzer",
    "generator_version": "1.2",
    "generated_at": "2026-05-11T03:00:00Z",
    "source_node_ids": ["query:snowflake:01abc...", ...],  # the contributing queries
    "parameters": {"lookback_days": 30, "min_query_count": 50}
  },
  "properties": {
    "left_dataset": "dataset:snowflake://analytics/marts/fct_orders",
    "right_dataset": "dataset:snowflake://analytics/marts/dim_customers",
    "join_keys": ["customer_id"],
    "join_type": "INNER",
    "occurrence_count": 1847,
    "median_runtime_ms": 1250,
    "p95_runtime_ms": 4800,
    "common_filters": ["order_date > current_date - 30"],
    "example_query": "SELECT ... FROM fct_orders o JOIN dim_customers c ON ...",
    "description": "..."  # editable by curator
  }
}
```

**`AccessPattern`** — pairs of datasets co-accessed in the same query above threshold:

```python
{
  "entity_type": "AccessPattern",
  "properties": {
    "datasets": ["dataset:...fct_orders", "dataset:...dim_products"],
    "co_access_count": 920,
    "co_access_ratio": 0.73,  # of queries that read fct_orders, 73% also read dim_products
    "typical_relationship": "join_on_product_id"
  }
}
```

**`HotDataset`** — top-N most-queried datasets per domain over a rolling window. Boosted in retrieval for broad/strategic queries.

**`QueryTemplate`** — clusters of similar `QueryExecution.query_text` parameterized into a reusable template. Useful as starting points for SQL-generation tasks; agents retrieve templates instead of inventing from scratch.

### Curated derivation cadence

- `JoinPattern`, `AccessPattern`: regenerated nightly via the analyzer. Generators are idempotent — the same input window produces the same patterns.
- `HotDataset`: refreshed weekly, often.
- `QueryTemplate`: monthly is plenty; clustering is expensive and slow-moving.

The analyzer is itself a separate piece of code (not an extractor — it doesn't ingest from an external source). It reads from the graph, computes patterns, and submits curated drafts through `MutationExecutor`. See [freshness-and-curation.md](freshness-and-curation.md) for the curator-script pattern.

### Refresh cadence (raw)

Push-driven via warehouse query-log streams (Snowflake's `QUERY_HISTORY` view, Databricks' `system.query.history`, BigQuery's INFORMATION_SCHEMA jobs). Stream → extractor → `POST /api/v1/extract/drafts`.

If push is unavailable, periodic poll with `started_at > <last_run>` bookmark. Hourly is reasonable for analytics warehouses.

### Retention

Raw `QueryExecution` rows balloon. The retention rule of thumb: keep raw rows for the lookback window your curated derivations need (e.g., 90 days), then archive. The curated entities reference `source_node_ids` — the references become dangling once raw rows are archived, but `Lifecycle.state="archived"` on the raw rows preserves them for audit.

---

## Recipe 5: Unity Catalog (Databricks metadata)

Authoritative source for Databricks-platform table shape. Treat it as a *snapshot*, not a live mirror — periodic re-extraction, not query-time pulls.

### The graph shape

```
        ┌──────────────────┐
        │ Catalog          │
        └────────┬─────────┘
                 │ contains
                 ▼
        ┌──────────────────┐
        │ Schema           │
        └────────┬─────────┘
                 │ contains
                 ▼
        ┌──────────────────┐  lineage_to  ┌──────────────────┐
        │ Dataset (Table)  │─────────────►│ Dataset          │
        │ - schema_name    │              └──────────────────┘
        │ - database_name  │
        │ - source_system  │  owned_by    ┌──────────────────┐
        │ - physical_uri   │─────────────►│ Team / Person    │
        │ - columns (JSON) │              └──────────────────┘
        └────────┬─────────┘
                 │ described_by
                 ▼
        ┌──────────────────┐
        │ Document         │  ← Markdown rendering of table schema + comments
        └──────────────────┘
```

### Entity types

| Type | When to emit |
|---|---|
| `Catalog` | One per UC catalog. |
| `Schema` | One per UC schema within a catalog. |
| `Dataset` (or `UC_TABLE` if you prefer the namespaced shape) | One per table / view / materialized view / streaming table. |

**Do NOT emit a node per column.** Columns are properties on `Dataset` (see *Schema explosion* anti-pattern in [modeling-guide.md](modeling-guide.md)). The exception: regulated columns with their own PII / governance lifecycle — see the modeling guide's worked example #1 for the structural-node pattern.

### Edges

- `contains`: `Catalog` → `Schema`, `Schema` → `Dataset`.
- `lineage_to` / `lineage_from`: between `Dataset`s. Unity Catalog tracks table lineage via the `system.access.table_lineage` view; this is the same `reads_from` / `writes_to` shape as OpenLineage but already aggregated.
- `owned_by`: `Dataset` → `Team` / `Person` (from UC ownership metadata).
- `governed_by`: `Dataset` → `Policy` (when UC has data governance tags). Optional, only if your deployment models policies as graph nodes.
- `described_by`: `Dataset` → `Document` (the rendered schema documentation).

### Content placement

| Field | Where |
|---|---|
| Routing properties (`source_system="databricks"`, `database_name`, `schema_name`, `physical_uri="databricks://workspace/<catalog>/<schema>/<table>"`) | Properties on `Dataset`. **Required for query routing.** |
| Column list (name, type, nullable, comment, tags) | JSON array as `Dataset.properties.columns`. |
| Partition keys, clustering keys | Properties on `Dataset`. |
| Row count, table size, last_modified | Properties — but refresh-volatile. Consider tracking the `last_seen_at` timestamp so consumers know how fresh the metric is. |
| Table comment | Inline if short; document store + `described_by` edge if it's long-form. |
| Sample data | **Blob store**, not graph. Per-table samples can be > 10MB; do not stuff into the graph. |

### Stable identity

```python
entity_id = f"dataset:databricks://{workspace}/{catalog}/{schema}/{table}"
```

UC tables can be renamed within a schema; rename produces a `supersedes` edge from new name to old, and both versions live in SCD-2 history of the old identity until the old name's `valid_to` is set.

### Curated derivations

- **`DataMart` rollups**: curated `DataMart` nodes that group related `Dataset`s by business domain. Curator-maintained or LLM-generated from schema comments + table-name clustering.
- **Quality scores**: combine UC's `expectations` results + dbt test results + freshness signals into a curated `DataQuality` node per dataset. Refreshed daily.
- **`HotDataset`**, **`JoinPattern`**, etc. — same as the SQL query log recipe, but driven by UC's query history view rather than raw warehouse logs.

### Refresh cadence

Unity Catalog has no event stream — pull-only. Periodic re-extraction (`trellis extract refresh --source unity-catalog`) on a schedule. Daily is normal; faster cadences for environments where schema changes are common.

Schema changes (column added, table renamed) produce `TAGS_REFRESHED` events with the structured diff, which agents can consume to invalidate cached pack content.

---

## Recipe 6: Git repositories

The classic graph-friendly source: commits, files, authors, repos. The temptation is to model too much — see modeling-guide.md worked example #2 for the function-and-parameter anti-pattern.

### The graph shape

```
        ┌──────────────────┐
        │ Repository       │
        └────────┬─────────┘
                 │ contains
                 ▼
        ┌──────────────────┐  modifies   ┌──────────────────┐
        │ File             │◄────────────│ Commit           │
        │ - path           │             │ - sha            │
        │ - language       │             │ - timestamp      │
        │ - functions (JSON)│            │ - message        │
        └────────┬─────────┘             └────────┬─────────┘
                 │ described_by                    │ authored_by
                 ▼                                 ▼
        ┌──────────────────┐             ┌──────────────────┐
        │ Document         │             │ Person           │
        │ (README content) │             └──────────────────┘
        └──────────────────┘
```

### Entity types

| Type | When to emit |
|---|---|
| `Repository` | One per git repo. |
| `File` | One per file in HEAD. Files removed in HEAD live as SCD-2 history of a previously-emitted `File` with `Lifecycle.state="archived"`. |
| `Commit` | One per commit. Immutable evidence record — never modify. |
| `Person` | One per author / committer. |

**What's NOT a node:** functions, classes, parameters, imports, lines. These go into `File.properties.functions` as a JSON array if you need them indexed; otherwise omit. Code-search deployments with real static-analysis needs may promote *public-API* functions to structural nodes — see modeling-guide.md worked example #2.

### Edges

- `contains`: `Repository` → `File`.
- `modifies`: `Commit` → `File` (the files touched by the commit). High fan-out; deduplicate aggressively.
- `authored_by`: `Commit` → `Person`.
- `imports`: `File` → `File` (when language allows static import resolution). Cross-repository imports use `allow_dangling=True`.
- `described_by`: `Repository` → `Document` (README); `File` → `Document` (per-file docs, only when valuable).

### Content placement

| Field | Where |
|---|---|
| File metadata (path, language, line count, last_modified) | Properties on `File`. |
| File content for arbitrary source code | **Don't store.** Reference the repo via `Repository.properties.clone_uri`; agents that need source content clone or fetch as needed. Trellis is not a code mirror. |
| README, ARCHITECTURE, CONTRIBUTING, similar curated documentation | Document store, `described_by` edge. These earn inline-document status because they're prose. |
| Commit messages | Inline on `Commit.properties.message`. |
| Commit diffs | **Don't store.** Reference the repo for diff retrieval. |
| Tags / branches | Properties on `Repository` (list of names) or `Commit` (for tags pointing at it). |

### Stable identity

```python
# Repos
entity_id = f"repo:{provider}:{owner}:{name}"
# e.g. "repo:github:anthropics:claude-code"

# Files (path scoped to repo)
entity_id = f"file:{repo_id}:{path}"
# e.g. "file:repo:github:anthropics:claude-code:src/main.py"

# Commits
entity_id = f"commit:{repo_id}:{sha}"
```

File paths change over time. When the extractor detects a rename, it emits a `supersedes` edge from the new-path `File` to the old-path `File`. Git's rename detection is heuristic; perfect tracking requires using git's own rename detection rather than path-string-matching.

### Curated derivations

- **`ArchitectureSummary`**: curator-maintained or LLM-generated narrative of the repository's structure. Refreshed when significant changes land.
- **Author rollups**: `Person` properties capturing "primary owner of these files" derived from blame statistics. Refreshed weekly.
- **`HotFile`**: files modified by recent commits above a threshold. Useful for "where is current activity?" queries.

### Refresh cadence

Push-driven via webhook (`GitHub Actions`, `GitLab CI`) is ideal — commits trigger an extractor run scoped to the affected files. Periodic full-repo re-extract weekly for catch-up.

For very large monorepos, scope extractor invocations to changed directories rather than the whole repo. The extractor accepts a `since_commit` parameter and processes the diff.

---

## Cross-cutting patterns

### `mentions` edges connect everything

Every recipe above emits `mentions` edges from `Document` / `Issue` / `Commit` content to graph entities. These edges are what make Trellis interesting — they're how an agent asks "what tickets reference `fct_orders`?" and gets results across Jira, Confluence, git commit messages, query comments, all at once.

Implement `mentions` extraction as a *separate pass* after the source-specific extractor runs. Read all `Document` content from the document store, run a mention-detection extractor (deterministic for fully-qualified IDs like `JIRA-123` or `dataset:...`; LLM for prose mentions), emit `mentions` edges as a second-stage extraction.

### Stale data is normal; invent the freshness contract once

Every source needs a refresh story. Codify it once — see [freshness-and-curation.md](freshness-and-curation.md) — and apply it consistently. Per-source ad-hoc refresh scripts always drift.

### Routing properties are the cross-source contract

Every dataset-shaped entity across the recipes above carries `source_system`, `database_name`, `schema_name`, and `physical_uri` per [modeling-guide.md](modeling-guide.md#cross-database-routing-properties-for-queryable-datasets). A `mentions` edge from a Confluence page to a UC dataset becomes useful precisely *because* the routing properties on the dataset tell the agent how to query it.

---

## Further reading

- [modeling-guide.md](modeling-guide.md) — the foundational decisions
- [extractor-authoring.md](extractor-authoring.md) — how to build the extractor that emits these shapes
- [freshness-and-curation.md](freshness-and-curation.md) — how to keep them fresh
- [`src/trellis_workers/extract/dbt_manifest.py`](../../src/trellis_workers/extract/dbt_manifest.py), [`openlineage.py`](../../src/trellis_workers/extract/openlineage.py) — reference implementations of the dbt + lineage recipes
