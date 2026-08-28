"""Write-boundary rejection telemetry and backend health aggregation.

The mutation pipeline already audits itself: the executor emits
``MUTATION_EXECUTED`` on success and ``MUTATION_REJECTED`` (with the
rejecting stage) on validate / policy / idempotency failures. What it
cannot see is the stage *before* it — an agent-facing tool rejecting a
payload that never became a Command at all. Those rejections were, until
this module, observable only by the caller that made them: the 2026-08-07
recall-gap study counted 13 across 12 sessions (invalid ``source`` enum
values, ``artifacts`` nested under ``outcome``, trailing-comma JSON),
each silently costing a retry and sometimes dropped content.

Three pieces close that hole:

* :func:`classify_rejection` — a deterministic taxonomy over validation
  failures. No model calls, no heuristics: pydantic error types map to a
  closed set of :data:`RejectionKind` slugs with dotted field paths.
* :func:`record_write_rejection` — emit a ``WRITE_REJECTED`` event,
  fail-soft: telemetry must never turn a rejected write into a crashed
  tool. The event's ``source`` uses the same ``mcp:<tool>`` string the
  executor stores in ``requested_by``, so accept/reject join per tool.
* :func:`summarize_backend_health` — the operator/grooming surface:
  acceptance vs rejection rates per tool and stage, repeated schema
  collisions, pack-attribution coverage, session-capture coverage
  (:mod:`trellis.ops.capture_coverage`), and a deterministic
  ``ok | warn`` status with named reasons. ``trellis analyze health``
  is a thin shell over it.

Hints are derived from the *live* Pydantic models (``Outcome.model_fields``
etc.), never hand-written field lists — the study's root cause was prose
describing a schema narrower than the real one, and a hint table that
could drift would recreate exactly that failure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from pydantic import Field, ValidationError

from trellis.core.base import TrellisModel
from trellis.feedback.attribution import payload_is_attributed, payload_pack_id
from trellis.ops.capture_coverage import (
    CaptureCoverageReport,
    summarize_capture_coverage,
)
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)

#: Closed taxonomy of boundary-rejection kinds. ``other`` is the explicit
#: catch-all so an unrecognized pydantic error type can never silently
#: mint a new category and fragment the aggregation.
RejectionKind = Literal[
    "extra_forbidden",
    "enum",
    "missing",
    "json_invalid",
    "type",
    "value",
    "empty_required",
    "dangling_reference",
    "other",
]

#: Pydantic error-type prefixes mapped to taxonomy slugs, checked in
#: order. Pydantic's ``type`` strings are stable public API
#: (``extra_forbidden``, ``enum``, ``missing``, ``json_invalid``,
#: ``string_type``, ``int_parsing``…); anything unmatched falls to
#: ``other`` rather than inventing a kind.
_PYDANTIC_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("extra_forbidden", "extra_forbidden"),
    ("enum", "enum"),
    ("missing", "missing"),
    ("json_invalid", "json_invalid"),
    ("json_type", "json_invalid"),
    ("value_error", "value"),
    ("greater_than", "value"),
    ("less_than", "value"),
)

_MAX_MSG = 240
_DEFAULT_EVENT_LIMIT = 5000

#: Attempts below this leave rates statistically meaningless — a single
#: rejection out of two attempts is not a 50%-bad system.
_MIN_ATTEMPTS_FOR_RATE = 5
#: Warn when more than this fraction of attempts is rejected.
_REJECTION_RATE_WARN = 0.10
#: Warn when one (kind, loc) pair recurs this often — the signature of a
#: schema/docs collision rather than a one-off agent mistake.
_REPEAT_COLLISION_WARN = 3
#: Warn when fewer than this fraction of packs carry ``injected_items[]``
#: — below it, the learning join is starved regardless of feedback volume.
_ATTRIBUTION_COVERAGE_WARN = 0.5

#: The assumption ``untargeted_feedback`` silently makes, stated so the
#: number is not read as stronger evidence than it is (#365).
#:
#: ``write.rejected`` (#297) means a *write* that fails at the tool boundary
#: is recorded. There is no equivalent for a *read*. A ``get_context`` that
#: fails in transport — a permission or host-channel failure, rather than a
#: store error — leaves no trace anywhere: no ``PACK_ASSEMBLED``, therefore
#: feedback with no ``pack_id``, therefore a row in ``untargeted_feedback``.
#: Which is byte-for-byte what an agent that simply never retrieved
#: produces.
#:
#: #344 reads that population as **retrieve-adoption**, and mostly it is.
#: But that reading assumes every non-retrieval was a *choice*, and on
#: 2026-08-27 the assumption was violated: every ``mcp__trellis__*`` call
#: from several agents failed with ``PreToolUse hook did not respond before
#: its timeout``, so the tool never executed. The damage was small and
#: time-boxed — that is not the point. The point is that nothing in this
#: report could have told anyone, and if a longer outage ever overlapped a
#: measurement window the conclusion drawn would be "agents are not
#: retrieving" and the remedy chosen would be prompting or ergonomics,
#: neither of which touches an unreachable transport.
#:
#: Stating it is the whole fix, deliberately. The two alternatives #365
#: lists are worse first steps: recording an attempt *on arrival* cannot see
#: a call that never arrives, which is this exact failure; and a client-side
#: reporter is a second unmeasured write path added to compensate for an
#: unmeasured read path, which fails the same way and hides it the same way.
RETRIEVAL_AVAILABILITY_ASSUMPTION = (
    "retrieval availability is UNMEASURED: a get_context that fails in "
    "transport leaves no trace and lands here, indistinguishable from an "
    "agent that chose not to retrieve. Read untargeted_feedback as an upper "
    "bound on non-retrieval, not as a count of it (#365)."
)


def _kind_for(pydantic_type: str) -> str:
    for prefix, kind in _PYDANTIC_KIND_PREFIXES:
        if pydantic_type.startswith(prefix):
            return kind
    return "type" if pydantic_type.endswith("_type") else "other"


def classify_rejection(error: Exception) -> list[dict[str, str]]:
    """Reduce a validation failure to taxonomy rows ``{kind, loc, msg}``.

    Deterministic and total: any exception produces at least one row, so
    a caller can always emit telemetry for a rejection it is about to
    surface.
    """
    if isinstance(error, ValidationError):
        rows = []
        for err in error.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()))
            rows.append(
                {
                    "kind": _kind_for(str(err.get("type", ""))),
                    "loc": loc,
                    "msg": str(err.get("msg", ""))[:_MAX_MSG],
                }
            )
        return rows
    if isinstance(error, json.JSONDecodeError):
        return [{"kind": "json_invalid", "loc": "", "msg": str(error)[:_MAX_MSG]}]
    return [{"kind": "other", "loc": "", "msg": str(error)[:_MAX_MSG]}]


def _fields(model: type[TrellisModel]) -> str:
    return ", ".join(sorted(model.model_fields))


def hints_for_trace_rejections(rows: list[dict[str, str]]) -> list[str]:
    """Deterministic field-relocation hints for ``Trace`` payload rejections.

    Every hint is computed from the live models at call time, so this can
    never describe a schema the code does not have. Returns at most one
    hint per distinct problem, deduplicated, order-stable.
    """
    hints: list[str] = []

    def add(hint: str) -> None:
        if hint not in hints:
            hints.append(hint)

    for row in rows:
        kind, loc = row["kind"], row["loc"]
        if kind == "extra_forbidden" and loc.startswith("outcome."):
            field = loc.split(".", 1)[1]
            if field.startswith("artifact"):
                add(
                    "artifacts belong at top level: "
                    '"artifacts_produced": [{"artifact_id": ..., '
                    '"artifact_type": ...}] — not inside outcome'
                )
            else:
                add(
                    f"Outcome accepts only [{_fields(Outcome)}]; put stray "
                    "values in outcome.metrics or top-level metadata"
                )
        elif kind == "extra_forbidden" and loc.startswith("context."):
            add(
                f"TraceContext accepts only [{_fields(TraceContext)}]; "
                "other keys go in top-level metadata"
            )
        elif kind == "extra_forbidden":
            add(f"Trace accepts only [{_fields(Trace)}]; unknown keys go in metadata")
        elif kind == "enum" and loc == "source":
            allowed = ", ".join(s.value for s in TraceSource)
            add(f"source must be one of: {allowed}")
        elif kind == "json_invalid":
            add("trace_json must be strict JSON — no trailing commas or comments")
    return hints


def record_write_rejection(
    event_log: EventLog | None,
    *,
    tool: str,
    error: Exception | None = None,
    rejections: list[dict[str, str]] | None = None,
    hints: list[str] | None = None,
    payload_chars: int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Emit a ``WRITE_REJECTED`` event for a boundary rejection, fail-soft.

    Returns ``{rejections, hints}`` whether or not the emit succeeded, so
    the caller can fold the classification into the error it is about to
    raise. Telemetry failure downgrades to a warning log — a broken event
    log must never escalate a rejected write into a crashed tool.
    """
    rows = (
        rejections
        if rejections is not None
        else classify_rejection(error or ValueError("rejected"))
    )
    details: dict[str, Any] = {"rejections": rows, "hints": hints or []}
    if event_log is None:
        return details
    payload: dict[str, Any] = {
        "tool": tool,
        "stage": "boundary",
        "error_class": type(error).__name__ if error is not None else None,
        "payload_chars": payload_chars,
        "rejections": rows,
        "hints": hints or [],
    }
    try:
        event_log.emit(
            EventType.WRITE_REJECTED,
            source or f"mcp:{tool}",
            payload=payload,
        )
    except Exception:
        logger.warning("write_health.emit_failed", tool=tool, exc_info=True)
    return details


class ToolWriteStats(TrellisModel):
    """Accept/reject counts for one write surface (``mcp:<tool>``)."""

    accepted: int = 0
    boundary_rejected: int = 0
    executor_rejected: int = 0

    @property
    def attempts(self) -> int:
        return self.accepted + self.boundary_rejected + self.executor_rejected


class WriteHealthReport(TrellisModel):
    """Aggregated write-path health over one time window."""

    window_days: int
    accepted: int
    boundary_rejected: int
    executor_rejected: int
    attempts: int
    rejection_rate: float
    by_tool: dict[str, ToolWriteStats] = Field(default_factory=dict)
    boundary_kinds: dict[str, int] = Field(default_factory=dict)
    executor_reasons: dict[str, int] = Field(default_factory=dict)
    repeated_collisions: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "ok"
    reasons: list[str] = Field(default_factory=list)


class ServeAttributionReport(TrellisModel):
    """The serve-side coverage numbers the learning join lives or dies on.

    Deliberately narrow — deep pack telemetry already exists in
    ``trellis analyze pack-telemetry``. These are the two coverage rates
    whose silent collapse starved the loop for a month: packs carrying
    ``injected_items[]`` (without which the promote join has no rows) and
    feedback carrying item attribution (without which the demote half has
    no signal).

    ``attribution_rate`` divides by *every* feedback event, and that
    denominator mixes two populations with nothing in common (backlog A4).
    A caller who names a ``pack_id`` and cites no items lost signal it
    held. A caller grading a trace it produced with no pack in hand held no
    signal to lose: nothing in the payload could ever join, because there
    is no pack on the other side of the join. Measured on the reference
    deployment over 30 days, 19 of 25 unattributed events were the second
    kind — feedback on work for which no pack had been assembled anywhere
    in the preceding six hours.

    One number over both answers neither question. So the pack-targeted
    population is reported separately: ``pack_attribution_rate`` is the
    citation rate among callers who *could* cite, and is the number an
    ergonomic change at the feedback surface can actually move.
    ``untargeted_feedback`` counts the rest — not a failure, and not
    something to suppress, but not evidence about attribution either.

    ``attribution_rate`` itself is unchanged and still divides by
    ``feedback_events``: DoD-3 thresholds and the nightly roadmap driver
    read it, and a metric that improves because its denominator was
    quietly narrowed is the failure this decomposition exists to expose.
    """

    packs: int = 0
    packs_with_injected_items: int = 0
    injected_coverage: float = 0.0
    feedback_events: int = 0
    feedback_attributed: int = 0
    attribution_rate: float = 0.0
    #: Feedback events naming a ``pack_id`` — the population where
    #: attribution is possible at all, and the exact predicate
    #: ``join_pack_feedback`` uses to decide whether an event can join.
    pack_targeted_feedback: int = 0
    #: Of those, the events that cited at least one element.
    pack_targeted_attributed: int = 0
    #: ``pack_targeted_attributed / pack_targeted_feedback``. Zero when no
    #: pack-targeted feedback exists — which reads as "no evidence", not
    #: as "nobody cited".
    pack_attribution_rate: float = 0.0
    #: Feedback naming no pack. Structurally unjoinable, legitimately so.
    untargeted_feedback: int = 0
    #: Whether retrieval *availability* was measured for this window. Always
    #: ``False`` — and that is the point (#365). See
    #: :data:`RETRIEVAL_AVAILABILITY_ASSUMPTION`.
    retrieval_availability_measured: bool = False
    #: The assumption ``untargeted_feedback`` rests on, stated in full, and
    #: attached **only when there is a number to over-read** — empty when
    #: ``untargeted_feedback`` is zero. A disclosure nobody can miss is worth
    #: more than a caveat in a docstring, but one that prints unconditionally
    #: is noise that gets skipped.
    retrieval_availability_note: str = ""


class BackendHealthReport(TrellisModel):
    """Composed backend health: write boundary + serve + capture coverage."""

    window_days: int
    write: WriteHealthReport
    serve: ServeAttributionReport
    #: What fraction of eligible sessions produced a memory, and — when it
    #: cannot be known — which of "not deployed", "stopped" and "running but
    #: capturing nothing" the log actually supports. See
    #: :mod:`trellis.ops.capture_coverage`. Required, not defaulted: an
    #: absent capture section and one reporting ``state="unobserved"`` say
    #: different things, and only the second is a measurement.
    capture: CaptureCoverageReport
    status: str = "ok"
    reasons: list[str] = Field(default_factory=list)


def summarize_write_health(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> WriteHealthReport:
    """Aggregate write acceptance vs rejection over the window.

    Acceptance comes from ``MUTATION_EXECUTED`` (keyed by
    ``payload.requested_by``), executor-stage rejection from
    ``MUTATION_REJECTED`` (same key plus ``payload.reason``), boundary
    rejection from ``WRITE_REJECTED`` (keyed by ``payload.tool``). Empty
    window yields a zero report with ``status="ok"`` rather than raising.

    Per-tool ``accepted`` counts *executed commands*, not tool calls — one
    ``save_experience`` call fans out to N governed commands (trace ingest
    plus extraction entities/edges), all carrying the same
    ``requested_by``. Read it as write volume on behalf of the tool; the
    rejection counts, by contrast, are one per rejected call.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)
    by_tool: dict[str, ToolWriteStats] = {}

    def stats(key: str) -> ToolWriteStats:
        return by_tool.setdefault(key or "(unknown)", ToolWriteStats())

    accepted_events = event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED, since=since, limit=limit
    )
    for event in accepted_events:
        stats(str(event.payload.get("requested_by") or "")).accepted += 1

    executor_reasons: dict[str, int] = {}
    for event in event_log.get_events(
        event_type=EventType.MUTATION_REJECTED, since=since, limit=limit
    ):
        stats(str(event.payload.get("requested_by") or "")).executor_rejected += 1
        reason = str(event.payload.get("reason") or "failed")
        executor_reasons[reason] = executor_reasons.get(reason, 0) + 1

    boundary_kinds: dict[str, int] = {}
    collision_counts: dict[tuple[str, str], int] = {}
    boundary_events = event_log.get_events(
        event_type=EventType.WRITE_REJECTED, since=since, limit=limit
    )
    for event in boundary_events:
        tool = str(event.payload.get("tool") or "")
        stats(
            f"mcp:{tool}" if tool and ":" not in tool else tool
        ).boundary_rejected += 1
        for row in event.payload.get("rejections") or []:
            kind = str(row.get("kind", "other"))
            loc = str(row.get("loc", ""))
            label = f"{kind}@{loc}" if loc else kind
            boundary_kinds[label] = boundary_kinds.get(label, 0) + 1
            collision_counts[(kind, loc)] = collision_counts.get((kind, loc), 0) + 1

    accepted = sum(s.accepted for s in by_tool.values())
    boundary_rejected = sum(s.boundary_rejected for s in by_tool.values())
    executor_rejected = sum(s.executor_rejected for s in by_tool.values())
    attempts = accepted + boundary_rejected + executor_rejected
    rate = (boundary_rejected + executor_rejected) / attempts if attempts else 0.0

    repeated = [
        {"kind": kind, "loc": loc, "count": count}
        for (kind, loc), count in sorted(
            collision_counts.items(), key=lambda item: -item[1]
        )
        if count >= _REPEAT_COLLISION_WARN
    ]

    reasons: list[str] = []
    if attempts >= _MIN_ATTEMPTS_FOR_RATE and rate > _REJECTION_RATE_WARN:
        reasons.append(
            f"rejection rate {rate:.0%} over {attempts} attempts "
            f"(warn above {_REJECTION_RATE_WARN:.0%})"
        )
    for item in repeated:
        loc = str(item["loc"]) or "(payload)"
        reasons.append(
            f"repeated schema collision: {item['kind']} at {loc} "
            f"x{item['count']} — same mistake recurring means the schema "
            "and its docs/skill disagree"
        )
    if boundary_rejected > 0 and accepted == 0:
        reasons.append(
            f"{boundary_rejected} boundary rejection(s) and zero accepted "
            "writes in window — every write attempt is failing"
        )

    return WriteHealthReport(
        window_days=days,
        accepted=accepted,
        boundary_rejected=boundary_rejected,
        executor_rejected=executor_rejected,
        attempts=attempts,
        rejection_rate=round(rate, 4),
        by_tool=by_tool,
        boundary_kinds=boundary_kinds,
        executor_reasons=executor_reasons,
        repeated_collisions=repeated,
        status="warn" if reasons else "ok",
        reasons=reasons,
    )


def summarize_serve_attribution(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> ServeAttributionReport:
    """Coverage of the two joins the learning loop depends on."""
    since = datetime.now(tz=UTC) - timedelta(days=days)

    packs = packs_with_items = 0
    for event in event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, since=since, limit=limit
    ):
        packs += 1
        if event.payload.get("injected_items"):
            packs_with_items += 1

    feedback = attributed = 0
    pack_targeted = pack_targeted_attributed = 0
    for event in event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED, since=since, limit=limit
    ):
        feedback += 1
        # Both predicates come from ``trellis.feedback.attribution`` so the
        # health surface and the MCP boundary cannot drift on what
        # "attributed" or "pack-targeted" means in one deployment.
        is_attributed = payload_is_attributed(event.payload)
        if is_attributed:
            attributed += 1
        if payload_pack_id(event.payload):
            pack_targeted += 1
            if is_attributed:
                pack_targeted_attributed += 1

    return ServeAttributionReport(
        packs=packs,
        packs_with_injected_items=packs_with_items,
        injected_coverage=round(packs_with_items / packs, 4) if packs else 0.0,
        feedback_events=feedback,
        feedback_attributed=attributed,
        attribution_rate=round(attributed / feedback, 4) if feedback else 0.0,
        pack_targeted_feedback=pack_targeted,
        pack_targeted_attributed=pack_targeted_attributed,
        pack_attribution_rate=(
            round(pack_targeted_attributed / pack_targeted, 4) if pack_targeted else 0.0
        ),
        untargeted_feedback=feedback - pack_targeted,
        retrieval_availability_note=(
            RETRIEVAL_AVAILABILITY_ASSUMPTION if feedback - pack_targeted else ""
        ),
    )


def summarize_backend_health(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> BackendHealthReport:
    """One deterministic health verdict for the grooming loop to watch."""
    write = summarize_write_health(event_log, days=days, limit=limit)
    serve = summarize_serve_attribution(event_log, days=days, limit=limit)
    capture = summarize_capture_coverage(event_log, days=days, limit=limit)

    reasons = list(write.reasons)
    if serve.packs > 0 and serve.injected_coverage < _ATTRIBUTION_COVERAGE_WARN:
        reasons.append(
            f"only {serve.packs_with_injected_items}/{serve.packs} packs "
            "carry injected_items[] — the learning join is starved below "
            f"{_ATTRIBUTION_COVERAGE_WARN:.0%} coverage"
        )
    if serve.feedback_events > 0 and serve.feedback_attributed == 0:
        # Same trigger condition as before — ``status`` is read by the
        # nightly roadmap driver and must not shift under a wording
        # change. What is new is naming *which* population is responsible,
        # because the two call for opposite fixes: uncited pack feedback is
        # an ergonomics problem at the feedback surface, whereas feedback
        # that names no pack means retrieval is not happening at all, and
        # no change to the feedback surface can reach it.
        if serve.pack_targeted_feedback == 0:
            # Deliberately hedged. The pre-#365 wording said "retrieval is
            # not happening before the graded work" — an assertion this
            # report cannot support, because a retrieval that failed in
            # transport produces an identical row.
            detail = (
                f"all {serve.feedback_events} name no pack, so none could "
                "join — either retrieval is not happening before the graded "
                "work, or it is failing unobserved (see #365)"
            )
        else:
            detail = (
                f"{serve.pack_targeted_feedback} named a pack and cited nothing from it"
            )
        reasons.append(
            f"{serve.feedback_events} feedback event(s), none carrying item "
            f"attribution ({detail}) — demote/promote loops receive zero signal"
        )

    # Capture states warn on different evidence, and only "degraded" is a
    # defect in *this* deployment. "unobserved" and "stale" are reported as
    # facts without escalating status: on a fresh install, or on any store
    # that simply has no capture worker pointed at it, a permanent warn is
    # noise — and a health surface that always warns is one nobody reads.
    if capture.state == "degraded":
        reasons.append(
            f"capture sweeps ran but adjudicated no sessions: {capture.degraded_reason}"
        )
    elif capture.state == "stale":
        reasons.append(
            f"no capture sweep in {days} day(s) "
            f"(last {capture.last_sweep_at:%Y-%m-%d %H:%M} UTC) — "
            "session capture has stopped, so coverage is unmeasured, not zero"
        )

    return BackendHealthReport(
        window_days=days,
        write=write,
        serve=serve,
        capture=capture,
        status="warn" if reasons else "ok",
        reasons=reasons,
    )
