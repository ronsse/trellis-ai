# Operations Reference

Complete CLI and Python API reference for the Trellis.

All CLI commands support `--format json` for machine-readable output. Use `--format json` when calling from scripts or agent tool adapters.

---

## Admin Commands

### `trellis admin init`

Initialize Trellis stores and configuration.

```bash
trellis admin init [--data-dir PATH] [--force] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir` | Platform default | Custom data directory path |
| `--force` | `false` | Overwrite existing config |
| `--format` | `text` | Output format |

**JSON output (success):**

```json
{"status": "initialized", "config_dir": "/home/user/.config/trellis", "data_dir": "/home/user/.local/share/trellis"}
```

**JSON output (already exists):**

```json
{"status": "exists", "config_dir": "/home/user/.config/trellis"}
```

### `trellis admin health`

Check health of Trellis stores.

```bash
trellis admin health [--format text|json]
```

**JSON output:**

```json
{
  "config": true,
  "data_dir": true,
  "stores_dir": true,
  "documents.db": true,
  "graph.db": true,
  "vectors.db": false,
  "events.db": true,
  "traces.db": true
}
```

A value of `false` means the store file does not exist. Run `trellis admin init` to create missing stores.

### `trellis admin stats`

Show store counts.

```bash
trellis admin stats [--format text|json]
```

**JSON output:**

```json
{"status": "ok", "traces": 42, "documents": 15, "document_rows": 37, "nodes": 23, "edges": 31, "events": 127}
```

`documents` counts **whole documents** and reconciles with
`GET /api/v1/documents`' `total`; `document_rows` counts **every stored row**,
including the `<parent>#chunk-N` fragments corpus ingestion writes, and
reconciles with `GET /api/v1/documents?include_chunks=true` (#412). The same
two keys are returned by `GET /api/v1/stats`, field for field.

> **Breaking, since #451:** `documents` used to be the row count. A consumer
> that wants that number reads `document_rows`. Nothing errors on the change —
> a reader that is not updated silently reports a different population.

### `trellis admin serve`

Start the REST API server.

```bash
trellis admin serve [--port PORT] [--host HOST] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `8420` | Port to listen on |
| `--host` | `0.0.0.0` | Host to bind to |

---

## Ingest Commands

### `trellis ingest trace`

Ingest a trace from a JSON file or stdin.

```bash
trellis ingest trace <file> [--format text|json]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | No | Path to trace JSON file. Use `-` or omit for stdin. |

**From file:**

```bash
trellis ingest trace /tmp/my-trace.json --format json
```

**From stdin:**

```bash
cat <<'EOF' | trellis ingest trace - --format json
{
  "source": "agent",
  "intent": "Refactored database connection pooling",
  "steps": [
    {
      "step_type": "tool_call",
      "name": "edit_file",
      "args": {"file": "src/db/pool.py"},
      "result": {"status": "applied"},
      "duration_ms": 200
    }
  ],
  "outcome": {"status": "success", "summary": "Replaced manual connections with pool"},
  "context": {"agent_id": "code-orchestrator", "domain": "backend"}
}
EOF
```

**JSON output (success):**

```json
{"status": "ingested", "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E", "source": "agent", "intent": "Refactored database connection pooling"}
```

**JSON output (validation error):**

```json
{"status": "error", "message": "1 validation error for Trace\nsource\n  Field required"}
```

**Error cases:**
- File not found: exit code 1, prints error message
- Invalid JSON: exit code 1, prints parse error
- Schema validation failure: exit code 1, prints Pydantic validation error

#### Trace → graph extraction (opt-in)

By default trace ingestion is write-only to the TraceStore — the trace is stored but no graph nodes/edges are created. Set the environment variable `TRELLIS_ENABLE_TRACE_EXTRACTION=1` (also accepts `true`/`yes`/`on`) to turn on a **post-ingest** deterministic extraction stage that mines the trace's structured fields into the knowledge graph through the governed `MutationExecutor`.

The flag applies identically across all three trace-ingest paths: the CLI `trellis ingest trace`, the REST `POST /api/v1/traces`, and the MCP `save_experience` tool. Extraction always runs *after* the trace is durably stored, only ever *reads* the trace (traces stay immutable), and is fully fail-soft — a broken extraction is logged and swallowed, never failing the ingest.

What gets extracted (deterministic, structured fields only) is documented in [trace-format.md → Graph Extraction](trace-format.md#graph-extraction-opt-in). Every emitted node and edge carries property-based provenance: `source_trace_id`, `agent_id`, `extractor_tier`, and `extraction_confidence`.

A second, separately opt-in variable gates weak drafts: `TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE=<0.0-1.0>` drops drafts scoring below the floor, plus any edge left pointing at a dropped entity. Unset (the default) means no gate — enabling extraction never also enables a silent drop. It applies to both the live hook and `trellis extract traces`, and the reported entity/edge counts are counted *after* the gate.

When the flag is on, the CLI JSON output gains an `extraction` block:

```json
{"status": "ingested", "trace_id": "01JRK5...", "source": "agent", "intent": "...", "extraction": {"entities": 5, "edges": 4, "failed": 0, "executed": true}}
```

`entities` / `edges` count the commands *submitted*; `failed` counts those the executor rejected. The batch runs `CONTINUE_ON_ERROR`, so a non-zero `failed` is not an error for the ingest — the trace is stored either way — but it does mean some drafts did not land. Persistent non-zero `failed` is worth investigating; the `trace_extraction_commands_failed` log line carries the executor messages.

### `trellis extract traces` (backfill)

Backfill the graph from traces that were already ingested before the flag was enabled (or that need re-extraction). Iterates the TraceStore and runs the same `TraceExtractor` + governed-batch path the live hook uses.

```bash
trellis extract traces [--since <days>] [--domain <name>] [--limit <n>] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--since` | `7` | Backfill traces ingested within the last N days. |
| `--domain` | (none) | Optional `TraceContext.domain` filter. |
| `--limit` | `1000` | Max traces to scan. |
| `--dry-run` | off | Tally and print per-trace draft counts without executing the mutation batch. |
| `--format` | `text` | `text` or `json`. |

This command does **not** require the `TRELLIS_ENABLE_TRACE_EXTRACTION` flag — it is the explicit, operator-driven backfill path. `--dry-run` previews the graph a real run would create without writing anything.

**JSON output:**

```json
{"status": "backfilled", "traces_scanned": 12, "total_entities": 58, "total_edges": 44, "dry_run": false, "per_trace": [{"trace_id": "01JRK5...", "domain": "backend", "entities": 5, "edges": 4}]}
```

#### Document → vector embedding (opt-in)

By default document ingestion is write-only to the DocumentStore — the document is retrievable by keyword FTS but invisible to `SemanticSearch`, because nothing embeds it. Set `TRELLIS_ENABLE_EMBED_ON_INGEST=1` (also accepts `true`/`yes`/`on`) to turn on a **post-ingest** embedding stage that runs the stored content through the registry's configured `embedding_fn` and upserts a vector keyed by the document's `doc_id`.

The flag applies identically across the three document-ingest paths: the REST `POST /api/v1/documents`, the REST `POST /api/v1/evidence`, and the MCP `save_memory` tool. It requires an `embeddings:` block in `config.yaml` (or `TRELLIS_EMBEDDING_FN`) *and* a configured vector store — when either is missing the hook logs a warning and no-ops. Embedding always runs *after* the document is durably stored and is fully fail-soft: a broken or unreachable embedder is logged and swallowed, never failing the ingest.

The vector row's metadata carries a `content` excerpt (500 chars — what `SemanticSearch` renders as the pack excerpt), the document metadata, `doc_id`, and a `created_at` recency stamp.

**That metadata is a snapshot taken at embed time.** A metadata-only re-put to the document store does not re-embed, so anything written to a document *after* it was embedded is invisible to `SemanticSearch` unless something mirrors it across. Two writers now do: retention's archive/restore mirrors the `lifecycle` stamp (#337), and the classify layer — `apply_noise_tags` and `reclassify_item` — mirrors `content_tags` / `auto_importance` (#338), both through `trellis.core.vector_metadata.sync_vector_metadata`, a metadata-only re-upsert that re-embeds nothing. Rows that diverged **before** those writers existed are repaired by `trellis admin resync-vector-metadata` (below), not by `reindex-vectors --force`, which would pay one embedding call per document to fix metadata already sitting in the document store for free.

That stored excerpt is produced by `truncate_excerpt` **at embed time**, not at retrieval time (#310): `build_vector_row` is the last point on the semantic path that still holds the full document, so it is the only point where the cut can break on a boundary and can name how much it dropped (`… [+12.4k chars]`). Retrieval re-runs `truncate_excerpt` over the stored string, which is a no-op for rows this hook wrote. Rows embedded before #310 hold a raw `content[:500]` slice and keep their mid-word cut until re-embedded — `trellis admin reindex-vectors --force` rewrites them through the same shared builder.

On the retrieval side, embedded documents are visible to every `SemanticSearch`/`PackBuilder` consumer. Since #262 the MCP `get_context` and `search` macro tools route through `PackBuilder`, which fuses the keyword, graph and **semantic** axes with Reciprocal Rank Fusion (deduplicated by `item_id`): when the embedder + vector store pair is configured, the query is embedded and vector hits join the fusion. The semantic axis is additive and degrades gracefully — a down embedder means keyword + graph results only, never a failed tool call.

#### `save_memory` fuzzy dedup — seeding the MinHash index (opt-in)

`save_memory` dedups in two stages: an exact content-hash match, then a
MinHash/LSH near-duplicate check that catches typos, casing and punctuation
variants. **Stage 2 only sees documents the index holds**, and by default the
index is seeded with nothing — so it holds only the memories written by that
same server process. A fresh MCP server cannot detect a fuzzy duplicate of
anything already in your store. Before #402 that was a defect (the seed read
`search("")`, which returns zero rows on every backend); it is now the
documented default, with a switch.

`TRELLIS_MINHASH_SEED_MAX_DOCS=<n>` loads the newest `n` whole documents into
the index the first time it is built. Chunk rows are excluded — a chunk is not
in fact a near-duplicate of its parent (measured over the reference
deployment's 740 chunk rows: median Jaccard 0.294, max 0.641, zero pairs over
the 0.85 threshold), but a `Fuzzy duplicate: <parent>#chunk-3` verdict names a
fragment when the whole document is in the index too.

The number is both the switch and the bound, because the cost is the point:

- Seeding is O(corpus). A MinHash signature costs roughly **32 ms per
  document**, so a 735-document corpus takes about **24 seconds of blocking
  CPU** on the first `save_memory` of a process, growing linearly. Under the
  `stdio` transport that is once per session, in every repository the server
  is wired into. Under `http` the prewarm pays it once at boot.
- What it buys, measured on the same deployment over eight weeks: a complete
  seed would have rejected **13 of 735** whole-document writes (1.8%), every
  one verified a genuine near-duplicate. Roughly 1.6 rejections a week.
- Newest-first ordering means a bound below your corpus size misses *old*
  duplicates. On that corpus a bound of 500 covers 68% of it and misses 8 of
  its 17 near-duplicate pairs.

Unset, `0`, negative and unparseable all mean **seed nothing** — a typo must
not switch on a rejection path, nor an unbounded walk. `trellis admin
write-config --format json` reports the effective value; the server logs
`minhash_index_initialized` at INFO with `size`, `rows_read` and `seconds`,
warns `minhash_index_seed_disabled` when a stocked store is running unseeded,
and warns `minhash_index_seed_empty` when a seed read rows but indexed none.

The same index backs the reconcile-on-write verdict tier
(`TRELLIS_ENABLE_RECONCILE_ON_WRITE`), which adjudicates near — not exact —
matches with a model. That tier is equally blind across process boundaries
without a seed.

### `trellis admin reindex-vectors` (backfill)

Backfill embeddings for documents that were ingested before the flag was enabled (or that need re-embedding). Pages through the DocumentStore and builds the same vector rows the live hook writes.

```bash
trellis admin reindex-vectors [--batch-size <n>] [--limit <n>] [--force] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size` | `100` | Documents per page / vectors per bulk upsert. |
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--force` | off | Re-embed documents that already have a vector. |
| `--dry-run` | off | Count what would be embedded without calling the embedder. |
| `--format` | `text` | `text` or `json`. |

This command does **not** require the `TRELLIS_ENABLE_EMBED_ON_INGEST` flag — it is the explicit, operator-driven backfill path. It **does** require the embedder and vector store to be configured, and exits non-zero when they are not. Content-less documents are skipped; per-document embed failures are counted and logged, never fatal. Rerunning is always safe (rows are keyed by `doc_id`).

**JSON output:**

```json
{"status": "ok", "scanned": 240, "embedded": 198, "skipped_existing": 30, "skipped_empty": 12, "errors": 0, "dry_run": false}
```

### `trellis admin resync-vector-metadata` (divergence repair)

Re-sync stale vector-row metadata from the document store. Pages the DocumentStore, compares each document against its vector row on `content_tags` / `auto_importance`, and metadata-only re-upserts the ones that disagree — **calling no embedder and needing none configured**.

```bash
trellis admin resync-vector-metadata [--batch-size <n>] [--limit <n>] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size` | `500` | Documents per `list_documents` page. |
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--dry-run` | off | Count divergent rows without rewriting any. |
| `--format` | `text` | `text` or `json`. |

This is the one-time repair for rows written before the write-through shipped (#338 measured 45 noise-tagged documents in production, none of whose vector rows agreed — so the default `signal_quality="noise"` exclusion could not hold for any of them on the semantic axis). Documents with no vector row are counted under `no_vector_row`, not as errors — embed-on-ingest is opt-in. Per-row failures are counted and logged, never fatal.

It is **idempotent**: a row already in agreement is not rewritten, so a second run reports `repaired: 0`. That makes it safe to schedule and turns "is the write-through holding?" into a question with an answer — a steady-state run reporting non-zero means some writer is still bypassing it.

**JSON output:**

```json
{"status": "ok", "scanned": 1019, "divergent": 45, "repaired": 45, "no_vector_row": 109, "errors": 0, "keys": ["content_tags", "auto_importance"], "dry_run": false}
```

#### Document → content tags (opt-in)

By default document ingestion writes no retrieval-shaping tags — the document is retrievable, but `PackBuilder`'s `tag_filters`, the `signal_quality="noise"` exclusion and the tag-derived importance boost have nothing to work with. Set `TRELLIS_ENABLE_CLASSIFY_ON_INGEST=1` (also accepts `true`/`yes`/`on`) to turn on **classify-on-write**: an inline, deterministic pass (structural + keyword-domain + source-system classifiers, no LLM, microseconds) that stamps `metadata.content_tags` and `metadata.auto_importance` as the document is written.

One flag, five write seams:

| Surface | Seam |
|---------|------|
| `trellis ingest corpus`, `trellis ingest conversations`, the session-capture sweep | `ingest_corpus.sync.sync_records` — parents *and* chunks (the chunk is the retrievable unit, so it inherits the parent's tags) |
| MCP `save_memory` | after both dedup stages, before the put — including every reconcile-on-write verdict that stores a document |
| MCP `save_knowledge` | the evidence document it auto-creates (`mutate.evidence.ensure_evidence_document`) |
| REST `POST /api/v1/documents` | before the store put |
| REST `POST /api/v1/evidence` | before the store put |

The four single-document seams call one helper, `trellis.classify.ingest.classify_metadata_on_write`, so the persisted tag shape does not drift: flag-gated (off returns the caller's metadata untouched), fill-if-absent (existing `content_tags` — an earlier write, or the LLM enrichment pass — are never clobbered), fail-soft on *any* error including a `metadata` that is not a mapping, no auto-`domain` (see below), and a no-op on empty or whitespace-only content, matching the embed-on-ingest skip. The bulk seam calls the same `classify_for_ingest` core inline and shares the flag gate, fill-if-absent and no-auto-`domain`; it has neither the empty-content skip nor the non-mapping guard, because it classifies against metadata already merged with the stored document's.

**Not covered.** `trellis ingest evidence` and `trellis ingest dbt-manifest` put documents straight to the store with no classify hook, so they stay untagged whatever the flag says. `trellis classify backfill` is the fix for them, exactly as for pre-flag rows.

One deliberate omission: the classifier-derived **`domain` facet is dropped** before persisting. `domain` is the only facet that *hard-excludes* a document from a domain-scoped query on mismatch, and the deterministic keyword / source-system classifiers will confidently assign a code-flavoured domain to personal content. The operator-set scalar `metadata['domain']` (the `--domain` flag) is a separate key and is untouched. Every facet that *is* persisted only shapes ranking, sectioning, or noise exclusion — a wrong value degrades ranking at worst, it never hides content.

The drop is persisted as `content_tags.domain: []`, not as a missing key — so **an empty list facet default-passes a tag filter exactly like an absent one** (both document stores, since #282). This is load-bearing rather than incidental: the stores originally default-passed only a `NULL` facet, so `[]` read as a value and every classify-on-write-tagged document was hard-excluded from every domain-scoped query. Turning tagging on would have hidden content. The rule now is *empty carries no value, and no value cannot exclude*; it also repairs rows already written with `domain: []` without a backfill. See [schemas.md → ContentTags](schemas.md#contenttags).

### `trellis classify backfill`

Backfill tags for documents ingested before the flag was enabled, and refresh tags that have drifted (the keyword vocabulary grew, the graph around a document changed). Pages the DocumentStore and re-runs the deterministic tagging pipeline over every item whose `content_tags.classified_at` is missing or older than `--max-age-days`, through the same `reclassify_item` core the programmatic path uses.

```bash
trellis classify backfill [--max-age-days <n>] [--limit <n>] [--page-size <n>] [--include-domain] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--max-age-days` | `30` | Re-tag items whose tags are older than this. `0` re-tags every scanned item. |
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--page-size` | `100` | Documents fetched per store round-trip. |
| `--include-domain` | off | **Dangerous** — let the classifiers (re)assign the hard-excluding `domain` facet. See below. |
| `--dry-run` | off | Report what would change without writing tags or emitting events. |
| `--format` | `text` | `text` or `json`. |

Tag writes pass `preserve_updated_at=True`: re-tagging is derived metadata and does not make a document's information fresher, while `updated_at` drives the recency decay in `retrieve.strategies`. Without it a first backfill over an untagged corpus would rewrite every row and flatten recency ordering across the whole store.

Like the two backfills above, this command does **not** require the `TRELLIS_ENABLE_CLASSIFY_ON_INGEST` flag — invoking it *is* the opt-in. It never deletes tags: an item the pipeline produces no signal for keeps whatever it had, and is counted under `skipped_no_signal`. Each write emits a `TAGS_REFRESHED` event carrying the before/after tag diff; a dry run emits nothing.

Every scanned document lands in exactly one bucket: `refreshed`, `skipped_fresh` (stamp newer than `--max-age-days`), `skipped_unchanged` (stale stamp, but re-running the pipeline produces the same tags — so nothing is written and no event is emitted), `skipped_no_signal`, `skipped_missing_content`, or `errors`. That means **`--dry-run` previews what would *change*, not what is merely stale**, and the two runs agree. An unchanged document keeps its old `classified_at` and is re-scanned (never rewritten) by the next backfill.

The scan is fail-soft per document: a row that fails to process — a hand-edited `auto_importance`, a `content_tags` value of the wrong shape — is logged with a traceback, counted in `errors`, and skipped; the rest of the store is still backfilled. `status` is then `"partial"` rather than `"ok"`. The exit code stays `0` (matching `admin reindex-vectors`) — read `errors` / `status`, not the exit code, to detect a partial run. A malformed `classify:` block in `config.yaml` is a different failure: the command exits `1` with `{"status": "error", ...}` before scanning anything.

`--include-domain` re-enables the facet classify-on-write deliberately drops, with the same hazard: a deterministic keyword match that assigns the wrong `domain` will hide the document from domain-scoped retrieval rather than merely mis-rank it. Leave it off for a backfill over mixed content; use it only with a vocabulary you trust (`classify.domain_keywords` in `config.yaml`) or an enrichment-mode pipeline.

Two things to know about how this differs from classify-on-write:

- **The backfill honours `classify.domain_keywords`; classify-on-write does not.** The write path uses built-in defaults because it drops the `domain` facet anyway. Here a keyword hit still contributes `retrieval_affinity`, adds the classifier to `classified_by`, and raises that classifier's confidence (which drives per-facet merge precedence) — so a backfilled document can carry slightly different tags than the same document tagged at ingest, even with `--include-domain` off. That is intentional: a backfill is an explicit operator action, and reading the operator's configured vocabulary is the point of writing it.
- **Tag writes are a sanctioned exception to the governed-mutation pipeline.** Both this command and classify-on-write write `metadata.content_tags` / `metadata.auto_importance` straight to the DocumentStore and emit `TAGS_REFRESHED` by hand rather than routing through `MutationExecutor`. A refresh rewrites derived metadata on an existing row — no entity created, no content changed, fully reconstructible by re-running the pipeline — while per-row validate/policy/idempotency stages are uneconomical at whole-store scale. The `TAGS_REFRESHED` event preserves the audit trail. See the module docstring of `trellis/classify/refresh.py`.

**JSON output:**

```json
{"status": "ok", "scanned": 240, "refreshed": 31, "skipped_fresh": 200, "skipped_unchanged": 0, "skipped_no_signal": 6, "skipped_missing_content": 3, "errors": 0, "dry_run": false, "include_domain": false, "item_ids_refreshed": ["doc-1", "doc-2"]}
```

### `trellis classify shadow`

Record what an LLM says about stored documents **without serving it** — the precondition for teaching the deterministic classifier a vocabulary it has never observed ([#321](https://github.com/ronsse/trellis-ai/issues/321) Phase 1).

```bash
trellis classify shadow [--limit <n>] [--max-age-days <n>] [--page-size <n>] [--model-id <label>] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--max-age-days` | `-1` | Re-judge shadow records older than this. The default judges only documents with **no** shadow record — each one costs a model call. |
| `--page-size` | `100` | Documents fetched per store round-trip. |
| `--model-id` | `""` | Label recorded on each record and event (e.g. `hermes3:8b`). Defaults to the classifier's name. |
| `--dry-run` | off | Compute records without writing them or emitting events. |

Requires an `llm:` block in `config.yaml`; without one the command exits `1` naming what is missing rather than silently shadowing nothing. A **local** model is the intended configuration — the pass is ~1.6 s per document against a whole corpus, which is free and private on-host and expensive anywhere else.

**Why a separate key.** Verdicts are written to `metadata.content_tags_shadow`, never to `content_tags`. Two things follow, and both are the point:

- **Retrieval does not move.** No pack ranking changes while the corpus accrues, so this is safe to run against a production store. The guarantee is enforced in two independent places: tag filters address the JSON path `$.content_tags.<facet>`, so a sibling top-level key is structurally unaddressable; and `trellis.retrieve.servable.strip_non_servable` runs where `PackBuilder` collects every strategy's results, so it covers strategies added later and out of tree, not just the built-ins. Note the scope of that claim — shadow tags never reach a *pack*; they are not confidential. `GET /api/v1/documents` returns them to a read-scoped caller, correctly, since that is the same access path as the content they describe.
- **It avoids destroying data on a vocabulary collision.** The deterministic path emits `ContentType` values (`error-resolution` / `procedure` / `code`); the enrichment path emits `DEFAULT_CLASSIFICATIONS` (`reference` / `research` / `notes` / …). The two overlap in exactly one value, `documentation`. Writing LLM output into `content_tags` would replace one taxonomy with another — and would in fact raise `ValidationError`, since `ContentTags.content_type` is a closed `Literal`. The shadow record stores the LLM's vocabulary verbatim, because the disagreement is the measurement Phase 2 mines.

Each judged document also emits a leak-safe `MEMORY_OP_JUDGED` event ([#264](https://github.com/ronsse/trellis-ai/issues/264)'s substrate — this is its classify-layer instance, not a parallel channel): digest, `content_type` verdict label, confidence, and a subject pointer. The open-vocabulary `domain` tags stay **off** the event — a value like `yellowstone-national-park` reveals subject matter, and the event log has a different access/retention profile than the doc store. A document that produces no tags still emits a verdict of `unclassified`: "the model looked and produced nothing" is the coverage signal the pass exists to measure.

This is a batch pass only, never an ingest hook. Classify-on-write is deterministic and inline for a reason (microseconds, no network); a model call there would spend the write path's latency budget on a measurement. Documents written since the last pass are covered by re-running it.

**JSON output:**

```json
{"status": "ok", "scanned": 999, "written": 964, "skipped_fresh": 0, "skipped_no_signal": 31, "skipped_missing_content": 4, "errors": 0, "stale_snapshot": 0, "dry_run": false, "item_ids_written": ["doc-1", "doc-2"]}
```

`stale_snapshot` ([#421](https://github.com/ronsse/trellis-ai/issues/421)) counts documents another writer changed while the model was judging them. The model call sits between the row's read and its write, so the write re-reads and overlays only the shadow key — the record still lands, on the current content, and every live tag written in the meantime survives. It is a **subset of `written`**, not a failure bucket, so it does not move `status`. A row deleted inside that window is not re-inserted and is counted in `errors`.

### `trellis classify shadow-report`

Compare shadow tags against live tags, per facet and per document. Read-only.

```bash
trellis classify shadow-report [--limit <n>] [--page-size <n>] [--per-document] [--format text|json]
```

Answers the two questions that decide whether a shadow corpus is worth promoting from: where does the LLM produce a facet the deterministic pipeline leaves empty (`live_missing` — the coverage gain), and where do the two disagree outright (`disagreed`).

`agreement_rate` is `null` when nothing is comparable, rather than `0.0` or `1.0`. A rate over an empty denominator is not a measurement, and reporting one as a number is how a metric ends up wired to a constant.

An empty `domain` list counts as **absent**, not as a value. Every document classify-on-write tags stores `domain: []` deliberately, so counting it as "live said something different" would report near-total disagreement on the one facet where the live side has, by design, said nothing.

`out_of_vocabulary_content_types` counts shadow `content_type` values outside the live `ContentType` vocabulary — the collision above, made countable. A non-empty map means promoting shadow `content_type` wholesale would mean *adopting a different taxonomy*, not refining the current one.

**JSON output:**

```json
{"status": "ok", "scanned": 999, "with_shadow": 964, "facets": {"content_type": {"agreed": 88, "disagreed": 42, "live_missing": 780, "shadow_missing": 12, "both_missing": 42, "agreement_rate": 0.677}}, "out_of_vocabulary_content_types": {"reference": 310, "notes": 204}}
```

### `trellis classify tag-candidates`

Mine the shadow corpus for keyword rules the deterministic classifier could own, so the LLM can be switched off for what has been learned ([#321](https://github.com/ronsse/trellis-ai/issues/321) Phase 2) — "documents containing `todoist` were tagged `task-management` in 40 of 41 cases; propose that keyword".

```bash
trellis classify tag-candidates [--facet <name>] [--limit <n>] [--emit/--no-emit] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--facet` | `domain` | Shadow facet to mine. `domain` is the only facet with a config write target. |
| `--limit` | `50000` | Cap on documents scanned. |
| `--emit / --no-emit` | emit | Emit a `TAG_KEYWORD_CANDIDATE` event per candidate. `--no-emit` is a dry run and does not advance the cooldown. |

**Surface-only, never auto-applied.** The command proposes; it never writes `config.yaml`. The text output ends with a paste-ready `classify.domain_keywords` block — pasting it promotes, deleting those lines revokes. That is not a missing feature: `domain` is the one facet that *hard-excludes* a document from a domain-scoped query, so a wrong promoted keyword **hides** content rather than merely mis-ranking it (the #282 failure mode). A human approves every domain promotion.

Thresholds come from the `ParameterRegistry` under component `learning.tag_evolution`. The **analyzer** never substitutes a default — a missing key raises, naming it and its recommended seed. The **command** seeds the recommended values for that run and warns on stderr, matching `trellis analyze schema-evolution`; nothing seeds this component automatically, so hard-failing would mean failing on every deployment. Seed them for real (making runs reproducible) from `trellis.learning.tag_evolution.RECOMMENDED_SEED_VALUES`:

| Key | Seed | Gate |
|-----|------|------|
| `tag_keyword_min_support` | `30` | Documents containing the keyword **and** carrying the tag. |
| `tag_keyword_min_precision` | `0.75` | `support / keyword_documents` — how reliably the keyword predicts. |
| `tag_keyword_min_lift` | `0.25` | Minimum `lift - 1` over the tag's base rate. |
| `tag_keyword_min_corpus` | `30` | Shadow-corpus floor; below it, nothing is surfaced at all. |
| `tag_keyword_cooldown_days` | `7` | Days before an unchanged candidate re-surfaces. |

`min_support` and `min_lift` follow the precedent in `learning/tuners/auto_promote.py` (30 samples / 0.25 effect) rather than inventing a second statistical convention.

An out-of-range threshold (`min_precision` outside `(0, 1]`, a negative `min_lift`) or a malformed `classify.domain_keywords` block exits `1` with the message, not a traceback.

**Lift is the load-bearing gate.** In a corpus where one tag is on most documents, *every* keyword has high precision for it — precision alone would surface the whole vocabulary as "perfectly predictive". Lift measures how much better than the tag's own base rate the keyword does, so a keyword that only matches the base rate dies.

The analyzer shares the four constraints of the well-known promotion loop it is modelled on: read-only against the corpus (the only write is the event), thresholds-or-raise, idempotent across runs (a candidate re-surfaces only on ≥20% support growth or an elapsed cooldown — shared with `schema_evolution` via `trellis.learning.cooldown`), and it **filters its own writes** — a keyword the live classifier already owns is excluded before counting, so a promoted keyword can neither be re-proposed nor read its own prior promotion as fresh evidence.

Two properties of `KeywordDomainClassifier` shape the safety story. It matches keywords as *substrings* while the analyzer mines whole *tokens*, so a mined keyword always fires (the mining is strictly more conservative than the matching). And a domain needs **two** distinct keyword hits before it is assigned — so promoting a single keyword cannot by itself make the classifier assign a domain, and one bad promotion cannot hide a document on its own.

**What this does not measure.** Promotion is gated on *agreement with the LLM*, not on retrieval outcome. Agreement shows the deterministic rule imitates the model; it does not show either was useful. Closing that requires joining promoted tags back to pack feedback — the same attribution machinery `learning/pack_observations.py` runs. Until that join exists this is a distillation step with a human gate, not a closed learning loop, and every candidate's `notes` say so.

**JSON output:**

```json
{"status": "ok", "facet": "domain", "emitted": true, "candidates": [{"candidate_id": "3f2a...", "facet": "domain", "keyword": "todoist", "tag": "task-management", "support": 40, "keyword_documents": 41, "tag_documents": 44, "corpus_documents": 964, "precision": 0.976, "recall": 0.909, "lift": 21.4, "has_write_target": true, "example_item_ids": ["doc-1"], "recurrence_count": 0, "notes": ["gated on agreement with the LLM, not on retrieval outcome — this shows the rule imitates the model, not that either helped", "domain hard-excludes on mismatch: a wrong keyword hides documents from domain-scoped queries rather than re-ranking them — human approval required"]}], "domain_keywords_fragment": {"task-management": ["todoist"]}}
```

### `trellis ingest evidence`

Ingest evidence from a JSON file.

```bash
trellis ingest evidence <file> [--format text|json]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | **Yes** | Path to evidence JSON file |

**Example:**

```bash
cat <<'EOF' > /tmp/evidence.json
{
  "evidence_type": "snippet",
  "content": "The connection pool should use a max of 20 connections per process.",
  "source_origin": "manual",
  "uri": "https://wiki.internal/db-guidelines"
}
EOF

trellis ingest evidence /tmp/evidence.json --format json
```

**JSON output (success):**

```json
{"status": "ingested", "evidence_id": "01JRK6M3QF8GHTM2XVZP3CWD9E", "evidence_type": "snippet"}
```

### `trellis ingest corpus`

Sync a directory of files (a notes vault, a folder of transcripts) into
the document store, idempotently. See
[`adr-corpus-ingestion.md`](../design/adr-corpus-ingestion.md).

```bash
trellis ingest corpus <path> [--source-system corpus] [--domain X] \
    [--tag k=v ...] [--include '*.md'] [--dry-run] [--prune] [--extract] [--format json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `path` | — | Directory (or single file) to ingest |
| `--source-system` | `corpus` | Corpus namespace — part of every `doc_id` (`corpus:<source_system>:<sha1(relpath)>`); the classification layer keys on it (e.g. `obsidian`) |
| `--domain` | — | Domain tag applied to every written document |
| `--tag k=v` | — | Extra metadata (repeatable) |
| `--include` | all files | Glob filter over relative paths (repeatable) |
| `--dry-run` | off | Report the full plan (files, chunk counts, skips) without writing |
| `--prune` | off | Delete documents whose source file vanished |
| `--extract` | off | Mine entities/edges from prose into the graph (see below) |

Re-running over an unchanged tree performs zero writes (`content_hash`
comparison). Edited files re-put under the same `doc_id` and re-embed
changed chunks; moved files are re-keyed via `get_by_hash`, not
duplicated. Documents longer than the 8,000-char embed cap are split
into paragraph-aware **chunk documents**
(`<parent_doc_id>#chunk-<i>`, metadata `{parent_doc_id, chunk_index,
chunk_count, source_path, char_span}`) — with
`TRELLIS_ENABLE_EMBED_ON_INGEST=1` the chunks are what gets embedded,
so long-document content is semantically retrievable. Markdown YAML
frontmatter becomes document metadata and `[[wikilinks]]` are collected
into `metadata.wikilinks` (candidates only — no graph writes).
Cross-file near-duplicates are warned about in the report, never
skipped. Every new/changed file emits `MEMORY_STORED`; each run emits a
`CORPUS_SYNCED` summary event.

**Entity extraction (`--extract`)** is **double-gated**: the `--extract`
flag *and* the `TRELLIS_ENABLE_MEMORY_EXTRACTION` env flag must both be
set (at corpus scale it's a per-run LLM-cost decision). When on and an
LLM client is configured, each new/changed document's prose is mined for
entity/edge drafts (the same `build_save_memory_extractor` pipeline the
MCP `save_memory` path uses — deterministic alias-match + LLM residue)
and routed through the governed `MutationExecutor`; the run report gains
`entities_extracted` / `edges_extracted`. Fully fail-soft — a broken
extractor never fails the ingest. With either gate off (the default), no
extraction runs.

**JSON output (abridged):**

```json
{"status": "synced", "counts": {"files_seen": 3, "ingested": 2, "updated": 1, "moved": 0, "skipped_unchanged": 0, "skipped_unsupported": 1, "pruned": 0, "chunks_written": 6, "warnings": 0}, "files": [{"path": "runbooks/deploy.md", "doc_id": "corpus:obsidian:7417df…", "action": "new", "chunks": 6}]}
```

### `trellis ingest conversations`

Sync a **Claude conversation export** — the personal context in your
everyday Claude chat — into the document store as one document per
conversation. This is the capture path for real usage: the memories you
accumulate by talking to Claude, which the Claude Code / MCP path never
sees. Shares the idempotent sync core with `ingest corpus`.

```bash
trellis ingest conversations <path> [--source-system claude-ai] \
    [--domain X] [--tag k=v ...] [--dry-run] [--prune] [--extract] [--format json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `path` | — | A `conversations.json`, the `.zip` export containing it, or a directory holding it |
| `--source-system` | `claude-ai` | Corpus namespace — part of every `doc_id` |
| `--domain` / `--tag k=v` | — | Metadata applied to every written document |
| `--dry-run` | off | Report the plan without writing |
| `--prune` | off | Delete conversations no longer present in the export |
| `--extract` | off | Mine entities/edges from conversation prose into the graph (double-gated with `TRELLIS_ENABLE_MEMORY_EXTRACTION`; see `ingest corpus`) |

**Getting the export:** in claude.ai, Settings → Privacy → *Export data*;
you'll be emailed a `.zip`. Point this command at the `.zip` (or the
unzipped `conversations.json`). Each conversation becomes a document with
`doc_id = conversation:<source_system>:<uuid>`, a speaker-labelled
transcript (`**You:**` / `**Claude:**`), and metadata `{conversation_id,
title, created_at, updated_at, message_count, document_form:
"conversation"}`. (`document_form` was a flat `content_type` key before
#288, which collided with the closed `ContentTags.content_type` facet;
read it with `document_form_of(metadata)`, which accepts both spellings —
stored documents are not migrated, only normalised when the ingest seam
next rewrites them. See [schemas.md → DocumentMetadata](schemas.md#documentmetadata).)
Long conversations are chunked and (with
`TRELLIS_ENABLE_EMBED_ON_INGEST=1`) embedded, so a topic buried deep in a
chat is retrievable. Re-exporting and re-syncing is idempotent:
unchanged conversations skip, conversations that grew new turns re-put and
re-embed. Identity is the conversation's own uuid, so re-imports never
duplicate. The reader tolerates both claude.ai message shapes (top-level
`text` and `content` block lists) and skips thinking / tool-use blocks and
empty turns. Mining entities (people, accounts, preferences) out of the
prose into the graph is the flag-gated `--extract` follow-up (ADR §5).

### `trellis ingest dbt-manifest`

Import a dbt manifest into the knowledge graph.

```bash
trellis ingest dbt-manifest <manifest-path> [--format text|json]
```

Creates entities for models, seeds, snapshots, sources, and tests. Creates `depends_on` edges from the manifest's dependency graph. Indexes descriptions into the document store.

**JSON output:**

```json
{"status": "ok", "nodes_created": 12, "edges_created": 8}
```

### `trellis ingest openlineage`

Import OpenLineage events into the knowledge graph.

```bash
trellis ingest openlineage <events-path> [--format text|json]
```

Reads a JSON array or newline-delimited JSON file of OpenLineage events. Creates dataset and job entities with `reads_from` and `writes_to` edges.

**JSON output:**

```json
{"status": "ok", "nodes_created": 6, "edges_created": 4}
```

---

## Curate Commands

### `trellis curate promote`

Promote a trace to a precedent (reusable institutional knowledge).

```bash
trellis curate promote <trace_id> --title <title> --description <description> [--by <who>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `trace_id` | **Yes** | -- | Trace ID to promote |
| `--title` | **Yes** | -- | Title for the precedent |
| `--description` | **Yes** | -- | Description of the pattern |
| `--by` | No | `"cli"` | Who is promoting |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate promote 01JRK5N7QF8GHTM2XVZP3CWD9E \
  --title "Database pool configuration pattern" \
  --description "When configuring connection pools, use max 20 connections per process with 30s idle timeout" \
  --by code-orchestrator \
  --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK7A3QF8GHTM2XVZP3CWD9E",
  "operation": "precedent.promote",
  "message": "Precedent promoted",
  "created_id": "01JRK7A4QF8GHTM2XVZP3CWD9E"
}
```

### `trellis curate link`

Create a directed edge between two entities.

```bash
trellis curate link <source_id> <target_id> [--kind <edge_kind>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `source_id` | **Yes** | -- | Source node ID |
| `target_id` | **Yes** | -- | Target node ID |
| `--kind` | No | `entity_related_to` | Edge kind (any string; well-known values below) |
| `--format` | No | `text` | Output format |

**Well-known EdgeKind values** (custom domain-specific values are also accepted):

| Value | Meaning |
|-------|---------|
| `trace_used_evidence` | Trace consumed this evidence |
| `trace_produced_artifact` | Trace created this artifact |
| `trace_touched_entity` | Trace interacted with this entity |
| `trace_promoted_to_precedent` | Trace was promoted to this precedent |
| `entity_related_to` | General entity relationship |
| `entity_part_of` | Entity is part of another |
| `entity_depends_on` | Entity depends on another |
| `evidence_attached_to` | Evidence is attached to a target |
| `evidence_supports` | Evidence supports a claim |
| `precedent_applies_to` | Precedent applies to this domain/entity |
| `precedent_derived_from` | Precedent was derived from this source |

**Example:**

```bash
trellis curate link 01JRK5N7QF auth_service --kind entity_depends_on --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK8B2QF8GHTM2XVZP3CWD9E",
  "operation": "link.create",
  "message": "Link created",
  "created_id": "01JRK8B3QF8GHTM2XVZP3CWD9E"
}
```

### `trellis curate label`

Add a label to an entity.

```bash
trellis curate label <target_id> <label> [--format text|json]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `target_id` | **Yes** | Entity ID to label |
| `label` | **Yes** | Label string to add |

**Example:**

```bash
trellis curate label 01JRK5N7QF critical-path --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "operation": "label.add",
  "message": "Label added",
  "created_id": null
}
```

### `trellis curate redact`

Redact (hard-purge) a graph entity through the governed pipeline. Irreversible:
removes **all** SCD-2 versions of the node, every edge touching it, its aliases,
and its vector entry. The `REDACTION_APPLIED` audit event records the shape of
what was removed (counts + id pointers), never the content.

Scope boundaries, stated plainly:

- **Linked documents are not cascaded** — the union of the purged node's
  document links across all versions rides the audit payload for a follow-up
  document-level redaction.
- **Observation/Measurement nodes about the subject are not cascaded** — they
  are independent governed nodes and property-based queries
  (`query_observations`, `GET /measurements`) keep serving them after the
  purge. Their ids ride the payload (`linked_observation_ids` /
  `linked_measurement_ids`); redact each individually if needed.
- **Prior Operational-Plane events are immutable** — the original
  `entity.created` / `entity.updated` payloads (which include the name) remain
  readable via the explore API. Redaction clears the Knowledge-Plane serving
  path; it does not rewrite the audit log.

Prompts for confirmation unless `--yes` is passed (required for scripted use).
Exit codes follow the `exit_codes` map: `2` (rejected — e.g. blank/over-long
reason), `5` (failed — e.g. target not found), `0` success.

```bash
trellis curate redact <target_id> --reason <text> [--yes] [--by <caller>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `target_id` | **Yes** | -- | Entity/node ID to hard-purge |
| `--reason` | **Yes** | -- | Audit-trail justification (non-empty, ≤2000 chars, keep it content-free) |
| `--yes`, `-y` | No | `false` | Skip the interactive confirmation |
| `--by` | No | `cli:redact` | Audit-trail identifier for the caller |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate redact 01JRK5N7QF --yes --reason "defect-minted entity (#299)" --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "target_id": "01JRK5N7QF",
  "message": "Entity redacted: 01JRK5N7QF (1 version(s), 0 edge(s), 0 alias(es), vector_deleted=False)"
}
```

On failure/rejection the JSON is `{"status": "failed"|"rejected", "command_id": ..., "message": ...}` — `command_id` joins the attempt to its audit event.

### `trellis curate feedback`

Record feedback (rating and optional comment) on a trace or precedent.

```bash
trellis curate feedback <target_id> <rating> [--comment <text>] [--pack-id <id>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `target_id` | **Yes** | -- | Trace or precedent ID |
| `rating` | **Yes** | -- | Rating as float (0.0 to 1.0 by convention) |
| `--comment` | No | `null` | Optional text comment |
| `--pack-id` | No | `null` | Context pack the feedback is about — the join key |
| `--format` | No | `text` | Output format |

`--pack-id` is what lets the learning loop see the event at all:
`join_pack_feedback` matches `FEEDBACK_RECORDED` to `PACK_ASSEMBLED` strictly
on `payload.pack_id` and skips events without it. This command still records a
**pack-level grade only** — the per-item `helpful_item_ids` /
`unhelpful_item_ids` attribution the promote half consumes comes from the MCP
`record_feedback` tool or `POST /packs/{pack_id}/feedback`.

**Example:**

```bash
trellis curate feedback 01JRK5N7QF 0.9 --comment "Solid pattern, well-documented" --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRKAB1QF8GHTM2XVZP3CWD9E",
  "operation": "feedback.record",
  "message": "Feedback recorded",
  "created_id": null
}
```

---

## Policy Commands

Stage 2 of the [governed mutation pipeline](#python-only-mutation-api) evaluates
governance policies against every command. These commands declare them.

**Default posture: Trellis ships zero policies, and a deployment with none
behaves exactly as if no gate existed.** An empty gate allows every command and
adds nothing to the audit payload. You have to write a policy for anything here
to have an effect. Wiring the gate is not itself a restriction.

### Where policies live

`<data_dir>/stores/policies.json` — written by `trellis policy add` and by
`POST /policies`, read by the mutation pipeline. All three are resolved through
one helper (`trellis.mutate.resolve_policy_path`) so the file you write is the
file that gets enforced.

A file at the pre-unification CLI path `<data_dir>/policies.json` is still
honoured, with a warning naming both paths; the canonical path wins if both
exist. Move it when you see the warning.

The file is **loaded strictly**: if it exists but is corrupt, unparseable, has
no `"policies"` key, or contains an invalid policy, mutation surfaces raise
`ConfigError` rather than degrading to zero policies. Silently falling back
would mean a damaged access-control file disables access control while the
caller believes it is governed. A deployment with *no* policy file is
unaffected — strictness only begins once you have declared something.

Policies are read per call, so an edit takes effect without a restart.

### If the file is damaged

The CRUD store (`trellis policy`, `/api/policies`) and the enforcement reader
treat a damaged file differently, on purpose:

* **Enforcement fails closed.** Every mutation surface raises `ConfigError`
  naming the file. The pipeline stops; it does not run ungoverned.
* **`trellis policy list` still works**, showing whatever parsed — that is the
  point of a separate CRUD reader — but it prints a `POLICY STORE DEGRADED`
  banner above the listing and **exits 5**, because a partial view of an
  access-control file must not be scriptable as the whole ruleset. `--format
  json` carries a `store_degradation` object with the same detail.
* **Every write is refused**, on the CLI (exit 5) and over REST (HTTP 409),
  including `remove`. A store that could not read its own file has no business
  rewriting it: the rewrite would produce a *valid* file containing only what
  parsed, which enforcement would then accept as the truth (#413).
* Recovery is yours to run, and every surface prints it:
  `mv <path> <path>.corrupt`. Nothing moves the file for you — the readers here
  include pack assembly, and a read must not mutate your filesystem. After the
  `mv` the path is absent, which is a legitimate transparent zero-policy
  deployment, so **re-declare your policies**.

### If another process wrote the file first

Two processes write `policies.json` on a typical deployment — `trellis
policy add` on the host and `POST /api/policies` in a container, against
the same data dir. Both stores whole-file-rewrite, so a write from a view
that predates the other's write would silently delete it.

A write is therefore refused when the file changed after the store read it:
exit **5** on the CLI, HTTP **409** with `"code": "stale_store_write"` over
REST. Unlike the damaged case this one is **transient** — re-read and redo
the operation. It is a compare-and-swap rather than a lock, so it closes
the wide window and narrows the tiny one; last-writer-wins is still the
model for two writes that genuinely race.

### `--format json` shapes

`trellis policy list` and `show` carry the house `status` key (`"ok"` /
`"degraded"`), plus `store_degradation` when degraded. `list` also reports
`policy_file` and `policy_file_present`.

**`show` returns an envelope** — `{"status", "policy"}` — not a bare
`Policy` dump. Every Trellis schema is `extra="forbid"`, so a dump with a
`store_degradation` key alongside it is a payload `Policy.model_validate`
rejects; the envelope is what lets the degradation ride along. Read the
policy from `.policy`.

`trellis policy list` also distinguishes the two ways of getting an empty
answer: an absent file (the shipped default) says nothing extra, while a file
containing `{"policies": []}` says so and notes that Stage 2 is transparent.
Enforcement deliberately does not make that distinction — every *dangerous*
route to zero policies raises, so what is left is you having declared zero.

### Scope levels — which commands a policy matches

| Level | Matches when |
|-------|--------------|
| `global` | always |
| `domain` | `command.metadata["domain"]` equals the scope value |
| `team` | `command.metadata["team"]` equals the scope value |
| `entity_type` | `command.target_type` equals the scope value |

**Deny wins.** Every matching policy is evaluated, broadest first, and the first
blocking rule stops the command. Specificity orders evaluation; it is **not** an
override mechanism — a narrow `allow` cannot carve an exception out of a broad
`deny`, because the broad policy is evaluated first and returns. This is the
explicit-deny-wins posture of an IAM policy, and it is deliberate: for access
control, the conservative resolution is the safe one. Express an exception by
narrowing the `deny` rule's `operation` pattern or its scope.

`allow` rules are therefore inert — a command matching no policy is already
allowed, so an `allow` documents intent without granting anything.

### Enforcement levels — what a matching rule does

| `rule.action` | `--enforcement enforce` | `--enforcement warn` | `--enforcement audit_only` |
|---|---|---|---|
| `allow` | pass | pass | pass |
| `deny` | **block** | warn | log only |
| `require_approval` | **block** | warn | log only |
| `warn` | warn | warn | log only |

"Warn" means the command **succeeds** and the warning is returned on
`CommandResult.warnings` and recorded as `policy_warnings` on the
`MUTATION_EXECUTED` audit event. "Log only" is silent to the caller and leaves
only a structlog record. Use `warn` to stage a policy before enforcing it.

### `trellis policy add`

```bash
trellis policy add --operation <pattern> [--action ...] [--scope ...] [--enforcement ...]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--operation` | **Yes** | -- | Operation pattern: `entity.create`, `entity.*`, or `*` |
| `--action` | No | `deny` | `allow`, `deny`, `require_approval`, `warn` |
| `--scope` | No | `global` | `global`, `domain`, `team`, `entity_type` |
| `--scope-value` | No | `null` | Required for non-global scopes |
| `--type` | No | `mutation` | `mutation`, `access`, `retention`, `redaction` |
| `--condition` | No | `always` | Human-readable label; echoed in the rejection message |
| `--enforcement` | No | `enforce` | `enforce`, `warn`, `audit_only` |
| `--format` | No | `text` | Output format |

`--condition` is a **label, not a predicate** — it is not evaluated. It exists
so the rejection message explains itself to whoever hits it. Write it for that
reader.

**Example — freeze entity creation, warning first:**

```bash
trellis policy add --operation entity.create --action deny \
  --enforcement warn --condition "entity freeze pending schema review" --format json
```

Promote to blocking by removing that policy and re-adding it with
`--enforcement enforce`.

### `trellis policy list` / `show` / `remove`

```bash
trellis policy list [--format text|json]
trellis policy show <policy_id_or_prefix> [--format text|json]
trellis policy remove <policy_id_or_prefix> [--format text|json]
```

`show` and `remove` accept an ID prefix.

### What a blocked command looks like

A denied command returns a `CommandResult` with `status="rejected"` and a
message naming the condition, and emits a `MUTATION_REJECTED` event with
`payload.reason="policy_violation"`. The handler is never reached, so there is
no partial write.

That rejection is **loud by design**: it lands in the same channel
`trellis analyze health` counts (`executor_reasons.policy_violation`) and that
the MCP capture-health banner reads. A policy that blocks every write on a
surface will therefore trip the banner — that is correct, not a false positive.
A policy silently dropping writes is the failure mode this observability exists
to prevent, so check `trellis analyze health` after adding an enforcing policy.

---

## Retrieve Commands

### `trellis retrieve trace`

Retrieve a specific trace by ID.

```bash
trellis retrieve trace <trace_id> [--format text|json]
```

**JSON output (found):** Full trace JSON as defined in [trace-format.md](trace-format.md).

**JSON output (not found):**

```json
{"status": "not_found", "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E"}
```

Exit code 1 when not found.

### `trellis retrieve traces`

List recent traces.

```bash
trellis retrieve traces [--domain DOMAIN] [--limit N] [--fields FIELDS] [--truncate N] [--quiet] [--format text|json|jsonl|tsv]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | `null` | Domain filter |
| `--limit` | `20` | Maximum results |
| `--fields` | all | Comma-separated field list |
| `--truncate` | `null` | Max chars per text field |
| `--quiet` | `false` | Suppress Rich formatting on the **text** branch. A no-op under `--format json` / `jsonl` / `tsv`, which never reach Rich (#403) |
| `--format` | `text` | Output format |

### `trellis retrieve search`

Full-text search across the document store.

```bash
trellis retrieve search <query> [--limit N] [--domain DOMAIN] [--include-chunks] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `query` | **Yes** | -- | Search query string |
| `--limit` | No | `20` | Maximum results |
| `--domain` | No | `null` | Domain scope filter |
| `--include-chunks` | No | `false` | Include `<parent>#chunk-N` fragment rows. Excluded by default (#396): they are slices of documents the same search already ranks. Pushed into the store, so the row cap refills with whole documents rather than the list simply getting shorter |
| `--format` | No | `text` | Output format |

> **`--format json` is safe on its own; `--quiet` is no longer needed for it.**
> It used to be. The payload printed through Rich, which rewrites `:name:` emoji
> shortcodes inside string values — `corpus:notes:<sha>` came back naming a
> document that exists in no store — and line-wraps at the console width, folding
> literal newlines into JSON strings so a wide payload did not parse at all.
> Whether `--format json` parsed depended on the terminal it ran in
> ([#403](https://github.com/ronsse/trellis-ai/issues/403)). Every machine-output
> path now writes straight to stdout, and `tests/unit/test_machine_output_rule.py`
> fails the build if a serialized payload is handed back to Rich. `--quiet` still
> does what it says for the **text** branch.

**Example:**

```bash
trellis retrieve search "connection pool configuration" --limit 5 --format json
```

**JSON output:**

```json
{
  "status": "ok",
  "query": "connection pool configuration",
  "count": 3,
  "results": [
    {"doc_id": "01JRK5N7QF", "content": "...", "snippet": "...", "metadata": {}}
  ]
}
```

### `trellis retrieve entity`

Retrieve a specific entity by ID.

```bash
trellis retrieve entity <entity_id> [--format text|json]
```

**JSON output (found):**

```json
{
  "node_id": "01JRK5N7QF",
  "node_type": "service",
  "properties": {"name": "auth-service", "domain": "platform"}
}
```

**JSON output (not found):**

```json
{"status": "not_found", "entity_id": "01JRK5N7QF"}
```

### `trellis retrieve precedents`

List precedents, optionally filtered by domain.

```bash
trellis retrieve precedents [--domain DOMAIN] [--limit N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | `null` | Filter by domain |
| `--limit` | `20` | Maximum results |
| `--format` | `text` | Output format |

**JSON output:**

```json
{
  "status": "ok",
  "count": 2,
  "items": [
    {
      "event_id": "01JRKBC1QF",
      "entity_id": "01JRKBC2QF",
      "payload": {"title": "Database pool configuration pattern", "domain": "backend"}
    }
  ]
}
```

### `trellis retrieve file-context`

Show what memory already holds about specific files — the shell-callable surface behind read-time file context (#307). Matches stored `metadata.source_path` values (exact, or a `/`-boundary suffix match in either direction where the suffix spans at least one directory, so an absolute path finds the relpath corpus ingest stored) and adds the graph entities doc-linked to those documents.

A **single-segment** stored value matches by equality only. `README.md` / `TODO.md` sit at the root of the vault and of every repo, so treating a bare basename as a suffix would answer a read of one project's file with another's notes.

```bash
trellis retrieve file-context <path> [<path>...] [--include-unconfirmed] [--format text|json|jsonl] [--quiet]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--include-unconfirmed` | `false` | Also surface unconfirmed extraction mints (#301) |
| `--format` | `text` | `text`, `json`, or `jsonl` (one object per path). `tsv` is refused with exit 2 — a path entry carries nested document and entity lists, and flattening them into cells would emit Python reprs |
| `--quiet` / `-q` | `false` | Write raw lines instead of Rich output. Use this from a hook: Rich hard-wraps at 80 columns when stdout is a pipe, splitting long absolute paths across two lines |

Batch the paths into one call — the lookup scans the document store once per call, regardless of how many paths it is given.

**JSON output:**

```json
{
  "status": "ok",
  "count": 1,
  "paths": [
    {
      "path": "src/trellis/retrieve/pack_builder.py",
      "documents": [
        {
          "doc_id": "corpus:vault:9f2c",
          "source_path": "src/trellis/retrieve/pack_builder.py",
          "source_system": "vault",
          "title": "Pack builder notes",
          "excerpt": "Gotcha: the two-stage budget truncates before scoring.",
          "created_at": "2026-08-01T09:12:00+00:00",
          "updated_at": "2026-08-14T10:00:00+00:00"
        }
      ],
      "entities": [
        {
          "entity_id": "01JRK5N7QF",
          "name": "PackBuilder",
          "entity_type": "concept",
          "node_role": "semantic",
          "description": "Assembles token-budgeted packs",
          "document_ids": ["corpus:vault:9f2c"],
          "created_at": "2026-08-02T00:00:00+00:00",
          "updated_at": "2026-08-03T00:00:00+00:00"
        }
      ],
      "newest_item_at": "2026-08-14T10:00:00+00:00"
    }
  ],
  "graph_scan_truncated": false
}
```

`newest_item_at` is the newest stamp across a path's documents and entities, or `null` when nothing matched. It is what a client staleness gate compares against the file's mtime: an mtime newer than `newest_item_at` means the stored context describes an older version of the file.

`graph_scan_truncated` is `true` when the graph carries more doc-linked nodes than the scan cap, i.e. the entity lists may be short. It exists so "no entities" and "couldn't look" don't read the same. The scan is narrowed store-side to doc-linked nodes (`FilterClause("document_ids", "exists")`), so unlinked nodes — the bulk of a mature graph — never consume the cap.

### `trellis retrieve pack`

Assemble a retrieval pack for a given intent.

```bash
trellis retrieve pack --intent <text> [--domain DOMAIN] [--agent AGENT_ID] [--max-items N] [--include-chunks] [--format text|json]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--intent` | **Yes** | -- | Intent for context assembly |
| `--domain` | No | `null` | Domain scope |
| `--agent` | No | `null` | Agent ID scope |
| `--max-items` | No | `50` | Maximum items |
| `--include-chunks` | No | `false` | Include `<parent>#chunk-N` fragment rows (#396). Excluded by default, same rule as `retrieve search` |
| `--format` | No | `text` | Output format |

> **`--format json` is safe on its own** — the Rich mangling described under
> `retrieve search` above is fixed ([#403](https://github.com/ronsse/trellis-ai/issues/403)),
> so `--quiet` is no longer required to parse the output.

**Example:**

```bash
trellis retrieve pack --intent "deploy checklist for staging" --domain platform --max-items 10 --format json
```

**JSON output:**

```json
{
  "status": "ok",
  "intent": "deploy checklist for staging",
  "domain": "platform",
  "agent_id": null,
  "count": 5,
  "items": ["01JRK5N7QF", "01JRK6M3QF", "01JRK7A3QF", "01JRK8B2QF", "01JRK9C1QF"]
}
```

#### `PACK_ASSEMBLED` event payload

Every `PackBuilder` build with an `event_log` configured emits one `PACK_ASSEMBLED` event with `entity_id` = `pack_id`. It is the read side of two loops — `trellis analyze pack-telemetry` reads the rejection/budget/strategy keys, and the learning join in `learning/pack_observations.py` joins it to `FEEDBACK_RECORDED` on `pack_id`.

**Two payload shapes, told apart by `entity_type`.** `build()` emits `"pack"`; `build_sectioned()` — behind `get_objective_context` / `get_task_context` / `get_sectioned_context` — emits `"sectioned_pack"` with a *different* key set (below). Branch on `entity_type`, or read with `.get()`. `_emit_telemetry` / `_emit_sectioned_telemetry` in `retrieve/pack_builder.py` are the exhaustive key lists; the table covers the flat payload's non-obvious semantics.

| Key | Notes |
|-----|-------|
| `run_id` | **Request-scoped attribution.** The unit of work the pack was served to — narrower than `session_id`. The learning join prefers the feedback payload's `run_id` and falls back to this one; when both are absent the observation buckets under `unknown-run`. |
| `intent_family` | Canonical intent bucket, read by the metrics `group_by=intent_family` axis. Never null on a `PackBuilder`-assembled pack: when the caller supplies none it is derived via `normalize_intent_family(intent=...)`, which falls back to `general_context` rather than an empty value. |
| `injected_item_ids` | The served set — and the **fallback** source for the `reference_rate` metric, not the first one: `compute_timeseries` reads the feedback payload's `items_served` and only uses this when that is empty. `PackFeedback.from_agent_signal` deliberately leaves `items_served` empty (cited ids are what the agent *referenced*, not what the pack contained), so agent-graded packs do land here. |
| `injected_item_hashes` | `{item_id: hash}` of each item's **excerpt**, so a later build in the same session can re-serve an item whose content changed rather than suppressing it as already-seen (#258). Additive — older events without the key fall back to id-only suppression. |
| `injected_items[]` | Per-item detail — `item_id`, `item_type`, `rank`, `selection_reason`, `score_breakdown`, `estimated_tokens`, `strategy_source`, `injected_advisory_ids`, plus the **attribution fields** below. This is the per-item row the learning join reads. |
| `rejected_items[]` | `{item_id, item_type, relevance_score, reason, strategy_source}`. Known `reason` values: `dedup`, `archived`, `noise`, `structural_filter`, `meta_activity_filter`, `max_items`, `token_budget`, `session_dedup`, `semantic_dedup`, `content_floor` — `archived` and `noise` are the collect-seam gates #404 started recording. Rows are per **(strategy, item)**, so one id retrieved by two axes and gated on both appears twice; the `withholding` summary counts distinct ids and is the per-item number. |
| `content_floor` | `{mode, min_distinct_words, penalty, exempt_item_types, penalized_count, penalized_item_ids, excluded_count, excluded_item_ids}`. Emitted even when nothing tripped, so "floor ran, nothing thin" is distinguishable from "floor never ran". |
| `index_mode` | `true` when the pack was assembled for the one-line-per-item index rendering (`get_context(index=True)` / `search(index=True)`, #305) instead of excerpt bodies. Always present on packs built after #305; read it with a `False` default. **Branch on it before aggregating token numbers** — see the two rows below. |
| `budget_trace[]` | One `{item_id, item_tokens, running_total, included}` row per candidate the token stage weighed, against the `budget_max_items` / `budget_max_tokens` alongside it. `item_tokens` is whichever cost was *charged*: the item's excerpt normally, its rendered index line when `index_mode` is `true`. |
| `token_*` | `token_counter`, `token_budget_safety_margin`, `token_budget_effective`, `token_total_estimated` always. `token_counter_validator`, `token_total_validated`, `token_count_delta`, `token_count_delta_pct` **only when a `token_budget_validator` is configured** — check before reading. `token_total_estimated` sums the same charge `budget_trace[].item_tokens` records, so on an `index_mode` pack it is index-line tokens, not excerpt tokens — averaging it across a mixed corpus mixes two units. The per-item excerpt read cost stays on `injected_items[].estimated_tokens` in both modes. |
| `strategy_failures[]` | Strategies that raised but did not block the build (a sibling succeeded). Empty on a clean build. |
| `meta_filtered_count` | Graph nodes dropped by the default meta-`Activity` filter — the same drops `rejected_items` records under `meta_activity_filter`. **Not a count of meta-Activities suppressed.** Since #375/#436 the recorder mints them `node_role="structural"` and `GraphSearch` drops them *inside the strategy*, before the candidate slice, so they never become candidates and this field reads `0` while suppression happens on every pack. It counts only what reached `PackBuilder`: pre-#436 `semantic` rows, and any meta-Activity reached by a non-graph axis. |

Plus the scope keys `intent` / `domain` / `agent_id` / `session_id`, the roll-ups `items_count` / `strategies_used` / `candidates_found`, and `advisory_ids` / `reranker` / `semantic_dedup_enabled` / `semantic_dedup_rejected`, all as named.

**The sectioned payload is not a superset.** It carries `section_count`, `total_items` and `sections[]` (`{name, items_count, item_ids, injected_advisory_ids}`) *instead of* `items_count`, `injected_item_ids`, `injected_items[]`, `strategies_used`, `candidates_found`, `budget_max_items`, `budget_max_tokens`, `budget_trace[]` and `rejected_items[]` — none of which it emits. Everything else above is shared. Two consequences to know before reading the numbers: `trellis analyze pack-telemetry` counts a sectioned pack as a **0-item pack**, because it reads `injected_item_ids`; and since the learning join reads `injected_items[]`, a sectioned pack yields **zero per-item rows** — `run_id` and `intent_family` are emitted there for symmetry but stay inert until that gap is closed.

**Attribution fields (`title` / `category` / `domain_system`).** Added in #285 because the promotion candidates written to `intent_learning_candidates.json` previously carried only opaque `item_id`s. All three are derived from metadata the strategies already attach — nothing new is computed. `title` reads `title`, then `capture_title` (what the session-capture ingest writes), then `name`. `category` is the `ContentTags.content_type` facet and **only** that: a flat `metadata["content_type"]` is deliberately not read as a fallback, because ingest handlers stamp their own vocabulary there (`"conversation"`, `"entity_summary"`), and mixing the taxonomies would make the column ambiguous. An empty value is omitted rather than emitted as `null`, so thin items keep the pre-existing payload shape.

**Graph-row fields (`node_type` / `node_role`).** Added in #375 (gate 4). Items sourced from a graph node — the `graph` and `observation` axes — also carry the row's stored `node_type` and its `node_role` (`structural` / `semantic` / `curated`). **Only graph-sourced items carry them, and absence is recorded by omitting the key**, the same convention the fields above use: a document has no `node_role`, and filling one in as `"semantic"` would make a per-role split a statement about the default rather than about the corpus. Before this, splitting the graph axis's citations by type or role required joining every served `item_id` back to `nodes WHERE valid_to IS NULL` — a mutable table being asked to describe a past serving, which answers for a different row once a node is deleted, re-typed or re-minted. The graph axis's value is sharply concentrated by type (see `docs/design/plan-375-graph-candidates.md` §2.1), so that split is a standing question, not a one-off. `graph_selection` (`seeded` / `recency_window`, #371) sits alongside them and answers how the candidate was chosen. Read the pair beside `strategy_source` rather than instead of it: a vector row written from a graph node (the Neo4j shape-#2 layout, where the vector `item_id` *is* the `node_id`) carries them onto a `semantic` item legitimately.

#### Pack excerpts and the content floor

Excerpts are cut at a **sentence boundary, else a word boundary, else hard** — never mid-word — and the cut is marked `… [+2.3k chars]`, naming how much was dropped so a consumer can judge whether fetching the full source is worth the tokens (#310). A boundary is only honoured if it retains at least half the budget, so a leading `"Note. "` cannot gut an excerpt. The 500-character cap (`EXCERPT_MAX_CHARS`) matches the raw slice this replaced, so per-item token estimates did not shift.

Three things to know about the size note. It is charged **against** the cap, not added on top of it — a truncated excerpt gives up ~14 characters of body to carry it, and is still never longer than the raw `text[:500]` slice it replaces. Its count is exact: the truncator reserves the widest possible note before choosing the cut, so what it quotes is what was actually dropped. And `count_substance_words` strips it before measuring, so it can never lift a name-only stub over the content floor below. Limits under 100 characters (`ELISION_NOTE_MIN_LIMIT`) get the bare `…` instead — at those budgets the note would crowd out the text it annotates.

The **content floor** handles substance-free items — the name-only graph stubs whose "excerpt" is a three-word entity name. An item with fewer than `min_distinct_words` (default `5`) distinct words in its excerpt is, by default, **demoted, not dropped**: its `relevance_score` is multiplied by `0.35`.

| Knob | Default | Notes |
|------|---------|-------|
| `mode` | `penalize` | `penalize` \| `exclude` \| `off`. Set via `PackBuilder(content_floor=ContentFloorConfig(mode=...))`; there is no env var or config key. |
| `min_distinct_words` | `5` | Sits in the gap between a name-shaped stub (1–4 tokens) and the tersest real memory (a full clause, ~9 words). |
| `penalty` | `0.35` | Multiplier in `penalize` mode. |
| `exempt_item_types` | `{"observation"}` | Item types whose excerpt is structured rather than prose — a Measurement excerpt is `"row_count = 41823"`, complete and two words by construction. |

Why the default demotes rather than excludes: the shortest genuinely useful memory this system serves — a one-line gotcha, a two-clause procedure — is exactly the shape a hard length filter would silently delete. A multiplicative penalty pushes a stub behind anything substantive while leaving it eligible when the candidate pool is thin. Either way the decision is observable — `PACK_ASSEMBLED.payload["content_floor"]` above, plus `content_floor_penalty` / `content_floor_substance_words` on the item's `score_breakdown`; `mode="exclude"` additionally emits a `rejected_items` row with reason `content_floor`.

#### Withholding is stated, not implied (#404)

A pack that withheld candidates now says so, in one line **in its header**:

```
**Withheld:** 3 items matched this intent but were not served (noise 2, archived 1). Counts only — no ids or content.
```

It renders on every pack surface a caller reads — `get_context` (full and `index=True`), `search`, `get_task_context` / `get_objective_context` / `get_sectioned_context` — and, most importantly, on the **empty** pack, where `No context found for: …` is otherwise identical whether the corpus was empty or every match was demoted.

Three properties worth knowing:

- **Withheld means absent, computed as `{rejected ids} − {served ids}`.** A rejection is not an absence: the losing copy of a dedup, or a row one axis gated and another served, is *on screen* and is never reported as withheld. Reasons seen only on such rejections are recorded under `non_absence_reasons` in telemetry, so a `total` of zero reads as "nothing was withheld", not "nothing was rejected". An id removed by more than one gate is attributed to the **first** one.
- **Counts and reasons only — never ids, never excerpts.** Naming the ids would invite a re-fetch of exactly what a gate decided not to serve. The ids stay in `PACK_ASSEMBLED.payload["withholding"]["withheld_item_ids"]`, which is an operator surface with a different access path.
- **Graduated disclosure (#359) contributes nothing here.** A pointer is a *served* item — ranked, cited, fetchable via `get_items` — and calling it withheld would blur the distinction that change was careful to make.

`PACK_ASSEMBLED.payload["withholding"]` is emitted on both pack kinds **even when nothing was withheld** (`{"total": 0, "by_reason": {}, ...}`), so an analyzer can tell "the summary ran and found nothing" from "the summary never ran". The two collect-seam gates — `signal_quality="noise"` and `Lifecycle.state="archived"` — now write `rejected_items` rows (reasons `noise` and `archived`) where previously their only observable was a `logger.debug` line that fires under **no shipped default** (`configure_stderr_logging` pins INFO, the CLI pins WARNING). `trellis -vv` or `TRELLIS_LOG_LEVEL=DEBUG` does enable it — which is precisely the point: the signal existed only for someone who already suspected the problem and went looking.

---

## Python-Only Mutation API

The following operations exist in the `MutationExecutor` and `OperationRegistry` but are not yet exposed as CLI commands. Use them via the Python API.

### Operations

All operations go through the governed mutation pipeline: validate, policy check, idempotency check, execute, emit event.

```python
from trellis.mutate.commands import Command, Operation
from trellis.mutate.executor import MutationExecutor

executor = MutationExecutor(event_log=event_log)
result = executor.execute(command)
```

### Trace Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `trace.ingest` | `trace` (dict) | Ingest a full trace via the mutation pipeline |
| `trace.append_step` | `trace_id`, `step` (dict) | Append a step to an existing trace |
| `trace.record_outcome` | `trace_id`, `outcome` (dict) | Record the outcome of a trace |

**Example -- append step:**

```python
cmd = Command(
    operation=Operation.TRACE_APPEND_STEP,
    args={
        "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E",
        "step": {
            "step_type": "tool_call",
            "name": "run_tests",
            "result": {"passed": 42, "failed": 0},
            "duration_ms": 15000,
        },
    },
    target_id="01JRK5N7QF8GHTM2XVZP3CWD9E",
    target_type="trace",
    requested_by="code-orchestrator",
)
result = executor.execute(cmd)
```

### Evidence Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `evidence.ingest` | `evidence` (dict) | Ingest evidence via the mutation pipeline |
| `evidence.attach` | `evidence_id`, `target_id`, `target_type` | Attach evidence to a trace, entity, or precedent |

### Entity Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `entity.create` | `entity_type`, `name` | Create a new entity |
| `entity.update` | `entity_id` | Update entity properties |
| `entity.merge` | `source_id`, `target_id` | Merge two entities |

**Example -- create entity:**

```python
cmd = Command(
    operation=Operation.ENTITY_CREATE,
    args={
        "entity_type": "service",
        "name": "auth-service",
        "properties": {"language": "python", "team": "platform"},
    },
    requested_by="code-orchestrator",
)
result = executor.execute(cmd)
# result.created_id contains the new entity ID
```

### Precedent Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `precedent.promote` | `trace_id`, `title`, `description` | Promote a trace to a precedent (also available via CLI) |
| `precedent.update` | `precedent_id` | Update an existing precedent |

### Link Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `link.create` | `source_id`, `target_id`, `edge_kind` | Create a directed edge (also available via CLI) |
| `link.remove` | `edge_id` | Remove an edge |

### Label Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `label.add` | `target_id`, `label` | Add a label (also available via CLI) |
| `label.remove` | `target_id`, `label` | Remove a label |

### Feedback Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `feedback.record` | `target_id`, `rating` | Record feedback (also available via CLI) |

### Maintenance Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `redaction.apply` | `target_id`, `reason` | Hard-purge a graph entity: all SCD-2 versions, its edges, aliases, and vector entry. Emits `REDACTION_APPLIED` (counts + id pointers, never content). Also available via `trellis curate redact`. |
| `retention.prune` | `criteria`, `reason` | Archive low-value derived items. **Phase one is archival, not deletion**: candidates are stamped `Lifecycle.state="archived"` and retrieval stops serving them; content stays in the store, so a wrong prune is walked back by re-stamping. **`dry_run` defaults to `true`** — a run writes nothing unless it is passed `false` (CLI: `--apply`). `criteria` selects `noise_documents` / `unconfirmed_mints` / `lifecycle_states`, with `older_than_days` gating the age-based two only. Traces and event-log rows are never candidates (the resolver cannot reach them); confirmed entities are never candidates at any age. Emits `RETENTION_PRUNED` in both modes. See [`adr-retention-prune.md`](../design/adr-retention-prune.md) §6. |
| `retention.restore` | `item_ids`, `reason` | Return archived items to `Lifecycle.state="current"`. The governed inverse of `retention.prune`, and what makes phase-one archival's reversibility real — direct store writes are forbidden and there is no governed document-update verb. Takes **explicit ids** (they ride the `RETENTION_PRUNED` payload), not criteria: re-deriving the set would re-run the selection that was wrong. Ids that are not archived are skipped, not errors. Emits `RETENTION_RESTORED`. |

### Batch Execution

Execute multiple commands as a batch:

```python
from trellis.mutate.commands import CommandBatch, BatchStrategy

batch = CommandBatch(
    commands=[cmd1, cmd2, cmd3],
    strategy=BatchStrategy.STOP_ON_ERROR,
    requested_by="code-orchestrator",
)
results = executor.execute_batch(batch)
```

| Strategy | Behavior |
|----------|----------|
| `sequential` | Execute all commands in order |
| `stop_on_error` | Stop on first failure or rejection |
| `continue_on_error` | Execute all, collect all results |

### CommandResult

Every mutation returns a `CommandResult`:

| Field | Type | Description |
|-------|------|-------------|
| `command_id` | `string` | ID of the executed command |
| `status` | `CommandStatus` | `success`, `rejected`, `failed`, or `duplicate` |
| `operation` | `string` | The operation that was executed |
| `target_id` | `string` or `null` | Target entity ID |
| `created_id` | `string` or `null` | ID of newly created object |
| `message` | `string` | Human-readable result message |
| `warnings` | `list[string]` | Policy warnings |
| `metadata` | `dict` | Additional metadata |
| `executed_at` | `datetime` | When the command was executed |

### Idempotency

Set `idempotency_key` on a `Command` to prevent duplicate execution:

```python
cmd = Command(
    operation=Operation.ENTITY_CREATE,
    args={"entity_type": "service", "name": "auth-service"},
    idempotency_key="create_auth_service_20260310",
    requested_by="code-orchestrator",
)
```

If the same key has been seen before (in-memory or in the event log), the executor returns `CommandStatus.DUPLICATE` without re-executing.

---

## Analyze Commands

### `trellis analyze context-effectiveness`

Analyze which context items correlate with task success.

```bash
trellis analyze context-effectiveness [--days N] [--min-appearances N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `30` | Days of history to analyze |
| `--min-appearances` | `2` | Minimum item appearances to include |

Shows per-item success rates and flags noise candidates (items correlating with failure).

### `trellis analyze token-usage`

Analyze token usage across CLI, MCP, and SDK layers.

```bash
trellis analyze token-usage [--days N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `7` | Days of history to analyze |

Shows total tokens, average per response, breakdown by layer and operation, and over-budget alerts.

### `trellis analyze cost`

Estimate **Trellis's cost overhead** — the dollar cost of the context
Trellis injects into agent turns. This is the "what does memory cost on
top of my agent bill?" number.

```bash
trellis analyze cost [--days N] [--model claude-opus] \
    [--price-per-mtok 3.0] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `7` | Days of history to analyze |
| `--model` | `claude-sonnet` (or `TRELLIS_COST_MODEL`) | Consuming model for input pricing; resolves by family (`claude-opus-4-8` → `claude-opus`) |
| `--price-per-mtok` | model table (or `TRELLIS_COST_PRICE_PER_MTOK`) | Explicit input price override, USD per 1M tokens |

Prices the `response_tokens` of every `TOKEN_TRACKED` event (emitted by
`get_context` / `get_lessons` / the other MCP macro tools) at the
consuming model's **input** rate — those injected tokens land in the
agent's next prompt. The **token count is exact**; the **dollar figure is
an estimate** (the ~4-chars/token counter, and an operator-owned price —
`price_source` in the output says which input won: explicit / env /
model-table / default). It reports the *absolute* overhead in tokens and
dollars, not a ratio: Trellis never sees the agent's own generation, so
compare `overhead_dollars` against your provider's input-token bill for
the same window to get the overhead fraction. Local models (Ollama) price
at `--model local` → $0.

**JSON output (abridged):**

```json
{"period_days": 7, "overhead_events": 28, "overhead_tokens": 34800, "model": "claude-opus", "price_per_mtok": 15.0, "price_source": "model_table", "overhead_dollars": 0.522, "by_operation": [{"operation": "get_context", "layer": "mcp", "calls": 20, "tokens": 30000, "dollars": 0.45}], "estimator": "estimate_4_chars_per_token"}
```

### `trellis analyze domains`

Read-only usage report for the primary retrieval slice, `domain`. Joins observed
domain values across three sources — `TraceContext.domain` (TraceStore),
`ContentTags.domain` in document metadata (DocumentStore), and pack + feedback
events (EventLog, grouped by the pack payload's `domain`).

```bash
trellis analyze domains [--days N] [--limit N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `30` | Days of pack/feedback history to analyze |
| `--limit` | `1000` | Max traces, documents, and events per source to scan |

Per domain it reports: document count, trace count, packs served, graded packs,
and success rate from `FEEDBACK_RECORDED`. Items, traces, and packs with **no**
domain are surfaced under `(none)` so coverage gaps stay visible. Text mode
prints a Rich table sorted by document count descending; JSON mode emits
`{"status": "ok", "days": N, "domains": [...]}`.

Use this to decide which domains to seed in
[`classify.domain_keywords`](tagging-for-retrieval.md#seeding-your-own-domains).

> **Out of scope:** automatic domain discovery/clustering and a domain
> *promotion* analyzer. Those follow the column-leaf pattern (contract first,
> gated on production telemetry) — see
> [`adr-column-leaf-modeling-guardrails.md`](../design/adr-column-leaf-modeling-guardrails.md)
> and [`adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md) tier 2.

---

## Worker Commands

Unattended learning/curation workers. See [`../design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md) for the four-tier autonomy model these commands operate under.

### `trellis worker tune`

Run one `RuleTuner` pass and, when **Tier-1 auto-promotion** is enabled, auto-promote every qualifying proposal through the same governance pipeline `trellis metrics promote --commit` uses — then arm post-promotion monitoring so degradation auto-rolls-back.

```bash
trellis worker tune [--tuner-name NAME] [--since-days N] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--tuner-name` | `rule_tuner` | Logical tuner name (cursor + proposal scope). |
| `--since-days` | (cursor) | Force a rescan of the last N days, ignoring the tuner cursor. |
| `--dry-run` | off | Report what *would* auto-promote without mutating stores or emitting events. |
| `--format` | `text` | `text` or `json`. |

**Default behaviour is a pure tuner pass.** Auto-promotion is **off by default** (global default OFF, per Tier-1 invariant (d)). With it disabled, `worker tune` is byte-identical to `trellis metrics tune`: it produces/refreshes proposals and promotes nothing. Non-qualifying proposals always stay `pending` for manual review via `trellis metrics promote` — they are reported, never rejected.

#### Enabling Tier-1 auto-promotion

Add a `learning.auto_promote` section to `~/.trellis/config.yaml` (or `$TRELLIS_CONFIG_DIR/config.yaml`):

```yaml
learning:
  auto_promote:
    enabled: true              # default false — master switch, global default OFF
    min_sample_size: 30        # stricter than manual promote (5); must be >= 5
    min_effect_size: 0.25      # stricter than manual promote (0.15); must be >= 0.15
    require_baseline: true      # no baseline => nothing to roll back to => left for a human
    post_min_samples: 20        # min post-promotion outcomes before a degradation verdict
    post_regression_threshold: 0.10  # success-rate drop (abs) that triggers auto-rollback
    post_lookback_days: 7       # monitoring window on either side of the promotion
```

The auto thresholds **must be at least as strict as the manual-promote defaults** — the config loader (and `AutoPromotePolicy`) rejects looser values loudly rather than silently weakening the autonomous gate. Monitoring is always armed (`auto_demote` is forced on); you cannot auto-promote without an armed rollback.

#### Audit trail

Each autonomous action emits a **dedicated, self-identifying** event in addition to the normal governance event:

| Event | Emitted when |
|-------|--------------|
| `parameters.auto_promoted` (`PARAMS_AUTO_PROMOTED`) | A qualifying proposal is auto-promoted (alongside `PARAMS_UPDATED`). |
| `parameters.auto_rolled_back` (`PARAMS_AUTO_ROLLED_BACK`) | Post-promotion monitoring demotes a degraded snapshot (alongside the rollback's `PARAMS_UPDATED` and `PARAMETERS_DEGRADED`). |

Degradation that accrues *after* the promoting pass is caught on a later pass: each `worker tune` run re-monitors recent auto-promotions and rolls back any that have since degraded.

The manual `trellis metrics promote` path is unchanged and emits only `PARAMS_UPDATED` — the dedicated events distinguish "a human promoted this" from "the system promoted this on its own."

### `trellis worker curate`

Run one **full curation cycle** (Tier-2). Calls the curation library functions directly — no shelling out — in this fixed order:

1. **effectiveness feedback** (`run_effectiveness_feedback`) — demote: noise-tag low-value items;
2. **advisory generation** (`AdvisoryGenerator.generate`);
3. **advisory fitness loop** (`run_advisory_fitness_loop`) — adjust confidence / suppress weak advisories;
4. **learning candidates** (`build_learning_observations_from_event_log` → `analyze_learning_observations` → `write_learning_review_artifacts`) — writes promote-half review artifacts to `--output-dir`.

```bash
trellis worker curate --output-dir DIR [--days N] [--interval SECONDS] \
  [--dry-run] [--reconcile-first] \
  [--skip-noise-tags] [--skip-advisories] [--skip-learning] \
  [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--output-dir` / `-o` | (required) | Directory for the learning-candidate review artifacts. |
| `--days` | `30` | Days of EventLog history to scan. |
| `--interval` | (off) | Loop mode: re-run the cycle every N seconds until SIGINT/SIGTERM. Plain sleep — **no scheduler dependency** (APScheduler/Celery deliberately rejected). |
| `--dry-run` | off | Analyze only — no noise tags, no advisory mutations, no artifacts written. |
| `--reconcile-first` | off | Backfill `pack_feedback.jsonl` into the EventLog (`reconcile_feedback_log_to_event_log`) before the cycle. |
| `--skip-noise-tags` | off | Skip stage 1. |
| `--skip-advisories` | off | Skip stages 2 + 3. |
| `--skip-learning` | off | Skip stage 4. |
| `--format` | `text` | `text` or `json`. |

**Promotion stays human-gated.** This command writes learning candidates for review and **never promotes** (Tier-2 invariant). To promote, review the emitted `promotion_decisions.template.json`, set `approved: true` on the rows you want, then run `trellis curate promote-learning`. In `--interval` mode each cycle logs one structured `worker_curate.cycle` line with the headline counts (noise-tagged, advisories generated/suppressed, candidates written); SIGINT/SIGTERM drains the current cycle and exits cleanly.

### `trellis worker enrich`

Batch-enrich under-tagged documents via the LLM `EnrichmentService`, writing the suggested tags / classification / importance back into each document's `metadata.content_tags`.

```bash
trellis worker enrich [--concurrency N] [--limit N] \
  [--reenrich] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--concurrency` | `3` | Parallel enrichment requests. |
| `--limit` | `50` | Max documents to enrich this run. |
| `--reenrich` | off | Also take documents that have already been through this path. |
| `--dry-run` | off | Select + report candidates without calling the LLM. |
| `--format` | `text` | `text` or `json`. |

**Selection predicate.** A document is a candidate when its `metadata.content_tags` is missing/empty, **or** when `content_tags.classified_mode != "enrichment"` — i.e. it has never been through this path. `--reenrich` takes the already-enriched too. (This used to select on `content_tags.tag_confidence`, a key `ContentTags` does not define and nothing ever wrote, so the threshold could never skip anything.)

**Concurrency counters in `--format json` (#421).** The candidate page is a snapshot taken before *N* model calls, so the write-back re-reads each row and merges only the keys this path owns. `stale_snapshot` counts rows another writer changed inside that window — those are still enriched, onto their current content, so it is a concurrency signal rather than a failure count. `vanished` counts rows deleted inside the window, which are **skipped** (writing them back would resurrect a deleted document). A standing non-zero `stale_snapshot` means the batch is racing another writer and wants a smaller `--limit`.

**Requires an LLM extra.** Enrichment needs a configured `llm:` block plus the matching `[llm-openai]` / `[llm-anthropic]` extra. With no buildable client the command **exits non-zero with an actionable message** naming the missing config/extra — it never silently no-ops.

### `trellis worker mine-precedents`

Mine precedent candidates from failure / partial traces (wraps `PrecedentMiner.generate_precedent_candidates`).

```bash
trellis worker mine-precedents [--domain D] [--min-traces N] \
  [--limit N] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | (all) | Restrict mining to this trace domain. |
| `--min-traces` | `3` | Minimum failure/partial traces required to mine. |
| `--limit` | `100` | Max traces to analyze. |
| `--dry-run` | off | Report how many failure/partial traces are in scope without calling the LLM. |
| `--format` | `text` | `text` or `json`. |

Candidates are **surfaced** (the miner emits `PRECEDENT_PROMOTED` events as it intends) but **not auto-promoted** into the graph — review before acting. Like `enrich`, this requires an LLM extra and exits loudly when none is configured.

### `trellis worker capture-sessions`

Run one Claude Code session-capture sweep — the `trellis worker` front door for the same code path as the `trellis-session-capture` console script and `python -m trellis_workers.session_capture`.

```bash
trellis worker capture-sessions [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | off | Plan the sweep without writing memories or advancing the watermark. |
| `--format` | `text` | `text` or `json`. |

Everything else is configured by the `TRELLIS_CAPTURE_*` environment variables documented in [session-auto-capture.md](session-auto-capture.md). **Requires a distillation judge** (an `llm:` block): distillation fail-closes, so a sweep without one captures nothing — the command exits non-zero rather than reporting a clean no-op. A judge that goes away *mid*-sweep is counted in `sessions_judge_unavailable`, reported as `"status": "partial"`, and also exits non-zero unless `TRELLIS_CAPTURE_STRICT=0`.

---

## API Authentication

The REST API authenticates with scoped API keys (roadmap item E.5, issue #191).

### Modes

`TRELLIS_AUTH_MODE` controls the posture:

| Mode | No credential | Invalid credential | Valid credential |
|------|---------------|--------------------|------------------|
| `off` | passes, all scopes | passes, all scopes | passes, all scopes |
| `optional` | passes, all scopes (migration mode) | 401 | scoped |
| `required` | 401 | 401 | scoped |

When `TRELLIS_AUTH_MODE` is **unset**, the mode is inferred for backwards
compatibility: `required` if `TRELLIS_API_KEY` is set, else `off`. An invalid
value crashes at startup — there is no silent fallback. `off` and `optional`
log a loud startup warning.

### Token format and headers

Tokens look like `trellis_ak_<key_id>.<secret>` — `key_id` is 12 hex chars,
the secret half is never stored (only its SHA-256). Present a token on either
header (`X-API-Key` wins when both are sent):

```
X-API-Key: trellis_ak_4f3a2b1c0d9e.zJx...
Authorization: Bearer trellis_ak_4f3a2b1c0d9e.zJx...
```

**Legacy shared secret:** a token exactly matching `TRELLIS_API_KEY` is
granted all scopes, so single-secret deployments keep working while
migrating to scoped keys.

### Scopes

Four scopes: `read`, `ingest`, `mutate`, `admin`. `admin` implies all
others. The router → scope map:

| Router (`/api/v1`) | Required scope |
|--------------------|----------------|
| `retrieve` (search, packs, entities, traces, precedents) | `read` |
| `ingest` (traces, evidence, vectors, bulk) | `ingest` |
| `mutations` (`/commands/batch`) | `mutate` |
| `curate` (precedents, links, entities, documents, feedback) | `mutate` |
| `extract` (`/extract/drafts`) | `mutate` |
| `admin` (health, stats, effectiveness, advisories, vector reset) | `admin` |
| `observations` GETs | `read` |
| `observations` POSTs (`/observations`, `/measurements`) | `mutate` |
| `policies` GETs | `read` |
| `policies` POST / DELETE | `admin` |

`/healthz`, `/readyz`, `/api/version`, `/metrics`, and `/ui` stay reachable
without a credential by default (see [Production exposure](#production-exposure)
for what `/readyz` and `/metrics` reveal to anonymous callers). A valid key
lacking the required scope gets 403; missing or invalid credentials get an
undifferentiated 401 (the failure category is logged server-side only).

### Managing keys

```bash
# Mint a key — the token is printed ONCE and never stored
trellis admin api-keys create --name ci-reader --scopes read --format json

# Multiple scopes
trellis admin api-keys create --name ingest-bot --scopes read,ingest

# List keys (key_id, name, scopes, created_at, revoked — never the hash)
trellis admin api-keys list --format json

# Revoke (exits non-zero if unknown or already revoked)
trellis admin api-keys revoke <KEY_ID> --format json
```

### Production exposure

Three env toggles control what an unauthenticated caller can reach.
All three validate at startup — an unrecognized value crashes
`create_app` rather than guessing a posture.

| Env var | Values | Default | Effect |
|---------|--------|---------|--------|
| `TRELLIS_UI_ENABLED` | `true` / `false` | `true` | `false`: `/ui` is not mounted and `/` redirects to `/api/version` instead of `/ui/`. |
| `TRELLIS_OPS_DETAIL` | `authenticated` / `public` | `authenticated` | Who sees the per-backend `/readyz` breakdown (backend names, latencies, raw error strings). The `{"status": ...}` line is always public, so orchestrator probes need zero credentials. `public` restores the pre-gating full body for everyone. |
| `TRELLIS_METRICS_PUBLIC` | `true` / `false` | `true` | `false`: `/metrics` requires a valid credential (any scope) and returns 401 otherwise. `true` keeps it open for credential-less Prometheus scrape jobs. |

"Authenticated" for `/readyz` detail and gated `/metrics` means
*any* valid credential — no specific scope is required. The check
follows the effective `TRELLIS_AUTH_MODE`: in `off` every request
counts as authenticated (dev behavior unchanged), in `optional`
anonymous callers pass with full scopes, so the gates only bite in
`required` mode. A presented-but-invalid credential always gets 401
on these endpoints — it is never silently downgraded to the
anonymous minimal response.

**UI key flow.** The static UI at `/ui` stores an API key in browser
`localStorage` and sends it as `X-API-Key` on every API fetch. Use the
key icon in the nav bar to set or clear the key; when any API call
returns 401, the UI surfaces the key-entry banner automatically. Mint
a key with `trellis admin api-keys create` — the dashboard's stats /
health / effectiveness cards are `admin`-scoped, while the graph,
traces, and precedents views need `read`, so an `admin` key covers the
whole UI.

---

## REST API

Start with `trellis admin serve` or `trellis-api`. Base path: `/api/v1/`.

### Ingest

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/traces` | Trace JSON | Ingest a trace |
| POST | `/evidence` | Evidence JSON | Ingest evidence |

### Retrieve

| Method | Endpoint | Params/Body | Description |
|--------|----------|-------------|-------------|
| GET | `/search` | `?q=...&domain=...&limit=20&include_chunks=false` | Full-text search. `<parent>#chunk-N` fragment rows are **excluded by default** (#396) — they are slices of documents the same search already ranks, and on the reference deployment 56% of rows are chunks. The exclusion is pushed into the store rather than applied to the response, so the result set refills with the documents the fragments were sliced from instead of simply getting shorter — a short result set still means "that is all there is". Pass `include_chunks=true` for the unfiltered set; the applied setting is echoed on the response. |
| POST | `/packs` | `{intent, domain?, max_items?, max_tokens?, run_id?, intent_family?}` | Assemble context pack. `run_id` / `intent_family` ride the `PACK_ASSEMBLED` event so the learning loop can credit the run and bucket the intent instead of falling back to `unknown-run` / `general_context`; `intent_family` is derived from `intent` when omitted. |
| GET | `/entities/{id}` | — | Get entity with subgraph |
| GET | `/traces` | `?domain=...&limit=20` | List traces |
| GET | `/traces/{id}` | — | Get trace by ID |
| GET | `/precedents` | `?domain=...&limit=20` | List precedents |

### Curate

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/precedents` | `{trace_id, title, description}` | Promote trace |
| POST | `/links` | `{source_id, target_id, edge_kind?}` | Create edge |
| POST | `/entities` | `{entity_type, name, properties?}` | Create entity |
| POST | `/feedback` | `{target_id, rating, comment?, pack_id?}` | Record feedback (pass `pack_id` so it joins) |
| POST | `/packs/{pack_id}/feedback` | `{rating, success?, notes?}` | Pack-specific feedback |

### Admin

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/stats` | Store statistics |
| GET | `/effectiveness` | Context effectiveness report |
| GET | `/metrics/timeseries` | Improvement-metric trend series (see below) |

### Review queue (admin scope)

The Review view in the static UI (`/ui/` → **Review**) is a human-decision
inbox. Every endpoint below is on the admin router, so it requires the
`admin` scope, respects `TRELLIS_UI_ENABLED` / ops-gating, and — for the
write actions — emits a `REVIEW_DECISION_RECORDED` audit event stamped
with the authenticated key identity (in addition to the surface-specific
event the underlying pipeline already emits). The autonomy tiers that
decide which surfaces are human-gated are described in
`docs/design/adr-autonomy-ladder.md`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/proposals` | List pending tuner proposals with `effect_size` / `sample_size` / `baseline_values` / `proposed_values` |
| GET | `/proposals/{id}/preview` | Dry-run a promotion — predict promote/reject, mutate nothing |
| POST | `/proposals/{id}/promote` | Promote through the governed pipeline (same logic as `trellis metrics promote --commit`) |
| POST | `/proposals/{id}/reject` | Reject (human-gated); body `{reason?}` |
| GET | `/learning/candidates` | Serve the most-recent `intent_learning_candidates.json` artifact (empty + `hint` when none found) |
| POST | `/learning/promotions` | Promote approved candidates via `MutationExecutor`; body `{decisions: [{candidate_id, approved, rationale?}]}` |
| GET | `/schema-evolution/candidates` | List latest `WELL_KNOWN_CANDIDATE` event per `candidate_id` |
| POST | `/schema-evolution/{id}/draft-adr` | Render the promotion-ADR markdown (copyable/downloadable in the UI). **Only** action — there is no promote endpoint; promotion is a one-way ADR commitment |
| GET | `/code-proposals` | List recent `PROPOSAL_DRAFTED` events with `markdown_preview` (read-only) |

**Sections in the UI.** The Review view has four collapsible queue sections,
each with a live count:

1. **Tuner proposals** — Approve / Reject buttons. Approve first runs the
   dry-run preview and shows the predicted decision before a confirm step.
   Approve wraps `promote_proposal`; Reject wraps `reject_proposal`. The
   CLI (`trellis metrics promote`) keeps working identically — both share
   `trellis.learning.tuners.preview_promotion`.
2. **Learning promotion candidates** — candidate cards with metrics, an
   approve checkbox + rationale field, and a single submit that runs the
   existing `prepare_learning_promotions` → `MutationExecutor` path.
3. **Schema-evolution candidates** — only a **Draft ADR** action, which
   returns the rendered ADR markdown in a copyable / downloadable panel.
   No approve/promote button (a tooltip explains why).
4. **Code-authoring proposals** — read-only list with markdown preview.

**Learning artifacts directory.** `GET /learning/candidates` and `POST
/learning/promotions` read the `intent_learning_candidates.json` artifact
that `trellis analyze learning-candidates --output-dir <dir>` writes. The
server resolves `<dir>` from `TRELLIS_LEARNING_ARTIFACTS_DIR`, falling back
to `<data_dir>/learning`. When no artifact is found, the list endpoint
returns an empty list plus a `hint`, and the promote endpoint returns
`409`.

### Improvement-metrics dashboard (admin scope)

The **Metrics** view in the static UI (`/ui/` → **Metrics**) charts five
improvement metrics over time. Every series is computed **server-side from
the EventLog on read** — there is no new storage and no caching layer (POC
scale). The endpoint is on the admin router, so it requires the `admin`
scope and respects `TRELLIS_UI_ENABLED` / ops-gating like the rest of
admin. It is read-only and never mutates a store.

```
GET /api/v1/metrics/timeseries?metric=<name>&days=<n>&bucket=day&group_by=<axis>
```

| Param | Values | Default | Notes |
|-------|--------|---------|-------|
| `metric` | one of the five below | — (required) | Unknown value → `422` |
| `days` | positive int | `30` | Look-back window; non-positive → `422` |
| `bucket` | `day` | `day` | Only daily buckets are implemented; anything else → `422` |
| `group_by` | `domain` \| `intent_family` \| `none` | `none` | Unknown value → `422` |

The five metrics (priority order; each a named `metric` value):

| Metric | Definition |
|--------|------------|
| `pack_success_rate` | Share of graded packs with a positive outcome per bucket (`PACK_ASSEMBLED ⋈ FEEDBACK_RECORDED` on `pack_id`, same join as `learning/pack_observations.py`). |
| `reference_rate` | `items_referenced / items_served` per bucket — the best "are packs getting better" proxy. Pooled per bucket (sum referenced / sum served). |
| `advisory_fitness` | Mean advisory confidence per bucket (from the fitness loop's `ADVISORY_SUPPRESSED` / `ADVISORY_RESTORED` `new_confidence`); `sample_count` is the suppressed-advisory count. |
| `noise_tag_volume` | Items flipped to `signal_quality="noise"` per bucket, counted from `TAGS_REFRESHED` audit events whose `after` tags carry the noise label. |
| `parameter_promotions` | Governance event counts per bucket (`PARAMS_UPDATED` + `TUNER_PROPOSAL_CREATED` / `_REJECTED` + `PARAMETERS_DEGRADED`). There are no `PARAMS_AUTO_*` events in this tree; `parameter_promotions` groups by event **type**, not by `domain` / `intent_family`. |

**Response shape.** A list of series, each with a `group_key` and a list of
`{bucket_start, value, sample_count}` points sorted ascending. **Buckets with
no data are omitted** (not zero-filled) — clients infer gaps from the missing
`bucket_start` keys (an absent day means "no signal", not "zero"). Grouping
resolves `domain` from the `PACK_ASSEMBLED` payload and `intent_family` from
the `FEEDBACK_RECORDED` payload; events lacking the requested dimension fall
under `"all"`. The aggregation lives in
`trellis.retrieve.metrics_timeseries.compute_timeseries` (the route is a thin
adapter).

**Definitional parity.** Where a metric overlaps with the agent-loop
convergence scenario, the formula matches that scenario's helpers in
`eval/scenarios/_convergence_common.py` (`pack_success_rate` ↔
`round_success_rate`; `reference_rate` ↔ the useful-fraction in
`_base_round_metrics` / `_convergence_stats`; `advisory_fitness`'s suppressed
count ↔ `advisories_suppressed_total`). Each shared formula is cross-referenced
in a code comment at its call site.

**UI.** Metrics 1–4 render as inline-SVG line charts (zero dependencies, no
build step); `parameter_promotions` renders as an annotated events strip
(grouped bars by event type). A domain / intent-family group-by selector and a
day-window selector drive all charts.

---

## MCP Macro Tools

Start with `trellis-mcp`. 16 tools returning token-budgeted markdown — 10 core tools, 3 sectioned-context tools for richer pack assembly, and 3 structured tools (observations + the mutation escape hatch) that return JSON.

**Core tools**

| Tool | Args | Returns |
|------|------|---------|
| `get_context` | `intent`, `domain?`, `max_tokens?`, `session_id?`, `run_id?`, `sections?`, `index?` | Markdown pack fusing keyword + graph + semantic axes (RRF, recency/importance decay, session dedup) with a citable `pack_id`. Pass `sections` for the sectioned layout. Pass `run_id` (the unit of work this context is for — narrower than `session_id`) so later feedback can credit the runs a memory actually helped; without it the learning join buckets the pack under `unknown-run`. Pass `index=True` for the id index (see "Progressive disclosure" below); mutually exclusive with `sections`. |
| `save_experience` | `trace_json` | Confirmation with trace_id |
| `save_knowledge` | `name`, `entity_type?`, `properties?`, `relates_to?`, `edge_kind?` | Confirmation with entity_id |
| `save_memory` | `content`, `metadata?`, `doc_id?` | Confirmation with doc_id. Tags the document inline when `TRELLIS_ENABLE_CLASSIFY_ON_INGEST=1` (see "Document → content tags"), embeds it when `TRELLIS_ENABLE_EMBED_ON_INGEST=1`. |
| `get_lessons` | `domain?`, `limit?`, `max_tokens?` | Markdown list of precedents |
| `get_graph` | `entity_id`, `depth?`, `max_tokens?` | Markdown subgraph. Renders each node's `document_ids` as copy-pastable evidence pointers — an `Evidence documents` section for the root entity, an inline `docs:` suffix per neighbor — so entity → evidence is followable with `get_items`. |
| `get_items` | `item_ids`, `pack_id?`, `max_tokens?` | Markdown bodies for known ids (max 50), resolved against the document store, the graph, then the trace store. Items over budget are omitted whole with their ids listed for a follow-up call — never truncated; unknown ids are listed as not found. Emits `PACK_ITEMS_FETCHED` with the served ids; pass the `pack_id` that surfaced them to keep the fetch attributable. |
| `record_feedback` | `trace_id?`, `pack_id?`, `success?`, `rating?`, `notes?`, `helpful_item_ids?`, `unhelpful_item_ids?`, `followed_advisory_ids?` | Confirmation |
| `search` | `query`, `limit?`, `max_tokens?`, `index?` | Markdown search results |
| `get_file_context` | `paths`, `include_unconfirmed?`, `max_tokens?` | Markdown context per file path (#307): documents whose `metadata.source_path` names the path (exact, or a `/`-boundary suffix match so absolute paths find stored relpaths) plus graph entities doc-linked to them. Every item carries store timestamps and each path a `Newest memory` line so a client can staleness-gate against the file's mtime. Unconfirmed extraction mints are excluded unless `include_unconfirmed=true` (#301). |

**Sectioned-context tools (deprecated aliases — #262)**

All three now route through the same one retrieval path as `get_context` and are retained as thin aliases for one release. Prefer `get_context` — for custom sections pass `get_context(intent, sections=[...])` (identical schema to `get_sectioned_context`); the objective/task presets are fixed section layouts over the same path.

| Tool | Args | Returns |
|------|------|---------|
| `get_objective_context` | `intent`, `domain?`, `max_tokens?`, `session_id?` | Markdown pack with fixed `Domain Knowledge` + `Operational Context` sections; designed to be called once at workflow start. |
| `get_task_context` | `intent`, `entity_ids?`, `domain?`, `max_tokens?`, `session_id?` | Markdown pack scoped to specific entities; designed for per-step retrieval inside a workflow. |
| `get_sectioned_context` | `intent`, `sections`, `domain?`, `max_tokens?`, `session_id?` | Markdown pack with caller-defined sections (custom affinities, content types, scopes, per-section budgets). |

**Structured tools (JSON, not markdown)**

| Tool | Args | Returns |
|------|------|---------|
| `record_observation` | `subject_entity_id`, `subject_entity_type`, `observer_agent_id`, `content`, `confidence`, `evidence_ref?`, `metadata?` | JSON `{"status": "ok", "observation_id": ...}`. Lands an `Observation` node with a `hasObservation` edge from the subject — see [schemas.md → Observation](schemas.md#observation). Routed through the governed pipeline (`observation.record`). |
| `query_observations` | `subject_entity_id?`, `observer_agent_id?`, `limit?` | JSON `{"status": "ok", "observations": [...]}` — each entry is the Observation property dict plus `node_id` and `node_type`. |
| `execute_mutation` | `operation`, `args`, `idempotency_key?`, `actor?` | JSON `CommandResult`. MCP parity with `POST /commands/batch` for a single command; same five-stage governed pipeline, so policy gates and audit events apply identically. `operation` accepts the wire value (`"link.create"`) or the enum key (`"LINK_CREATE"`). |

`session_id` lets every context tool deduplicate items returned by recent calls in the same session. Token budgets default to the values in `retrieval.budgets` (`config.yaml`); pass `max_tokens > 0` to override.

All read tools track token usage in the event log for observability.

### Progressive Disclosure — index, traverse, fetch

Reading a full pack to decide which items were worth reading is the expensive way round. Three layers let the model filter ids before paying for bodies:

1. **Index** — `get_context(intent, index=True)` (or `search(query, index=True)`) renders one line per item: `- `item_id` (type, ~N tok) Title`. No excerpt bodies, so the same `max_tokens` surveys far more items. The label is the item's `title` metadata when it has one, otherwise the head of its excerpt; `~N tok` is what serving that item in a full pack *would* have cost.
2. **Traverse** — `get_graph(entity_id)` renders each node's `document_ids` as evidence pointers, so an interesting entity leads to the documents that attest it.
3. **Fetch** — `get_items(item_ids=[...], pack_id=...)` returns the bodies actually wanted.

The index is a **real pack**, not a preview: same `pack_id`, same `PACK_ASSEMBLED` telemetry (per-item hashes, `injected_items`, session dedup), so `record_feedback(pack_id=..., helpful_item_ids=[...])` attributes exactly as it does for a full pack. Only the budget charge and the rendering differ — the payload carries `index_mode: true` so analysis can tell the two serve shapes apart, and an index serve counts as a serve for session dedup (`refresh=True` is still the re-injection escape hatch).

Passing `pack_id` to `get_items` is what keeps the fetch attributable: the `PACK_ITEMS_FETCHED` event records `served_item_ids` against the pack whose index surfaced them, so "fetched but graded unhelpful" is distinguishable from "never fetched at all".

```
get_context("pgbouncer failover", index=True)
  → # Context index for: pgbouncer failover
    **pack_id:** `01HABCDEF...`
    - `doc-7` (document, ~410 tok) Failover runbook
    - `svc-api` (entity, ~28 tok) API Gateway

get_graph("svc-api")   → ## Evidence documents (1)
                         - `doc-7`

get_items(["doc-7"], pack_id="01HABCDEF...")   → full body
```

### Capture-health banner

Every pack-returning tool — `get_context` (flat and sectioned), `search`, and the three sectioned aliases — prepends a one-block warning when a **write** surface has gone dark (#309):

```markdown
> **WARNING: memory capture is failing.** 5 write attempt(s) rejected and 0 accepted in the last 24h from: mcp:save_experience (since 2026-08-21 04:12 UTC). New experience from this session is NOT being saved. Diagnose with `trellis analyze health`.
```

The rule is **per surface**, evaluated over a trailing window: a surface warns when it has at least `TRELLIS_CAPTURE_WARN_THRESHOLD` rejected writes — boundary `WRITE_REJECTED` (#297) plus executor `MUTATION_REJECTED`, aggregated under one `mcp:<tool>` label — and **no** accepted write (`MUTATION_EXECUTED`) of its own. Per-surface, not global, because the incident this exists to catch has successful writes in the same window by construction: every MCP `save_*` call rejected while a nightly `trellis ingest corpus` keeps landing rows. A global "zero accepted anywhere" rule would stay silent through exactly that outage.

Executor rejections with `reason="idempotency_replay"` are excluded — a replayed command is a duplicate submission of a write that already landed, not a write that went dark.

| Knob | Default | Notes |
|------|---------|-------|
| `TRELLIS_CAPTURE_WARN_THRESHOLD` | `3` | Rejections one surface needs before it warns (claude-mem's three-consecutive-failures rule). **`0` disables the check entirely** — and skips the store round-trip. |
| `TRELLIS_CAPTURE_WARN_WINDOW_HOURS` | `24` | Trailing window the counts are taken over. |

These are read-side knobs — they shape what retrieval *serves*, not what ingest *writes* — so they live in `src/trellis/ops/capture_health.py`, not `write_config.py`, and do **not** appear in `trellis admin write-config`.

Two properties worth knowing before you read a banner:

- **It rides outside `max_tokens`.** The pack is budgeted first and the banner prepended after, because a warning the budget can evict is a warning that disappears on exactly the small packs a dark capture path produces. Budget for ~60 extra tokens while capture is down.
- **The empty state carries it too.** A pack with no items and no banner means greenfield; a pack with no items and a banner means the writes never landed. Distinguishing those is the point.

The check is advisory telemetry and fails soft: if it raises, the pack is served unbannered (the `track_token_usage` GRACEFUL-DEGRADATION posture). Indeterminate presents as healthy. Run `trellis analyze health` for the full per-tool accept/reject breakdown and rejection-kind taxonomy.

### Citing Pack Elements in Feedback

The three sectioned-context tools render each response with a `pack_id` header and full item/advisory IDs in backticks so agents can cite specific elements when calling `record_feedback`:

```markdown
# Context for: deploy checklist
**pack_id:** `01HABCDEF...`

## Domain Knowledge
- `doc_ownership_rules` (document, 0.82): Ownership rules for platform...

## Advisories
1. `adv_01HXYZ` **[pattern]** Always run dry-run first (n=12, effect=+18%)

---
*Cite feedback via `record_feedback(pack_id="01HABCDEF...", success=..., helpful_item_ids=[...], unhelpful_item_ids=[...])`.*
```

When an agent finishes the task, it calls `record_feedback` with the copied IDs:

- `pack_id` (preferred over `trace_id` when feedback follows a context retrieval) — attributes the outcome to the pack.
- `rating` (0.0–1.0) — grades how useful the pack actually was. Omit `success` when grading: it is derived from the rating (≥ 0.5 counts as a success), so a mediocre pack is not recorded as an unqualified win. Passing `success` alone still works and is recorded as `rating` 1.0 / 0.0.
- `helpful_item_ids` / `unhelpful_item_ids` — cite the specific pack items that actually helped or were noise.
- `followed_advisory_ids` — cite the advisories whose guidance was acted on.

These element-level signals land in the `FEEDBACK_RECORDED` event payload so the fitness loops (`trellis analyze apply-noise-tags`, `trellis analyze advisory-effectiveness`) can attribute outcomes more precisely than pack-level success alone.

The cited ids are *not* written as `items_served` — they are what the agent referenced, not what the pack contained. The payload leaves `items_served` empty so the reference-rate metric keeps joining against the pack's own `PACK_ASSEMBLED` `injected_item_ids`; synthesizing it from the citations would report a 100% reference rate for every attributed pack.

The MCP tool and `POST /api/v1/packs/{pack_id}/feedback` share both the mapping (`PackFeedback.from_agent_signal`) and the writer (`trellis.feedback.recording.record_feedback`), so identical inputs mean the same thing on either surface: one call appends the durable `pack_feedback.jsonl` row under `<stores_dir>/feedback/` **and** emits the authoritative `FEEDBACK_RECORDED` event. The emit fails soft — when the event sink is down the tool still returns, the audit row is on disk, and `trellis admin reconcile-feedback --log-dir <stores_dir>/feedback` (or `worker curate --reconcile-first`, which scans that directory) replays it.

### Retrieval Budgets

The three sectioned-context tools (`get_objective_context`, `get_task_context`, `get_sectioned_context`) resolve their token budgets from the `retrieval.budgets` section of `~/.config/trellis/config.yaml`. This lets you right-size budgets per tool and per domain without touching code.

```yaml
retrieval:
  budgets:
    default:
      max_tokens: 4000
      max_items: 30
    by_tool:
      get_objective_context:
        max_tokens: 5000
        max_items: 25
      get_task_context:
        max_tokens: 2500
      get_sectioned_context:
        max_tokens: 8000
    by_domain:
      orders:
        max_tokens_multiplier: 1.25
```

**Resolution order** (highest to lowest precedence):

1. Caller-supplied `max_tokens` argument (when positive — `0` is the sentinel for "use config").
2. `by_tool.<tool_name>` entry (complete `BudgetSpec`; unspecified fields fall back to the spec's built-in defaults, *not* to the `default` section).
3. `default` section.
4. Hardcoded fallback: `max_tokens=4000`, `max_items=30`.

A `by_domain` multiplier, when present, scales the resolved `max_tokens`/`max_items` before caller overrides are applied. Caller overrides bypass domain multipliers.

### OpenClaw Setup

OpenClaw has native MCP client support. Add Trellis to your `openclaw.json`:

```json
{
  "mcpServers": {
    "trellis-ai": {
      "command": "trellis-mcp",
      "args": []
    }
  }
}
```

Or install via ClawHub:

```bash
clawhub install trellis-ai
```

After restarting OpenClaw, the agent has access to all 16 macro tools above. See [`examples/integrations/openclaw/`](../../examples/integrations/openclaw/) for the full setup guide and skill definition.

---

## Python SDK

```python
from trellis_sdk import TrellisClient

# HTTP client against a running trellis-api
client = TrellisClient(base_url="http://localhost:8420")

# Tests: in-process ASGI client, no network listener
from trellis.testing import in_memory_client
with in_memory_client(tmp_path / "stores") as client:
    ...
```

### Client Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `ingest_trace(trace: dict)` | `str` (trace_id) | Ingest a trace |
| `search(query, domain?, limit?)` | `list[dict]` | Search documents |
| `get_trace(trace_id)` | `dict \| None` | Get trace by ID |
| `list_traces(domain?, limit?)` | `list[dict]` | List recent traces |
| `assemble_pack(intent, **kwargs)` | `dict` | Assemble context pack |
| `get_entity(entity_id)` | `dict \| None` | Get entity |
| `create_entity(name, entity_type?, properties?)` | `str` (node_id) | Create entity |
| `create_link(source_id, target_id, edge_kind?)` | `str` (edge_id) | Create edge |
| `record_feedback(pack_id, success, helpful_item_ids?, unhelpful_item_ids?, followed_advisory_ids?, target_id?, rating?, comment?)` | `PackFeedbackResponse` | Record element-level pack feedback |
| `close()` | — | Release resources |

`record_feedback` mirrors the MCP `record_feedback` tool and routes through
`trellis.feedback.recording.record_feedback` server-side: it appends the durable
`pack_feedback.jsonl` audit row and emits the authoritative `FEEDBACK_RECORDED`
event to the operational EventLog. The returned `PackFeedbackResponse` carries
`event_log_in_sync` — check it to confirm the event reached the log. `False`
means only the JSONL row landed and a reconcile is owed (the emission
soft-failed); the SDK does not swallow that signal. `AsyncTrellisClient` exposes
the same method as a coroutine.

### Skill Functions

Pre-summarized markdown for LLM context injection:

```python
from trellis_sdk.skills import (
    get_context_for_task,
    get_latest_successful_trace,
    save_trace_and_extract_lessons,
    get_recent_activity,
)

context = get_context_for_task(client, "implement retry logic", domain="backend")
```

All skill functions return `str` (markdown), not data objects.

### Workflow Integration Hooks

`trellis_sdk.hooks` packages the pre-task / post-task integration points a
workflow engine needs into three classes. Each wraps a `TrellisClient` and
**degrades gracefully** — a hook method never raises into the host workflow.
On a Trellis outage (server down, 4xx, version mismatch, mid-call drop) the
hook logs a `structlog` warning and returns a sentinel, so the host agent's
task never fails because Trellis is down. Pass `raise_errors=True` to the
constructor to opt into exceptions instead.

| Class | When | Key method(s) | Sentinel on failure |
|-------|------|---------------|---------------------|
| `ContextInjector` | pre-task | `for_intent(intent, domain?, max_tokens?)`, `for_entities(entity_ids, intent?, domain?, max_tokens?)` | `""` (empty markdown) |
| `TraceRecorder` | post-task | `record(step_name, status, duration_ms, entity_ids?, summary?, metrics?, error?, domain?)` | `None` (no trace_id) |
| `ResultFeedback` | post-task | `record_success(target_entity_id, result_name, summary, full_content?, metadata?, pack_id?, helpful_item_ids?)`, `record_failure(target_entity_id, error_summary, trace_id?, pack_id?, unhelpful_item_ids?)` | `HookResult(ok=False)` |

```python
from trellis_sdk import ContextInjector, TraceRecorder, ResultFeedback, TrellisClient

client = TrellisClient(base_url="http://127.0.0.1:8420")
injector = ContextInjector(client)
recorder = TraceRecorder(client, workflow_id="run-42", agent_id="planner")
feedback = ResultFeedback(client)

brief = injector.for_intent("add rate limiting", domain="backend")   # -> markdown
trace_id = recorder.record("plan", "success", 1200, summary="done")   # -> trace_id | None
result = feedback.record_success(                                     # -> HookResult
    target_entity_id="entity:orders-api",
    result_name="rate-limit config",
    summary="token bucket",
    pack_id="pack:abc",          # also grades the supporting pack (positive)
    helpful_item_ids=["doc:1"],
)
```

`ContextInjector` calls `assemble_pack` and falls back to per-entity lookups
(`for_entities`) when the pack is empty, splitting the token budget across
included entities. `TraceRecorder` builds a `source="workflow"` trace tying
every step to `workflow_id` and records failures as well as successes
(`status` is coerced to `unknown` if not one of `success|failure|partial`).
`ResultFeedback` creates a `DOCUMENT` entity + `DESCRIBED_BY` edge on success
and routes pack grading through the SDK's `record_feedback` method (the
authoritative EventLog path) — never a hand-rolled HTTP call. An
`AsyncResultFeedback` variant wraps `AsyncTrellisClient`.

Because the SDK is HTTP-only, the hooks require a running `trellis-api`
server. For tests and examples without a network listener, construct the
hooks against the in-process client from `trellis.testing.in_memory_client`.
A runnable end-to-end demo lives at `examples/hooks_generic_workflow.py`.
