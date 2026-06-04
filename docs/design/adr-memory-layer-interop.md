# ADR: Trellis over an External Memory Layer

**Status:** Proposed
**Date:** 2026-06-04
**Deciders:** Trellis core
**Related:**
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — defines the Knowledge/Operational planes and the `DocumentStore`/`BlobStore` whose ownership this ADR delegates outward.
- [`./adr-arcadedb-blessed-substrate.md`](./adr-arcadedb-blessed-substrate.md) — the graph + vector substrate Trellis retains; the read-time coupling this ADR accepts depends on it staying warm.
- [`./adr-extraction-mutation-core-boundary.md`](./adr-extraction-mutation-core-boundary.md) — the pure-extractor → drafts → `MutationExecutor` pipeline the Memory Layer plugs into as a *source*.
- [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md) / [`./adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) — the promotion ladder; the Memory Layer becomes the pre-promotion origin of candidates.
- [`./adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md) — the curation loop; this ADR chains a second, lower curation stage (the Memory Layer's own) beneath it.
- [`./adr-importance-score-freshness.md`](./adr-importance-score-freshness.md) — Trellis freshness/decay; contrast with the Memory Layer's by-design forgetting.
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — the entity type a promoted, summarized memory becomes (§2.8).
- [`./adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) — unchanged; callers still go through the `GraphStore` ABC + canonical DSL.
- [`./adr-terminology.md`](./adr-terminology.md) — canonical meaning of "plane" and "substrate".
- [`../../src/trellis/extract/`](../../src/trellis/extract/) — extractors; the Memory Layer source extractor lands here.
- [`../../src/trellis/retrieve/pack_builder.py`](../../src/trellis/retrieve/pack_builder.py) — dedup-by-`item_id`; the assembler that resolves overlap.
- [`../../src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py) — `document_ids` property = the addressing bridge.

> **Terminology note.** "Memory Layer" here is an **external** store that Trellis *consumes*. It is **not** a Trellis "plane" and **not** a "substrate" in the [`adr-terminology.md`](./adr-terminology.md) sense (a blessed backend of a Trellis plane). It is a separate system, owned by agents, that Trellis sits on top of. The capitalized term is local to this ADR.

---

## 1. Context

### The shape that emerged

Trellis is a governed, curated, relational experience store. A separate need keeps surfacing: agents want a **fast, file-native, self-curating memory** — working notes, learned skills, reference documents — that they read and write at file speed, that a human can open in Obsidian or diff in git, and that is portable with zero lock-in. The motivating exploration weighed a markdown-on-filesystem store (local/S3) indexed by DuckDB/Parquet against the graph, with NotebookLM and Obsidian as reference points.

The tempting move is to make that the new *bottom tier of Trellis*. **This ADR rejects that.** It draws the boundary the other way: Trellis stays exactly what it is and **sits on top of** an external Memory Layer. The dependency points one way — Trellis depends on the Memory Layer; the Memory Layer knows nothing about Trellis — which is what keeps Trellis's identity intact and makes the Memory Layer independently useful (humans, Obsidian, other agents, other tools can use it *without* Trellis).

### What the Memory Layer is

- **Owned by agents**, written at file speed, append-heavy.
- **Self-curating**: recency, salience, decay/forgetting, summarization/compaction. It manages *its own* sense of what matters for the agent's current effectiveness.
- **Reference implementation**: markdown + frontmatter content on FS/S3, indexed by DuckDB (a metadata manifest + a `vss` HNSW vector index), with a blob store for large payloads. **But Trellis depends on an *interface*, not this implementation** (§2.2) — "something like this" should be swappable.

### Two things both called "curation"

The Memory Layer self-curates, which raises the question: *if it already decides what's important, what is Trellis for?* They look redundant but optimize opposite objectives:

| | Memory Layer | Trellis |
|---|---|---|
| Scope | one agent, private | all agents, shared |
| Optimizes | "what helps *me* with *this task now*" | "what is *true, durable, reusable* across the system" |
| Mechanism | recency, salience, decay, compaction | validation, policy, dedup, evidence, promotion, audit |
| Horizon | short — effectiveness now | long — collective truth over time |
| Forgets? | yes, by design | no — durable record |
| Relationships | flat-ish (recency/similarity) | typed, traversable, temporal graph |

A self-curating memory cannot, on its own, deliver the right column: it has no cross-agent view, no governance, no relationship graph, and it actively forgets. Those four things — **cross-agent sharing, governance/policy/audit, the relationship graph, and durable truth (vs decay)** — are Trellis's non-redundant value. (If a Memory Layer ever genuinely delivers all four, it *has become* Trellis.)

### The decision to make

- **(A)** Fold a memory tier *into* Trellis as the bottom rung of the promotion ladder.
- **(B)** Keep Trellis separate; it sits *on top of* an external Memory Layer via a small interface. Within B, a sub-choice on document ownership: **role-split** (Trellis keeps curated docs) vs **delegate** (Trellis holds only the graph).
- **(C)** Put everything — documents included — into the ArcadeDB substrate as records.

---

## 2. Decision

**Option B, delegate variant.** Trellis sits on top of an external Memory Layer through a small read/subscribe interface. The Memory Layer owns all document/memory **content**; Trellis owns only the **graph** — typed relationships + governance metadata that *reference* Memory-Layer content by ID and never copy it.

### 2.1 Layering and dependency direction

```
┌──────────────────────────────────────────────────┐
│  TRELLIS  (on top — the differentiated layer)      │
│  governed mutations · curated graph · promotion ·  │
│  context packs · policy · audit                    │
└──────▲────────────────────────────────┬───────────┘
       │ reads as a source               │ (optional) projects curated
       │ (extractor + change feed)       │ artifacts back down as files
┌──────┴────────────────────────────────▼───────────┐
│  MEMORY LAYER  (underneath — external, swappable)  │
│  markdown+frontmatter (FS/S3) + DuckDB (VSS +      │
│  manifest) + blobs                                 │
│  agent working memory · notes · skills (raw) ·     │
│  human-editable (Obsidian/git)                     │
└────────────────────────────────────────────────────┘
```

Content lives in exactly one place (the Memory Layer). The graph is structure laid *over* that content, not a second copy of it.

**Scope (resolved).** Memory Layers are **per-agent (or per-scope) and private**; Trellis is the single **shared** tier. Promotion (§2.4, §2.8) *is* the privacy/sharing boundary — the act that turns one agent's private learning into collective, governed knowledge. This is the sharpest statement of why Trellis is not redundant with a smart memory: it is the *only* cross-agent surface. A team/project scope between "fully private" and "fully shared" is a later refinement, not a separate mechanism.

### 2.2 The Memory Layer interface (not the implementation)

Trellis depends on a small read/subscribe contract; the markdown+DuckDB store is one conforming implementation. When this ADR is accepted, the Protocol lands at `src/trellis/memory/base.py` (sibling to the `llm/` provider protocols); it is shown here so the boundary is concrete:

```python
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MemoryItem:
    item_id: str                    # stable ID; becomes a graph node's document_id
    content: str                    # the markdown/text body
    metadata: dict[str, object]
    updated_at: str                 # ISO-8601; drives change detection


@runtime_checkable
class MemoryLayer(Protocol):
    """External, agent-owned memory/document store that Trellis sits on top of.

    Trellis binds to this interface, never to a concrete backend. Content lives
    here exactly once; Trellis stores only graph references to it (``document_ids``).
    """

    def read(self, item_id: str) -> MemoryItem | None:
        """Fetch one item by stable ID; ``None`` if absent or GC'd."""
        ...

    def search(
        self, query: str, *, filters: dict[str, object] | None = None, k: int = 20
    ) -> list[str]:
        """Similarity + metadata search. Returns IDs; caller fetches via ``read``."""
        ...

    def list_changed_since(self, cursor: str | None) -> tuple[list[str], str]:
        """IDs changed since ``cursor`` plus a new opaque cursor. Drives
        incremental re-curation (§2.5). ``cursor=None`` means 'from the start'."""
        ...

    def write_back(
        self, item_id: str, content: str, metadata: dict[str, object]
    ) -> None:
        """Optional: project a curated artifact (summary, skill) back as a file so
        the human/Obsidian view stays complete (§2.4, §2.8). May raise
        ``NotImplementedError`` on read-only Memory Layers."""
        ...
```

Any store satisfying this contract can be the Memory Layer — mirroring Trellis's existing posture (ABCs + swappable backends + open-string types), extended one layer *down*. `read`/`search` are the retrieval path; `list_changed_since` is the sync trigger (§2.5); `write_back` is the optional loop-closer (§2.8).

### 2.3 Delegate: content lives once, the graph references it

- Trellis retains `GraphStore` + `VectorStore` (embeddings as node properties per the ArcadeDB shape; see [`adr-arcadedb-blessed-substrate.md`](./adr-arcadedb-blessed-substrate.md)).
- The role of `DocumentStore`/`BlobStore` shifts: they become a **read-through client over the Memory Layer**, or are bypassed in favor of fetching content live at pack-assembly time. (Which one — §5.)
- Entity nodes' existing `document_ids` ([`stores/base/graph.py`](../../src/trellis/stores/base/graph.py)) now point at **Memory-Layer file IDs**. The addressing bridge already exists; delegate just aims it outward.

**`DocumentStore` fate (resolved).** Rather than bypass it, `DocumentStore` gains a thin **read-through backend** over the `MemoryLayer` interface, registered like any other backend so `PackBuilder`, retrieval, and the contract tests are unchanged: `get`/`search` delegate to `MemoryLayer.read`/`search`; raw writes are out-of-band (agents write the Memory Layer directly), so the backend's write path is reserved for `write_back` of curated artifacts (§2.8). This keeps the ABC contract intact while moving content *ownership* outward.

### 2.4 Chained curation, not competing curation

The two curations run in **sequence**, not in competition:

1. **Memory Layer curates first** — salience/decay/compaction, per-agent, private. When it consolidates ("this has stabilized / this matters"), it produces stable, important candidates.
2. **Trellis curates second** — those candidates enter the existing pipeline (validate → policy → idempotency → execute → emit) and the promotion ladder, becoming shared, governed, *connected* knowledge.

The promotion ladder's *origin* moves down into the Memory Layer. Trellis does not re-implement recency/salience; the Memory Layer does not implement governance/relationships.

### 2.5 Freshness and sync

The "keep a live mirror" problem is avoided because Trellis does **not** mirror all of memory:

- **Content freshness is free.** Trellis stores references, not copies; content is fetched live at read time. A doc edited in the Memory Layer is already current to Trellis.
- **Only *derived structure* can go stale** — the entities/edges extracted from a doc. Structure changes far less than content, so re-extraction is rare and targeted.
- **Re-curate on the Memory Layer's own compaction events, not on raw writes.** The change feed (`list_changed_since`) surfaces material changes; re-extraction piggybacks on the Memory Layer's consolidation cadence. Sync collapses from "continuous reconciliation" to "react to occasional candidates."

### 2.6 Integration via the existing extraction pipeline

The Memory Layer is **just another source**, like dbt manifests or query logs today (see [`adr-extraction-mutation-core-boundary.md`](./adr-extraction-mutation-core-boundary.md)):

```
Memory Layer  --(list_changed_since / read)-->  MemorySource / MarkdownVaultExtractor
   -> EntityDraft/EdgeDraft  ->  MutationExecutor  ->  GraphStore (+ EventLog)
```

No change to the `GraphStore` ABC, the canonical layer, or `MutationExecutor`. Extraction gains a source; governance is unchanged.

### 2.7 Retrieval: one assembler, dedup, precedence, corroboration-as-metadata

Even with content stored once, the same fact can surface twice at retrieval — once as the agent's raw note (Memory Layer), once as the governed claim derived from it (graph). Beyond token waste, **repetition biases LLM attention**: a duplicated fact is over-weighted and crowds out other context. Resolution, in priority order:

1. **One assembler, shared IDs, dedup.** Context assembly routes through a single component that queries both sources and dedups *before* anything reaches the context window. [`PackBuilder`](../../src/trellis/retrieve/pack_builder.py) already dedups by `item_id`; the `document_ids` lineage (§2.3) lets it collapse a memory item and its derived graph claim. Extend that dedup across the Memory source.
2. **Precedence on overlap.** When a fact exists raw and governed, pick one — default: the governed version wins (verified, carries relationships) and suppresses the raw copy; freshest-wins where currency beats governance.
3. **Corroboration as metadata, not repetition.** Agreement between an agent's own experience and the governed graph *is* signal — but capture it by collapsing to one representation and *annotating* it ("confirmed by prior runs + governed, N agents") rather than pasting it twice. The agent gets the trust boost without the attention distortion. **Repetition in a context window is a bug; corroboration metadata is the feature it was groping toward.**

Underneath all three: **don't always pull both.** Memory Layer alone for first-person-recent context; Trellis when the cross-agent, relational, or governed view is needed; merge-and-dedup only when a task genuinely needs both.

### 2.8 Capturing important memories — summarize, link, prioritize

The unit that crosses from Memory Layer to Trellis is **not a raw note but a distilled, linked memory** — a promoted summary that becomes a first-class "context point." Four moves, each reusing existing machinery:

- **Decide (what's important).** The promotion trigger fuses Memory-Layer salience (recency, access frequency, agent-marked importance) with signals only Trellis can see: effectiveness feedback (`FEEDBACK_RECORDED` — did this context actually help?), corroboration (referenced across runs/agents), and graph centrality. Reuses `compute_importance()` and the dual loop ([`adr-importance-score-freshness.md`](./adr-importance-score-freshness.md), [`adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md)).
- **Summarize (don't copy).** Promotion distills the source memory or cluster into a concise summary — Memory-Layer compaction plus optional LLM enrichment (`EnrichmentService`). The summary is the durable artifact; the raw memory stays below in the Memory Layer.
- **Link.** The summary lands as a graph node — naturally an **Observation** ([`adr-observation-entity-type.md`](./adr-observation-entity-type.md)) — carrying **provenance edges** to its source Memory-Layer IDs (via `document_ids`) and **semantic edges** to the entities it concerns. The links are what make it more than a note: connected, traversable, and auditable back to its evidence.
- **Prioritize.** These promoted summaries carry high importance, so `PackBuilder` surfaces them preferentially within the `max_items`/`max_tokens` budget, and the §2.7 precedence rule makes the *summary* (not a raw duplicate) the representation that reaches the agent — annotated with corroboration rather than repeated.

So "important context points" becomes a concrete object: a high-salience, summarized, link-rich Observation whose creation *is* the promotion event and whose retrieval priority reflects its proven value. Optionally, `write_back` (§2.2) mirrors the summary back down into the Memory Layer as a file, so the human/Obsidian view also gains the distilled version.

### 2.9 What is NOT in scope

- **The Memory Layer's internal design** (salience algorithm, decay policy, compaction) — Trellis depends only on the interface (§2.2).
- **Cross-system transactions.** The Trellis↔Memory-Layer relationship is eventually consistent, like the existing sanctioned bridges in [`adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) §2.5. No two-phase commit.
- **Changing the `GraphStore` ABC or canonical layer.**
- **Building the Memory Layer itself.** This ADR fixes the boundary and the seam, not the implementation.

---

## 3. Options considered

### Option A: Fold the memory tier into Trellis

**How it works:** Add a cheap raw tier *inside* Trellis as the bottom rung of the promotion ladder; agent memory and notes become Trellis-owned stores.

**Pros:** One product, one mental model; the promotion ladder is contiguous.

**Cons:** Trellis absorbs cheap-tier concerns it has no business owning — high-churn writes, file ergonomics, cold-start latency. Routing every agent scratchpad write through the governed pipeline is uneconomical and dilutes the "governed, curated" identity. The Memory Layer can no longer serve non-Trellis consumers (Obsidian, humans, other agents) on its own.

**Verdict:** Rejected. It blurs the line that makes Trellis coherent; "Trellis becomes everything" is how these systems die.

### Option C: Everything in the ArcadeDB substrate (documents as records)

**How it works:** Use ArcadeDB's multi-model document support to hold notes/memory as JSON records alongside graph + vector. One engine, one transaction boundary, no sync.

**Pros:** Operationally simplest; no cross-system coupling; co-located graph/doc/vector.

**Cons:** ArcadeDB "documents" are records in memory-mapped page files, **not** files — no `grep`, `git diff`, Obsidian, or PR-review, which is exactly the portability/human-readability that motivated the exploration. A resident JVM carries the high-churn cheap tier it's poorly suited for (vs DuckDB/SQLite microsecond cold start).

**Verdict:** Rejected *for the memory/notes content*. ArcadeDB remains the right home for the curated graph + vector; it is the wrong home for a file-native, human-facing, high-churn memory tier.

### Sub-option within B: role-split vs delegate

- **Role-split:** Memory Layer = raw docs; Trellis keeps a *curated* `DocumentStore` (copies promoted content up). **Pro:** Trellis is self-contained at read time — a pack never calls down to the Memory Layer; the governed/immutable/audited guarantee holds for curated content. **Con:** curated content exists twice (a file below, a governed doc above) — the duplication/double-influence risk is real and must be managed.
- **Delegate (chosen):** Memory Layer owns all content; Trellis holds only the graph (references). **Pro:** content lives once — duplication is structurally minimized. **Con:** read-time coupling — a content-bearing pack needs both layers online.

**Verdict:** **Delegate.** No-duplication plus the avoidance of double-influence outweigh read-time coupling, given both layers are warm services in the target self-hosted AWS deployment. Role-split's self-containment is not worth re-introducing the very duplication problem we're trying to avoid.

---

## 4. Consequences

### Positive

- **Trellis identity preserved with near-zero core change** — a Memory source extractor + a change-feed client + an optional write-back projector. Graph, `MutationExecutor`, promotion loop, and packs are untouched. (That this is the *whole* diff is the tell the boundary is right.)
- **Memory Layer is independently useful** — Obsidian, humans, other agents, and the analytical stack can use it without Trellis.
- **Swappable Memory Layer** — Trellis binds to an interface, not the markdown+DuckDB impl.
- **Duplication structurally minimized** (content once, graph references) and **double-influence handled** (dedup + precedence + corroboration-as-metadata).
- **Sync is cheap** — content freshness is free; re-extraction is event-driven on compaction, not on raw writes.

### Negative

- **Read-time coupling.** A content-bearing pack needs both the graph engine **and** the Memory Layer online/fast — directly tied to ArcadeDB staying warm ([`adr-arcadedb-blessed-substrate.md`](./adr-arcadedb-blessed-substrate.md)). *Mitigation:* both as warm services in the target deployment; if hot-path latency demands, an opt-in small content cache on hot items (accepting bounded, deliberate duplication).
- **Two systems to operate** — loosely coupled, but two. *Mitigation:* the Memory Layer is simple (files + DuckDB), low ops.
- **New machinery:** the change feed + incremental re-extraction. Derived structure is eventually consistent between compaction events. *Mitigation:* accept eventual consistency, as with existing cross-plane bridges.
- **`document_ids` now cross a system boundary.** Referential integrity is best-effort; a doc GC'd by the Memory Layer can dangle. *Mitigation:* the existing `allow_dangling` edge path + tombstone/SCD-2 `valid_to` closure on retraction.

### Neutral

- `DocumentStore`/`BlobStore` ABCs persist as read-through clients or are bypassed — an implementation decision (§5), not an architectural one.
- The Memory Layer's internal curation is, by design, outside Trellis's governance scope.

---

## 5. Open questions / implementation sketch

This ADR fixes the boundary and the seam. Two questions raised in drafting are now **resolved in §2** — `DocumentStore` fate (§2.3, a read-through backend) and identity scope (§2.1, per-agent private + a single shared Trellis tier). The following remain deferred to implementation or a follow-up ADR:

1. **Change-feed mechanism** — poll a DuckDB manifest diff (mtime/hash) vs an emitted event stream. Start with manifest-diff polling; upgrade if latency demands.
2. **Precedence default** — governed-wins vs freshest-wins (§2.7), and whether it's per-fact configurable.
3. **Write-back scope** — which curated artifacts (summaries, skills) get projected back as files (§2.8), and in what format.
4. **Referential integrity** — how the assembler resolves a dangling `document_id` (skip, tombstone, or re-fetch) when the Memory Layer GCs an item.
5. **Assembler merge** — how Memory `search` results and graph retrieval are fused, scored, and budgeted within the two-stage pack budget (`max_items` then `max_tokens`).
