# Research: Compaction & Agent Patterns from Claude Code Internals

**Date:** 2026-04-01
**Sources:**
- [Claude Code's Compaction Engine](https://barazany.dev/blog/claude-codes-compaction-engine) — Jonathan Barazany's analysis of the open-sourced compaction system
- [The 5-Hour Quota (Token Caching)](https://barazany.dev/blog/claude-code-token-caching) — Follow-up article on cache monitoring and token economics
- [instructkr/claude-code](https://github.com/instructkr/claude-code) (now `claw-code`) — Clean-room Rust reimplementation of Claude Code's agent harness, 117k+ stars

---

## What Claude Code Actually Does

### Three-Tier Compaction Hierarchy

Claude Code applies compaction in three progressively expensive tiers:

| Tier | Mechanism | Cost | When |
|------|-----------|------|------|
| **1. Preprocessing** | Clear old tool results, keep only 5 most recent. Replace others with `[Old tool result content cleared]` | Zero LLM cost | Before every API call |
| **2. API-level** | Server-side `cache_edits` — surgical deletion of specific tool result blocks by `tool_use_id` without invalidating prompt cache | Zero LLM cost | When cache is warm |
| **3. Full summarization** | LLM generates structured 9-section summary with chain-of-thought scratchpad (stripped afterward) | Full LLM call | Last resort, context under pressure |

**Key principle:** Cheap deterministic operations first. LLM summarization is the last resort because it's expensive and lossy.

### The `cache_edits` Insight

The most architecturally significant finding. Naive prompt rebuilding after compaction causes 98% cache miss rate (different token prefix = different cache key). Claude Code instead:

1. Leaves the cached message history untouched locally
2. Sends `cache_edits` directives alongside the API request
3. The server deletes specific tool results by ID without breaking the cache prefix
4. Result: 90% cache discount preserved instead of paying 1.25x cache write cost

### Structured Summarization (Tier 3)

When triggered, the summary captures 9 typed facets:

1. Intent (what the user wanted)
2. Technical concepts referenced
3. Files touched
4. Errors encountered and fixes applied
5. All user messages (preserved verbatim)
6. Pending tasks
7. Current work in progress
8-9. (Two additional sections not individually named in the blog)

The summarization call **reuses the exact same system prompt, tools, and message prefix** as the main conversation to preserve the cache key. The compaction instruction is appended as a new user message.

### Post-Compaction Reconstruction

After summarization, context is rebuilt in order:

1. Boundary marker with pre-compaction metadata
2. The structured summary
3. **5 most recently read files**, capped at **50K tokens**
4. Skills sorted by recency
5. Tool definitions re-announced
6. Session hooks re-run
7. CLAUDE.md restored
8. Continuation message: "resume directly -- do not acknowledge the summary, do not recap"

### Multi-Round Summary Merging

From the Rust reimplementation: when compacting a session that already has a compacted summary, the system merges them with layered structure:

```
Previously compacted context:
  [old summary highlights]

Newly compacted context:
  [new summary highlights]

Key timeline:
  [only from new segment]
```

This prevents unbounded summary growth while preserving historical context depth.

### Compaction Trigger Conditions (from Rust source)

```python
should_compact = (
    len(compactable_messages) > preserve_recent_messages  # default: 4
    and estimated_tokens(compactable_messages) >= max_estimated_tokens  # default: 10,000
)
```

Token estimation: `len(text) // 4 + 1` per content block (same heuristic XPG uses).

### Budget-Aware Instruction Loading

CLAUDE.md/instruction files are loaded with hard limits:
- 4,000 chars per file
- 12,000 chars total budget
- Deduplicated by content hash (normalized with blank-line collapse)
- Truncated with `[truncated]` marker when over budget

### System Prompt Architecture

A `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker separates static from dynamic content. Everything above the boundary is stable across turns (cacheable); everything below may change per turn. This enables efficient prompt caching.

---

## Gap Analysis: XPG vs. These Patterns

### What XPG Already Does Well

| Capability | XPG Implementation | Status |
|---|---|---|
| Token budgeting at retrieval | PackBuilder with max_items + max_tokens | Solid |
| Token estimation | `len(text) // 4 + 1` (same as Claude Code) | Matches |
| Importance scoring | Composite: signal_quality + scope boosts | Good |
| Noise filtering | Default exclusion of `signal_quality=noise` | Good |
| Deterministic classification | 4 classifiers in pipeline, no LLM needed | Strong |
| Structured summarization | EnrichmentService generates auto_summary + auto_importance | Present |
| Precedent distillation | PrecedentMiner extracts patterns from trace clusters | Unique strength |
| Append-only audit trail | Traces immutable, SCD Type 2 for graph nodes | Strong |

### Gaps Where Claude Code Patterns Apply

#### Gap 1: No Tiered Compaction for Stored Content

**Current state:** XPG stores everything at full fidelity. Budgeting only happens at retrieval time (PackBuilder). Old traces accumulate without compaction.

**What Claude Code does:** Three tiers of progressively expensive compression applied proactively, not just at retrieval.

**Opportunity:** Add a background worker that applies tiered compaction to stored content:

- **Tier 1 (deterministic, cheap):** Strip large tool outputs from old traces, keeping only summaries. Analogous to Claude Code clearing old tool results. Target: traces older than N days where `outcome.status != failure`.
- **Tier 2 (heuristic):** Merge near-duplicate documents (same content hash prefix or high cosine similarity). The document store already has `get_by_hash()` — extend it for fuzzy dedup.
- **Tier 3 (LLM summarization):** For trace clusters in the same domain/workflow, generate a consolidated summary document that replaces N individual traces in retrieval. The PrecedentMiner already does a version of this — generalize it.

**Implementation sketch:**

```
CompactionWorker:
  tier_1_strip_tool_outputs(traces, age_threshold_days=30)
  tier_2_merge_duplicates(documents, similarity_threshold=0.95)
  tier_3_consolidate_traces(domain, min_cluster_size=5, max_age_days=90)
```

#### Gap 2: No Structured Summary Format for Pack Items

**Current state:** `format_pack_as_markdown()` formats items as flat markdown sections. Each item gets its full excerpt.

**What Claude Code does:** Compacted summaries use a typed 9-section structure (intent, files, errors, pending tasks, etc.) that preserves the most actionable information in a compact format.

**Opportunity:** Define a `CompactedPackFormat` that structures pack output into typed sections rather than item-by-item listing:

```
## Context Pack for: {intent}

### Relevant Patterns (from precedents)
- ...

### Known Issues (from error-resolution items)
- ...

### Key Entities (from graph)
- ...

### Related Traces (summaries only)
- ...

### Active Constraints
- ...
```

This would let the PackBuilder produce more information-dense output within the same token budget. The classification system already tags items with `content_type` (pattern, decision, error-resolution, procedure, constraint) — use those tags to route items into sections.

#### Gap 3: No Retrieval-Time Result Trimming by Staleness

**Current state:** PackBuilder ranks by `relevance_score` (search similarity * importance). No time decay.

**What Claude Code does:** Keeps only the 5 most recent tool results; older ones are cleared. Reconstruction prioritizes the 5 most recently read files.

**Opportunity:** Add recency decay to the relevance scoring in PackBuilder:

```python
def _apply_recency_decay(base_score: float, age_days: float, half_life: float = 30.0) -> float:
    decay = 0.5 ** (age_days / half_life)
    return base_score * (0.3 + 0.7 * decay)  # floor at 30% of original score
```

This ensures recent experiences surface ahead of stale ones when relevance scores are close, without completely suppressing old high-relevance content.

#### Gap 4: No "Continuation Context" for Multi-Session Workflows

**Current state:** Each `get_context` call is stateless. No awareness of what the agent already knows from prior calls in the same session.

**What Claude Code does:** Post-compaction reconstruction includes boundary markers, re-injected skills, and continuation instructions. The agent doesn't recap.

**Opportunity:** Add session-aware retrieval:

1. Track `PACK_ASSEMBLED` events per `agent_id` within a time window
2. In subsequent `get_context` calls, deprioritize items already served in recent packs
3. Add a `session_id` parameter to `get_context` that enables this dedup

This prevents the agent from receiving the same context repeatedly across multiple tool calls in one conversation.

#### Gap 5: No Deferred/Lazy Content Loading

**Current state:** Pack items include full excerpts inline. All content loaded eagerly.

**What Claude Code does:** `ToolSearch` enables deferred tool loading — tools are mentioned by name but full schemas are only fetched on demand.

**Opportunity:** For large pack results, return summaries with IDs first, let the agent request full content for specific items:

- `get_context` returns compact summaries (title + 1-line description + relevance score)
- New `get_detail(item_id)` tool returns full content for a specific item

This is a natural fit for the MCP model where tool calls are cheap. It would reduce wasted tokens when the agent only needs 2 of 10 returned items.

#### Gap 6: No Summary Merging for Incremental Compaction

**Current state:** No compaction means no merging. But if Gap 1 is implemented, the multi-round merge pattern becomes relevant.

**What Claude Code does:** Layered "Previously compacted / Newly compacted" structure prevents unbounded summary growth.

**Opportunity:** If trace consolidation (Gap 1, Tier 3) is implemented, use the same pattern:

```
## Domain: deployment-pipelines

### Previously consolidated (2026-01 through 2026-02)
- 12 traces consolidated: CI failures due to Docker layer caching...

### Newly consolidated (2026-03)
- 5 traces consolidated: Flaky integration tests on ARM runners...
```

Store these as versioned documents with SCD Type 2 semantics (the graph store already supports this).

---

## Prioritized Recommendations

| Priority | Change | Effort | Impact |
|----------|--------|--------|--------|
| **P0** | Add recency decay to PackBuilder relevance scoring | Small (10 lines in strategies.py) | Immediate improvement to retrieval quality |
| **P0** | Add session-aware dedup to `get_context` (track recently served items) | Medium (new param + event log query) | Prevents redundant context in multi-call sessions |
| **P1** | Structured pack format using content_type tags to route items into sections | Medium (new formatter function) | More information-dense output within same token budget |
| **P1** | Tier 1 compaction worker: strip old tool outputs from traces | Medium (new maintenance worker) | Reduces storage growth, improves search signal |
| **P2** | Add `get_detail(item_id)` MCP tool for lazy content loading | Small (new MCP tool) | Reduces wasted tokens in large result sets |
| **P2** | Tier 2 compaction: fuzzy document dedup | Medium (similarity check + merge logic) | Prevents near-duplicate pollution |
| **P3** | Tier 3 compaction: LLM-driven trace consolidation with summary merging | Large (new worker + merge logic) | Long-term storage sustainability |
| **P3** | Dynamic boundary marker in system prompt for pack caching | Research needed | Could enable pack-level caching if agents make repeated similar queries |

---

## Memory Management Patterns

### How Claude Code Handles Memory

The original TypeScript had a full `memdir/` subsystem (8 modules: `findRelevantMemories`, `memoryAge`, `memoryScan`, `memoryTypes`, `paths`, `teamMemPaths`, `teamMemPrompts`), a `SessionMemory` service, and agent-level memory tools (`agentMemory`, `agentMemorySnapshot`). **None of these are in the open-source Rust port** — they remain proprietary.

What we can observe from the system prompt and behavior:

- Memory files live at `~/.claude/projects/<project-hash>/memory/`
- A `MEMORY.md` index is loaded into every conversation context
- Individual memory files use YAML frontmatter (`name`, `description`, `type`) with markdown content
- Memory types: `user`, `feedback`, `project`, `reference`
- The system is instructed to check if memories are still valid before acting on them
- Memory files have a **200-line truncation** on the index file

**Key design choices:**
- Memory is file-based, not database-backed
- Memory is always loaded (not on-demand) — the index is part of the system prompt
- Memory has explicit "what NOT to save" rules (no code patterns, no git history, no ephemeral state)
- Memories can become stale — the system is told to verify against current code before recommending

### Implicit Context Shrinking Techniques

Beyond the 3-tier compaction, Claude Code uses several implicit context management techniques:

1. **Thinking block stripping:** `Thinking` and `RedactedThinking` blocks from the API response are silently discarded — never stored in session history. This prevents chain-of-thought from consuming context on subsequent turns.

2. **Tool result replacement:** Old tool results aren't just dropped — they're replaced with `[Old tool result content cleared]` (a 40-char placeholder). This preserves the conversational flow (the model can see it called a tool) without the payload.

3. **Git diff in system prompt (no truncation):** Both `git status` and `git diff` are included in the system prompt with no truncation. This is a deliberate choice to give full awareness of pending changes, but it means large diffs consume significant context budget.

4. **No selective message dropping:** The Rust port implements only "keep last N, summarize the rest." No content-aware selection (e.g., keeping error messages but dropping success output). The selection is purely positional.

### Token Budget Monitoring (from caching follow-up article)

A separate blog post ([The 5-Hour Quota](https://barazany.dev/blog/claude-code-token-caching)) reveals:

- A **728-line diagnostic system** monitors cache hit rates per API call
- A **5% + 2,000 token threshold** triggers `.diff` file writing when cache hits drop — cache misses are treated as bugs
- A **"willow" warning** fires after **75 minutes idle + 100K tokens in conversation**, warning that prompt cache TTL (~1 hour) has likely expired
- `DANGEROUS_uncachedSystemPromptSection()` — a function name that forces engineers to write justification for any content placed in the uncached portion of the system prompt

### Context Window Partitioning

Claude Code does **not** explicitly partition the context window (no "X tokens for system prompt, Y for history, Z for tools"). Everything is sent; compaction is the only overflow protection. The system prompt is divided by a `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker:

| Section | Position | Cacheability |
|---------|----------|-------------|
| Intro, rules, task discipline, actions guidance | Above boundary | Stable, globally cached |
| **--- DYNAMIC BOUNDARY ---** | | |
| Environment, project context, git status/diff | Below boundary | Per-session |
| Instruction files (4K/file, 12K total) | Below boundary | Per-project |
| Runtime config, LSP context | Below boundary | Per-turn |

The boundary enables Anthropic's prompt caching — the static prefix is shared across all users (`scope: 'global'`).

---

## Memory & Document Lifecycle Gaps in XPG

### Current State

XPG's `DocumentStore` (used by `save_memory`) has minimal lifecycle management:

| Feature | Documents | Traces | Graph Entities |
|---------|-----------|--------|----------------|
| Immutability | No (destructive upsert) | Yes (append-only) | Versioned (SCD Type 2) |
| Version history | None — old content lost on update | N/A | Full (`valid_from`/`valid_to`) |
| TTL / Expiration | None | None | None |
| Access tracking | None | Via feedback events | None |
| Dedup at write | Manual (`get_by_hash()` exists, not wired in) | By trace_id | By node_id (upsert) |
| Event emissions | None | `TRACE_INGESTED` etc. | `ENTITY_CREATED` etc. |
| Staleness detection | `StalenessDetector` (reporting only, no action) | `RetentionWorker` (marks for prune) | None |

### Gap 7: No Memory vs Knowledge Distinction

**Current state:** `save_memory` and `save_knowledge` both store content, but documents have no metadata distinguishing ephemeral session notes from durable facts. Both go into the same `DocumentStore` with no TTL.

**What Claude Code does:** Explicit memory types (`user`, `feedback`, `project`, `reference`) with different save/update/remove semantics. The system actively avoids saving ephemeral content (debugging solutions, in-progress work, git history).

**Opportunity:** Add a `memory_type` metadata field to documents stored via `save_memory`:

```python
# Ephemeral: session notes, scratch, temporary context
save_memory(content, metadata={"memory_type": "session", "expires_after_days": 7})

# Durable: lessons learned, preferences, project context
save_memory(content, metadata={"memory_type": "durable"})
```

Then wire `memory_type` into:
- **Retention:** Auto-expire `session` documents after N days
- **Retrieval:** Boost `durable` documents in relevance scoring
- **Staleness:** Only flag `durable` documents as stale (session docs just expire)

### Gap 8: No Deduplication at Write Time

**Current state:** `save_memory()` does a raw `put()` into the document store. Same content can be stored multiple times with different `doc_id`s.

**What Claude Code does:** Instruction files are deduplicated by content hash (normalized). The system instructs "Do not write duplicate memories. First check if there is an existing memory you can update."

**Opportunity:** Check `get_by_hash()` in `save_memory()` before writing:

```python
existing = doc_store.get_by_hash(content_hash(content))
if existing:
    return f"Memory already exists: {existing['doc_id']}"
```

### Gap 9: No Event Emissions for Document Mutations

**Current state:** `save_memory()` emits no events to the EventLog. No audit trail for document writes, updates, or deletes. Contrast with traces and graph entities which emit lifecycle events.

**Opportunity:** Emit `DOCUMENT_STORED` / `DOCUMENT_UPDATED` events from `save_memory()`. This enables:
- Audit trail
- Staleness workers reacting to document changes
- Session-aware retrieval (know what was recently stored)

### Gap 10: No Document Versioning

**Current state:** `DocumentStore.put()` with an existing `doc_id` overwrites destructively. Old content is permanently lost. The architecture doc calls for SCD Type 2 temporal queries, but only the graph store implements this.

**What Claude Code does:** Memory files are written to disk — git tracks their history. The system can update or remove memories, but the git layer provides implicit versioning.

**Opportunity:** Add `valid_from`/`valid_to` to documents (matching the graph store pattern), or at minimum preserve `created_at` across updates (currently clobbered).

### Gap 11: Staleness Detection Without Action

**Current state:** `StalenessDetector.check()` returns a list of stale document IDs. Nothing consumes this list. `RetentionWorker` handles traces but not documents.

**Opportunity:** Add a `DocumentRetentionWorker` that:
1. Runs `StalenessDetector.check()`
2. For `session`-type documents past TTL: delete or archive
3. For `durable`-type documents past staleness threshold: flag for review
4. Emit events for all actions

---

## Agent Memory vs Trellis Memory

### The Distinction

Agent harnesses (Claude Code, Cursor, etc.) manage their own **local memory**:
- Session notes, user preferences, working context
- Per-project, per-agent, fast, ephemeral-to-medium-term
- Stored in files the agent controls (e.g. `~/.claude/projects/.../memory/`)
- 4 typed memory types: user, feedback, project, reference
- Explicit "what NOT to save" rules — no code patterns, no git history, no ephemeral state

The experience graph manages **shared organizational knowledge**:
- Cross-agent, cross-project learning
- Governed mutations, immutable audit trail
- Patterns, precedents, entity relationships, evidence
- Long-term, versioned, classified

**These are complementary, not competing.** The agent's local memory is "what I need to work effectively right now." The experience graph is "what the organization has learned across all agents and projects."

### Where `save_memory` Falls Down

Currently `save_memory` is a raw `DocumentStore.put()` — an unstructured dump disconnected from both the learning pipeline and the graph. It tries to serve both use cases and serves neither:

- **It's not agent-local memory** — the agent harness already handles that. Agents don't need XPG to store their session notes.
- **It's not shared learning** — documents saved via `save_memory` never get classified, enriched, or promoted to the graph. They're searchable via FTS but orphaned from the precedent pipeline.

### What Agents Should Save to the Trellis

The experience graph should capture **learnings**, not scratch. The filter:

| Agent produces... | Save to XPG? | How? |
|---|---|---|
| "I discovered this deployment requires flag X" | Yes — shared constraint | `save_knowledge` (constraint entity) |
| "Task succeeded after retrying with approach B" | Yes — execution pattern | `save_experience` (trace with outcome) |
| "This API returns 429 under load" | Yes — operational learning | `save_knowledge` (entity) or `save_experience` (if part of a task) |
| "User prefers short responses" | No — agent-local preference | Agent's own memory system |
| "Currently working on file X" | No — ephemeral session state | Agent's own memory system |
| "These tests are flaky on ARM" | Yes — shared observation | Currently nowhere — this is the gap |

The last row is what `save_memory` should handle: **shared observations that don't fit traces or entities yet**, but need to enter the learning pipeline.

### Proposed: `save_memory` as Observation Ingestion Funnel

Instead of being a dead-end document store, `save_memory` becomes the front door for unstructured observations that the enrichment pipeline classifies and routes:

```
Agent calls save_memory("ARM runners fail intermittently on integration tests",
                         metadata={"domain": "ci"})
  ↓
1. Dedup check (get_by_hash) — skip if identical content exists
  ↓
2. Emit DOCUMENT_STORED event — enables downstream workers
  ↓
3. ClassifierPipeline tags it:
   content_type=error-resolution, scope=project, signal_quality=standard
  ↓
4. EnrichmentService adds:
   auto_summary, auto_importance=0.6, auto_tags=["ci", "flaky-tests", "arm"]
  ↓
5. Routing based on content_type + importance:
   - High importance pattern/decision → PrecedentMiner.extract_precedent_from_document()
   - Entity-like (constraint, system) → auto-create graph node via save_knowledge
   - Low importance / ephemeral → set TTL, auto-expire in 30 days
   - Noise → mark signal_quality=noise, excluded from future retrieval
```

**What changes:**
- `save_memory` emits `DOCUMENT_STORED` events (enables the pipeline)
- New `DocumentEnrichmentWorker` runs ClassifierPipeline + EnrichmentService on new documents
- New `DocumentPromotionWorker` graduates high-value documents to precedents or graph entities
- `memory_type` metadata distinguishes observations from session scratch (if agents do send scratch)
- TTL on low-importance documents prevents unbounded accumulation

**What stays the same:**
- Agent-local memory remains the agent's job (Claude Code's `~/.claude/memory/`, etc.)
- `save_experience` and `save_knowledge` remain the primary structured ingestion paths
- The enrichment and classification systems are reused, not rebuilt

### The Learning Lifecycle

```
                    Agent Activity
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
      save_experience  save_knowledge  save_memory
      (traces)         (entities)     (observations)
            │            │            │
            │            │      ┌─────┴─────┐
            │            │      ▼           ▼
            │            │  classify    enrich
            │            │      │           │
            │            │      └─────┬─────┘
            │            │            │
            │            │     route by type
            │            │     ┌──────┼──────┐
            │            │     ▼      ▼      ▼
            │            │  promote  entity  expire
            │            │  to prec  create  (TTL)
            │            │     │      │
            ▼            ▼     ▼      ▼
      ┌─────────────────────────────────────┐
      │        Knowledge Graph + Events     │
      │  (precedents, entities, feedback)   │
      └──────────────┬──────────────────────┘
                     │
              ┌──────┴──────┐
              ▼             ▼
         get_context    get_lessons
         (retrieval)    (precedents)
              │             │
              ▼             ▼
          Agent uses learned context
```

### What This Means for the TODO

The previous gaps (7-11) collapse into one coherent initiative:

**"Wire `save_memory` into the governed enrichment pipeline"**

Steps:
1. Emit `DOCUMENT_STORED` events from `save_memory` (enables everything downstream)
2. Dedup at write via `get_by_hash()`
3. `DocumentEnrichmentWorker` — classify + enrich new documents (reuses ClassifierPipeline + EnrichmentService)
4. `DocumentPromotionWorker` — route high-value documents to precedents or graph entities
5. TTL metadata for auto-expiry of low-value documents
6. `DocumentRetentionWorker` — enforce TTL, prune expired documents

---

## Updated Prioritized Recommendations

| Priority | Change | Effort | Impact |
|----------|--------|--------|--------|
| | **Retrieval Quality** | | |
| **P0** | Add recency decay to PackBuilder relevance scoring | Small | Immediate retrieval quality improvement |
| **P0** | Session-aware dedup in `get_context` | Medium | Prevents redundant context in multi-call sessions |
| **P1** | Structured pack format using content_type tags | Medium | More info per token in pack output |
| **P2** | `get_detail(item_id)` MCP tool for lazy loading | Small | Reduces wasted tokens |
| | **Observation Pipeline (wire save_memory into learning)** | | |
| **P0** | Dedup check in `save_memory` via `get_by_hash()` | Small | Prevents duplicate document pollution |
| **P0** | Emit `DOCUMENT_STORED`/`DOCUMENT_UPDATED` events from `save_memory` | Small | Enables all downstream workers |
| **P1** | `DocumentEnrichmentWorker` — classify + enrich new documents | Medium | Observations enter the learning pipeline |
| **P1** | `DocumentPromotionWorker` — route high-value docs to precedents/graph | Medium | Observations graduate to shared knowledge |
| **P1** | TTL metadata + `DocumentRetentionWorker` for auto-expiry | Medium | Prevents unbounded document accumulation |
| | **Compaction** | | |
| **P1** | Tier 1 compaction worker: strip old tool outputs from traces | Medium | Reduces storage noise |
| **P2** | Tier 2 compaction: fuzzy document dedup | Medium | Prevents near-duplicate pollution |
| **P3** | Tier 3 compaction: LLM trace consolidation + summary merging | Large | Long-term storage sustainability |
| | **Store Infrastructure** | | |
| **P3** | SCD Type 2 versioning for documents | Large | Temporal queries, non-destructive updates |
| **P3** | Dynamic boundary in pack output for caching | Research | Pack-level caching for repeated queries |

---

## Non-Applicable Patterns

Some Claude Code patterns don't transfer to XPG:

- **`cache_edits` mechanism:** Specific to Anthropic's API prompt caching. XPG doesn't control the LLM's cache. Not applicable.
- **Summarization call piggybacking on conversation cache:** Same reason — XPG doesn't own the conversation context. The agent's harness handles this.
- **Tool result clearing by `tool_use_id`:** XPG stores domain knowledge, not conversation turns. The traces already have a different structure.
- **Bootstrap fast paths:** Specific to CLI startup optimization. XPG's MCP server is long-running; startup cost is amortized.

---

## Key Metrics from Claude Code (Reference)

| Metric | Value | XPG Equivalent |
|--------|-------|----------------|
| Tool results retained | 5 most recent | N/A (stores all) |
| Cache hit discount | 90% | N/A (no API cache control) |
| Cache miss penalty | 1.25x write cost | N/A |
| Cache TTL | ~1 hour | N/A |
| Cache miss detection threshold | 5% + 2K tokens drop | N/A |
| "Willow" warning trigger | 75 min idle + 100K tokens | N/A |
| Recent files post-compaction | 5, capped at 50K tokens | PackBudget: 50 items, 8K tokens |
| Summary sections | 9 structured | EnrichmentService: 4 fields |
| Summary text truncation | 160 chars per block, 200 for current work | N/A |
| Max files in summary | 8 | N/A |
| Max recent user requests in summary | 3 | N/A |
| Token estimation | `len/4 + 1` | `len//4 + 1` (identical) |
| Instruction file budget | 4K per file, 12K total | No limit currently |
| Instruction file dedup | Content hash (normalized) | N/A |
| MEMORY.md index truncation | 200 lines | N/A |
| Memory types | 4 (user, feedback, project, reference) | 1 (undifferentiated document) |
| Preserve recent messages | 4 (default) | N/A |
| Compaction trigger | >10K estimated tokens in compactable messages | N/A |
| Max output tokens (Opus) | 32K | N/A |
| Max output tokens (other) | 64K | N/A |
| Sub-agent max iterations | 32 | N/A |
| Document staleness threshold | N/A | 90 days (reporting only) |
| Trace retention max age | N/A | 365 days |
