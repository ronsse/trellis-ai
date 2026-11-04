# ADR: The precedent record and event-only candidates

**Status:** Accepted (record, distiller and governed candidate write); activation deferred
**Date:** 2026-11-04
**Deciders:** Trellis core

## Context

A promoted precedent today is an excerpt of an item that was already served,
re-wrapped. It says nothing its source did not, so a pack pays its tokens
twice. The traces we capture hold something better: a step that failed with an
error, and the step that worked afterwards. That is the most precedent-shaped
content we have, and nothing reads it. The one trace reader that makes
precedents (`PrecedentMiner.extract_precedent_from_trace`) has no caller in
`src/`, and emits `PRECEDENT_CANDIDATE_MINED` outside the executor.

Serving is frozen until a blind experiment reads out, so whatever lands now
must change nothing a pack serves.

## Decision

**The record.** `PrecedentRecord` (`schemas/precedent_record.py`) is a lesson in
four parts: `situation` (`intent_family`, `domain`, `entity_ids`, `trigger`),
`action` (`do` or `avoid`, plus text), `because`, and `evidence` (`trace_ids`,
`step_indexes`, `feedback_ids`, `pack_ids`). It also carries `confidence`,
`provenance_tier`, `distiller_version` and `lifecycle: "candidate"`. Every text
field is length-bounded. `lifecycle` is the record's own vocabulary; it is not
added to `LifecycleState`, whose retrieval gates withhold only `demoted` and
`archived`.

**Tiers.** `deterministic` (rules over one trace, no LLM), `local_model` and
`human`. The deterministic tier records from any caller. `human` is reserved and
refused as a policy violation (`policy_violation`, policy id
`precedent_candidate_provenance_tier`).

**The local-model tier.** `local_model` records come from
`trellis worker distill-precedents-local` (`learning/precedent_local.py`): a
local model distils the traces the rules do not cover and merges near-duplicate
candidates. Four rules hold it.

* *Local only.* The two router consumers, `precedent_distill` and
  `precedent_merge`, refuse any route not declared `local: true`
  (`route_not_local`, a typed error, counted, no call made), because traces carry
  internal content. The check reads the resolved flag, so an unrouted consumer
  that inherits a cloud parent block is refused. The egress gate is defence in
  depth behind that check: it keeps content labelled above `internal` off a
  non-local route, so it cannot fire on a route that has already passed
  `require_local`.
* *Caller check, then proof.* `precedent.candidate` accepts a `local_model` record
  only when two things hold. The `requested_by` is in
  `LOCAL_MODEL_CANDIDATE_REQUESTERS`; and the event log holds the judged operation
  that produced the record (`learning/precedent_judged.py`). `requested_by` is a
  caller-set string on MCP `execute_mutation` and REST `/commands/batch` when no
  principal is configured, so the first conjunct alone is a claim, not a check.
  The judged event is something no MCP tool, REST route or governed operation can
  emit: all six `MEMORY_OP_JUDGED` emitters are in-process code with a fixed
  `source`. A distillation record needs `distillation` / `lesson` from the distil
  prompt's source about the one trace it cites; a merge record needs
  `reconciliation` / `same` from the merge prompt's source about the
  `precedent_pair` its two `merged_from` ids make. The lookup is keyed by event
  type, subject and source and capped at a handful of rows, never a scan. The
  command logs the event before it records, and a refusal carries a fixed message
  with no record text. This proves a verdict was logged for that trace or pair; the
  judged payload is a pinned contract with no field for the record's text, so a
  caller who also holds the requester string could still attach other text to a
  trace that has a real `lesson` verdict. The tier stays event-only and unserved.
* *Merge never rewrites.* A merge records a new candidate whose evidence is the
  union of both parents and whose `evidence.merged_from` names them. It deletes
  nothing and rewrites nothing; the parents stay recorded, and the merged
  record's own id differs from both. `merged_from` is hashed like any evidence
  list, but an empty list is left out so no existing id changes.
* *Every call is a judged op.* One `MEMORY_OP_JUDGED` event per model call, in
  dry-run and apply (`distillation` for a trace, `reconciliation` for a pair),
  with ids, digests and the verdict, never text. A `lesson` or `same` verdict is
  also what lets the governed operation record the resulting candidate. The prompt version
  (`precedent-distill/v1`, `precedent-merge/v1`) is the event's `source`, because
  the payload's field set is pinned. A reply that is not exactly the documented
  JSON shape is `unparsed`, logged, and never repaired.

Candidates from this tier are event-only and unserved like every other.

**The distiller.** `learning/precedent_distill.py` is pure. A failed step with a
later success of the same `step_type`/`name` is a recovery, and every failure of
that kind since the last success is weighed against the step that worked. A
failure with the same arguments as the success was transient: it gives no
`avoid` (the distiller never recommends avoiding the step that worked), and a
recovery whose failures were all transient is skipped as `transient_retry`
rather than recorded as a "retry it" lesson that says nothing reusable. Each
distinct failed approach gives one `avoid`, and the step that worked gives one
`do`. A `failure` or `partial` outcome with a summary gives an `avoid`; a smooth
run gives nothing. A candidate whose action only restates the intent is dropped,
with a skip reason, and a trace whose records fail the schema's bounds is
skipped as `invalid_record` (counted, no text) so one bad trace never stops a
run.

**The id.** The candidate id is a hash of a canonical encoding (JSON, sorted
keys) of the record's model dump minus the fields that are not content:
`candidate_id` itself and `lifecycle`, which has one value. The dump is the
hash's input, not a hand-written list, so a field added to the record or to a
nested model enters the hash without anyone remembering to add it; a test builds
one variant per field from `model_fields` and requires each to change the id. The
evidence lists are sorted before hashing, so the same lesson cited in another
order is the same candidate. A rerun derives the same ids; a forged record cannot
take the id the distiller derives for a real trace, because its own content
derives another one and the handler refuses a record whose `candidate_id` does
not match.

**Candidates are event-only.** The governed operation `precedent.candidate`
emits exactly one `PRECEDENT_CANDIDATE_MINED` event whose payload holds the
record, stamped with the executor's source. Nothing is written to the document,
graph or vector store, so no retrieval surface can serve a candidate. The
idempotency key is the candidate id: a rerun records nothing new and counts the
repeats as duplicates. Every reader of promotions asks the
event log for `PRECEDENT_PROMOTED` rows, so a candidate, which is a different
event type, never reaches `list_precedents`, `get_lessons`, the precedent routes
or promotion reads. `is_miner_candidate_event` is unchanged.

**The command.** `trellis worker distill-precedents` is a dry run by default and
reports counts and ids only (no trace text); `--apply` records. It is not wired
into any nightly script. Two quiet limits are counted rather than silent:
over-long entity ids left out of the situation (`entity_id_overlong`) and failed
approaches beyond the cap of `avoid` lines (`failed_approach_over_cap`).

## Deferred: activation

Minting a servable precedent document from a candidate is a separate change,
after the freeze readout. Its criterion is left open as an experiment: whether an
activated candidate's citations count as calibrated, beside or apart from the
promoted precedents measured today, is a question the readout has to answer
first. Promotion code (`_build_precedent_description`, `candidate_auto_promote`,
the promote handler's rendered text) is untouched here.

## Stage 2 activation criteria

The gate has two stages. **Stage 1, mint,** is automatic and dedup only: a
candidate is recorded when it is new. **Stage 2, activate,** asks whether the
citations a target collected are better than chance. The existing gate
(`candidate_auto_promote`) asks for a fixed count of helpful citations and no
unhelpful one, which at the observed citation rates takes dozens of servings with
no unhelpful citation to reach. Stage 2 replaces that count with a test.

A target is activated when all of these hold, checked in this order (the first
one missed is the one reported):

1. **h >= 3**: at least three packs cited it helpful.
2. **u < h**: fewer packs cited it unhelpful than helpful.
3. **>= 2 distinct sessions** among the helpful packs. A pack with no session id
   never counts as one.
4. **P < 0.05** under a within-pack permutation null.
5. **lift >= 1.2**: the target's helpful rate over its pack baseline.

The unit is a pack. A pack counts once whatever the number of graders; verdicts
are unioned per pack and helpful wins over unhelpful on one item. Only packs a
grader named items in count as servings.

**The null keeps the packs' structure.** A pack has a size n (items served) and a
citation count k (items cited helpful). Shuffling the helpful labels within the
pack makes the target helpful with probability k / n, so the null draws one
Bernoulli per pack the target was served in and sums them. Pack sizes and
citation counts never change, so a target is not credited for sitting in small,
heavily cited packs, where it is helpful by arithmetic. A null that used one
global helpful rate would be wrong exactly there; a test pins the difference on
packs that vary in size and citation count. P = (1 + #{null >= observed}) /
(1 + R) with R = 2000 resamples, a fixed seed and a per-target generator of its
own, so a target's P does not depend on the other targets or on global random
state. The pack baseline is the null's mean, sum(k / n) over the target's
servings, and lift is the observed helpful rate over it.

**This is an experiment, not a finding.** Whether a criterion on calibrated
citations is good enough to serve on is the open question. The thresholds are
arguments of a frozen, validated dataclass, not settings; nothing here reads them
from configuration. `trellis curate evaluate-precedent-gate` only counts, night by
night, how many candidates would mint and how many would activate beside what the
current gate passes; it serves, promotes and writes nothing, and implicit signals
feed it nothing.

## Consequences

- Candidates accumulate in the event log and are inspectable there. They cost
  event-log rows and nothing at serving time.
- Candidate text is derived from trace text, which may contain what a trace
  contained. Redaction coverage of candidate events is not extended here:
  candidate events copy trace text into a second immutable row, so any future
  trace redaction must also cover `PRECEDENT_CANDIDATE_MINED`. This must land
  before the command is scheduled.
- `extract_precedent_from_trace` still emits outside the executor: its tests
  assert that emit, so it is left alone and noted.
