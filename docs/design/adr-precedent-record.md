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

**Tiers.** `deterministic` (this change: rules over one trace, no LLM),
`local_model` and `human` are reserved so a later tier can fill `feedback_ids`
and `pack_ids`, which the deterministic tier leaves empty. Only the
deterministic tier exists, so `precedent.candidate` refuses a record naming
`local_model` or `human` as a policy violation (`policy_violation`, policy id
`precedent_candidate_provenance_tier`); a later tier gets its own caller check
when it lands.

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
keys) of the record's evidence and its whole content: rule, situation, action,
because, confidence, provenance tier and distiller version. The evidence lists
are sorted before hashing, so the same lesson cited in another order is the same
candidate. A rerun derives the same ids; a forged record cannot take the id the
distiller derives for a real trace, because its own text derives another one and
the handler refuses a record whose `candidate_id` does not match.

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
into any nightly script.

## Deferred: activation

Minting a servable precedent document from a candidate is a separate change,
after the freeze readout. Its criterion is left open as an experiment: whether an
activated candidate's citations count as calibrated, beside or apart from the
promoted precedents measured today, is a question the readout has to answer
first. Promotion code (`_build_precedent_description`, `candidate_auto_promote`,
the promote handler's rendered text) is untouched here.

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
