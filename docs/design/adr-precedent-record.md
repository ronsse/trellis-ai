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
and `pack_ids`, which the deterministic tier leaves empty.

**The distiller.** `learning/precedent_distill.py` is pure. A failed step with a
later success of the same `step_type`/`name` gives an `avoid` and a `do`; a
`failure` or `partial` outcome with a summary gives an `avoid`; a smooth run
gives nothing. A candidate whose action only restates the intent is dropped,
with a skip reason. The candidate id derives from (trace id, rule, step
indexes, distiller version), so a rerun derives the same ids.

**Candidates are event-only.** The governed operation `precedent.candidate`
emits exactly one `PRECEDENT_CANDIDATE_MINED` event whose payload holds the
record, stamped with the executor's source. Nothing is written to the document,
graph or vector store, so no retrieval surface can serve a candidate. The
idempotency key is the candidate id: a rerun records nothing new and counts the
repeats as duplicates. `is_miner_candidate_event` also matches on the event type,
so a reader that skips by that predicate reads an executor-stamped candidate as
not-promoted; no existing row's answer changes.

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
  contained. Redaction coverage of candidate events is not extended here.
- `extract_precedent_from_trace` still emits outside the executor: its tests
  assert that emit, so it is left alone and noted.
