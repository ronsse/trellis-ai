# ADR: `retention.prune` disposition — governed handler or explicit retirement

**Status:** Proposed — final call is an **owner judgment gate**; see §6. This ADR proposes, the owner decides.
**Date:** 2026-08-21
**Deciders:** Trellis core (recommendation); owner (decision)
**Related:**
- [ronsse/trellis-ai#312](https://github.com/ronsse/trellis-ai/issues/312) — the handler-or-retire disposition this ADR answers; sourced from the claude-mem comparison audit (2026-08-19, TODO.md §"claude-mem comparison audit — steal-list")
- [`../../src/trellis/mutate/commands.py`](../../src/trellis/mutate/commands.py) — `Operation.RETENTION_PRUNE` enum verb, registered with an empty required-args schema and **no handler**
- [`../../src/trellis/mutate/handlers.py`](../../src/trellis/mutate/handlers.py) — `RedactionApplyHandler` (#302), the governance template Option A is modeled on
- [`../../src/trellis/schemas/classification.py`](../../src/trellis/schemas/classification.py) — `Lifecycle` staleness states (candidate-set input); `ContentTags.signal_quality`
- [`../../src/trellis_workers/maintenance/retention.py`](../../src/trellis_workers/maintenance/retention.py) — orphaned `RetentionWorker` / `StalenessDetector` (~220 LOC, `KEEP — VALIDATE` in the orphan audit)
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
| `RetentionWorker` / `StalenessDetector` | Orphaned (~220 LOC in `trellis_workers/maintenance/retention.py`, referenced nowhere in `src/`). The worker only *marks* old traces in the event log — it never deletes, because traces are immutable. `KEEP — VALIDATE BEFORE DELETING` in the orphan audit, conditional on the retention backlog items staying real. **This ADR is that validation:** the disposition here resolves the orphan either way (§3.4, §4). |

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
consumer, and an orphan worker — is not on the menu. That is claude-mem's
`relevance_count` column wearing our naming conventions.

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
- **Orphan resolution:** `StalenessDetector` becomes the candidate feeder it
  was designed to be; `RetentionWorker`'s trace-marking mode is retired (it
  predates the immutability hard rule doing that job by construction).

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
  property-invariants row — the API stops advertising a verb it rejects.
- Delete `trellis_workers/maintenance/retention.py` per the orphan audit's
  delete branch (`RetentionWorker` outright; `StalenessDetector` goes with it
  unless a live consumer materializes first).
- Either delete `PolicyType.RETENTION` or annotate it as reserved-for-future
  with a pointer to this ADR — a policy type nothing consumes is the same
  dead-schema smell either way.
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

- Zero build cost; removes ~220 LOC of orphan code and a dead verb — the
  codebase gets *smaller* and stops lying about its capabilities.
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
reserved as an **owner judgment gate** (`blocked:owner-decision` in the
roadmap's gate-state labels): the ADR proposes (§5), the owner decides.

The gate resolves by editing this section with the chosen option, the date,
and the rationale, then flipping **Status** to Accepted (A) or Accepted —
Retired (B). Until then:

- `Operation.RETENTION_PRUNE` stays as-is (rejected at dispatch) — neither
  wired nor deleted ahead of the call.
- The orphan module keeps its `KEEP — VALIDATE` status; this ADR *is* the
  validation, pending the gate.
- No new writer of `Lifecycle` ships against the assumption of either option.

**Decision:** _pending owner review._
