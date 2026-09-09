# Plan 4 — Give `get_file_context` a producer: stamp trace evidence onto the trace document

> **Nightly TODO.** Tracked as a trellis-ai issue on board #275. **Part A is eligible for
> the autonomous code-authoring loop** (`mechanical` + `ready`, with the `files_allowed`
> block below). Part B is ops and is owner-run.

**Repo:** `ronsse/trellis-ai` (Part A) · `~/projects/skynet-hub/stacks/trellis` (Part B).

## Verified premise (production, 2026-09-09)

`get_file_context(paths=[...])` is a shipped MCP tool (`src/trellis/mcp/server.py:2267`)
whose docstring promises *"documents whose `source_path` names that file"*. It returns
nothing for any repo source file, and it structurally cannot return anything, because
**nothing writes a repo file path into `source_path`.**

All three writers, exhaustively (`grep -rn 'source_path=\|"source_path"' src/ --include=*.py`):

| Writer | Value it produces |
|---|---|
| `src/trellis_workers/session_capture/transcripts.py:222` | the transcript **JSONL** path (`SessionDigest(source_path=str(path))`) |
| `src/trellis_workers/trace_embed/render.py:189` | the synthetic `f"trace/{trace.trace_id}"` |
| `src/trellis/ingest_corpus/sync.py:406,615` | the corpus-relative path of an ingested `*.md` |

Live document store (Postgres, `trellis_knowledge`), 1,875 documents:

```
  811  session/*                    160  <no source_path>
   21  *.md                         rest  conversation titles ("Evaluating job offer: …")
    0  trace/*
    0  any source_path ending .py/.ts/.tsx/.js/.sh/.yaml/.yml/.toml/.sql/.html
```

Two zeros, and they are different failures:

- **`0` code extensions** is the declared-but-unreachable path. `_matching_documents`
  (`src/trellis/retrieve/file_context.py:174-215`) pages the whole document store reading
  one key — `metadata["source_path"]` — through `source_path_matches`. No writer can put a
  repo path there, so the scan is a guaranteed miss. Confirmed behaviourally earlier in this
  review: the tool returned *"No stored context for this path."* for the three most-worked
  files in this repo.
- **`0` `trace/*`** is separate and was not expected: **`trellis worker embed-traces` has
  never run on this deployment.** 199 traces exist (newest `2026-09-09 02:28Z`), and not one
  has been rendered into a retrievable document. The command exists
  (`src/trellis_cli/worker.py`, `@worker_app.command("embed-traces")`) and appears in **no**
  script under `~/projects/skynet-hub/stacks/trellis/` — the nightly cron runs
  `capture` (03:00), `curate` (03:30), `backup` (04:30), `roadmap` (05:00) and nothing else.

Meanwhile the evidence the tool wants is already parsed and already deterministic.
`parse_trace_evidence` (`src/trellis/extract/evidence.py:229`) reads a trace's `tool_call`
steps — Edit/Write/MultiEdit/NotebookEdit shapes and unified-diff `+++`/`---` hunks — into
`TraceEvidence.files_touched` (`:202`), and #308 already established that this key is
**evidence-only**: a model's *claim* about what it modified is demoted to a
`files_touched_unverified` companion and can never displace it. Today that lands on the
Activity graph node (`apply_trace_evidence`, `:303-332`) and stops there. Nothing carries it
to the document layer, which is the layer `get_file_context` reads.

**The decision this plan makes.** The alternative was to retire the tool. It is rejected:
the evidence exists on 199 traces, the reader is written and tested, and "what do I already
know about this file" is precisely the mid-work retrieval the rest of this review found
missing. Retiring a working reader because its producer was never built is the wrong half to
delete. Owner may override — the retire arm is a one-commit deletion of the tool, its
formatter and `file_context.py`.

## Part A — the code (autonomous-eligible)

### A1. Writer — `src/trellis_workers/trace_embed/render.py`

`build_trace_metadata(trace)` (`:176-201`) already holds the whole `Trace`. Add:

```python
evidence = parse_trace_evidence(trace)
if evidence.files_touched:
    raw["files_touched"] = list(evidence.files_touched)
```

**No schema change.** `DocumentMetadata` is `extra="forbid"`, but `from_mapping` routes an
unknown key into `custom` and `to_metadata` emits `custom` **flat** at the top level
(`src/trellis/schemas/document_metadata.py`, `to_metadata`: `flat = dict(self.custom)` then
core fields) — so the stored dict carries `metadata["files_touched"]` as a plain top-level
list and `json_extract(metadata, '$.files_touched')` works. Adding a core field was
considered and rejected: it changes a schema six writers share to serve one reader.

Only `files_touched` is stamped. `files_read` and `commands_run` are deliberately excluded
— #308 takes the *union* with unattested values for those two, so they are a weaker claim,
and "this trace read the file" is a much noisier match than "this trace changed it".

### A2. Reader — `src/trellis/retrieve/file_context.py`

In `_matching_documents`, match on **either** key:

```python
stored = metadata.get("source_path")
matched = source_path_matches(stored, path)
via = "source_path" if matched else ""
if not matched:
    touched = metadata.get("files_touched")
    if isinstance(touched, list):
        matched = any(source_path_matches(entry, path) for entry in touched)
        via = "files_touched" if matched else ""
if not matched:
    continue
```

Reuse `source_path_matches` per member rather than writing a second matcher — it already
encodes the rules that matter (equality; `/`-boundary suffix in either direction; a
single-segment value matches by **equality only**, so a stored `TODO.md` does not answer a
read of another project's `TODO.md`). A second matcher is how the two drift.

Thread `matched_via` into `_document_entry` (`:144-155`) as a new key.

### A3. Formatter — `src/trellis/retrieve/formatters.py`

`format_file_context_as_markdown` (`:619`) renders each document as
`- **{label}** \`{doc_id}\` (updated {stamp})`. Append a marker when
`matched_via == "files_touched"` — e.g. `(updated {stamp}, touched by this trace)` — so a
caller can tell "a memory *about* this file" from "a trace that *changed* it". Those are
different claims and rendering them identically is the `content_type`/`document_form` drift
this repo keeps producing.

### A4. Tests

- `tests/unit/workers/trace_embed/test_render.py` — a trace with an `Edit` step naming
  `src/foo.py` produces `metadata["files_touched"] == ["src/foo.py"]`, **top-level, not
  nested under `custom`** (that assertion is the one that catches a `to_metadata` change);
  a trace with no file-touching steps omits the key entirely (not `[]` — an empty list is a
  claim that nothing was touched, and absence is the honest shape); a step whose *args*
  claim a file the evidence does not attest does **not** reach the key (#308's rule, at the
  document layer this time).
- `tests/unit/retrieve/test_file_context.py` — seed **three** documents: one matching on
  `source_path`, one matching only via `files_touched`, one matching neither. Assert exactly
  two returned, with the right `matched_via` on each. A population of two cannot separate
  "reads the new key" from "returns everything". Also: a `files_touched` entry that is not a
  string, and a `files_touched` that is a bare string rather than a list, are both ignored
  without raising; a document matching on **both** keys is returned once with
  `matched_via == "source_path"` (first match wins, deterministic).
- `tests/unit/retrieve/test_formatters.py` — the marker renders for `files_touched` and does
  **not** render for `source_path`.

Mutants to kill: dropping the `files_touched` branch; matching with `in` / substring instead
of `source_path_matches`; `matched_via` hard-coded to one value (the three-document fixture
is what kills this — a uniform pool would not, per #447); the writer stamping
`files_read` too.

```files_allowed
src/trellis_workers/trace_embed/render.py
src/trellis/retrieve/file_context.py
src/trellis/retrieve/formatters.py
tests/unit/workers/trace_embed/test_render.py
tests/unit/retrieve/test_file_context.py
tests/unit/retrieve/test_formatters.py
```

## Part B — ops (owner-run, outside the allowlist)

**A1–A3 change nothing observable until `embed-traces` runs.** The producer writes at
trace-render time, and zero traces have ever been rendered.

1. Backfill once, against prod:
   `trellis-skynet worker embed-traces` — needs an embedder and a vector store (host Ollama
   `nomic-embed-text` is already what `trellis-skynet` points at); safe to interrupt, the
   cursor is advisory and the done-check asks the vector store.
2. Add it to the nightly. Natural home is `~/projects/skynet-hub/stacks/trellis/curate-nightly.sh`
   (03:30, already post-capture) or a new `embed-nightly.sh` in the same cron block. Traces
   are written continuously; a pass that only ever ran once decays back to this state.
3. Re-probe: `get_file_context(paths=["src/trellis/retrieve/pack_builder.py"])` should return
   the traces that edited it.

## Measurement

Before: documents with a code-extension `source_path` = **0**; `get_file_context` hit rate on
the three most-edited files in this repo = **0/3**.

After Part A + B: count documents carrying a non-empty `metadata.files_touched`, and re-run
the same 3-file probe. Report the hit rate, not "the tool now works" — and report it against
the 199 existing traces, since that is the corpus the backfill has to work with. A hit rate
that is still 0 after the backfill means the traces' steps do not carry the tool-call shapes
`parse_trace_evidence` reads, which is a different (and more interesting) finding than this
plan predicts.

## Non-goals

- No change to `parse_trace_evidence`, to `apply_trace_evidence`, or to the Activity-node
  stamping. The evidence parser is correct; only the transport to the document layer is
  missing.
- No new `source_path` writer. Overloading `source_path` with a list is the obvious wrong
  fix — it is a single locator and three other readers key off it.
- No `files_read` / `commands_run` stamping (see A1).
- No re-embedding. This is metadata on a document written at render time; the trace-embed
  pass writes both together, so a backfill is a normal pass, not a `resync`.

## Risks

- **Part A ships and nothing changes**, because Part B never runs. This is the likeliest
  failure and the reason Part B is written into the same plan rather than filed separately.
- **`metadata["files_touched"]` colliding with a future core field of that name.** If one is
  ever added, `to_metadata` emits the core field over the `custom` key — the values would be
  the same list, but pin the top-level assertion in A4 so the move is caught.
- **Chunk rows.** A chunk inherits only `CLASSIFY_METADATA_KEYS`, so a chunked trace
  document's chunks would not carry the key. Trace renders are short and are not chunked
  today; `_matching_documents` already excludes chunk rows from the document list
  (`is_chunk_doc_id`), so the reader is unaffected either way.
- **Noise.** A trace that touched forty files stamps forty paths, and a heavily-edited file
  will match many traces. The existing `max_tokens` budget (default 2000) and
  `truncate_excerpt` are the only limiters. Measure the per-path document count after the
  backfill before adding a cap — a cap chosen before the distribution is known is the
  `max_items`-as-quota mistake (#359).
