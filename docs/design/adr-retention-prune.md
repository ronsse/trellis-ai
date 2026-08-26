# ADR: `retention.prune` disposition — governed handler or explicit retirement

**Status:** **Accepted (Option A)** — decided 2026-08-25; see §6. Phase one (archival) implemented.
**Date:** 2026-08-21
**Deciders:** Trellis core (recommendation); owner (decision)
**Related:**
- [ronsse/trellis-ai#312](https://github.com/ronsse/trellis-ai/issues/312) — the handler-or-retire disposition this ADR answers; sourced from the claude-mem comparison audit (2026-08-19, TODO.md §"claude-mem comparison audit — steal-list")
- [`../../src/trellis/mutate/commands.py`](../../src/trellis/mutate/commands.py) — `Operation.RETENTION_PRUNE` enum verb, registered with an empty required-args schema and **no handler**
- [`../../src/trellis/mutate/handlers.py`](../../src/trellis/mutate/handlers.py) — `RedactionApplyHandler` (#302), the governance template Option A is modeled on
- [`../../src/trellis/schemas/classification.py`](../../src/trellis/schemas/classification.py) — `Lifecycle` staleness states (candidate-set input); `ContentTags.signal_quality`
- [`../../src/trellis_workers/maintenance/retention.py`](../../src/trellis_workers/maintenance/retention.py) — orphaned `RetentionWorker` / `StalenessDetector` (266 lines; `KEEP — VALIDATE` in the orphan audit)
- [`./audit-trellis-workers-orphan-decision-frames.md`](./audit-trellis-workers-orphan-decision-frames.md) — the orphan audit's decision frame for that module
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — why `Lifecycle` is a separate axis from `signal_quality`

---

## 1. Context

### The anti-lesson

The claude-mem comparison audit (2026-08-19) found that the 66K-star plugin has
**no retention story at all**: no pruning, no compaction, unbounded observation
and vector growth, a `relevance_count` column that nothing ever writes, and dead
schema still migrated on every fresh install. At their adoption scale that is
their most visible unaddressed liability — and retrofitting retention into a
live memory store, under users, is exactly the position they are now in.

The audit's takeaway for Trellis: **decide before dogfood scale makes it
hurt.** Not necessarily *build* — decide. An explicit "unbounded growth is
acceptable at target scale, here is why" is a legitimate outcome; an enum verb
that silently fails is not.

### What Trellis already has

The vocabulary is half-built, which is the worst of the available states:

| Piece | State |
|---|---|
| `Operation.RETENTION_PRUNE = "retention.prune"` | Enum verb exists in [`commands.py`](../../src/trellis/mutate/commands.py) with `set()` required args and **no handler** in `create_curate_handlers` — a real deployment's executor rejects every command with "No handler registered". The exact gap `redaction.apply` had before #302. The property-invariants suite even lists `(RETENTION_PRUNE, {})` in its valid-command alphabet — the shape validates; only mock handlers ever execute it. |
| `PolicyType.RETENTION` | Exists in [`enums.py`](../../src/trellis/schemas/enums.py). No consumer — no gate, no handler, no policy ever typed against it. |
| `Lifecycle` | Shipped in [`classification.py`](../../src/trellis/schemas/classification.py) with states `draft / current / deprecated / superseded / archived`, explicitly "defined so the shape is stable before any consumer ships; no classifier populates it and no policy gate enforces it yet." |
| `ContentTags.signal_quality = "noise"` | **Live.** Populated by the tagging pipeline and by `apply_noise_tags()` (the demote half of the feedback loop); noise items are already excluded from packs by default. |
| `redaction.apply` (#302) | **Live.** Governed hard-purge of a single graph entity: policy-gated through the standard pipeline, refuses `NullEventLog`, race-checked, emits a content-free `REDACTION_APPLIED` payload. Shipped as a single PR and already exercised for real defect cleanup. |
| Store-level maintenance primitives | **Live but manual.** `GraphStore.compact_versions` (drops *closed* SCD-2 rows, never current ones; emits `GRAPH_VERSIONS_COMPACTED`) and `BlobStore.sweep_expired` (TTL sweep; emits `BLOB_GC_SWEPT`). Both support `dry_run`, both are direct API calls — not governed commands, no scheduler. |
| `RetentionWorker` / `StalenessDetector` | Orphaned (266 lines in `trellis_workers/maintenance/retention.py`, referenced nowhere in `src/`). The worker only *marks* old traces in the event log — it never deletes, because traces are immutable. `KEEP — VALIDATE BEFORE DELETING` in the orphan audit, conditional on the retention backlog items staying real. **This ADR is that validation:** §3.3 and §4.1 answer the audit's driver question — "are the TTL + `DocumentRetentionWorker` P1 items in TODO.md still binding?" — explicitly and differently under each option. |
| Sibling unhandled verbs | `retention.prune` is not alone in this state. Seven other `Operation` members carry `OperationRegistry` schemas ([`commands.py`](../../src/trellis/mutate/commands.py)) and have **no handler class anywhere in `src/`**: `trace.append_step`, `trace.record_outcome`, `evidence.ingest`, `evidence.attach`, `precedent.update`, `entity.merge`, `link.remove`. `create_curate_handlers` is the only handler factory and registers 11 of the enum's 19 verbs. The half-built verb is a codebase-wide habit, not a retention anomaly — which is why §4.1's cleanup cannot be sold as fixing the class. |

### Hard constraints any option must respect

- **Traces are immutable.** Pruning never touches the trace store. Full stop.
- **The EventLog is append-only.** It is the audit trail; retention of *event
  rows* is a store-operations concern (Postgres partitioning, archival), not a
  mutation verb.
- **All mutations go through the governed pipeline.** If pruning exists, it is
  a `Command` through `MutationExecutor` — validate → policy → idempotency →
  execute → emit — not a script with store handles.

### Why growth pressure is real but not acute

Target scale today is a single-operator dogfood deployment (order 10²–10³
documents) on SQLite substrates comfortable into the 10⁶-row range.
Retrieval quality is already defended independently of store size: noise
exclusion, the content floor, unconfirmed-mint gating, and hard token budgets
mean a growing store degrades *scan cost and storage*, not pack quality,
long before it degrades what agents see. The pressure that exists is
extraction-side: memory extraction mints entities faster than curation
confirms them, and the #298 family (same-day stub noise) shows derived junk
accumulating even at dogfood scale.

---

## 2. The decision to make

Two dispositions, mutually exclusive:

- **(A)** Implement `retention.prune` as a governed mutation handler modeled on
  `redaction.apply` — policy-gated, evented, never touching traces, with
  noise-tagged and lifecycle-stale **derived** items as the candidate set.
- **(B)** Retire the concept: delete the dead verb and the orphan worker, and
  document explicitly why unbounded growth is acceptable at target scale.

Doing nothing — keeping a verb that always fails, a policy type with no
consumer, and an orphan worker — is not on the menu *for retention*. Left
alone it is claude-mem's `relevance_count` column wearing our naming
conventions. Seven sibling verbs sit in the same state (§1), so the shape is
a codebase-wide habit rather than a retention-specific defect. That makes it
worth naming, not worth ignoring — but the other seven are out of scope here:
this ADR resolves one instance and does not pretend to resolve the class.

---

## 3. Option A — governed `retention.prune` handler

### 3.1 Semantics: retention is not redaction

`redaction.apply` answers "this content must cease to exist" — compliance,
single named target, purges all history, and a redaction that time-travel can
resurrect would not be a redaction. Retention answers "this content stopped
earning its storage" — hygiene, batch, criteria-driven, and reversibility is a
*virtue* rather than a defect. The design consequences:

- **Criteria-driven, not target-driven.** The command names a candidate
  *predicate*, and the handler resolves it to concrete ids at execute time.
- **Dry-run first.** Like `compact_versions` / `sweep_expired`, a dry run
  returns the same report shape and emits the same event flagged
  `dry_run=True`, so an operator previews the candidate set before anything
  is removed. Unlike redaction, destructive-by-default is wrong here.
- **Two-phase disposal (recommended shape).** Phase one is *archival*: an
  `entity.update`-shaped write setting `Lifecycle.state="archived"`, which
  retrieval excludes (the first real consumer of `Lifecycle`). Phase two is
  *purge*: physical removal of items already archived longer than a grace
  period, reusing the `delete_node` purge machinery `redaction.apply` is
  built on. A single-phase purge-only handler is the smaller build, but
  two-phase is what makes a wrong prune survivable — the claude-mem lesson
  argues for the version an operator can walk back.

### 3.2 Candidate set

Included — **derived, low-value Knowledge-Plane items only**:

- Graph entities tagged `signal_quality="noise"` (the demote loop's output
  finally gets a disposal path instead of accumulating as excluded-but-stored
  rows).
- Items with `Lifecycle.state` in `{deprecated, superseded, archived}` past a
  configurable grace period — `superseded_by` links mean the replacement
  survives.
- Unconfirmed extraction mints (`extraction_status="unconfirmed"`) older than
  a grace period that no curation pass ever confirmed — the #298-family stub
  noise, which is the one population actually growing without bound today.
- Orphaned vector entries whose backing item was previously pruned.

Excluded — never candidates, enforced in the handler, not left to policy:

- **Traces** (immutable, hard rule).
- **EventLog rows** (append-only audit; the record *of* pruning lives here).
- **Confirmed entities and their documents**, regardless of age — age alone is
  not a value signal in a memory system whose oldest confirmed facts are often
  its most valuable.
- **Anything a policy rule protects** (`PolicyType.RETENTION` gets its first
  consumer: a deny rule on `retention.prune` scoped by entity type or domain
  blocks pruning there).

### 3.3 Pipeline fit (the `redaction.apply` template)

- **Args schema:** `{"criteria": {...}, "reason": str}` required, `dry_run`
  defaulting true — replacing today's empty `set()` registration. `reason` is
  written verbatim to the audit log with the same non-empty / length-capped
  validation as redaction.
- **Policy stage:** the standard gate; a `PolicyType.RETENTION` deny rule is
  the operator's kill switch.
- **Evented:** new `EventType.RETENTION_PRUNED` with a content-free payload —
  criteria, counts per store, id pointers (capped, follow-up pointers not an
  exhaustive index — the `_LINKED_SIGNAL_LIMIT` convention), `dry_run` flag.
  Refuses `NullEventLog` for destructive runs, exactly as redaction does: the
  event is the only record that survives the purge.
- **Idempotency:** `Command.idempotency_key` for at-most-once batch runs;
  re-pruning an already-pruned id is a recorded no-op inside the batch, not a
  failure (unlike redaction's single-target `NotFoundError`, a criteria-driven
  batch tolerates a moving candidate set).
- **Orphan resolution:** the module is retired under Option A too, not
  rehabilitated. `RetentionWorker`'s trace-marking mode predates the
  immutability hard rule doing that job by construction.
  `StalenessDetector.check()` returns exactly one thing — document ids whose
  `updated_at` is older than `staleness_days` (default 90), a pure age test
  over `DocumentStore.list_documents`. §3.2's included set contains no
  documents at all, and its exclusions rule them out on precisely that
  ground: age alone is not a value signal. Under §3.2's own rules the
  detector's entire output is non-candidates, so it is not the feeder Option
  A needs; the handler resolves criteria against the graph and vector stores
  directly. A document-age candidate class would be a separate decision with
  its own justification, not an inheritance from this module.
- **Backlog ruling (the orphan audit's driver question):** the two open
  `TODO.md` P1 items — "TTL metadata + `DocumentRetentionWorker` for
  auto-expiry" — are **superseded, not deferred**, under Option A. Auto-expiry
  becomes a criteria class of the governed verb rather than a standalone
  worker; the items are rewritten to point at this ADR. Blob TTL already has
  its mechanical answer in `BlobStore.sweep_expired`.

### 3.4 Consequences

Positive:

- The half-built state ends in the coherent direction: the verb works, the
  policy type has a consumer, `Lifecycle` gets its first writer and its first
  enforcement point, the orphan is wired or retired deliberately.
- The demote loop closes physically: today `apply_noise_tags` demotes items
  into a store-forever purgatory; pruning is the missing terminal state.
- Retrofitting cost is paid while it is cheap — one deployment, one operator,
  a governance pipeline that makes the handler small (`redaction.apply` landed
  as one PR: a ~190-LOC handler plus CLI glue and tests, ~800 lines total;
  this is the same shape plus candidate-set resolution).
- The differentiator story stays honest: "governed mutations, immutable audit,
  policy-based access control" currently has a maintenance verb that fails on
  contact.

Negative:

- Real build and review cost for a pressure that is projected, not felt —
  at 10² items nothing hurts yet, and the effort competes with loop-starvation
  work that is hurting now.
- Pruned items dangle in history: old `PACK_ASSEMBLED.injected_items[]` rows
  point at ids that no longer resolve. Redaction already set this precedent
  (pointers survive, content does not), but a *batch* verb multiplies it, and
  effectiveness analysis must tolerate unresolvable item ids.
- A value-judging deleter can be wrong. Grace periods, dry-run-default, and
  two-phase archival mitigate; they do not eliminate. Every mitigations knob
  is also new configuration surface.

---

## 4. Option B — retire the concept

### 4.1 What retiring means concretely

Retirement is an *action*, not a shrug:

- Delete `Operation.RETENTION_PRUNE`, its `OperationRegistry` entry, and the
  property-invariants row. This retires **one of eight** advertised-but-
  rejected verbs (§1); the other seven still fail at dispatch, three of them
  core curation (`precedent.update`, `entity.merge`, `link.remove`) rather
  than maintenance. Option B buys a smaller codebase, not an honest verb
  surface.
- Delete `trellis_workers/maintenance/retention.py` per the orphan audit's
  delete branch — both classes, not just `RetentionWorker`. Under Option A
  `StalenessDetector` is retired too (§3.3), so no option keeps it.
- **Strike the backlog items the orphan audit's driver question turns on.**
  The audit conditions its `KEEP` on "are the TTL + `DocumentRetentionWorker`
  P1 items in `TODO.md` still binding?" — both are still open and unchecked,
  in the Agent & Compaction Improvements list and again in the Phase 3
  integration tail. Option B's answer is **no**: they are struck, not
  deferred, because a `DocumentRetentionWorker` is exactly the thing being
  retired. Deleting the module while leaving open P1 items that demand it
  reproduces the half-built state one level up. (Option A's answer is
  *superseded* — see §3.3.)
- Either delete `PolicyType.RETENTION` or annotate it as reserved-for-future
  with a pointer to this ADR — a policy type nothing consumes is the same
  dead-schema smell either way. The delete branch is costlier than it looks:
  `retention` is a `RESERVED_NAMESPACES` entry, so `ContentTags` actively
  rejects `retention:` tags with the message "retention is expressed via
  Policy (`PolicyType.RETENTION`), not content tags". That reference is a
  string literal, not a symbol, and the test asserting it matches on the
  substring — deleting the enum member breaks no import and leaves the suite
  green while `ContentTags` points users at a type that no longer exists.
  Silently pointing at nothing is the very smell §2 rules off the menu, so
  the delete branch has to fix that message and its assertion too.
- Add a "Growth posture" note to the PRD/roadmap stating the accepted
  position: unbounded Knowledge-Plane growth is acceptable at target scale
  because retrieval quality is size-independent (noise exclusion, content
  floor, unconfirmed gating, token budgets) and the store substrates carry
  orders of magnitude more rows than dogfood produces.
- Keep the store-level primitives (`compact_versions`, `sweep_expired`) as
  the documented operator story for the two growth vectors that *are*
  mechanical: closed SCD-2 version rows and expired blobs.

### 4.2 The honest version of the argument

Trellis's differentiator is **measured attribution** — the learning loop joins
feedback to served items across time. Deletion is in tension with that: every
pruned item is a hole in a future join. At a scale where storage is free and
scans are fast, the maximally attribution-friendly store is the one that keeps
everything and lets retrieval-side gating do the value judgment. claude-mem's
failure is not "they kept everything"; it is that they kept everything *by
accident, with no gating and no decision*. An explicit Option B is not that.

### 4.3 Consequences

Positive:

- Zero build cost; removes 266 lines of orphan code and a dead verb — the
  codebase gets *smaller*, and stops lying about *this* capability (seven
  other verbs keep lying; §4.1).
- No deleter to mis-configure; the attribution history stays whole.
- The decision is recorded and reversible: re-introducing the verb later is
  additive (a new enum member, handler, and event), not a migration.

Negative:

- The claude-mem position, entered deliberately: if adoption ever outruns the
  posture, retention gets retrofitted into a live store under users — with
  data-shape decisions (what got tagged, what got confirmed) already made
  without disposal in mind.
- The demote loop stays physically open: noise accumulates as
  excluded-but-stored rows forever, and the unconfirmed-mint population (the
  one *observed* growth problem, #298) has no terminal state — its cleanup
  remains manual `redaction.apply` calls, which scale with operator patience.
- `Lifecycle` loses its most plausible first consumer and stays
  schema-without-behavior indefinitely.

---

## 5. Recommendation

**Option A, in its minimal governed form — but sequenced behind the work that
is currently hurting.**

The tiebreaker is that both options must pay the cleanup cost of ending the
half-built state, so the true marginal cost of Option A is the handler itself —
small, with `RedactionApplyHandler` as a proven template and the contract-test
purge semantics already pinned. Against that: Option B's core claim
("acceptable at target scale") is true today and silently expires the moment
target scale moves — and the roadmap's Productionization milestone is exactly
the boundary between "works on skynet" and "safe to recommend to a second
deployer", i.e. moving it is the plan.
Meanwhile the unconfirmed-mint population is already the observed growth
problem, and its only current disposal path is one-at-a-time redaction.

Minimal form means: criteria-driven candidate set exactly as §3.2, dry-run
default, single new event type, `PolicyType.RETENTION` deny rule as the kill
switch, and — if the two-phase shape is judged too large — phase one
(archival via `Lifecycle.state="archived"`) alone is an acceptable v1: it
closes the loop's terminal state and defers physical purge until the archive
population is real. Not recommended ahead of loop-starvation and curation
work; recommended ahead of any scale-up of ingestion volume (Claude Code
session auto-capture would multiply the derived-item mint rate and turns this
projected pressure into a felt one).

---

## 6. Decision — owner judgment gate

**This ADR does not decide.** The choice between A and B is a product-posture
call — how much the owner values a complete governance story against build
effort at current scale — not a technical deduction, and it is explicitly
reserved as an **owner judgment gate**: the ADR proposes (§5), the owner
decides. Issue #312 carries the roadmap's `blocked:owner-decision` label so
the gate is visible to the roadmap driver, not just asserted here.

The gate resolves by editing this section with the chosen option, the date,
and the rationale, then flipping **Status** to Accepted (A) or Accepted —
Retired (B).

**Decision: Option A, phase one (archival). Owner, 2026-08-25.**

### 6.1 Why the gate reopened

The gate was never actually resolved — it went *invisible*. Issue #312 was
closed as COMPLETED on 2026-08-22 with zero comments, and `TODO.md` marked it
`[x] LANDED via #314`. But #314 was this ADR: 340 lines of docs and a
one-line doc tweak, no code. The TODO entry's own wording gives it away — it
says #314 *"supports closing"* the chip. Supporting the closure was recorded
as being the closure. Because `blocked:owner-decision` is only read on *open*
issues, closing it also removed it from the roadmap driver's view, so nothing
would have surfaced the gate again.

### 6.2 What changed since §5 was written

§5 recommended Option A but sequenced it behind loop-starvation work,
"recommended ahead of any scale-up of ingestion volume (Claude Code session
auto-capture would multiply the derived-item mint rate and turns this
projected pressure into a felt one)."

That scale-up shipped on 2026-08-24, the day after this ADR was written.
Measured against the live corpus on 2026-08-25:

| Metric | Value |
|---|---|
| Documents created 2026-08-24 | **103** (prior baseline: 1 on 08-19, 1 on 08-15) |
| Corpus total | 1102 — the top of §1's own 10²–10³ target band |
| Noise-tagged among that day's 103 | **24 (23%)** |
| Share of the corpus's entire lifetime noise population minted that day | **24 of 45 — 53%** |

Those 24 are job-description captures that had to be demoted to
`signal_quality="noise"` rather than removed, and they remain stored and
embedded. §1's "growth pressure is real but not acute" was true when written
and expired within 24 hours.

### 6.3 Correction to §3.2's candidate set

**§3.2's first bullet names an empty set.** It lists "graph entities tagged
`signal_quality='noise'`", but `signal_quality` is a `ContentTags` facet and
the demote loop that writes it — `apply_noise_tags` — takes a `DocumentStore`
and writes through `document_store.put`. Zero graph nodes carry the facet in
production, and none ever have. Implemented literally, that criterion could
only ever return zero — the exact defect class this ADR exists to end.

Measured population of §3.2 as literally written, at decision time:

| §3.2 criterion | Prod population |
|---|---|
| Graph entities tagged `signal_quality="noise"` | **0** |
| `Lifecycle.state` ∈ {deprecated, superseded, archived} | **0** (nothing populates `Lifecycle`) |
| Unconfirmed mints | **3** |
| *(not in §3.2)* noise-tagged **documents** | **24** |

The implementation follows the ADR's *reasoning* over its wording, and takes
noise **documents** as the primary candidate class. §3.4 promises "the demote
loop closes physically: today `apply_noise_tags` demotes items into a
store-forever purgatory; pruning is the missing terminal state" — a promise
only documents can keep. And §3.2's exclusion is explicitly age-based
("confirmed entities and their documents, *regardless of age*"), which a
noise tag is not: it is a quality verdict recorded by the feedback loop, and
§3.3 rejects `StalenessDetector` precisely *because* it is a pure age test.

Consequently **grace periods gate the age-based criteria only**
(`unconfirmed_mints`, `lifecycle_states`) and not `noise_documents`. Under a
30-day default grace the 24 documents that motivated this decision would have
been unarchivable for a month.

### 6.4 What shipped

- `RetentionPruneHandler` wired into `create_curate_handlers`;
  `Operation.RETENTION_PRUNE`'s empty `set()` args schema replaced with
  `{"criteria", "reason"}`.
- Phase one is **archival**: candidates are stamped
  `Lifecycle.state="archived"` — its first writer — and
  `trellis.retrieve.lifecycle.exclude_archived` drops them at the
  `PackBuilder` collect seam, its first enforcement point. Physical purge is
  deferred until the archived population is real.
- **Dry-run by default.** `trellis curate prune` previews and writes nothing
  unless `--apply` is passed. Both modes emit `RETENTION_PRUNED`.
- Traces and EventLog rows are excluded *by construction* — the resolver
  reads the document and graph stores and nothing else, pinned by a test that
  greps the module source.
- The orphan `trellis_workers/maintenance/retention.py` (266 LOC) and its
  414-line test file are **deleted**, per §3.3.
- The two `TODO.md` P1 items ("TTL metadata + `DocumentRetentionWorker`") are
  **superseded**, per §3.3's backlog ruling, and rewritten to point here.
- **`retention.restore`** (`Operation.RETENTION_RESTORE`, `trellis curate
  restore`) — the governed inverse. Phase one is archival *because* "a wrong
  prune is walked back by re-stamping" (§3.1), and re-stamping needs a
  sanctioned path: direct store writes are forbidden, and no governed
  document-update verb exists, so without this the reversibility argument was
  rhetorical. It takes **explicit ids rather than criteria** — the ids ride
  the `RETENTION_PRUNED` payload, and re-deriving them from criteria would
  re-run the selection that was wrong in the first place. Emits
  `RETENTION_RESTORED`; a non-archived id is skipped, not raised.

**Why that verb was needed immediately.** The first production run archived
45 documents. Grouping them by *who* applied the noise tag showed two
distinct populations: 24 manually demoted job-description captures
(correctly noise), and **21 demoted by the nightly `curate` effectiveness
pass** — which include durable technical memories such as "Hermes: local
patches that must be re-applied after any hermes-agent update" and "Any
trellis test that enters the real FastAPI lifespan". Effectiveness analysis
demotes items that were served but never cited as helpful, and pack feedback
has carried **no item attribution**, so "never cited" is unfalsifiable rather
than informative. That is a defective input signal, not a quality verdict —
and archival being reversible is exactly what made it recoverable.

### 6.5 Two defects the first production run exposed

Both were invisible to the test suite as written and only appeared against
the real corpus.

**1. Archival did not reach the vector row.** A vector row's metadata is a
snapshot taken at *embed* time, and the semantic strategy builds its
`PackItem` from that snapshot rather than from the document store. An
archival written only through `document_store.put` therefore left the
semantic path serving the item unchanged — `exclude_archived` reads
`item.metadata` and never saw a lifecycle key. All 35 archived documents
kept vector rows still reading `signal_quality="standard"`. The pack-level
test could not catch it because it exercised `KeywordSearch` alone, which
reads the document store: **one strategy was tested and a collect-seam
guarantee was claimed.** Fixed by mirroring the stamp onto the vector row
(metadata-only re-`upsert`, nothing re-embedded), and a re-run now repairs
rows stamped before the sync existed.

*This had a pre-existing sibling, filed separately and since fixed in #338:*
the same staleness meant `signal_quality="noise"` written by
`apply_noise_tags` never reached the vector row either, so noise exclusion
had silently not held on the semantic path. That fix generalised the mirror
into `trellis.core.vector_metadata.sync_vector_metadata` and added
`trellis admin resync-vector-metadata` as the backfill for rows that
diverged before either writer existed. It also found that a *correctly
synced* vector row would still have been served: `SemanticSearch` strips
`content_tags` from the filters it forwards, so the store-side noise
predicate never reached that axis at all. The boundary is now enforced at
the collect seam (`trellis.retrieve.noise.exclude_noise`), beside
`exclude_archived` and for the same reason.

**2. Restore was not durable.** `retention.restore` un-archives but does not
re-classify — the noise tag that selected the item is the classify layer's
data and stays on the document. So the next criteria-driven prune re-archived
everything a human had just rescued. All 10 restored documents still carried
`signal_quality="noise"`. Fixed by treating an explicit
`Lifecycle.state="current"` as a human-decision tombstone: retention is
`Lifecycle`'s only writer, so that value can only have come from a restore.
Protected from the *criteria*, not from the operator — a restored item can
still be archived by naming it.

### 6.6 Deliberately deferred

- **Phase two (physical purge).** `DocumentStore.delete` exists, so the
  primitive is there; what is missing is a real archived population to purge
  and a grace period calibrated against it.
- **Store-level pushdown of the archived filter.** Post-filtering at the
  collect seam is backend-agnostic and correct for every strategy including
  injected ones, but an archived item still consumes its strategy's `limit`
  budget. That cost is negligible at 24 items and becomes real later; the
  count that decides it is `RETENTION_PRUNED.payload["archived"]`.
- **A document-age candidate class.** Still a separate decision with its own
  justification, exactly as §3.3 says — nothing here inherits it.
