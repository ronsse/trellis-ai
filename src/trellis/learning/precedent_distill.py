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

A trace whose records fail the schema's bounds, or whose arguments cannot be
compared (nested too deep, a cycle, keys that do not sort), is skipped as
``invalid_record`` (counted, no text) and the batch goes on: one bad trace never
stops a run.

Two things are cut silently by the schema's bounds, and each is counted rather
than left unsaid (``DistillTally.dropped``): an entity id longer than the id
bound is left out of a record, and failed approaches beyond the list cap are
not avoided (:data:`DROP_ENTITY_ID_OVERLONG`, :data:`DROP_FAILED_APPROACH_OVER_CAP`).

Arguments are compared as whole canonical JSON, with an integral float equal to
the integer (``1.0`` is ``1``): a retry that differs only in how a number was
written is still a retry.

An ``avoid`` whose shortened arguments read the same as the working call's would
be indistinguishable from the ``do`` it warns against, so its text names the
first top-level argument that differs.

``intent_family`` uses the frozen learning key
:func:`~trellis.learning.scoring.normalize_intent_family`, never the offline
relabel in :mod:`trellis.learning.intent_families`.

Every text field is cut with :func:`~trellis.retrieve.excerpts.truncate_excerpt`
to the schema's bound.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

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

#: Counters for what the schema's bounds cut without a skip (see the docstring).
DROP_ENTITY_ID_OVERLONG = "entity_id_overlong"
DROP_FAILED_APPROACH_OVER_CAP = "failed_approach_over_cap"

#: A deterministic rule never claims more than this: it saw one trace.
_CONFIDENCE_RECOVERY = 0.6
_CONFIDENCE_FAILURE = 0.5
_CONFIDENCE_PARTIAL = 0.4

_MAX_STEP_LABELS = 6
_MAX_ARGS_CHARS = 120
_MAX_KEY_CHARS = 40
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
    #: What the schema's bounds cut from this trace's records, by counter name.
    dropped: dict[str, int] = field(default_factory=dict)


@dataclass
class DistillTally:
    """What a batch of traces distilled to, in counts plus the records."""

    traces_read: int = 0
    records: list[PrecedentRecord] = field(default_factory=list)
    by_rule: Counter[str] = field(default_factory=Counter)
    skips_by_reason: Counter[str] = field(default_factory=Counter)
    dropped: Counter[str] = field(default_factory=Counter)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _label(step: TraceStep) -> str:
    return f"{step.step_type}/{step.name}"


def _canonical(value: Any) -> Any:
    """``value`` with an integral float written as the integer it equals.

    ``json.dumps`` writes ``1.0`` and ``1`` differently, so a retry whose number
    came back as a float would read as a different approach. ``True`` stays
    ``True``: a boolean is not the number 1. A non-finite float is left as is.
    """
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else value
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _args_key(step: TraceStep) -> str:
    """The step's whole arguments as a canonical string: for equality, not display."""
    if not step.args:
        return ""
    return json.dumps(
        _canonical(step.args), sort_keys=True, default=str, ensure_ascii=False
    )


def _first_difference(bad: TraceStep, good: TraceStep) -> str | None:
    """The first top-level argument (in key order) whose value differs, if any."""
    for key in sorted({*bad.args, *good.args}, key=str):
        if key not in bad.args or key not in good.args:
            return str(key)
        if json.dumps(
            _canonical(bad.args[key]), sort_keys=True, default=str
        ) != json.dumps(_canonical(good.args[key]), sort_keys=True, default=str):
            return str(key)
    return None


def _args_digest(step: TraceStep) -> str:
    return truncate_excerpt(_args_key(step), _MAX_ARGS_CHARS)


def _describe(step: TraceStep, *, against: TraceStep | None = None) -> str:
    """The step as ``type/name with <args>``.

    With ``against`` (the step that worked), arguments whose shortened text reads
    the same as that step's get the first differing top-level key appended, so a
    reader can tell the warning from the advice.
    """
    args = _args_digest(step)
    text = f"{_label(step)} with {args}" if args else _label(step)
    if against is not None and args == _args_digest(against):
        key = _first_difference(step, against)
        if key is not None:
            text += f" (differs at {truncate_excerpt(key, _MAX_KEY_CHARS)!r})"
    return text


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


def _entity_ids(trace: Trace) -> tuple[list[str], int]:
    """The trace's evidence ids that fit the schema, and how many did not."""
    ids = [ref.evidence_id for ref in trace.evidence_used]
    fit = [i for i in ids if len(i) <= MAX_ID_CHARS]
    return fit[:MAX_LIST_ITEMS], len(ids) - len(fit)


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
    entity_ids, _overlong = _entity_ids(trace)
    domain = trace.context.domain
    return PrecedentRecord.derive(
        rule=rule,
        situation=PrecedentSituation(
            intent_family=normalize_intent_family(intent=trace.intent),
            domain=(
                None if domain is None else truncate_excerpt(domain, MAX_DOMAIN_CHARS)
            ),
            entity_ids=entity_ids,
            trigger=truncate_excerpt(_clean(trigger), MAX_TRIGGER_CHARS),
        ),
        action=PrecedentAction(
            kind=kind, text=truncate_excerpt(_clean(action_text), MAX_ACTION_CHARS)
        ),
        because=truncate_excerpt(_clean(because), MAX_BECAUSE_CHARS),
        evidence=PrecedentEvidence(
            trace_ids=[trace.trace_id], step_indexes=step_indexes
        ),
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
    #: Distinct failed approaches beyond that cap, which are not avoided.
    dropped: int = 0


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
            ordered = sorted(latest.values())
            kept = ordered[-(MAX_LIST_ITEMS - 1) :]
            recoveries.append(
                _Recovery(
                    worked=index,
                    failed=tuple(kept),
                    dropped=len(ordered) - len(kept),
                )
            )
    return recoveries, len(pending)


def distill_trace(trace: Trace) -> DistillResult:
    """Distill one trace into zero or more candidate records, plus why not.

    A trace whose records fail the schema's bounds, or whose arguments cannot be
    compared, yields no records and one ``invalid_record`` skip, never an
    exception. The exceptions caught are the ones comparing arguments can raise
    (a cycle or a very deep nest, keys that do not sort), not a bare ``Exception``.
    """
    try:
        return _distill(trace)
    except (ValidationError, RecursionError, TypeError, ValueError):
        return DistillResult(skips=(DistillSkip(trace.trace_id, SKIP_INVALID_RECORD),))


def _distill(trace: Trace) -> DistillResult:
    records: list[PrecedentRecord] = []
    skips: list[DistillSkip] = []
    dropped: Counter[str] = Counter()

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
        dropped[DROP_FAILED_APPROACH_OVER_CAP] += recovery.dropped
        for failed in recovery.failed:
            bad = trace.steps[failed]
            trigger = _clean(bad.error)
            described = _describe(bad, against=good)
            keep(
                _record(
                    trace,
                    rule=RULE_FAILED_STEP_AVOID,
                    kind="avoid",
                    action_text=f"Avoid {described}",
                    trigger=trigger,
                    because=f"{_label(bad)} failed with: {trigger}",
                    step_indexes=[failed, recovery.worked],
                    confidence=_CONFIDENCE_RECOVERY,
                ),
                described,
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

    if records:
        dropped[DROP_ENTITY_ID_OVERLONG] += _entity_ids(trace)[1]
    return DistillResult(
        tuple(records), tuple(skips), {k: v for k, v in dropped.items() if v}
    )


def distill_traces(traces: Iterable[Trace]) -> DistillTally:
    """Distill a batch, keeping counts by rule and skips by reason."""
    tally = DistillTally()
    for trace in traces:
        tally.traces_read += 1
        result = distill_trace(trace)
        tally.records.extend(result.records)
        tally.by_rule.update(r.rule for r in result.records)
        tally.skips_by_reason.update(s.reason for s in result.skips)
        tally.dropped.update(result.dropped)
    return tally


__all__ = [
    "DISTILLER_VERSION",
    "DROP_ENTITY_ID_OVERLONG",
    "DROP_FAILED_APPROACH_OVER_CAP",
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
