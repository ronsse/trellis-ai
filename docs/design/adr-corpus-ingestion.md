# ADR: Corpus Ingestion — importers into Trellis

**Status:** Accepted — §7 phases 1–2 implemented 2026-07-11 (`trellis ingest corpus`, `src/trellis/ingest_corpus/`); phases 3–5 are incremental follow-ups
**Date:** 2026-07-08
**Deciders:** Trellis core
**Supersedes:** [`adr-memory-layer-interop.md`](./adr-memory-layer-interop.md) (external Memory Layer — see §6)
**Related:**
- [`adr-extraction-mutation-core-boundary.md`](./adr-extraction-mutation-core-boundary.md) — the pure-extractor → drafts → `MutationExecutor` pipeline the optional extraction pass reuses.
- [`../agent-guide/operations.md`](../agent-guide/operations.md) — "Document → vector embedding" (embed-on-ingest, the hook chunk embedding extends).
- [`classification-layer.md`](./classification-layer.md) — already treats `"obsidian"` as a first-class `source_system` with a `documentation` tag mapping; the classifiers are ready for a source that doesn't exist yet.
- [`../research/memory-systems-landscape.md`](../research/memory-systems-landscape.md) §13.2 — the competitive gap this closes.
- [`../../src/trellis/retrieve/embed_ingest_hook.py`](../../src/trellis/retrieve/embed_ingest_hook.py) — `run_embed_on_ingest` + `EMBED_INPUT_CHAR_CAP`.
- [`../../src/trellis/classify/dedup/minhash.py`](../../src/trellis/classify/dedup/minhash.py) — MinHash/LSH near-dup index reused for cross-file dedup.
- [`../../src/trellis/extract/save_memory.py`](../../src/trellis/extract/save_memory.py) — `build_save_memory_extractor`, the hybrid free-text extractor the `--extract` pass reuses.

---

## 1. Context

Trellis pivoted to a full memory system: `save_memory` deduplicates and
embeds single documents, and retrieval has a semantic axis. But **corpora
have no path in**. Every document on the live skynet deployment arrived
one-at-a-time through `save_memory` or `POST /documents`. A folder of
markdown notes, a meeting transcript, an email archive, or a PDF has:

- **no reader** — nothing walks a directory or accepts a file format;
  callers must supply a `content` string themselves;
- **no chunking** — `DocumentStore.put` stores one string per `doc_id`, and
  `run_embed_on_ingest` embeds only the first `EMBED_INPUT_CHAR_CAP = 8000`
  characters. Content past the cap is *semantically unretrievable*;
- **no idempotent re-sync** — `POST /documents` re-`put`s on every call
  (only MCP `save_memory` dedups), so re-ingesting a vault duplicates or
  churns rows;
- **no transcription** — audio cannot enter the system at all (`BlobStore`
  exists but has zero application callers).

The parked [`adr-memory-layer-interop.md`](./adr-memory-layer-interop.md)
answered this with an **external** file-native memory layer that Trellis
would reference but never copy. The owner has decided the opposite
direction: **importers into Trellis** — corpus content becomes ordinary
Trellis documents, deduplicated, embedded, and governed like everything
else. Rationale: one system of record instead of a read-time coupling to a
second store; the dogfood deployment showed the DocumentStore + embed
pipeline already does the storage half well; and the external layer's main
selling point (human-editable files) is preserved anyway because import is
non-destructive — the source files stay where they are and re-sync picks up
edits.

## 2. Decision — CLI surface

A new subcommand on the existing `ingest_app` Typer group
(`src/trellis_cli/ingest.py`, alongside `trace` / `evidence` /
`dbt-manifest` / `openlineage`):

```bash
trellis ingest corpus <path> [--source-system obsidian] [--domain X] \
    [--tag k=v ...] [--include '*.md'] [--dry-run] [--prune] [--extract] \
    [--format json]
```

- **Directory walker** over `<path>` (single file also accepted), filtered
  by a format-handler registry keyed on extension/content sniff.
- **Format handlers**, in delivery order:
  1. **Markdown** — YAML frontmatter → document `metadata`; `[[wikilinks]]`
     collected into `metadata.wikilinks` as alias/edge *candidates* (no
     graph writes in the reader — extraction is a separate pass, §5).
  2. **Plaintext / transcript** — `.txt` and simple speaker-labelled
     transcripts; speaker turns preserved verbatim, no diarization.
  3. **PDF** — later phase, behind an optional extra (text extraction only).
  4. **Audio — explicitly out of scope for core.** Transcription
     (e.g. Whisper on the deployment host) is an external pre-step whose
     output enters through the transcript handler. Core never gains an
     audio dependency.
- Per repo convention: `--format json` machine output, `--dry-run` prints
  the would-be plan (files, chunk counts, skips) without writing.
- Handlers are pure (`Path -> list[CorpusDocument]` dataclasses); all
  writes go through the shared ingest routine so CLI and any future REST
  bulk route behave identically.

## 3. Decision — chunking: chunk-as-separate-documents

Long documents are split into **chunk documents stored as ordinary
`DocumentStore` rows**, with the parent relationship carried in metadata:

- Parent doc: full original content, `metadata.source_path`, handler
  metadata, `chunk_count`. **Not embedded** when it exceeds the cap.
- Chunk docs: `doc_id = f"{parent_doc_id}#chunk-{i}"`, content = the chunk
  text, metadata `{parent_doc_id, chunk_index, chunk_count, source_path,
  char_span}`. **Chunks are what get embedded** via the existing
  `run_embed_on_ingest` hook — each chunk is under the 8,000-char cap by
  construction.
- Splitting: paragraph/heading-aware with a target size (~2–4k chars) and
  overlap; deterministic so re-chunking unchanged content yields identical
  chunks (required for idempotent re-sync, §4).

**Why this shape:** it needs **zero store-ABC changes** — chunks are just
documents, vector rows are already keyed per doc, and the metadata-dict
convention already exists (tags live there today).

**Rejected alternatives:**
- *A dedicated chunks table* — a schema migration in every DocumentStore
  backend (SQLite, Postgres) for what metadata already expresses.
- *Multi-vector per document* — a `VectorStore` ABC change rippling through
  four backends (SQLite, pgvector, ArcadeDB, Neo4j shape #2).

**Owned trade-offs:**
- Parent + chunks roughly **doubles storage** for chunked docs (full text
  on the parent, again across chunks). Accepted: corpora are small relative
  to Postgres capacity, and the parent copy is what makes "show me the
  whole note" cheap.
- **Retrieval follow-up (future work):** `PackBuilder` dedups by `item_id`,
  so two chunks of the same note can both enter a pack. A group-by-
  `parent_doc_id` dedup/rollup in the assembler is a planned follow-up, not
  part of this ADR's first delivery.
- FTS sees both parent and chunks (double hits); the explore/documents
  list view should default-filter `chunk_index` rows.

## 4. Decision — idempotent re-sync

Re-running `trellis ingest corpus` over the same tree must be a no-op for
unchanged files:

- **Stable identity:** `doc_id = f"corpus:{source_system}:{sha1(relpath)}"`
  — stable across runs, independent of content.
- **Change detection:** compare the stored `content_hash` (already returned
  by `document_store.get`) against the new content's hash
  (`trellis.core.hashing`). Unchanged → skip (no re-put, no re-embed —
  metadata-only re-puts deliberately don't re-embed today, and we keep
  that). Changed → `put` same `doc_id`, re-chunk, re-embed changed chunks,
  delete orphaned chunk docs beyond the new `chunk_count`.
- **Moved files:** `get_by_hash` lookup before creating a new row; a hit
  with a different `doc_id` is reported as a move (re-keyed, not
  duplicated).
- **Near-duplicates:** the `save_memory` MinHash/LSH index is reused to
  *warn* about cross-file near-dups in the run report — unlike
  `save_memory` it does **not** skip, because two legitimately similar
  notes are common in a vault.
- **Deletion:** `--prune` (default **off**) deletes documents whose source
  file vanished. Documents are deletable; traces remain immutable.
- **Audit:** each new/changed document emits `MEMORY_STORED` (same event
  the MCP path emits) so downstream consumers see one signal regardless of
  entry point. The run itself emits a summary event with counts
  (ingested/updated/skipped/pruned/warnings).

## 5. Optional graph extraction

Prose → graph mining reuses `build_save_memory_extractor`
(AliasMatch + LLM residue → governed `MutationExecutor` batch), gated
**twice**: the existing `TRELLIS_ENABLE_MEMORY_EXTRACTION` flag *and* an
explicit `--extract` opt-in — at corpus scale this is an LLM-cost decision
the operator must make per run, never a default. Known caveat carried over:
the extractor's alias resolver is an O(n) full-graph scan; acceptable at
dogfood scale, flagged for optimization before large-vault use.

## 6. Relationship to prior art

[`adr-memory-layer-interop.md`](./adr-memory-layer-interop.md) is marked
**Superseded by this ADR**. What it got right is preserved: import is
non-destructive (files remain the human-editable source; Obsidian/git
workflows keep working), `list_changed_since`-style incremental sync
survives as the `content_hash` comparison, and the classification layer's
`source_system="obsidian"` modeling is finally exercised. What changes is
ownership: content is copied into Trellis stores rather than referenced in
an external layer, eliminating the read-time coupling and the second
curation stage that ADR had to invent.

## 7. Implementation sketch (follow-up session)

| Phase | Files | Done when |
|---|---|---|
| 1. Reader + markdown handler + chunker | `src/trellis/ingest_corpus/` (walker, handlers, chunker — pure), `src/trellis_cli/ingest_corpus.py`, registered in `ingest.py` | `trellis ingest corpus vault/ --dry-run` reports files/chunks; real run stores parent+chunk docs with correct metadata; unit tests for handler + chunker determinism |
| 2. Idempotent re-sync | same + `trellis/core/hashing` reuse | second run over unchanged tree = 0 writes; edited file re-puts + re-embeds; moved file detected via `get_by_hash`; `--prune` removes vanished docs; tests cover all four |
| 3. Embed + transcript handler | embed hook wiring, `.txt`/transcript handler | chunks semantically retrievable via `search` on a live deployment (skynet vault dogfood) |
| 4. `--extract` pass | wiring to `build_save_memory_extractor` | flag-gated extraction produces governed entity/edge drafts from a sample vault |
| 5. PDF handler (optional extra) | `ingest_corpus/handlers/pdf.py` | text-PDF ingests; scanned PDFs rejected with a clear error |

**Size:** phases 1–2 ≈ one focused session; 3–5 incremental.
**Gating signal:** skynet dogfood — ingest the owner's real notes vault and
measure retrieval quality via the Memory Explorer packs view.
