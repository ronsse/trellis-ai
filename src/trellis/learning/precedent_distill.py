"""A pure, deterministic distiller: one trace in, candidate precedent records out.

No store writes, no LLM, no clock, no randomness: the same trace gives the
same records with the same ids. The records are *candidates*
(:mod:`trellis.schemas.precedent_record`); recording them is the governed
``precedent.candidate`` operation's job, not this module's.

Rules, applied to one :class:`~trellis.schemas.trace.Trace` in order:

``failed_step_avoid`` / ``failed_step_do``
    A step with a non-blank ``error``, followed later by a step of the same
    ``step_type`` and ``name`` with no error, is a recovery. Every failed step of
    that kind since the last success is weighed against the step that worked:

    * A failure whose arguments equal the success's was transient (the same call
      worked on a retry), so it teaches nothing about *what* to avoid and gives
      no ``avoid``. It is the one thing this module never does: recommend
      avoiding the step that worked.
    * Each *distinct* failed approach (arguments that differ from the success's)
      gives one ``avoid`` record, from its latest failure.
    * The step that worked gives one ``do`` record, but only when at least one
      different approach failed before it. A recovery whose failures were all
      transient is skipped whole as ``transient_retry`` (counted): "retry it"
      says nothing a later agent can reuse.

    The trigger of each ``avoid`` is its own error; the ``do``'s is the latest
    failure's. Evidence step indexes are ``[failed, succeeded]`` for an
    ``avoid`` and the failed approaches' indexes then ``succeeded`` for the
    ``do``. A failed step never followed by a success is skipped as
    ``error_without_recovery``, once per step kind.

``failed_outcome``
    A ``failure`` or ``partial`` outcome with a summary gives an ``avoid``
    record whose ``because`` is that summary and whose action names the steps
    taken. An outcome without a summary is skipped as ``outcome_without_summary``.

A ``success`` trace with no errored step gives nothing (``smooth_run``): a smooth
run teaches nothing reusable at this tier. A trace with no errored step and no
outcome, or an ``unknown`` one, gives nothing (``no_signal``). A candidate whose
action text would only restate the trace's intent is dropped as
``restates_intent``; a failed outcome with no steps is the usual case, since the
only text left to say is the intent itself.

A trace whose records fail the schema's bounds is skipped as ``invalid_record``
(counted, no text) and the batch goes on: one bad trace never stops a run.

``intent_family`` uses the frozen learning key
:func:`~trellis.learning.scoring.normalize_intent_family`, never the offline
relabel in :mod:`trellis.learning.intent_families`.

Every text field is cut with :func:`~trellis.retrieve.excerpts.truncate_excerpt`
to the schema's bound.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import ValidationError

from trellis.learning.scoring import normalize_intent_family
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.precedent_record import (
    MAX_ACTION_CHARS,
    MAX_BECAUSE_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_ID_CHARS,
    MAX_LIST_ITEMS,
    MAX_TRIGGER_CHARS,
    ActionKind,
    PrecedentAction,
    PrecedentEvidence,
    PrecedentRecord,
    PrecedentSituation,
    derive_candidate_id,
)
from trellis.schemas.trace import Trace, TraceStep

DISTILLER_VERSION = "deterministic-1"

RULE_FAILED_STEP_AVOID = "failed_step_avoid"
RULE_FAILED_STEP_DO = "failed_step_do"
RULE_FAILED_OUTCOME = "failed_outcome"
RULES = (RULE_FAILED_STEP_AVOID, RULE_FAILED_STEP_DO, RULE_FAILED_OUTCOME)

SKIP_SMOOTH_RUN = "smooth_run"
SKIP_NO_SIGNAL = "no_signal"
SKIP_ERROR_WITHOUT_RECOVERY = "error_without_recovery"
SKIP_OUTCOME_WITHOUT_SUMMARY = "outcome_without_summary"
SKIP_RESTATES_INTENT = "restates_intent"
SKIP_TRANSIENT_RETRY = "transient_retry"
SKIP_INVALID_RECORD = "invalid_record"

#: A deterministic rule never claims more than this: it saw one trace.
_CONFIDENCE_RECOVERY = 0.6
_CONFIDENCE_FAILURE = 0.5
_CONFIDENCE_PARTIAL = 0.4

_MAX_STEP_LABELS = 6
_MAX_ARGS_CHARS = 120
_RESTATE_OVERLAP = 0.8
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class DistillSkip:
    """A trace (or part of one) that yielded no candidate, and why."""

    trace_id: str
    reason: str


@dataclass(frozen=True)
class DistillResult:
    """What one trace distilled to."""

    records: tuple[PrecedentRecord, ...] = ()
    skips: tuple[DistillSkip, ...] = ()


@dataclass
class DistillTally:
    """What a batch of traces distilled to, in counts plus the records."""

    traces_read: int = 0
    records: list[PrecedentRecord] = field(default_factory=list)
    by_rule: Counter[str] = field(default_factory=Counter)
    skips_by_reason: Counter[str] = field(default_factory=Counter)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _label(step: TraceStep) -> str:
    return f"{step.step_type}/{step.name}"


def _args_key(step: TraceStep) -> str:
    """The step's whole arguments as a canonical string: for equality, not display."""
    if not step.args:
        return ""
    return json.dumps(step.args, sort_keys=True, default=str, ensure_ascii=False)


def _args_digest(step: TraceStep) -> str:
    return truncate_excerpt(_args_key(step), _MAX_ARGS_CHARS)


def _describe(step: TraceStep) -> str:
    args = _args_digest(step)
    return f"{_label(step)} with {args}" if args else _label(step)


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def restates_intent(action_text: str, intent: str) -> bool:
    """Whether ``action_text`` carries nothing the ``intent`` did not already say.

    True when the two share at least 80% of the action's words, compared as sets
    so word order and case do not matter. An empty action restates nothing.
    """
    action = _tokens(action_text)
    if not action:
        return False
    overlap = len(action & _tokens(intent)) / len(action)
    return overlap >= _RESTATE_OVERLAP


def _record(
    trace: Trace,
    *,
    rule: str,
    kind: ActionKind,
    action_text: str,
    trigger: str,
    because: str,
    step_indexes: list[int],
    confidence: float,
) -> PrecedentRecord:
    # Every field copied from the trace is cut or filtered to the schema's bound
    # here, so a long domain or id cannot fail the schema and stop a run.
    entity_ids = [
        ref.evidence_id
        for ref in trace.evidence_used
        if len(ref.evidence_id) <= MAX_ID_CHARS
    ][:MAX_LIST_ITEMS]
    domain = trace.context.domain
    situation = PrecedentSituation(
        intent_family=normalize_intent_family(intent=trace.intent),
        domain=None if domain is None else truncate_excerpt(domain, MAX_DOMAIN_CHARS),
        entity_ids=entity_ids,
        trigger=truncate_excerpt(_clean(trigger), MAX_TRIGGER_CHARS),
    )
    action = PrecedentAction(
        kind=kind, text=truncate_excerpt(_clean(action_text), MAX_ACTION_CHARS)
    )
    because_text = truncate_excerpt(_clean(because), MAX_BECAUSE_CHARS)
    evidence = PrecedentEvidence(trace_ids=[trace.trace_id], step_indexes=step_indexes)
    return PrecedentRecord(
        candidate_id=derive_candidate_id(
            rule=rule,
            situation=situation,
            action=action,
            because=because_text,
            evidence=evidence,
            confidence=confidence,
            provenance_tier="deterministic",
            distiller_version=DISTILLER_VERSION,
        ),
        rule=rule,
        situation=situation,
        action=action,
        because=because_text,
        evidence=evidence,
        confidence=confidence,
        provenance_tier="deterministic",
        distiller_version=DISTILLER_VERSION,
    )


@dataclass(frozen=True)
class _Recovery:
    """One step that worked, and the failed attempts of its kind before it."""

    worked: int
    #: The latest failure of each *distinct* failed approach (arguments that
    #: differ from the success's), ascending, at most ``MAX_LIST_ITEMS - 1``.
    failed: tuple[int, ...]


def _recoveries(steps: list[TraceStep]) -> tuple[list[_Recovery], int]:
    """Pair each success with the failed attempts of its kind since the last one.

    Returns ``(recoveries, unrecovered)``: a failure whose arguments equal the
    success's was transient and is dropped from ``failed``, so a recovery with
    an empty ``failed`` was a pure retry. ``unrecovered`` counts the step kinds
    that failed and never worked, once each however many attempts failed.
    """
    pending: dict[tuple[str, str], list[int]] = {}
    recoveries: list[_Recovery] = []
    for index, step in enumerate(steps):
        key = (step.step_type, step.name)
        if _clean(step.error):
            pending.setdefault(key, []).append(index)
        elif key in pending:
            worked_args = _args_key(step)
            latest: dict[str, int] = {}
            for failed in pending.pop(key):
                args = _args_key(steps[failed])
                if args != worked_args:
                    latest[args] = failed
            kept = sorted(latest.values())[-(MAX_LIST_ITEMS - 1) :]
            recoveries.append(_Recovery(worked=index, failed=tuple(kept)))
    return recoveries, len(pending)


def distill_trace(trace: Trace) -> DistillResult:
    """Distill one trace into zero or more candidate records, plus why not.

    A trace whose records fail the schema's bounds yields no records and one
    ``invalid_record`` skip, never an exception.
    """
    try:
        return _distill(trace)
    except ValidationError:
        return DistillResult(skips=(DistillSkip(trace.trace_id, SKIP_INVALID_RECORD),))


def _distill(trace: Trace) -> DistillResult:
    records: list[PrecedentRecord] = []
    skips: list[DistillSkip] = []

    def skip(reason: str) -> None:
        skips.append(DistillSkip(trace.trace_id, reason))

    def keep(record: PrecedentRecord, core: str) -> None:
        # ``core`` is the action without its fixed verb ("Avoid", "Use").
        if restates_intent(core, trace.intent):
            skip(SKIP_RESTATES_INTENT)
        else:
            records.append(record)

    recoveries, unrecovered = _recoveries(trace.steps)
    for recovery in recoveries:
        if not recovery.failed:
            skip(SKIP_TRANSIENT_RETRY)
            continue
        good = trace.steps[recovery.worked]
        for failed in recovery.failed:
            bad = trace.steps[failed]
            trigger = _clean(bad.error)
            keep(
                _record(
                    trace,
                    rule=RULE_FAILED_STEP_AVOID,
                    kind="avoid",
                    action_text=f"Avoid {_describe(bad)}",
                    trigger=trigger,
                    because=f"{_label(bad)} failed with: {trigger}",
                    step_indexes=[failed, recovery.worked],
                    confidence=_CONFIDENCE_RECOVERY,
                ),
                _describe(bad),
            )
        trigger = _clean(trace.steps[recovery.failed[-1]].error)
        keep(
            _record(
                trace,
                rule=RULE_FAILED_STEP_DO,
                kind="do",
                action_text=f"Use {_describe(good)}",
                trigger=trigger,
                because=(
                    f"{_label(good)} succeeded after an earlier attempt "
                    f"failed with: {trigger}"
                ),
                step_indexes=[*recovery.failed, recovery.worked],
                confidence=_CONFIDENCE_RECOVERY,
            ),
            _describe(good),
        )
    skips.extend(
        DistillSkip(trace.trace_id, SKIP_ERROR_WITHOUT_RECOVERY)
        for _ in range(unrecovered)
    )

    outcome = trace.outcome
    if outcome is not None and outcome.status in {
        OutcomeStatus.FAILURE,
        OutcomeStatus.PARTIAL,
    }:
        summary = _clean(outcome.summary)
        if not summary:
            skip(SKIP_OUTCOME_WITHOUT_SUMMARY)
        else:
            labels: list[str] = []
            for step in trace.steps:
                if _label(step) not in labels:
                    labels.append(_label(step))
            approach = " -> ".join(labels[:_MAX_STEP_LABELS]) or _clean(trace.intent)
            keep(
                _record(
                    trace,
                    rule=RULE_FAILED_OUTCOME,
                    kind="avoid",
                    action_text=f"Avoid this approach: {approach}",
                    trigger=trace.intent,
                    because=summary,
                    step_indexes=[],
                    confidence=(
                        _CONFIDENCE_FAILURE
                        if outcome.status == OutcomeStatus.FAILURE
                        else _CONFIDENCE_PARTIAL
                    ),
                ),
                approach,
            )
    elif not recoveries and not unrecovered:
        # Only a stated success is a smooth run; an absent or unknown outcome
        # says nothing either way.
        smooth = outcome is not None and outcome.status == OutcomeStatus.SUCCESS
        skip(SKIP_SMOOTH_RUN if smooth else SKIP_NO_SIGNAL)

    return DistillResult(tuple(records), tuple(skips))


def distill_traces(traces: Iterable[Trace]) -> DistillTally:
    """Distill a batch, keeping counts by rule and skips by reason."""
    tally = DistillTally()
    for trace in traces:
        tally.traces_read += 1
        result = distill_trace(trace)
        tally.records.extend(result.records)
        tally.by_rule.update(r.rule for r in result.records)
        tally.skips_by_reason.update(s.reason for s in result.skips)
    return tally


__all__ = [
    "DISTILLER_VERSION",
    "RULES",
    "RULE_FAILED_OUTCOME",
    "RULE_FAILED_STEP_AVOID",
    "RULE_FAILED_STEP_DO",
    "SKIP_ERROR_WITHOUT_RECOVERY",
    "SKIP_INVALID_RECORD",
    "SKIP_NO_SIGNAL",
    "SKIP_OUTCOME_WITHOUT_SUMMARY",
    "SKIP_RESTATES_INTENT",
    "SKIP_SMOOTH_RUN",
    "SKIP_TRANSIENT_RETRY",
    "DistillResult",
    "DistillSkip",
    "DistillTally",
    "distill_trace",
    "distill_traces",
    "restates_intent",
]
