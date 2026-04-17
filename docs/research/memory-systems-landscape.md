# Research: Agent Memory Systems Landscape

**Date:** 2026-04-11
**Status:** Baseline reference — captures the state of competing systems as of this date for positioning Trellis against the landscape.

**Sources (read directly during research):**
- [getzep/graphiti](https://github.com/getzep/graphiti) — `graphiti_core/graphiti.py`, `nodes.py`, `edges.py`, `search/search_config.py`, `search/search_config_recipes.py`, `utils/maintenance/node_operations.py`, `utils/maintenance/edge_operations.py`, `utils/maintenance/community_operations.py`, `driver/`, `server/graph_service/`, `mcp_server/`
- [Zep concepts](https://help.getzep.com/concepts), [getzep.com](https://www.getzep.com/)
- [mem0ai/mem0](https://github.com/mem0ai/mem0), [Mem0 Memory Operations](https://docs.mem0.ai/core-concepts/memory-operations)
- [letta-ai/letta](https://github.com/letta-ai/letta), [Letta memory blocks](https://docs.letta.com/guides/agents/memory-blocks)
- [topoteretes/cognee](https://github.com/topoteretes/cognee), [cognee core concepts](https://docs.cognee.ai/core-concepts)
- [How Cursor Indexes Codebases Fast](https://read.engineerscodex.com/p/how-cursor-indexes-codebases-fast), [Securely indexing large codebases](https://cursor.com/blog/secure-codebase-indexing)
- [Claude Code memory](https://code.claude.com/docs/en/memory), [Claude Code skills](https://code.claude.com/docs/en/skills)

**Related internal docs:**
- [`client-layer-inventory.md`](./client-layer-inventory.md) — Trellis's own client + core layer inventory (the "us" side of the comparison)
- [`../design/adr-deferred-cognition.md`](../design/adr-deferred-cognition.md) — The architectural principle this research informed

---

## 1. Executive summary

Trellis sits in a unique cell of a 2×2: **structured graph + no LLM in the write path**. Every other system in the landscape either uses an LLM at write time (Graphiti, Zep, Mem0, cognee) or uses flat unstructured memory (Mem0, Letta, Cursor, Claude Code). That cell is our wedge.

| | LLM in write path | No LLM in write path |
|---|---|---|
| **Structured graph** | Graphiti, Zep, cognee | **Trellis** |
| **Flat memory** | Mem0 | Letta (agent self-edits), Cursor (AST chunks), Claude Code (files) |

Three defensible architectural differentiators for Trellis:

1. **Governed 5-stage write pipeline** — `validate → policy → idempotency → execute → emit event`. Nobody else in the landscape has this shape. Every other system treats writes as fire-and-forget or LLM-arbitrated.
2. **SCD Type 2 node history with role immutability** — Graphiti historizes *edges*; Trellis historizes *nodes* via explicit `version_id` rows with `valid_from`/`valid_to`, and the `NodeRole` taxonomy is immutable across versions. Combined with `GenerationSpec`, this gives the cleanest provenance story in the space.
3. **Deterministic-first classification with effectiveness feedback** — Four deterministic classifiers run in microseconds at ingest, LLM only fires in enrichment mode, and `apply_noise_tags()` closes the loop from retrieval outcomes back into classification. Nobody else ships this closed loop.

Areas where the landscape is legitimately ahead and we should move:
- **Reranker sophistication** — Graphiti wins (cross-encoder, RRF, MMR, node-distance, episode-mentions rerankers). We have simple weighted sums.
- **LLM-driven fact extraction for dialogue** — Zep/Mem0/Graphiti win. Valid trade-off; we should ship this as an *enrichment worker*, never in the core write path.
- **Skill discoverability** — Claude Code wins with markdown-frontmatter skills. Our `trellis_sdk/skills.py` should evolve toward file-based skills.

---

## 2. Graphiti (getzep/graphiti) — the closest comparable

Graphiti is the system most architecturally adjacent to Trellis, and the one this research was originally prompted by. Both build a typed knowledge graph from incremental source events and support hybrid retrieval. Below the surface, the worldviews diverge hard.

### 2.1 Core data model

Graphiti is built around four primitive node classes in `graphiti_core/nodes.py`, all Pydantic `BaseModel` subclasses inheriting from an abstract `Node(BaseModel, ABC)` with fields `uuid, name, group_id, labels, created_at`:

- **`EntityNode`** — adds `name_embedding: list[float] | None`, `summary: str`, `attributes: dict[str, Any]`.
- **`EpisodicNode`** — Graphiti's equivalent of a Trellis trace. Fields: `source: EpisodeType` (enum: `message`/`text`/`json`), `source_description`, `content`, `valid_at: datetime`, `entity_edges: list[str]`.
- **`CommunityNode`** — derived cluster node with `name_embedding`, `summary`.
- **`SagaNode`** — groups sequences of episodes (`first_episode_uuid`, `last_episode_uuid`, `last_summarized_at`).

Edges live in `graphiti_core/edges.py`:

- **`EntityEdge`** — the "fact" edge: `name` (predicate), `fact` (natural-language fact string), `fact_embedding`, `episodes: list[str]` (provenance), `valid_at`, `invalid_at`, `expired_at`, `reference_time`, `attributes: dict[str, Any]`.
- **`EpisodicEdge`** — `MENTIONS`-style edge linking an episode to the entities it produced.
- **`CommunityEdge`** — `HAS_MEMBER` edges.
- **`HasEpisodeEdge`** / **`NextEpisodeEdge`** — structural edges for saga sequencing.

**Schema format:** Pydantic v2 (`ConfigDict(validate_assignment=True)`). **Extras are not explicitly forbidden** — unlike our `TrellisModel(extra="forbid")`, Graphiti models are lenient, and much of the "schema" lives inside `attributes: dict[str, Any]` on nodes and edges.

**Custom types:** users pass `entity_types: dict[str, type[BaseModel]]` and `edge_types: dict[str, type[BaseModel]]` plus an `edge_type_map: dict[tuple[str,str], list[str]]` directly to `add_episode()`. These are Pydantic models the LLM extraction prompts are conditioned on — this is Graphiti's "prescribed ontology" story. `graphiti_core/utils/ontology_utils/` validates them.

**Episode immutability:** not enforced. `Graphiti.remove_episode()` exists, and `add_episode` does not short-circuit on duplicate `uuid`.

### 2.2 Temporal model — bi-temporal on edges only

Graphiti's bi-temporal claim comes from `EntityEdge` carrying **two time axes**:

- **Transaction time:** `created_at` (when written to graph), `expired_at` (when marked superseded in graph).
- **Valid time:** `valid_at` (when fact became true in the world), `invalid_at` (when it stopped), `reference_time` (the episode's reference_time).

Invalidation is **not a tombstone and not a new version row**. In `utils/maintenance/edge_operations.py`, `resolve_edge_contradictions()` mutates the existing edge in place: `edge.invalid_at = resolved_edge.valid_at`, `edge.expired_at = utc_now()`. The old row is preserved with these timestamps; a new `EntityEdge` is written alongside.

**Critical divergence from Trellis:** this historizes edges but **not nodes**. `EntityNode` has only `created_at` — no `valid_from/valid_to`, no version chain, no `get_node_history()` equivalent. Entity summaries and attributes are overwritten in place. Trellis does the opposite: nodes get full SCD Type 2 versioning; edges are always current.

- **Graphiti historizes edges.** Good for "when did Alice start working at Acme" (fact-centric chat memory).
- **Trellis historizes nodes.** Good for "what did this service's properties look like on 2025-11-01" (entity-centric audit).

Neither is fully bi-temporal in the strict sense. They've each picked the half that fits their use case.

### 2.3 Ingestion pipeline — LLM-heavy and in-the-hot-path

`Graphiti.add_episode()` in `graphiti_core/graphiti.py`:

```python
async def add_episode(
    self, name, episode_body, source_description, reference_time,
    source=EpisodeType.message, group_id=None, uuid=None,
    update_communities=False, entity_types=None, excluded_entity_types=None,
    previous_episode_uuids=None, edge_types=None, edge_type_map=None,
    custom_extraction_instructions=None, saga=None, saga_previous_episode_uuid=None,
) -> AddEpisodeResults: ...
```

Stages:

1. `validate_entity_types()`, `validate_excluded_entity_types()`, `validate_group_id()` — schema-level only.
2. `retrieve_episodes()` — pull previous context episodes.
3. `extract_nodes()` — **LLM extraction** of entities from the episode body (prompts vary by `EpisodeType`).
4. `resolve_extracted_nodes()` in `utils/maintenance/node_operations.py` — dedup via `_collect_candidate_nodes()` / `_semantic_candidate_search()` (cosine embeddings, threshold ≈0.6), then `_resolve_with_similarity()` for deterministic matches, falling back to `_resolve_with_llm()` for ambiguous cases.
5. `extract_edges()` — **LLM extraction** of relationships.
6. `resolve_edge_pointers()` + `resolve_extracted_edges()` — edge dedup, plus `resolve_edge_contradictions()` (**LLM-driven**) which expires conflicting older edges.
7. `extract_attributes_from_nodes()` — **second LLM pass** to hydrate entity summaries and typed attributes.
8. `_process_episode_data()` → `add_nodes_and_edges_bulk()` persists in one transaction.
9. If `update_communities=True`, `update_community()` updates affected communities.

**Four to five LLM roundtrips per episode**, all synchronous-inside-the-async-call.

**Idempotency:** none. Submitting the same episode twice runs the whole pipeline twice and relies on downstream dedup to collapse nodes — but the raw `EpisodicNode` content will be written twice.

**Policy gates:** none. No permission check, no governed-pipeline layer, no domain event emission. `group_id` is the only multi-tenancy primitive and it's purely advisory — callers set it, queries filter on it. The `telemetry/` module is usage telemetry, not a domain event log.

### 2.4 Retrieval — composable config, no pack assembler

Two entry points on `Graphiti`:

- `search(query, center_node_uuid=None, group_ids=None, num_results=..., search_filter=None) -> list[EntityEdge]` — convenience, returns edges only.
- `search_(query, config: SearchConfig = COMBINED_HYBRID_SEARCH_CROSS_ENCODER, ...) -> SearchResults` — full-power, returns nodes, edges, episodes, communities.

`SearchConfig` composes four sub-configs: `edge_config`, `node_config`, `episode_config`, `community_config`, plus `limit` and `reranker_min_score`. Each selects from enums:

- **Methods:** `cosine_similarity`, `bm25`, `breadth_first_search` (nodes/edges); `bm25` only for episodes; `cosine_similarity`/`bm25` for communities.
- **Rerankers:** `reciprocal_rank_fusion`, `mmr`, `cross_encoder`, `node_distance`, `episode_mentions`.

Prebuilt recipes in `search_config_recipes.py`: `COMBINED_HYBRID_SEARCH_RRF`, `COMBINED_HYBRID_SEARCH_MMR`, `COMBINED_HYBRID_SEARCH_CROSS_ENCODER`, plus per-entity variants. Cross-encoder reranking is pluggable via `CrossEncoderClient` under `graphiti_core/cross_encoder/`.

**No context-pack assembler.** `search_` returns raw `SearchResults`. No `max_tokens` budget stage, no two-stage `max_items → max_tokens` trimming, no `PACK_ASSEMBLED` event, no tag-filter pre-stage, no `selection_reason` on results. Callers format results themselves.

This is one of the biggest divergences. Graphiti gives you better rerankers; Trellis gives you a better pack shape.

### 2.5 Storage backends — graph-DB-first

`graphiti_core/driver/` contains `driver.py` (abstract `GraphDriver`), `neo4j_driver.py`, `falkordb_driver.py`, `kuzu_driver.py`, `neptune_driver.py`.

**There is only one store abstraction: the graph driver.** No separate trace store, document store, blob store, event log, or vector store. Everything — episodes, entities, edges, embeddings, communities — lives in the graph DB as nodes/edges with properties. Embeddings are list-of-float properties indexed via the underlying DB's vector index (Neo4j vector, FalkorDB vector, Kuzu, or Neptune + OpenSearch Serverless for full-text).

**Biggest architectural difference from Trellis:** Graphiti is graph-DB-first with everything collapsed into the graph; Trellis has six ABCs (Trace/Document/Graph/Vector/Event/Blob) that can be backed by different engines.

### 2.6 Client / SDK surface

- **Python SDK:** one class, `Graphiti` in `graphiti_core/graphiti.py`. Async-only. Public methods: `add_episode`, `add_episode_bulk`, `add_triplet`, `build_communities`, `build_indices_and_constraints`, `close`, `search`, `search_`, `retrieve_episodes`, `summarize_saga`, `remove_episode`, `get_nodes_and_edges_by_episode`.
- **REST server:** `server/graph_service/` — FastAPI with two routers (`ingest.py`, `retrieve.py`). No curate, no admin/health. No auth.
- **MCP server:** `mcp_server/` — wraps episode add/retrieve/delete, entity ops, search, group management, graph maintenance. Exact tool names unverified.
- **CLI:** none.
- **Skills / agent recipes:** none.

Agent journey: (a) `await graphiti.add_episode(...)` runs the 8-stage LLM pipeline synchronously and returns extracted nodes/edges, (b) `await graphiti.search_(query, config=...)` returns `SearchResults`, (c) entity merging happens automatically inside `add_episode` — there's no explicit `merge_entity` API.

### 2.7 Communities and derived knowledge

`utils/maintenance/community_operations.py` implements community detection via **label propagation** (explicitly not Louvain/Leiden). `get_community_clusters()` builds a projection, `label_propagation()` iterates until stable. `build_community()` generates a `CommunityNode` via hierarchical pairwise LLM summarization: `summarize_pair()` merges summaries two at a time, `generate_summary_description()` names the final summary.

**No "precedent" or distilled-playbook concept.** Communities are clusters of co-related entities, not reusable patterns.

**No effectiveness tracking or feedback loop.** No equivalent to `apply_noise_tags()` or `PACK_ASSEMBLED` telemetry closing the loop from retrieval outcomes back into classification.

### 2.8 What Graphiti does NOT do

- No governed mutation pipeline (`validate → policy → idempotency → execute → emit`).
- No policy gates or permission checks.
- No immutable trace log — `remove_episode()` exists, duplicate uuids are not blocked.
- No idempotency check.
- No content classification tags (`ContentTags(domain, content_type, scope, signal_quality)`).
- No noise detection, no quality filtering.
- No multi-store abstraction.
- No context-pack assembler with token budgets.
- No domain event log.
- No CLI.
- Node-level temporal history is weaker (edges historized, nodes overwritten).

### 2.9 Ranked divergences from Trellis

1. **Governance.** Trellis treats writes as governed state transitions. Graphiti treats writes as "run LLMs, merge into graph, done." Worldview difference, not a feature gap.
2. **Store topology.** Trellis has six ABCs pluggable across SQLite/Postgres/S3/LanceDB/pgvector. Graphiti is graph-DB-first with four backends.
3. **Retrieval shape.** Trellis produces a budgeted, tag-filtered, dedup-and-token-capped context pack with telemetry events. Graphiti returns a typed `SearchResults` bag and lets you format it.
4. **Classification / quality layer.** Graphiti has no equivalent to `ClassifierPipeline`, `ContentTags`, noise filtering, or `compute_importance()`. Trellis's differentiator.
5. **Temporal semantics.** Both claim bi-temporal; Graphiti historizes edges, Trellis historizes nodes.
6. **LLM centrality.** Graphiti's ingestion is LLM-heavy (4-5 roundtrips per `add_episode`). Trellis's ingestion mode is deterministic-only.
7. **Immutability.** Graphiti deletes episodes via `remove_episode()`. Trellis's "traces are immutable" hard rule has no counterpart.

Where they overlap: both use Pydantic, both support custom entity types via user-supplied Pydantic models, both do hybrid retrieval (semantic + BM25 + graph), both expose an MCP server and a REST API, both are async-capable, and both build a knowledge graph incrementally from source events.

---

## 3. Zep (hosted, built on Graphiti)

**(a) Primitive storage unit.** Nodes (entities) and edges (facts/relationships) in a temporal knowledge graph, plus raw messages and a precomputed "Context Block" string. Facts live on edges, not nodes. Edges are mutable at the attribute level (validity intervals) but history is preserved.

**(b) Write path.** Heavy LLM-driven. Push via `thread.add_messages` (chat) or `graph.add` (arbitrary JSON/text/docs). Zep's pipeline runs entity extraction, relationship extraction, and fact extraction automatically. Contradictions trigger **fact invalidation**: the old edge gets an `invalid_at` timestamp rather than being deleted. No user-facing policy/approval layer — ingestion is automatic and opinionated. Idempotency is handled internally; message ingestion is thread-scoped.

**(c) Retrieval.** Hybrid. The core surface is `thread.get_user_context`, which returns a preformatted **Context Block** — a string combining a user summary plus relevant facts with temporal qualifiers. Under the hood: graph search + semantic similarity + reranking, tuned for sub-200ms P95. `graph.search` exposes raw hits. Budgeted but opaquely; Zep optimizes the block for LLM consumption rather than exposing knobs.

**(d) Temporal model.** Strong. Bi-temporal: facts track both when they were true in the world and when they were recorded. Fact invalidation produces time-ranges on edges (`valid_at`, `invalid_at`). Historical state queries are supported.

**(e) Extensibility & scoping.** Users, threads, and graphs form the scoping hierarchy. Each user has their own user graph; threads belong to users. Custom entity and edge types via Pydantic-like classes — schema is open. Multi-tenancy via user IDs is first-class.

**(f) Distinctive choice.** Turns raw dialogue into a temporally-valid fact graph and gives you back a pre-assembled context string. Opinionated about "here's the context, paste this into your prompt" rather than "here's a query API." What Zep adds over raw Graphiti: managed infrastructure, dialogue-ingestion pipeline, the Context Block abstraction, user/thread scoping, hosted SLA.

**(g) What it's bad at.** LLM extraction in write path → latency and cost, hidden quality variance. Heavily dialogue-centric — awkward as a task/work-trace store. Context Block format assumes a chat-agent consumer. Can't easily inspect or correct extraction decisions. Opaque retrieval tuning.

**Relevance to Trellis:** Zep's Context Block framing is what our sectioned pack wants to grow up to be. The difference: Zep's is tuned for dialogue continuity; ours is tuned for task context with explicit sections and budgets.

---

## 4. Mem0

**(a) Primitive storage unit.** A "memory" — short natural-language fact string with embedding and metadata (`user_id`, `agent_id`, `timestamp`, categories). Flat. Memories are mutable; the system actively edits them. Mem0g (graph variant) adds entity/relationship nodes on top of the same fact layer.

**(b) Write path.** The definitive LLM-in-the-write-path design. Two phases:
1. **Extraction:** an LLM reads recent messages (plus running summary) and emits *candidate facts* — short declarative statements worth remembering.
2. **Update:** for each candidate, Mem0 does similarity search against existing memories, then prompts an LLM to decide one of four operations: **ADD, UPDATE, DELETE, NOOP**. DELETE fires when a new fact contradicts an old one; UPDATE merges; NOOP drops duplicates.

No governance/policy layer — the LLM is both extractor and arbiter. `infer=False` bypasses LLM and stores raw payloads (also bypasses dedup). No formal idempotency; conflict resolution is delegated to LLM judgment.

**(c) Retrieval.** Primarily semantic search over the fact vector store, scoped by `user_id`/`agent_id`. Returns top-k. Mem0g adds graph traversal. No context-pack builder — caller assembles the prompt. Some reranking, core loop is vector similarity + metadata filter.

**(d) Temporal model.** Weak. `created_at`/`updated_at` + a history table tracking prior versions, but no true bi-temporal querying. `DELETE` is destructive and irreversible. Time-travel is not a headline feature.

**(e) Extensibility & scoping.** Scoping via `user_id`/`agent_id`/`run_id` metadata. Memory schema is closed — a memory is a text string with metadata; no custom memory types with structured fields. Flat categories.

**(f) Distinctive choice.** The four-operation LLM arbiter (ADD/UPDATE/DELETE/NOOP) in the write path. The system most committed to "memory should be *edited*, not just appended, and an LLM is the right editor."

**(g) What it's bad at.** DELETE is destructive — if the LLM misjudges a contradiction, the old fact is gone. No audit trail of *why* a decision was made. No temporal reasoning. Flat text loses structure. Write cost scales with conversation volume (LLM on every turn). Multi-tenant governance is "set user_id correctly." Ingestion is non-deterministic.

**Relevance to Trellis:** Mem0 is our direct philosophical opposite. For regulated workloads where audit trails matter, we win. For personal chatbots where conversation continuity matters more than auditability, they win.

---

## 5. Letta (formerly MemGPT)

**(a) Primitive storage unit.** Three tiers:
- **Core memory blocks:** labeled, length-capped text blocks (e.g., `persona`, `human`) that live permanently in the agent's context window as XML-like sections.
- **Archival memory:** vector store of arbitrary text passages for long-term recall.
- **Recall memory:** full message history, searchable by keyword.

Core blocks are mutable and are the primary interesting primitive. Archival is append-oriented with delete.

**(b) Write path.** The agent edits its own memory via **tool calls**. Letta exposes tools: `core_memory_append`, `core_memory_replace`, `archival_memory_insert`, `archival_memory_search`. The LLM decides when and how to mutate blocks during its reasoning loop. No separate extraction pipeline — memory edits are just another tool call. Blocks have descriptions telling the agent how to use them. Read-only blocks exist for shared policies. No governance beyond tool permissions; no idempotency; no approval.

**(c) Retrieval.** Core memory is *always in context*. Archival is agent-invoked semantic search. Recall is keyword search. No budgeted context pack — context window management is MemGPT's famous contribution: when context fills up, the agent summarizes and evicts, and archival becomes the overflow. Retrieval is agent-initiated, not pipeline-driven.

**(d) Temporal model.** Minimal. Messages have timestamps; blocks have last-modified. No bi-temporal reasoning, no time-travel, no fact invalidation. Edits overwrite (prior values may be recoverable from message history).

**(e) Extensibility & scoping.** Blocks are user-defined (labels, descriptions, character limits). Scoping is per-agent, with the novel twist that **blocks can be shared across agents** — edit once, visible everywhere. No user/session scoping at the framework level.

**(f) Distinctive choice.** Agent-as-OS: the LLM sees its own memory as a tool it operates. Memory management is a *skill the agent exercises*, not a service it consumes. Shared blocks across agents is the most differentiated feature for multi-agent systems.

**(g) What it's bad at.** Quality depends entirely on how well the agent edits itself — non-deterministic and prompt-sensitive. No separation between "what happened" and "what we learned" — both live in the same mutable substrate. No structured knowledge graph. No audit trail of edits as first-class data. Core blocks permanently consume context window space. Hard to use as a shared team/org knowledge base.

**Relevance to Trellis:** Letta and Trellis are orthogonal. Letta is about one agent being smart about its own state; Trellis is about fleets of agents sharing knowledge through a governed substrate. Letta's "shared blocks across agents" is the only overlap and points at a primitive we don't have.

---

## 6. cognee

**(a) Primitive storage unit.** A **DataPoint** — a Pydantic-typed structured unit that becomes a node in the knowledge graph while also being embedded into the vector store. Edges between DataPoints represent relationships. Three backends: relational (provenance), vector (similarity), graph (relationships).

**(b) Write path.** The **ECL (Extract-Cognify-Load)** pipeline:
1. **Extract:** ingest raw data via Loaders.
2. **Cognify:** LLM processes data into DataPoints, extracts entities and relationships, optionally grounds against user-supplied ontology (RDF/XML).
3. **Load:** persist to the three stores.

Writes are organized as **Tasks** composed into **Pipelines** — cognee's core programming abstraction. You can define custom tasks and plug them into the pipeline. An `improve()` / `memify` step supports learning from feedback and adding derived nodes. No formal policy/approval; idempotency is per-pipeline-task.

**(c) Retrieval.** Multiple named search types: `GRAPH_COMPLETION`, `RAG_COMPLETION`, `CHUNKS`, `INSIGHTS`, `SUMMARIES`, plus auto-routing. Node Sets (tagging) support filtering. Not a budgeted context pack in the strict sense, but retrieval is graph-structure-aware.

**(d) Temporal model.** Weak. DataPoints carry timestamps; no bi-temporal edge validity or native time-travel. Design is "evolving knowledge graph" rather than "temporal graph." `memify` rewrites knowledge based on new info — overwrite-with-provenance rather than versioned history.

**(e) Extensibility & scoping.** Highly extensible. DataPoints are user-defined Pydantic models; ontology layer supports RDF schemas. Tasks and Pipelines are programmable primitives. Node Sets provide tagging. Multi-tenancy via dataset scoping. The most "framework-like" system of the six — expects you to define your own types.

**(f) Distinctive choice.** Pipelines as the top-level abstraction. Memory construction is an ETL problem (literally Extract-Cognify-Load). Tasks compose into Pipelines like dbt models or Airflow DAGs. The ontology grounding (external RDF knowledge) is also unique.

**(g) What it's bad at.** Complexity — closer to a data platform than a drop-in memory library. Three storage backends to manage. Weak temporal semantics → awkward audit trails. LLM in write path during Cognify → expensive ingestion. Retrieval is flexible but the caller picks the strategy. Not session/dialogue-optimized.

**Relevance to Trellis:** cognee's Task/Pipeline abstraction is worth borrowing for `trellis_workers/`. Our workers are currently hardcoded subclasses; a composable pipeline makes `DbtManifestWorker → ClassifierPipeline → ImportanceScorer → apply_noise_tags` expressible as data.

---

## 7. Cursor (codebase indexing)

**(a) Primitive storage unit.** AST-aware **code chunk** — sub-tree of a parsed file, sized to token limits, with embedding and metadata (file path, line range, language, file hash). Stored in Turbopuffer. Chunks are content-addressed by hash. Mutable only by replacement.

**(b) Write path.** Deterministic, **not LLM-driven**. Client:
1. Walks the repo locally, parses with tree-sitter, chunks along AST boundaries.
2. Computes a **Merkle tree** of file hashes. Merkle root identifies codebase state.
3. On each sync (~5–10 min), diffs the Merkle tree to find changed files.
4. Sends only changed chunks for embedding. Embeddings cached by chunk hash in AWS.
5. File paths obfuscated client-side; source code never stored server-side, only embeddings + obfuscated metadata.

No LLM in write path. Idempotency automatic via content hashing. No policy/approval — it's an indexing service, not a governed store.

**(c) Retrieval.** Pure semantic kNN → Turbopuffer → obfuscated paths/ranges → client resolves to real files locally → LLM context. Metadata filters on file path. No graph structure despite AST origin (graph is *used to chunk*, not preserved as queryable edges). Single-shot similarity, not a pack builder.

**(d) Temporal model.** None user-facing. Merkle tree tracks codebase versions for sync efficiency, but you cannot query "what did this function look like last month" from the index. Overwrite-on-change. Git is the time-travel layer.

**(e) Extensibility & scoping.** Closed chunk schema. Per-repo scoping. No user memory; no cross-repo graph. Multi-tenancy via workspace isolation.

**(f) Distinctive choice.** Merkle-tree-keyed incremental sync with client-side obfuscation. Entire indexing design is optimized around one goal: keep a remote embedding index in sync with a local codebase as cheaply and privately as possible. AST-aware chunking is the second distinctive piece.

**(g) What it's bad at.** It's not memory — it's search over a snapshot. No learning, no facts, no corrections, no per-agent state. No temporal queries. No structured relationships between symbols (no call graph, no import graph as retrievable structure). Every query re-enters cold. Nothing about *tasks performed*, *bugs fixed*, or *decisions made* is captured.

**Relevance to Trellis:** The Merkle-tree incremental sync pattern is worth stealing for `DbtManifestWorker` and `OpenLineageWorker`. Today those workers re-ingest the whole manifest on each run; a hash-based diff would only touch changed models.

---

## 8. Claude Code (CLAUDE.md, skills, hooks, auto memory)

**(a) Primitive storage unit.** Multiple file types, each different:
- **CLAUDE.md:** human-authored markdown instructions, loaded in full at session start.
- **Auto memory (`MEMORY.md` + topic files):** agent-authored markdown notes under `~/.claude/projects/<project>/memory/`. First 200 lines / 25KB of `MEMORY.md` load at session start; topic files load on demand.
- **Skills (`SKILL.md` directories):** frontmatter + markdown + optional supporting files/scripts. Descriptions load at session start; full content loads only when invoked.
- **Path-scoped rules (`.claude/rules/*.md`):** markdown with optional `paths:` glob frontmatter, loaded conditionally.
- **Hooks:** `settings.json` entries binding shell commands to lifecycle events (enforcement primitive).

All are plain files. Mutable by the user; auto memory is additionally mutable by the agent.

**(b) Write path.** There is no pipeline. CLAUDE.md is human-written. Auto memory is Claude-written during a session via normal file tools when it judges something worth remembering. Skills are human-authored directories. No validation, no policy, no idempotency — the file system *is* the store.

**(c) Retrieval.** Load-at-startup hierarchy, not query-time retrieval:
- Managed policy CLAUDE.md (org) → project CLAUDE.md → user CLAUDE.md → `CLAUDE.local.md`, concatenated up the directory tree.
- Path-scoped rules activate when matching files are read.
- Skills: descriptions always in context; bodies loaded on invocation.
- Auto memory: `MEMORY.md` head loaded at start; topic files read on-demand.

No vector search. No similarity. Deterministic file-based context assembly with scope resolution rules.

**(d) Temporal model.** None. Files are files. Git is the audit trail if you check them in. Auto memory overwrites; `/compact` preserves root CLAUDE.md but drops nested memory state.

**(e) Extensibility & scoping.** Extremely extensible in a file-oriented way. Four scope layers (enterprise managed policy, project, user, local) with precedence rules. Skills support frontmatter fields for invocation control, tool allow-lists, path scoping, subagent forking. Monorepo support via `claudeMdExcludes`. Import syntax `@path/to/file` for composition.

**(f) Distinctive choice.** **Context-as-filesystem**. Memory is not a database — it's markdown files with load-order and scoping rules, and the agent's job is to read and write them with the same tools it uses for source code. Skills extend this by making *procedures* first-class files that load lazily. Hooks extend it in the opposite direction: deterministic enforcement that runs outside the LLM.

**(g) What it's bad at.** No similarity search → cannot handle "what have I learned that's vaguely relevant to this task." No cross-session fact aggregation beyond what Claude chooses to write. No multi-agent knowledge sharing — auto memory is machine-local. No structured relationships, no graph. Scale bounded by context window. No governance; CLAUDE.md conflicts are "resolved arbitrarily" per Anthropic's own docs. No temporal reasoning. Deliberately primitive; relies on the human to curate.

**Relevance to Trellis:** Claude Code is a single-agent human-curated memory that relies on git for history and the user for quality control. **Trellis is what you'd build if you wanted Claude Code's primitives to work across a fleet of agents.** The skills concept maps almost exactly to `trellis_sdk/skills.py` (pre-canned procedures that load on demand). Since Trellis targets Claude Code agents, we should consciously evolve the skills layer toward file-based markdown-frontmatter skills so they're discoverable the same way Claude Code's own are.

---

## 9. Comparison matrix

| System | Storage unit | Write model | Retrieval | Temporal | Type system | Distinctive choice |
|---|---|---|---|---|---|---|
| **Trellis** | Trace + Document + Entity + Edge + Evidence + Pack, each in its own store; SCD Type 2 on nodes | Governed 5-stage pipeline (validate/policy/idempotency/execute/emit); deterministic classification with LLM fallback in enrichment mode | `PackBuilder` orchestrates pluggable `SearchStrategy` protocols; two-stage budget (items → tokens); `PACK_ASSEMBLED` telemetry; curated-node boost; structural filtering | SCD Type 2 on nodes with `as_of` queries; role immutable across versions; edges always current | Open strings at storage boundary; StrEnum defaults at schema boundary; `NodeRole` (structural/semantic/curated); `GenerationSpec` provenance | Governed writes + deterministic ingestion + effectiveness feedback loop |
| **Graphiti** | EntityNode + EntityEdge + EpisodicNode + CommunityNode + SagaNode in one graph DB | LLM-heavy `add_episode` (4-5 LLM calls per episode: extract_nodes → resolve_nodes → extract_edges → resolve_contradictions → extract_attributes); no idempotency; no policy | `search_` with composable `SearchConfig` (methods × rerankers: RRF, MMR, cross_encoder, node_distance, episode_mentions); no pack assembler | Bi-temporal edges (`valid_at`/`invalid_at`/`expired_at`); nodes overwritten in place | Open via user-supplied Pydantic `entity_types`/`edge_types`/`edge_type_map` | LLM-extracted temporally-valid fact graph with sophisticated rerankers |
| **Zep** | Entity nodes + fact edges (mutable validity) + raw messages + precomputed Context Block | LLM extracts entities/facts from dialogue; fact invalidation on contradiction; opinionated auto-pipeline | Hybrid graph+semantic, returns pre-assembled Context Block string (~200ms) | Bi-temporal (`valid_at`/`invalid_at` on edges); time-travel is headline | Open: custom entity/edge types via Pydantic; users/threads/graphs scoping | Dialogue-grade fact graph with temporal validity, delivered as a paste-ready context string |
| **Mem0** | Flat text "memory" with embedding + metadata; mutable | LLM extraction → LLM arbiter emits ADD/UPDATE/DELETE/NOOP against existing memories | Vector similarity top-k, scoped by user/agent/session; no pack builder | Weak: `created_at`/`updated_at` + version history; no bi-temporal querying; DELETE destructive | Closed memory schema; scoping via `user_id`/`agent_id`/`run_id` metadata | LLM as the memory editor — four-op arbiter in the write path |
| **Letta** | Core memory blocks (always in context) + archival vector store + recall message log | Agent edits its own memory via tool calls (`core_memory_append`, `archival_memory_insert`, etc.) | Core: always in context. Archival: agent-invoked semantic search. Recall: keyword. No pipeline. | Minimal; overwrite on edit; no time-travel | Open block labels+descriptions; per-agent scope; **blocks shareable across agents** | Agent-as-OS: the LLM operates its own tiered memory as a tool; shared blocks for multi-agent systems |
| **cognee** | Typed Pydantic DataPoint as both graph node and vector embedding; 3 backends (relational, vector, graph) | ECL pipeline: Extract → Cognify (LLM + optional ontology) → Load; composable Tasks/Pipelines; `memify` for feedback | Multiple named modes (GRAPH_COMPLETION, RAG_COMPLETION, INSIGHTS, SUMMARIES) with auto-routing; Node Sets for filtering | Weak; timestamped but overwrite-oriented; no bi-temporal | Highly open: user-defined DataPoint schemas, RDF/XML ontologies, custom Tasks | Memory as ETL — Tasks + Pipelines + formal ontology grounding |
| **Cursor** | AST-aware code chunk + embedding + metadata (path, range, hash) in Turbopuffer | Deterministic client-side: tree-sitter chunk → hash → Merkle diff → embed only changes; no LLM | Pure semantic kNN → obfuscated path/range → local file fetch; no graph, no pack | None user-facing (Merkle tree is for sync, not queries); git is the time layer | Closed chunk schema; per-repo scoping; no user/agent memory | Merkle-tree-keyed incremental embedding sync with client-side path obfuscation |
| **Claude Code** | CLAUDE.md files + auto-memory markdown (`MEMORY.md` + topic files) + `SKILL.md` directories + path-scoped rules + hooks | Human writes CLAUDE.md / skills; agent writes auto-memory via normal file tools when it decides; hooks provide deterministic enforcement | Deterministic load-at-start hierarchy (managed → project → user → local), path-rule activation, skill descriptions always in context / bodies on invocation, on-demand file reads | None (files + optional git) | Open via markdown; four scope layers with precedence; frontmatter controls invocation/tools/paths/subagent | Context-as-filesystem: memory, procedures, and enforcement are all just files with load-order rules |

---

## 10. What to adopt, what to improve, what to avoid

Ranked recommendations extracted from the comparison. Tier 1 are real gaps in Trellis; Tier 2 are landscape ideas worth stealing; Tier 3 are things NOT to copy.

### Tier 1 — close real gaps

1. **Add an async SDK facade.** Our FastAPI server is async, workers need concurrency, and Graphiti-style async-throughout is table stakes. Not a rewrite — `AsyncTrellisClient` wrapping the same dispatch.
2. **Surface sectioned packs in the MCP server.** `get_context` currently returns a flat pack. Add a tool that accepts `sections=[{name, affinities, max_items, max_tokens}]` so Claude can use the tiered retrieval already built.
3. **Ship bulk mutation endpoints.** `POST /api/v1/commands/batch` taking a list of Commands with a `BatchStrategy`, so large imports don't go through 1000 individual executor calls.
4. **Write a policy CLI + API surface.** `trellis policy list / add / remove`, `GET/POST /api/v1/policies`. `PolicyGate` exists as a Protocol; it's currently invisible to operators.
5. **Automate the effectiveness feedback loop.** Run `apply_noise_tags()` on a schedule; emit `NOISE_TAGGED` events. Today it's manual (`trellis analyze context-effectiveness`).

### Tier 2 — steal from the landscape

6. **Cross-encoder reranking** — from Graphiti. Add a `Reranker` protocol to `PackBuilder` that runs after strategy merge but before budget enforcement. RRF and MMR rerankers are cheap to add.
7. **Task/Pipeline abstraction for workers** — from cognee. Our workers are currently hardcoded subclasses; a composable pipeline makes `DbtManifestWorker → ClassifierPipeline → ImportanceScorer → apply_noise_tags` expressible as data.
8. **Merkle-tree incremental sync for ingestion** — from Cursor. Today `DbtManifestWorker` re-ingests the whole manifest on each run; a hash-based diff would only touch changed models.
9. **Context Block as an SDK convenience** — from Zep. `client.get_context_block(intent, ...)` that returns a ready-to-paste string. Useful for agents that don't want to parse `Pack` objects.
10. **Markdown-frontmatter skills** — from Claude Code. Make `trellis_sdk/skills.py` entries discoverable as `skills/*.md` with frontmatter (`name`, `description`, `args`, `invocation_hints`). Aligns with how Claude Code agents find skills.

### Tier 3 — do NOT copy

- **Do not put an LLM in the write path.** That's our wedge. The moment we add LLM-driven fact extraction to `MutationExecutor`, we lose idempotency, auditability, and cost predictability. If we want LLM-extracted facts, put them in `trellis_workers/enrichment/` behind a policy-gated handler — never in the ingest critical path. See [`../design/adr-deferred-cognition.md`](../design/adr-deferred-cognition.md).
- **Do not collapse the stores** the way Graphiti collapses everything into the graph DB. Six ABCs is a cost worth paying for deployment flexibility.
- **Do not make entity updates in-place** the way Graphiti does on `EntityNode`. SCD Type 2 on nodes is a real feature; keep it.
- **Do not adopt Mem0's DELETE operation.** Immutable traces + soft-close-via-`valid_to` is the right audit story.

---

## 11. Confidence notes

- **High confidence:** all claims about Trellis's own code (based on direct source exploration), Graphiti's `nodes.py` / `edges.py` / `graphiti.py` / `search_config.py` / `maintenance/` files, Mem0's documented ADD/UPDATE/DELETE/NOOP arbiter, Letta's core memory block API, Cursor's Merkle-tree indexing approach.
- **Medium confidence:** Graphiti's MCP tool names (`mcp_server/src/` contents not directly inspected), Zep's internal pipeline stages (based on public docs, not source), cognee's `memify` semantics.
- **Lower confidence:** Graphiti node overwrite semantics (unverified whether any audit trail is preserved beyond `created_at`). Claude Code skills/memory loading specifics shift; treat the "markdown frontmatter skills" recommendation as directional.

## 12. Positioning statement

Trellis is:

> **A governed, auditable experience graph for fleets of agents that need deterministic ingestion, pluggable storage, and budgeted retrieval — where Graphiti is an LLM-driven dialogue fact graph, Mem0 is an LLM memory editor, Letta is an agent operating its own memory, cognee is a knowledge ETL framework, Cursor is code search, and Claude Code is context-as-filesystem for one agent.**
