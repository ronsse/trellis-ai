"""A pure, deterministic distiller: one trace in, candidate precedent records out.

No store writes, no LLM, no clock, no randomness: the same trace gives the
same records with the same ids. The records are *candidates*
(:mod:`trellis.schemas.precedent_record`); recording them is the governed
``precedent.candidate`` operation's job, not this module's.

Rules, applied to one :class:`~trellis.schemas.trace.Trace` in order:

``failed_step_avoid`` / ``failed_step_do``
    A step with a non-blank ``error``, followed later by a step of the same
    ``step_type`` and ``name`` with no error, is a recovery. Steps are paired
    in order: a later failure of the same kind replaces an earlier one that
    has not yet been answered, so a retry loop yields one pair, not one per
    attempt. The failed step gives an ``avoid`` record and the step that
    worked gives a ``do`` record. The trigger of both is the error, and the
    evidence step indexes are ``[failed, succeeded]``. A failed step never
    followed by a success is skipped as ``error_without_recovery``.

``failed_outcome``
    A ``failure`` or ``partial`` outcome with a summary gives an ``avoid``
    record whose ``because`` is that summary and whose action names the steps
    taken. An outcome without a summary is skipped as ``outcome_without_summary``.

A success trace with no errored step gives nothing (``smooth_run``): a smooth
run teaches nothing reusable at this tier. A trace with no outcome and no
errored step gives nothing (``no_signal``). A candidate whose action text would
only restate the trace's intent is dropped as ``restates_intent``; a failed
outcome with no steps is the usual case, since the only text left to say is the
intent itself.

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

from trellis.learning.scoring import normalize_intent_family
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.precedent_record import (
    MAX_ACTION_CHARS,
    MAX_BECAUSE_CHARS,
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


def _args_digest(step: TraceStep) -> str:
    if not step.args:
        return ""
    raw = json.dumps(step.args, sort_keys=True, default=str, ensure_ascii=False)
    return truncate_excerpt(raw, _MAX_ARGS_CHARS)


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
    evidence_ids = [ref.evidence_id for ref in trace.evidence_used][:MAX_LIST_ITEMS]
    return PrecedentRecord(
        candidate_id=derive_candidate_id(
            trace_ids=[trace.trace_id],
            rule=rule,
            step_indexes=step_indexes,
            distiller_version=DISTILLER_VERSION,
        ),
        rule=rule,
        situation=PrecedentSituation(
            intent_family=normalize_intent_family(intent=trace.intent),
            domain=trace.context.domain,
            entity_ids=evidence_ids,
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
        distiller_version=DISTILLER_VERSION,
    )


def _recoveries(steps: list[TraceStep]) -> tuple[list[tuple[int, int]], list[int]]:
    """Pair each errored step with the later success of the same kind.

    Returns ``(pairs, unrecovered)`` as step indexes. A later failure of a kind
    replaces an unanswered earlier one, so a retry loop pairs once.
    """
    pending: dict[tuple[str, str], int] = {}
    pairs: list[tuple[int, int]] = []
    for index, step in enumerate(steps):
        key = (step.step_type, step.name)
        if _clean(step.error):
            pending[key] = index
        elif key in pending:
            pairs.append((pending.pop(key), index))
    return pairs, sorted(pending.values())


def distill_trace(trace: Trace) -> DistillResult:
    """Distill one trace into zero or more candidate records, plus why not."""
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

    pairs, unrecovered = _recoveries(trace.steps)
    for failed, worked in pairs:
        bad, good = trace.steps[failed], trace.steps[worked]
        trigger = _clean(bad.error)
        indexes = [failed, worked]
        retried = _args_digest(bad) == _args_digest(good)
        keep(
            _record(
                trace,
                rule=RULE_FAILED_STEP_AVOID,
                kind="avoid",
                action_text=f"Avoid {_describe(bad)}",
                trigger=trigger,
                because=f"{_label(bad)} failed with: {trigger}",
                step_indexes=indexes,
                confidence=_CONFIDENCE_RECOVERY,
            ),
            _describe(bad),
        )
        keep(
            _record(
                trace,
                rule=RULE_FAILED_STEP_DO,
                kind="do",
                action_text=(
                    f"Retry {_label(good)}; it succeeded after failing once"
                    if retried
                    else f"Use {_describe(good)}"
                ),
                trigger=trigger,
                because=(
                    f"{_label(good)} succeeded after an earlier attempt "
                    f"failed with: {trigger}"
                ),
                step_indexes=indexes,
                confidence=_CONFIDENCE_RECOVERY,
            ),
            _describe(good),
        )
    for _ in unrecovered:
        skip(SKIP_ERROR_WITHOUT_RECOVERY)

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
    elif not records and not pairs and not unrecovered:
        skip(SKIP_SMOOTH_RUN if outcome is not None else SKIP_NO_SIGNAL)

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
    "SKIP_NO_SIGNAL",
    "SKIP_OUTCOME_WITHOUT_SUMMARY",
    "SKIP_RESTATES_INTENT",
    "SKIP_SMOOTH_RUN",
    "DistillResult",
    "DistillSkip",
    "DistillTally",
    "distill_trace",
    "distill_traces",
    "restates_intent",
]
