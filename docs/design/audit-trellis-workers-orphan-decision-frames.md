# Decision frames — `src/trellis_workers/` orphan-suspect modules

**Status:** Read-only analysis, 2026-05-16
**Owner:** prepared by swarm Unit A8 of [`plan-next-swarm-wave.md`](./plan-next-swarm-wave.md) §4
**Base SHA:** `9443537` (`origin/main` HEAD at audit time)
**Predecessor:** [`audit-trellis-workers-orphans-2026-05-14.md`](./audit-trellis-workers-orphans-2026-05-14.md) (the C1.9 audit that surfaced the three orphan-suspects via PR #143)

## 1. Why this document exists

C1.9 of [`plan-cleanup-dead-code.md`](./plan-cleanup-dead-code.md) explicitly
defers the *delete vs keep* call on the three orphan-suspects to a human.
The audit answered the **detection** question ("which modules in
`src/trellis_workers/` have zero production callers?"). It deliberately
did not answer the **decision** question ("should we delete them?")
because the C1.6 / C1.7 discipline says deletion needs more signal than
"no current caller."

This doc structures that decision per-module. For each of the three
orphan-suspects it answers four questions in the same order:

1. **What does it do?** (the module's API and intended behavior)
2. **Who was it built for?** (the ADR / plan / commit that authorized it)
3. **What would have to be true to use it?** (the missing wiring)
4. **What's lost by deleting?** (concretely — an ADR-promised surface? a
   designed test harness? just LOC?)

Each frame ends with a recommended decision tagged `[DELETE]`,
`[KEEP — VALIDATE BEFORE DELETING]`, or
`[KEEP — DECISION DOCUMENTED HERE]`. The recommendation is *advisory* —
the four answers are the load-bearing artifact. The user reads the
frames and makes the call. See §5.

## 2. `src/trellis_workers/extract/query_pattern_observer.py` (384 LOC)

### What does it do?

`QueryPatternObserver` is a deterministic-tier extractor (implements the
`trellis.extract.base.Extractor` Protocol, tier
`ExtractorTier.DETERMINISTIC`) that turns a batch of query-log records
into `EntityDraft` / `EdgeDraft` rows for the Observation /
Measurement canonical entity types.

Given a list of `QueryLogRecord` (or shape-compatible dicts), per
subject entity it emits:

- One `Measurement` `EntityDraft` with `metric_name="query_count"` and
  `metric_value=<count>`.
- One `Observation` `EntityDraft` whose `content` summarizes activity
  over the window (gated on `observation_min_query_count`).
- A `hasMeasurement` edge from subject → Measurement, and a
  `hasObservation` edge from subject → Observation.

The module is pure — no store writes, no mutation-pipeline calls. The
intended consumer is the standard `ExtractionDispatcher` → governed
`MutationExecutor` path (the same pattern `DbtManifestExtractor` and
`OpenLineageExtractor` use).

Loud failures: missing `subject_entity_id` or unparseable `timestamp`
raise `ValueError` — the extractor never skips malformed rows. This is
explicit per the program plan §2 ("no silent fallbacks").

### Who was it built for?

Originating PR: **#125** (`feat(retrieve,extract,eval): Observation
retrieval strategy + query-pattern extractor (Item 1 Phase 2)`),
commit `072cdf2` on 2026-05-13.

The module is the **Phase 3 sample extractor** of
[`plan-observation-entity-type.md`](./plan-observation-entity-type.md):

> ### 6. Phase 3 — sample extractor
> `src/trellis_workers/extract/query_pattern_observer.py` (new)
> [...] **Deterministic, no LLM.** This is the simplest end-to-end
> demonstration; LLM-based observation producers ship later.

The plan ships in two consecutive units (Phases 0+1, then Phases 2+3+4).
Phase 3 is the *demonstration* artifact for the
`Observation` / `Measurement` canonical types and the `hasObservation` /
`hasMeasurement` edges (see
[`adr-observation-entity-type.md`](./adr-observation-entity-type.md)).
It is explicitly framed as a sample — the plan does not promise it as
a permanent production surface.

### What would have to be true to use it?

Three pieces of wiring are missing, in order from cheapest to most
substantive:

1. **Register it with `ExtractionDispatcher`.** Today the dispatcher
   ships `JSONRulesExtractor` (in `trellis.extract`) and the dbt /
   OpenLineage extractors (`trellis_workers.extract`); `query-log` is
   not a registered `source_hint`. A caller would need to instantiate
   `QueryPatternObserver` and either register it or invoke it directly.

2. **A producer of `QueryLogRecord` batches.** Real query logs come
   from data-warehouse audit tables (Snowflake `QUERY_HISTORY`,
   BigQuery `INFORMATION_SCHEMA.JOBS`, Postgres `pg_stat_statements`,
   etc.). The module accepts already-normalized records; whoever runs
   it owns the normalization. No such ingestion path exists in
   `trellis_cli` / `trellis_api` today (`trellis_cli/ingest.py` accepts
   dbt-manifest and OpenLineage formats only).

3. **A triggered consumer.** Either:
   - A CLI surface (`trellis admin ingest query-log --path …`) that
     reads JSONL and routes through `MutationExecutor`, or
   - A scheduled worker that polls the warehouse audit log and feeds
     records in batches.

None of (1)-(3) ship today. The plan was clear that this is a
*demonstration* extractor; it never claimed end-to-end production
wiring.

Of separate interest: the **eval scenario for Item 1 Phase 4**
(`eval/scenarios/observation_retrieval.py`) constructs Observation /
Measurement entities **directly** via `EntityDraft` (see lines 82, 135
of that file), bypassing the extractor. So even the eval harness for
the canonical types it demonstrates does not exercise it.

### What's lost by deleting?

- **384 LOC** of implementation + the associated test module
  `tests/unit/workers/extract/test_query_pattern_observer.py`
  (~similar order of magnitude) + the `extract/__init__.py` re-export
  tuple for `QueryLogRecord` / `QueryPatternObserver`.
- **The Item 1 Phase 3 demonstration artifact.** The plan describes
  this as "the simplest end-to-end demonstration" of the
  `Observation` / `Measurement` types. After deletion the canonical
  types remain (they were added in Phases 0+1, shipped via the same
  PR), the retrieval strategy remains
  (`trellis.retrieve.observation_search`), and the eval scenario
  remains — but the *example of how to populate them from a real log
  shape* is gone.
- **A reference implementation of the loud-fail discipline.** The
  module is an unusually clean example of `ValueError`-on-malformed-row
  per `plan-self-improvement-program.md` §2. If a future contributor
  writes another deterministic extractor they would lose this template.
- **Documentation alignment.** `plan-observation-entity-type.md` §6
  and `adr-observation-entity-type.md` §3 both reference this file by
  path. Deleting it without updating the plan + ADR would create a
  documentation-vs-code drift.

### Recommended decision: `[KEEP — VALIDATE BEFORE DELETING]`

The decision turns on a question only the maintainer can answer: **has
the Item 1 demonstration purpose been served?**

- If yes — i.e., Phase 4's eval scenario is the canonical example and
  the maintainer is comfortable that `EntityDraft`-construction-in-eval
  is the documented "how to use Observation/Measurement" pattern — then
  the extractor is a tax (re-export, test maintenance, ruff/typecheck
  surface, dependency on `ExtractionContext`). Move to `[DELETE]`,
  remove the file + tests + re-export tuple in `extract/__init__.py`,
  add a one-line note to `plan-observation-entity-type.md` §6 marking
  the sample retired.
- If the maintainer wants a "from-log shape to drafts" example to live
  in the codebase (because that's the realistic future caller pattern —
  not in-test direct construction), then `[KEEP]`. Possibly move it
  out of `trellis_workers/` and into `eval/` or `examples/` so its
  status as a sample is unambiguous and it stops surfacing as an
  "orphan" in future audits.

A third path: **wire it up.** Add a `trellis admin ingest query-log
--path …` CLI command that reads JSONL → `QueryLogRecord` →
`MutationExecutor`. That promotes the module from sample to live
surface. This is a Phase-shaped item, not a cleanup item — defer to
plan ownership.

## 3. `src/trellis_workers/learning/miner.py` (272 LOC)

### What does it do?

`PrecedentMiner` is the background-loop component of the
**precedent-promotion feature**. Two paths:

1. **Deterministic per-trace** (`extract_precedent_from_trace(trace_id)`)
   — pulls a single trace from `TraceStore`, builds a `Precedent`
   directly from its outcome / intent / evidence, emits
   `PRECEDENT_PROMOTED`. No LLM. Returns the new `Precedent`.

2. **LLM batch** (`generate_precedent_candidates(domain, min_traces,
   limit)`) — queries traces (filtered by domain, capped at `limit`),
   filters to FAILURE / PARTIAL outcomes, takes up to 20, prompts the
   LLM for shared patterns, parses the JSON response into a list of
   `Precedent` candidates, emits `PRECEDENT_PROMOTED` per candidate.
   Parse / validation failures emit `EXTRACTION_FAILED` and raise
   `ExtractionFailureError`; LLM transport errors return `[]` (the
   `GRACEFUL-DEGRADATION` annotation explicitly notes this is the
   "best-effort background pass" pattern, not a silent fallback).

The class holds a `TraceStore`, an optional `EventLog`, and an optional
`LLMClient` — all dependency-injected, no global state.

### Who was it built for?

Originating commit: `3b9cedb` — the initial Trellis commit (2026-04-17).
This is the only `learning/`-package module that ships in
`src/trellis_workers/`. The package was sketched as part of the
original architecture and has carried a `__init__.py` of `1` line ever
since.

References to `PrecedentMiner`:

- [`adr-llm-client-abstraction.md`](./adr-llm-client-abstraction.md)
  table at line 42: "`PrecedentMiner` | `trellis_workers` | Optional
  failure-pattern analysis". And line 153: "`PrecedentMiner.__init__`
  accepts `LLMClient | None` only" — i.e., the LLM-client ADR Phase 3
  cut-over explicitly migrated this class along with `EnrichmentService`.
- `docs/research/compaction-and-agent-patterns.md` line 116: "Precedent
  distillation | PrecedentMiner extracts patterns from trace clusters |
  Unique strength" — frames it as part of the project's research-stage
  thesis ("what makes Trellis different").
- `docs/research/compaction-and-agent-patterns.md` lines 131, 449:
  "The PrecedentMiner already does a version of [Tier 3 summarization]
  — generalize it" and "High importance pattern/decision →
  PrecedentMiner.extract_precedent_from_document()".

So: not authorized by a numbered plan; **authorized by the original
architecture sketch + an active ADR (`adr-llm-client-abstraction.md`)
that treats it as a first-class consumer**. The class predates the
formal plan-and-ADR discipline that the codebase has adopted since.

The `PRECEDENT_PROMOTED` event it emits has a **separate, manual
producer** in production: `PrecedentPromoteHandler` in
`src/trellis/mutate/handlers.py` (governed via the
`Operation.PRECEDENT_PROMOTE` command). The downstream consumer
`trellis.retrieve.precedents.list_precedents()` queries the event type
and is wired into both CLI (`src/trellis_cli/retrieve.py`) and API
(`src/trellis_api/routes/retrieve.py`). **Precedents are a live
feature.** What's not wired is the background-mining variant: nothing
currently runs `PrecedentMiner` on a schedule or in response to a
trigger.

### What would have to be true to use it?

One of two wirings:

1. **A triggered consumer.** Either a `trellis admin mine-precedents
   --domain X` CLI command or a worker that subscribes to
   `TRACE_INGESTED` events and decides when to invoke. The class is
   ready to run today — its constructor takes the three stores it
   needs and `generate_precedent_candidates` is a single coroutine
   call. The missing piece is "who calls it, when".

2. **A test-only fixture path.** The class is exercised by
   `tests/unit/workers/learning/test_miner.py`, which is the only
   non-docstring caller. Tests construct the miner with a `MagicMock`
   `LLMClient` and assert the event-emission behavior. If the
   maintainer decides the test coverage of the *manual*
   `PrecedentPromoteHandler` is sufficient, the miner's tests provide
   redundant coverage of a code path that nothing in production
   triggers.

The deterministic per-trace path is the most plausible thing to wire
first — it's pure on a `TraceStore` read, has no LLM cost, and could
fire from a `TRACE_INGESTED` event handler.

### What's lost by deleting?

- **272 LOC** of implementation + the test module
  `tests/unit/workers/learning/test_miner.py` + the package init
  `src/trellis_workers/learning/__init__.py`.
- **The "automatic precedent distillation" thesis artifact.** The
  research doc (`compaction-and-agent-patterns.md`) frames this as one
  of the project's unique strengths. Deleting it removes the in-code
  embodiment of that thesis; only the schema (`Precedent`) and the
  manual-promotion path survive. The user has to decide whether the
  thesis still binds. If the project's positioning still says
  "precedent distillation is a unique strength" then deleting the only
  implementation of that strength is a real loss; if the project has
  pivoted to "precedents are operator-curated artifacts" then the
  manual-promotion path is the whole story and the miner is dead
  weight.
- **An ADR-referenced consumer.** [`adr-llm-client-abstraction.md`]
  Phase 3 explicitly catalogues `PrecedentMiner` as a migrated
  consumer of the new `LLMClient` protocol. Deleting it requires
  either accepting that the ADR's consumer list shrinks to one
  (`EnrichmentService`) or updating the ADR with a "since-superseded"
  note.
- **The deterministic path.** `extract_precedent_from_trace` is
  ~30 LOC of pure-Python trace → Precedent shaping logic — useful
  for *anyone* who eventually writes a precedent-promotion CLI even
  if the LLM batch path is retired.

### Recommended decision: `[KEEP — VALIDATE BEFORE DELETING]`

The decision turns on a project-positioning question:

- **If "automatic precedent distillation" remains a project goal**
  (per `compaction-and-agent-patterns.md`), then this module is the
  thesis's only embodiment — `[KEEP]`. Add a TODO.md follow-up to wire
  a triggered consumer (a CLI command is the cheapest way to make it
  live; a worker on `TRACE_INGESTED` is the most useful). The
  `[KEEP — VALIDATE BEFORE DELETING]` tag reflects that
  "validate" here means "operator decides whether the goal still
  binds" not "more code analysis."
- **If precedents have become an operator-curated-only artifact**
  (the manual `PrecedentPromoteHandler` is the whole story), then
  `[DELETE]` — and update `adr-llm-client-abstraction.md` Phase 3
  consumer list + `compaction-and-agent-patterns.md` "unique strength"
  claim to match. Possibly preserve `extract_precedent_from_trace`
  (the ~30-LOC deterministic shaper) into a smaller surface in
  `trellis/mutate/handlers.py` — that helper is independently useful
  even without the LLM-batch path.

## 4. `src/trellis_workers/maintenance/retention.py` (220 LOC)

### What does it do?

Two classes in one module — `RetentionWorker` (trace-store pruning)
and `StalenessDetector` (document-store age check):

1. `RetentionWorker.run(RetentionPolicy)` queries traces older than
   `max_age_days`, skips those whose outcome is in `preserve_outcomes`,
   and emits a `MUTATION_EXECUTED` event with `action="retention_prune"`
   for each marked trace (no physical delete — `TraceStore` is
   append-only). Returns a `RetentionReport` with scanned / marked /
   preserved counts + per-trace errors.

2. `StalenessDetector.check()` walks `DocumentStore.list_documents`,
   parses `updated_at`, classifies each as stale (older than
   `staleness_days`) or malformed (unparseable `updated_at`), emits
   `EXTRACTION_FAILED` per malformed doc with structured failure_kind,
   and raises `RetentionDriftError` if the malformed-fraction exceeds
   `MALFORMED_DOCUMENT_THRESHOLD` (1%). Returns a `StalenessReport`.

Both classes are loud-fail — `RetentionDriftError` and the per-row
malformed reporting were explicitly added in PR #116
(`fix(retention): surface malformed updated_at instead of silent skip
(C2 Phase 1.5)`). That PR is recent enough (2026-05-13) to confirm
the maintainer has invested in this module as part of the silent-fail
audit, not just left it untouched since the initial commit.

### Who was it built for?

Originating commit: `3b9cedb` — the initial Trellis commit (2026-04-17).
Like `learning/miner.py`, no numbered plan or ADR explicitly designs
it; it shipped with the original architecture as part of the
`trellis_workers/` package.

References to retention machinery in the design corpus:

- `docs/research/compaction-and-agent-patterns.md` line 323:
  "Staleness detection | `StalenessDetector` (reporting only, no
  action) | `RetentionWorker` (marks for prune) | None" — explicit
  framing that the two classes are both *already* part of the
  compaction taxonomy.
- Same doc line 379: "Currently `StalenessDetector.check()` returns a
  list of stale document IDs. **Nothing consumes this list.**
  `RetentionWorker` handles traces but not documents." (This is the
  audit doc's framing — the research doc had already noticed the
  unwired state.)
- Same doc line 381+: proposes a future `DocumentRetentionWorker` that
  *calls* `StalenessDetector.check()` and then prunes — the research
  doc identifies the missing wiring concretely.
- TODO.md lines 1120, 1446: "`P1:` TTL metadata +
  `DocumentRetentionWorker` for auto-expiry" — same proposal carried
  into the active backlog.

So: authorized by the original architecture + recognized in the
research doc as a known-incomplete part of the compaction story, with
the missing wiring already specified in TODO.md as a P1 item.

### What would have to be true to use it?

One of three wirings, in order of immediate impact:

1. **A cron-triggered worker.** The simplest possible wire: a
   `trellis admin retention run --max-age-days N` CLI command that
   constructs `RetentionWorker(trace_store, event_log)` and calls
   `run(policy)`. ~30 LOC of CLI glue + a click flag set. The class
   is ready.

2. **The proposed `DocumentRetentionWorker`** (per TODO.md /
   research doc): a small new class that calls
   `StalenessDetector.check()`, marks the returned stale docs for
   prune via the `MutationExecutor` pipeline (a new
   `DOCUMENT_RETENTION_PRUNE` command), and emits a report.
   ~80-100 LOC of new code, plus this module supplies the staleness
   half for free.

3. **The `RetentionDriftError` consumer.** Today the error raises but
   nothing catches it at a higher level (CLI handler / API exception
   middleware). A production operator would see a stack trace, not a
   structured alert. Adding the wiring is ~20 LOC of CLI / API glue.

### What's lost by deleting?

- **220 LOC** of implementation + the test module
  `tests/unit/workers/maintenance/test_retention.py` + the package
  init `src/trellis_workers/maintenance/__init__.py`.
- **A working `RetentionDriftError` template.** The
  `MALFORMED_DOCUMENT_THRESHOLD` constant + `RetentionDriftError`
  class + per-row report-tracking pattern is a clean reference
  implementation of the silent-fail-audit C2 rubric outcome. Future
  workers in the same shape (any "walk a bunch of rows, count bad
  ones, raise above a threshold" job) would have to re-derive this.
- **`StalenessDetector` specifically — the missing-piece half of an
  in-flight design.** The research doc says "`StalenessDetector`
  exists; the consumer doesn't yet" and TODO.md confirms this as a
  P1 backlog item. Deleting `StalenessDetector` reverses that
  half-done state to "neither piece exists" and lengthens the future
  `DocumentRetentionWorker` task by the ~60 LOC of `StalenessDetector`
  + `MALFORMED_DOCUMENT_THRESHOLD` + `RetentionDriftError` it would
  have to re-implement.
- **Production telemetry pattern.** The recent PR #116 work on this
  module is one of the cleanest examples of the audit-then-emit-then-
  raise discipline. Deleting it would erase the audit's most
  visible win in `trellis_workers/`.

### Recommended decision: `[KEEP — VALIDATE BEFORE DELETING]`

The decision turns on whether the **TODO.md P1 items** are still
binding:

- If TTL metadata + `DocumentRetentionWorker` + auto-expiry are real
  future work — and TODO.md lists them as P1, the research doc
  treats them as known opportunities, and `StalenessDetector` is the
  half that's already done — then `[KEEP]`. Optionally promote one
  of the three wirings in §3 above to a Wave 2/3 unit so the module
  stops being "orphan-suspect" and starts being "wired."
- If the maintainer wants to retire the P1 items entirely (decide
  that retention / staleness is operator-driven outside Trellis,
  managed at the database layer, etc.), then `[DELETE]` — and prune
  the TODO.md entries at the same time + add a note to the research
  doc that the compaction taxonomy's retention column is intentionally
  empty. **Don't delete this module without that TODO.md sweep** —
  otherwise the next contributor reads TODO.md, decides to build the
  P1 item, and re-derives the same code.

There is a non-obvious **partial-keep** option here: keep
`StalenessDetector` (the half that's the
explicit missing-half of an active design) and delete `RetentionWorker`
(which has no proposed consumer in the backlog — the manual
`PrecedentPromoteHandler` pattern argues that trace retention should
also be a governed command, not a worker). Listing this so the user
has the full menu.

## 5. How to use this document

This document does not auto-delete anything. It captures, for each
orphan-suspect, the four answers a maintainer needs in order to make
the delete-or-keep call:

1. **Read each frame** in §2 / §3 / §4.
2. **Compare the "What's lost" list against the project's current
   priorities.** Each frame highlights the ADR / plan / TODO.md item
   that the module is anchored against. If that anchor no longer binds
   the project, deletion is safe. If it does, deletion needs a
   companion edit to the anchor doc.
3. **Decide per-module.** The recommended-decision tags
   (`[DELETE]` / `[KEEP — VALIDATE BEFORE DELETING]` /
   `[KEEP — DECISION DOCUMENTED HERE]`) are the agent's read; the
   maintainer's call is the binding one.
4. **If deciding `[DELETE]`:**
   - Open a focused PR per module (don't bundle multiple deletes — the
     decisions are independent and revertable separately).
   - The PR removes: the module file, its test module, any
     `__init__.py` re-export, the entry from
     `src/trellis_workers/extract/__init__.py.__all__` for the
     query-pattern observer case.
   - Update any anchor doc that names the deleted file —
     `plan-observation-entity-type.md` §6 for the query-pattern
     observer, `adr-llm-client-abstraction.md` consumer list for the
     miner, TODO.md P1 retention items for the retention module.
   - Update [`audit-trellis-workers-orphans-2026-05-14.md`](./audit-trellis-workers-orphans-2026-05-14.md)
     to strike the deleted rows from the table.
5. **If deciding `[KEEP]`:**
   - Add a follow-up unit to the relevant plan that *wires* the module
     — that's the single best signal a maintainer can leave for the
     next audit ("this module is intentional; here is the issue that
     will wire it"). Otherwise the same three modules surface as
     orphan-suspects in the next C1.x sweep.
   - Optionally add a one-line `# AUDIT-DECISION 2026-05-XX: KEEP per
     <link>` comment at the top of the module so future audits can
     short-circuit.

Per the C1.6 / C1.7 discipline, deletion that lacks one of (a) a
roadmap that drops the use case, (b) a supersession by core, or
(c) a maintainer signal is not appropriate. This doc is the
"surface the decision" half of the work; the maintainer signal is the
other half.

## 6. Summary table

| Module | LOC | Recommended | Driver question |
|---|---:|---|---|
| `extract/query_pattern_observer.py` | 384 | `[KEEP — VALIDATE BEFORE DELETING]` | Has the Item 1 demonstration purpose been served? Has the eval scenario replaced the need for an in-code sample? |
| `learning/miner.py` | 272 | `[KEEP — VALIDATE BEFORE DELETING]` | Does the "automatic precedent distillation" thesis still bind? Or have precedents become operator-curated only? |
| `maintenance/retention.py` | 220 | `[KEEP — VALIDATE BEFORE DELETING]` | Are the TTL + `DocumentRetentionWorker` P1 items in TODO.md still binding? |
| **Combined** | **876** | | |

All three frames carry the same `KEEP — VALIDATE` tag because none of
the four-answer analyses produces an unambiguous `[DELETE]` signal
without a maintainer-side judgment call about project priorities. The
audit doc's "no production caller" is the *necessary* condition for
deletion, not the *sufficient* one. This doc surfaces the sufficient-
condition questions; the maintainer answers them.
