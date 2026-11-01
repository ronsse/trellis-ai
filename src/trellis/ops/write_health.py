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
  acceptance vs rejection rates per tool and stage, repeated rejections
  rated against each tool's attempts, how many rejected writes were
  followed by an accepted one, pack-attribution coverage, session-capture
  coverage
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
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import structlog
from pydantic import BaseModel, Field, ValidationError

from trellis.core.base import TrellisModel
from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.pack_holdout import drop_holdout, is_holdout, unscanned_pack_ids
from trellis.feedback.attribution import (
    StrayCitationTally,
    payload_is_attributed,
    payload_pack_id,
    served_item_ids,
)
from trellis.ops.capture_coverage import (
    CaptureCoverageReport,
    summarize_capture_coverage,
)
from trellis.ops.memory_extraction_health import (
    MemoryExtractionHealthReport,
    summarize_memory_extraction,
)
from trellis.ops.precedent_sync_health import (
    PrecedentSyncReport,
    precedent_sync_reasons,
    summarize_precedent_sync,
)
from trellis.ops.serving_builds import (
    ServingBuildsReport,
    mixed_builds_reason,
    summarize_serving_builds,
)
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceStep
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    Event,
    EventLog,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)

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
    # Not a payload problem and not the caller's to fix: the deployment's
    # own config would not load, so the write could never be attempted
    # (#425, ``trellis.mutate.policy_source``). Named rather than pooled
    # under ``other`` because the operator's response is different — fix a
    # file, not a payload — and because recurrence of *this* kind in
    # ``repeated_collisions`` reads correctly: the same unfixed file, over
    # and over.
    "config_unreadable",
    # The file read fine; another process wrote it between this writer's
    # load and its save, so the whole-file rewrite was refused rather than
    # replacing rows it never saw (#438,
    # ``DegradableJsonStore.refuse_if_stale``). Not folded into
    # ``config_unreadable``: that one needs a human to look at a broken
    # file, this one needs nobody at all unless it *recurs* — at which
    # point ``repeated_collisions`` reads exactly right, the same two
    # writers colliding on the same file night after night (#448).
    "stale_write",
    "other",
]

#: Pydantic error-type prefixes mapped to taxonomy slugs, checked in
#: order. Pydantic's ``type`` strings are stable public API
#: (``extra_forbidden``, ``enum``, ``missing``, ``json_invalid``,
#: ``string_type``, ``int_parsing``…); anything unmatched falls to
#: ``other`` rather than inventing a kind.
_PYDANTIC_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("extra_forbidden", "extra_forbidden"),
    # A tool call naming an argument the tool does not declare: the same
    # failure as a forbidden key, one level up (``unexpected_keyword_argument``).
    ("unexpected_keyword", "extra_forbidden"),
    ("enum", "enum"),
    ("missing", "missing"),
    ("json_invalid", "json_invalid"),
    ("json_type", "json_invalid"),
    ("value_error", "value"),
    ("greater_than", "value"),
    ("less_than", "value"),
)

_MAX_MSG = 240
#: Per-event-type read cap. Every read goes through
#: :func:`~trellis.stores.base.event_log.scan_events`, so a window with
#: more matches keeps the **newest** ones and the report says it capped
#: (#374). The direction matters most here: this is the surface that is
#: supposed to notice a write outage that started this morning, and under
#: the old ascending default those rejections were the first rows dropped.
_DEFAULT_EVENT_LIMIT = DEFAULT_SCAN_LIMIT

#: Attempts below this leave rates statistically meaningless — a single
#: rejection out of two attempts is not a 50%-bad system.
_MIN_ATTEMPTS_FOR_RATE = 5
#: Warn when more than this fraction of attempts is rejected.
_REJECTION_RATE_WARN = 0.10
#: A ``(kind, loc)`` rejection is *repeated* only when it was hit by at least
#: this many **distinct rejected attempts** (one ``WRITE_REJECTED`` event is
#: one attempt, however many error elements it lists). Counting elements let
#: one trace with many bad steps cross the line alone.
_REPEAT_COLLISION_WARN = 3
#: A repeated payload rejection also has to be a real share of its tool's
#: attempts: ``count / (accepted + rejected)`` for that tool must exceed this.
#: An absolute count has no denominator, so a busy healthy tool crosses it
#: by volume alone and the signal sits at ``warn`` permanently.
_COLLISION_RATE_WARN = 0.05
#: Below this many attempts on the tool the collision is not rated at all:
#: three rejections out of seven calls says nothing about the tool.
_MIN_ATTEMPTS_FOR_COLLISION_RATE = 10
#: These three numbers are starting points chosen by reading the code, not
#: values fitted to a deployment's traffic. 5% sits under the overall
#: ``_REJECTION_RATE_WARN`` because one ``(kind, loc)`` is a slice of all
#: rejections; 10 attempts is twice ``_MIN_ATTEMPTS_FOR_RATE`` because a
#: collision also needs ``_REPEAT_COLLISION_WARN`` rejections to exist. Tune
#: them from the ``rate`` and ``tool_attempts`` every ``repeated_collisions``
#: row now carries.

#: A rejection is counted as *recovered* when a call to the same tool was
#: accepted within this many seconds after it (see
#: :class:`RejectionRecoveryTracker`). It is meant to span one retry: the
#: caller reads the error, corrects the payload, and calls again. Longer
#: windows let an unrelated caller's accepted call stand in for the retry.
RECOVERY_WINDOW_SECONDS = 120

#: Said beside the recovered share, because the number cannot be read
#: without it. See :class:`RejectionRecoveryTracker` for why there is no
#: per-caller pairing to offer instead.
RECOVERY_APPROXIMATION_NOTE = (
    "approximate: a rejection counts as recovered when any call to the same "
    "tool was accepted within {window}s after it, by whichever caller (the "
    "server is stateless over HTTP, so there is no per-caller token, and none "
    "is stored). Concurrent callers make this an upper bound, and an accepted "
    "call is not shown to carry the rejected content."
)

#: Operation whose accepted count is the number of accepted *calls*, for a
#: tool whose one call fans out to several commands (see
#: :class:`ToolWriteStats`). Rating a collision against every fanned-out
#: command would divide by a number that grows with the payload and dilute
#: the rate. A tool absent here is rated against its ``accepted`` total,
#: which can only understate the rate.
_CALL_LEVEL_OPERATION = {"mcp:save_experience": "trace.ingest"}

#: What a recurrence *means*, per :data:`RejectionKind` — a ``(label,
#: explanation)`` pair rendered as ``repeated {label}: … — {explanation}``.
#:
#: ``repeated_collisions`` is a **routing** signal: it tells an operator
#: which kind of repair to reach for. One sentence for every kind sent
#: them to the wrong one (#485).
#:
#: The default below is for the payload kinds, and it states no cause. It
#: used to say that a recurring mistake means the schema and its docs/skill
#: disagree; nothing in a rejection row can establish that. The same count
#: is produced by a schema narrower than its docs, docs that misdescribe the
#: schema, a hint that does not say what to send, and a caller whose own
#: template is wrong. The row says how often and where, and the explanation
#: says that, so the operator looks before choosing a repair.
#:
#: The two file conditions implicate no schema, no doc and no agent, so each
#: gets a sentence of its own: ``config_unreadable`` needs a human to look
#: at a damaged file, ``stale_write`` needs two writers separated. They are
#: also not rated: their recurrence is the whole signal (a single stale
#: refusal needs nobody at all), and their surface has no accepted side to
#: divide by.
#:
#: Kinds absent here take the default; that is the safe direction, since
#: every kind but these two is a payload problem. The recovery advice the
#: stores compute (#427, a ``shlex``-quoted ``mv``) rides the rejection
#: ``msg`` and is dropped when collisions are keyed by ``(tool, kind,
#: loc)`` — getting it to the boundaries is #459, not this.
_DEFAULT_COLLISION_REASON: tuple[str, str] = (
    "rejection",
    (
        "cause not established: the schema, its docs or skill, or the "
        "rejection hint may be at fault, or callers may be sending a bad "
        "payload; the row says how often and where, not which"
    ),
)
_COLLISION_REASONS: dict[str, tuple[str, str]] = {
    "config_unreadable": (
        "unreadable config",
        (
            "one damaged file on disk, still unrepaired — nothing a caller "
            "sent is at fault and no payload change can clear it; repair or "
            "move the named file aside"
        ),
    ),
    "stale_write": (
        "writer collision",
        (
            "two writers keep racing for the same file — each refusal was "
            "safe and needed nobody, but recurring means their schedules "
            "overlap; stagger the two writers on that file, not a payload"
        ),
    ),
}

#: Marker every ``ScanCoverage.note`` opens with. Matched rather than
#: re-derived so the composed report can recognise — and supersede — the
#: write section's own truncation line.
_TRUNCATION_PREFIX = "TRUNCATED:"

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


#: What an unresolvable ``loc`` emits. It names **no model**, on purpose:
#: the failure this replaces was a confident hint pointing at the wrong
#: one. Saying the machinery ran and had nothing is a different, and
#: honest, signal from an empty ``hints`` list.
_UNRESOLVED_HINT = (
    "'{loc}' does not resolve to a field of the live trace schema — "
    "no field-level hint available; check the shape of the enclosing object"
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


def _fields(model: type[BaseModel]) -> str:
    return ", ".join(sorted(model.model_fields))


def _required(model: type[BaseModel]) -> str:
    return ", ".join(
        sorted(name for name, f in model.model_fields.items() if f.is_required())
    )


def _dict_fields(model: type[BaseModel]) -> list[str]:
    """Names of the model's free-form ``dict`` fields, sorted."""
    names = []
    for name, field in model.model_fields.items():
        annotation = _unwrap_optional(field.annotation)
        if annotation is dict or get_origin(annotation) is dict:
            names.append(name)
    return sorted(names)


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``None`` from ``X | None``, leaving ``X``; otherwise identity."""
    if get_origin(annotation) in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _list_element(annotation: Any) -> type[BaseModel] | None:
    """The model a ``list[Model]`` annotation holds, or ``None``."""
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    element = _unwrap_optional(args[0]) if args else None
    return element if _is_model(element) else None


def normalize_loc(loc: str) -> str:
    """Collapse list indices in a dotted ``loc``.

    ``steps.0.result`` becomes ``steps[].result``. Two rejections that
    differ only by which list element failed are the same schema
    collision, so hints and the repeat-collision counter treat them as
    one.
    """
    parts: list[str] = []
    for segment in loc.split("."):
        if segment.isdigit() and parts:
            parts[-1] = f"{parts[-1]}[]"
        else:
            parts.append(segment)
    return ".".join(parts)


@dataclass(frozen=True)
class _LocTarget:
    """Where a rejection's ``loc`` lands in the live ``Trace`` model tree.

    ``model`` is the model that actually rejected — the whole point of
    resolving rather than prefix-matching. ``field`` is the segment it
    names, or ``None`` when the ``loc`` stops on a list element (a
    ``type`` error on ``evidence_used.0`` names no field). ``annotation``
    is that field's live annotation, or ``None`` when the field is not
    declared at all, which is exactly the ``extra_forbidden`` case.
    """

    model: type[BaseModel]
    field: str | None
    annotation: Any | None
    path: str

    @property
    def container(self) -> str:
        """The dotted path of ``model`` itself — ``steps[]`` for ``steps[].result``."""
        if self.field is None:
            return self.path
        return self.path.rsplit(".", 1)[0] if "." in self.path else ""


def resolve_trace_loc(loc: str) -> _LocTarget | None:
    """Walk a rejection ``loc`` down the live ``Trace`` model tree.

    Returns ``None`` when the walk cannot proceed — an index into a
    non-list, a segment under a free-form ``dict``, an unknown segment
    that is not the last one. Callers must treat ``None`` as *no model
    may be named*: a hint that names the wrong model is worse than no
    hint (#472), which is precisely how the previous prefix-matching
    version misdirected 97 ``steps.*`` rejections onto ``Trace``.
    """
    if not loc:
        return None
    segments = loc.split(".")
    # ``current`` is the annotation the walk is standing on; ``owner`` and
    # ``field`` remember the last model/field pair it passed through, which
    # is what a hint needs to name. The optional unwrap happens on entry to
    # each segment, so ``current`` still holds the *declared* annotation
    # (``Outcome | None``, not ``Outcome``) when the loop exits.
    current: Any = Trace
    owner: type[BaseModel] = Trace
    field: str | None = None
    for index, segment in enumerate(segments):
        current = _unwrap_optional(current)
        if segment.isdigit():
            element = _list_element(current)
            if element is None:
                return None
            owner, field, current = element, None, element
            continue
        if not _is_model(current):
            return None
        model = current
        declared = model.model_fields.get(segment)
        if declared is None:
            if index != len(segments) - 1:
                return None
            return _LocTarget(
                model=model,
                field=segment,
                annotation=None,
                path=normalize_loc(loc),
            )
        owner, field, current = model, segment, declared.annotation
    return _LocTarget(
        model=owner, field=field, annotation=current, path=normalize_loc(loc)
    )


def _describe(annotation: Any) -> str:
    """Name the shape an annotation accepts, in caller-facing terms."""
    resolved = _unwrap_optional(annotation)
    if _is_model(resolved):
        return (
            f"an object of type {resolved.__name__} with fields [{_fields(resolved)}]"
        )
    origin = get_origin(resolved)
    if origin is list:
        element = _list_element(resolved)
        if element is not None:
            return (
                f"an array of {element.__name__} objects "
                f"with fields [{_fields(element)}]"
            )
        args = get_args(resolved)
        inner = _unwrap_optional(args[0]) if args else Any
        return f"an array of {getattr(inner, '__name__', inner)}"
    if resolved is dict or origin is dict:
        return "a JSON object, not a string or other scalar"
    return f"a value of type {getattr(resolved, '__name__', resolved)}"


def _freeform_clause(target: _LocTarget) -> str:
    """Where stray keys belong, derived from the live model's dict fields."""
    container = target.container
    own = _dict_fields(target.model)
    if own:
        prefix = f"{container}." if container else ""
        return "; put free-form values in " + " or ".join(prefix + n for n in own)
    root = _dict_fields(Trace)
    if root:
        return "; put free-form values in top-level " + " or ".join(root)
    return ""


#: Keys agents write on a step for free-form narration, none of which
#: ``TraceStep`` declares. Hand-kept rather than derived — like the two
#: hints below — because each is a specific, recurring confusion about
#: *where* narration goes, which the generic "put free-form values in
#: steps[].args or steps[].result" hint states too vaguely to fix on the
#: first retry.
_NARRATION_KEYS = frozenset(
    {
        "detail",
        "output",
        "observation",
        "action",
        "description",
        "summary",
        "outcome",
        "content",
        "text",
    }
)

_NARRATION_HINT = (
    "{loc} is not accepted on a step: what happened goes in steps[].result "
    'as {{"summary": "..."}}, a failure goes in steps[].error, and inputs '
    "go in steps[].args"
)


def _single_instance_models() -> list[tuple[str, type[BaseModel]]]:
    """``(path, model)`` for ``Trace`` and each model nested under it by a plain field.

    ``("", Trace)``, ``("context", TraceContext)``, ``("outcome", Outcome)``
    today, read off the live annotations. List elements (``steps[]``,
    ``artifacts_produced[]``…) are left out on purpose: they are records
    *about* the run, and a loose run-level key that shares a name with one
    (``started_at`` is on both ``context`` and every step) does not mean
    the step's field.
    """
    found: list[tuple[str, type[BaseModel]]] = [("", Trace)]
    seen: set[type[BaseModel]] = {Trace}
    for path, model in found:
        for name, field in model.model_fields.items():
            nested = _unwrap_optional(field.annotation)
            if _is_model(nested) and nested not in seen:
                seen.add(nested)
                found.append((f"{path}.{name}" if path else name, nested))
    return found


def _declared_elsewhere(target: _LocTarget) -> list[str]:
    """Where the rejected name *is* declared, when a rejection is a misplacement.

    Walks the models :func:`_single_instance_models` finds, so it covers
    every one reachable from ``Trace`` without a roster: a top-level
    ``domain`` resolves to ``context.domain``. Empty when the rejection
    sits inside a list element or when no other single-instance model
    declares the name — callers then fall back to the generic clause.
    """
    if target.field is None or "[]" in target.path:
        return []
    homes = []
    for path, model in _single_instance_models():
        if model is target.model or target.field not in model.model_fields:
            continue
        homes.append(f"{path}.{target.field}" if path else f"top-level {target.field}")
    return homes


def _extra_forbidden_hint(target: _LocTarget) -> str:
    if target.model is Outcome and (target.field or "").startswith("artifact"):
        # Hand-written on purpose: the generated form would list Outcome's
        # fields, which is true and useless — the caller needs to be told
        # where artifacts actually go, not where they do not.
        return (
            "artifacts belong at top level: "
            '"artifacts_produced": [{"artifact_id": ..., '
            '"artifact_type": ...}] — not inside outcome'
        )
    if target.model is Trace and target.field == "artifacts":
        # Hand-written: the one misplacement that is a *rename*, so no
        # model declares the name and the walk below cannot find it.
        return (
            "artifacts is not accepted on Trace: the top-level field is "
            'artifacts_produced: [{"artifact_id": ..., "artifact_type": ...}]'
        )
    if target.model is TraceStep and target.field == "index":
        return (
            f"{target.path} is not accepted: steps are ordered by position, so drop it"
        )
    if target.model is TraceStep and target.field in _NARRATION_KEYS:
        return _NARRATION_HINT.format(loc=target.path)
    homes = _declared_elsewhere(target)
    if homes:
        # Checked before the free-form clause on purpose: that clause is
        # what sent a top-level ``domain`` into ``metadata``, where it was
        # accepted and left ``context.domain`` empty.
        where = (
            f"belongs at {homes[0]}"
            if len(homes) == 1
            else f"is declared at {', '.join(homes)}; use the one that fits"
        )
        return (
            f"{target.path} is not accepted on {target.model.__name__}: "
            f"{target.field} {where}"
        )
    return (
        f"{target.path} is not accepted: {target.model.__name__} accepts only "
        f"[{_fields(target.model)}]{_freeform_clause(target)}"
    )


def _type_hint(target: _LocTarget) -> str:
    if target.annotation is None:
        return _UNRESOLVED_HINT.format(loc=target.path)
    if target.field is None:
        # The loc stops on a list element; ``_describe`` already names the
        # element model, so do not name the owner twice.
        return f"{target.path} entries must be {_describe(target.annotation)}"
    hint = (
        f"{target.path} must be {_describe(target.annotation)} "
        f"({target.model.__name__}.{target.field})"
    )
    if target.model is TraceStep and target.field == "result":
        # Hand-written on purpose: "a JSON object, not a string or other
        # scalar" names the shape but not a fix — the worked example is
        # what turns it into one on the first retry.
        hint += ', e.g. {"summary": "<text>"}'
    return hint


def _enum_hint(target: _LocTarget) -> str:
    resolved = _unwrap_optional(target.annotation)
    if isinstance(resolved, type) and issubclass(resolved, Enum):
        allowed = ", ".join(str(member.value) for member in resolved)
        return f"{target.path} must be one of: {allowed}"
    return _UNRESOLVED_HINT.format(loc=target.path)


def _missing_hint(target: _LocTarget) -> str:
    return (
        f"{target.path} is required: {target.model.__name__} "
        f"requires [{_required(target.model)}]"
    )


#: Rejection kinds whose fix is a *field* change, so a resolved ``loc``
#: can describe it. Everything not listed (``value``,
#: ``dangling_reference``, ``config_unreadable``, ``other``…) is not a
#: schema-shape problem and gets no invented guidance.
_HINT_BUILDERS: dict[str, Callable[[_LocTarget], str]] = {
    "extra_forbidden": _extra_forbidden_hint,
    "missing": _missing_hint,
    "type": _type_hint,
    "enum": _enum_hint,
}


def _hint_for_rejection(kind: str, loc: str) -> str | None:
    """One hint for one taxonomy row, or ``None`` when nothing is derivable."""
    if kind == "json_invalid":
        return "trace_json must be strict JSON — no trailing commas or comments"
    if kind == "enum" and loc == "source":
        # Hand-written on purpose: ``source`` is the field the recall-gap
        # study found agents guessing at, and naming it plainly beats the
        # generated form wrapped in path syntax.
        return f"source must be one of: {', '.join(s.value for s in TraceSource)}"
    builder = _HINT_BUILDERS.get(kind)
    if builder is None:
        return None
    target = resolve_trace_loc(loc)
    return _UNRESOLVED_HINT.format(loc=loc) if target is None else builder(target)


def hints_for_trace_rejections(rows: list[dict[str, str]]) -> list[str]:
    """Deterministic field-relocation hints for ``Trace`` payload rejections.

    Every hint is computed from the live models at call time, so this can
    never describe a schema the code does not have. Returns at most one
    hint per distinct problem, deduplicated, order-stable.

    The model a hint names is **resolved** by walking the rejection's
    ``loc`` down the model tree (:func:`resolve_trace_loc`), not matched
    against a roster of ``loc`` prefixes. The roster version handled two
    prefixes (``outcome.``, ``context.``) and two kinds, so on the
    production corpus it was silent on 184 of 318 rejections and named
    ``Trace`` — a model with no ``result`` field — for the 97 that
    rejected inside ``TraceStep``. A walk covers every model reachable
    from ``Trace``, including ones added later, for free.

    Three hand-written hints are kept deliberately, because each encodes a
    specific recurring confusion a generated hint would state worse: the
    ``artifacts``-under-``outcome`` relocation, the ``source`` enum
    listing, and the narration-family relocation for a step's free-form
    keys. Everything else is derived.
    """
    hints: list[str] = []
    for row in rows:
        hint = _hint_for_rejection(row["kind"], row["loc"])
        if hint and hint not in hints:
            hints.append(hint)
    return hints


#: An argument or location segment is echoed back only when it looks like
#: one: a caller-chosen key is caller content, and a long or odd one is
#: not something a reply should repeat.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")

#: ``difflib`` ratio at which an accepted argument name is offered as a
#: correction, and how many are offered.
_SUGGESTION_CUTOFF = 0.6
_MAX_SUGGESTIONS = 3

#: JSON-schema scalar types in caller-facing words.
_SCHEMA_TYPE_WORDS = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
}


def _safe_name(part: object) -> str:
    text = str(part)
    return text if _SAFE_NAME.fullmatch(text) else "<name not shown>"


def _schema_phrase(prop: Mapping[str, Any]) -> str:
    """The shape a tool parameter's JSON schema accepts, in caller-facing words."""
    options = [
        option
        for option in prop.get("anyOf", [prop])
        if isinstance(option, Mapping) and option.get("type") != "null"
    ]
    option = options[0] if len(options) == 1 else {}
    word = _SCHEMA_TYPE_WORDS.get(str(option.get("type")))
    if word is None:
        return "a different type"
    items = option.get("items")
    if isinstance(items, Mapping) and items.get("type") in _SCHEMA_TYPE_WORDS:
        return f"{word} of {items['type']}"
    return word


@dataclass(frozen=True)
class ArgumentRejection:
    """A tool call's argument failure, rewritten from the tool's own schema.

    ``message`` is what the caller is told, ``hints`` the guidance clauses
    inside it (what the ``WRITE_REJECTED`` event carries), ``rows`` the
    taxonomy rows. None of the three quotes a value the caller sent.
    """

    message: str
    hints: list[str]
    rows: list[dict[str, str]]


def _closest_names(name: str, accepted: list[str]) -> list[str]:
    """Accepted argument names to offer for an unknown ``name``, best first.

    A name that contains the unknown one, or is contained in it, is the
    likeliest slip (``item_ids`` for ``helpful_item_ids``) and outranks any
    spelling-distance match, which would otherwise surface an unrelated
    parameter that merely shares letters. Ties keep declared order, so the
    result does not depend on which candidate ``difflib`` happens to see first.
    """
    contained = [c for c in accepted if name in c or c in name]
    if contained:
        return contained[:_MAX_SUGGESTIONS]
    scored = sorted(
        (-SequenceMatcher(None, name, candidate).ratio(), index, candidate)
        for index, candidate in enumerate(accepted)
    )
    return [c for score, _, c in scored if -score >= _SUGGESTION_CUTOFF][
        :_MAX_SUGGESTIONS
    ]


def describe_argument_rejection(
    tool: str, parameters: Mapping[str, Any], error: ValidationError
) -> ArgumentRejection:
    """Describe a failed tool-call validation by the tool's declared parameters.

    FastMCP validates a call's arguments before the tool body runs, and
    renders the failure as pydantic's own text: ``item_ids / Unexpected
    keyword argument [... input_value=['a'] ...]``. That names no accepted
    parameter and quotes what the caller sent. This reads the same failure
    off ``errors(include_input=False)`` and the tool's JSON schema
    (``parameters``) instead, so the accepted names are never a copy that
    can drift from the signature. ``item_ids`` on ``record_feedback`` comes
    back as ``unknown argument item_ids; record_feedback accepts [...]; did
    you mean helpful_item_ids…?``.

    A name the caller chose is echoed only if it is identifier-shaped
    (:data:`_SAFE_NAME`). A value is never echoed.
    """
    properties = parameters.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    accepted = [str(name) for name in properties]
    problems: list[str] = []
    rows: list[dict[str, str]] = []
    unknown: list[str] = []
    missing: list[str] = []
    for err in error.errors(include_input=False):
        loc = tuple(err.get("loc", ()))
        kind = str(err.get("type", ""))
        label = ".".join(_safe_name(part) for part in loc) or "<arguments>"
        msg = sanitize_error_message(str(err.get("msg", "")))[:_MAX_MSG]
        rows.append({"kind": _kind_for(kind), "loc": label, "msg": msg})
        top = str(loc[0]) if loc else ""
        if kind == "unexpected_keyword_argument":
            unknown.append(top if _SAFE_NAME.fullmatch(top) else "")
            problems.append(
                f"unknown argument {unknown[-1]}"
                if unknown[-1]
                else "unknown argument (name not shown)"
            )
        elif kind == "missing_argument" and top in properties:
            missing.append(top)
            problems.append(f"missing required argument {top}")
        elif (
            len(loc) == 1 and top in properties and kind.endswith(("_type", "_parsing"))
        ):
            problems.append(f"{top} must be {_schema_phrase(properties[top])}")
            if top == "trace_json" and kind == "string_type":
                # Hand-written: an object where the schema wants a string is
                # the one wrong-type call with a known cause and a known fix.
                problems[-1] = (
                    "trace_json must be a JSON string: serialize the trace "
                    "object (e.g. json.dumps) rather than passing it as an object"
                )
        else:
            problems.append(f"{label}: {msg}")

    hints: list[str] = []
    if unknown:
        hints.append(f"{tool} accepts [{', '.join(accepted)}]")
    if missing:
        required = [n for n in parameters.get("required", []) if n in properties]
        hints.append(f"required: [{', '.join(str(n) for n in required)}]")
    named = [name for name in unknown if name]
    for name in named:
        close = _closest_names(name, accepted)
        if not close:
            continue
        options = (
            close[0] if len(close) == 1 else f"{', '.join(close[:-1])} or {close[-1]}"
        )
        suffix = f" for {name}" if len(named) > 1 else ""
        hints.append(f"did you mean {options}{suffix}?")
    return ArgumentRejection(
        message="; ".join([*problems, *hints]), hints=hints, rows=rows
    )


def surface_label(tool: str) -> str:
    """The ``mcp:<tool>`` label a rejection is counted under.

    A bare tool name gets the ``mcp:`` prefix the executor's ``requested_by``
    already carries; a colon-prefixed label (``config:policy_file``,
    ``cli:…``) is already a surface and passes through. The one place both
    the write side and the read side derive it, so they cannot drift.
    """
    return f"mcp:{tool}" if tool and ":" not in tool else tool


class RejectionRecoveryTracker:
    """Remember, per tool and in memory only, that a write was just rejected.

    The pairing that says a rejected write was retried and accepted. It holds
    a tool name, a count and a monotonic timestamp, and nothing else: no
    payload, intent, session or caller identifier, and none of it is
    persisted. Keying by tool is an **approximation**. The MCP server runs
    stateless over HTTP, where every request is a fresh session, so no
    per-connection token exists to pair a rejection with its own retry, and
    a caller's identity is exactly what must not be stored. So any accepted
    call to the same tool within ``window_seconds`` of the last rejection
    counts, whoever made it. Under concurrent callers that overstates
    recovery, which makes the reported share an upper bound.

    Bounded by the tool labels the code itself passes in, not by caller
    input. Thread-safe, since tool bodies may run on worker threads.
    """

    def __init__(
        self,
        *,
        window_seconds: int = RECOVERY_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # label -> (rejections since the last claim, time of the latest one)
        self._pending: dict[str, tuple[int, float]] = {}

    def note_rejection(self, label: str) -> None:
        """Record one rejected attempt on ``label``."""
        with self._lock:
            count, _ = self._pending.get(label, (0, 0.0))
            self._pending[label] = (count + 1, self._clock())

    def claim_recovery(self, label: str) -> int:
        """Rejected attempts on ``label`` that this acceptance recovers.

        Clears the entry either way: an acceptance consumes the rejections
        it follows, and a stale entry is dropped rather than kept. Returns
        ``0`` when nothing was pending or the latest rejection is older than
        the window.
        """
        with self._lock:
            entry = self._pending.pop(label, None)
            if entry is None:
                return 0
            count, at = entry
            return count if self._clock() - at <= self.window_seconds else 0


def record_write_rejection(
    event_log: EventLog | None,
    *,
    tool: str,
    error: Exception | None = None,
    rejections: list[dict[str, str]] | None = None,
    hints: list[str] | None = None,
    payload_chars: int | None = None,
    source: str | None = None,
    recovery: RejectionRecoveryTracker | None = None,
) -> dict[str, Any]:
    """Emit a ``WRITE_REJECTED`` event for a boundary rejection, fail-soft.

    Returns ``{rejections, hints}`` whether or not the emit succeeded, so
    the caller can fold the classification into the error it is about to
    raise. Telemetry failure downgrades to a warning log — a broken event
    log must never escalate a rejected write into a crashed tool.

    ``recovery`` is the tracker a later accepted call is paired against. When
    given, the event carries ``recovery_window_s`` (the window the pairing
    uses) so a reader can tell a rejection that was *tracked* from one
    written by a build that did not track, and the rejection is noted only
    once its event is on the log, so recovered can never outnumber tracked.
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
    if recovery is not None:
        payload["recovery_window_s"] = recovery.window_seconds
    try:
        event_log.emit(
            EventType.WRITE_REJECTED,
            source or f"mcp:{tool}",
            payload=payload,
        )
    except Exception:
        logger.warning("write_health.emit_failed", tool=tool, exc_info=True)
        return details
    if recovery is not None:
        recovery.note_rejection(surface_label(tool))
    return details


def record_write_recovery(
    event_log: EventLog | None,
    *,
    tool: str,
    rejected_attempts: int,
    window_seconds: int,
) -> None:
    """Emit ``WRITE_RECOVERED``: a call to ``tool`` was accepted right after rejections.

    ``rejected_attempts`` is how many tracked rejections this acceptance
    follows. Carries the tool and two integers, nothing about the call.
    Fail-soft for the same reason as :func:`record_write_rejection`.
    """
    if event_log is None:
        return
    try:
        event_log.emit(
            EventType.WRITE_RECOVERED,
            f"mcp:{tool}",
            payload={
                "tool": tool,
                "rejected_attempts": rejected_attempts,
                "window_s": window_seconds,
            },
        )
    except Exception:
        logger.warning("write_health.recovery_emit_failed", tool=tool, exc_info=True)


#: Write surfaces whose writes never reach ``MutationExecutor``, so
#: ``ToolWriteStats.accepted`` is structurally ``0`` for them rather than a
#: reflection of zero activity. Hand-kept, like ``_NARRATION_KEYS`` above:
#: a surface is added here only once its bypass is confirmed, not guessed
#: from an observed zero. ``mcp:record_feedback`` is the only member today
#: (see :class:`ServeAttributionReport` for where its signal is reported
#: instead).
_EXECUTOR_BYPASS_SURFACES = frozenset({"mcp:record_feedback"})


class ToolWriteStats(TrellisModel):
    """Accept/reject counts for one write surface (``mcp:<tool>``).

    ``accepted`` counts executed *commands*, not calls: a tool whose one
    call fans out to several operations (``save_experience`` ingests a
    trace and creates every extracted entity and edge as its own command)
    inflates ``accepted`` by exactly that fan-out. ``accepted_by_operation``
    breaks the same total down by ``Operation`` name, so a reader can
    answer "how many traces" instead of only "how many writes" — empty
    for a tool whose writes never reached the executor (e.g.
    ``mcp:record_feedback``, which bypasses it; see
    :class:`ServeAttributionReport` for that signal).
    """

    accepted: int = 0
    accepted_by_operation: dict[str, int] = Field(default_factory=dict)
    boundary_rejected: int = 0
    executor_rejected: int = 0

    @property
    def attempts(self) -> int:
        return self.accepted + self.boundary_rejected + self.executor_rejected


class ToolRecovery(TrellisModel):
    """Tracked boundary rejections for one surface and how many were recovered."""

    tracked: int = 0
    recovered: int = 0
    #: ``recovered / tracked``; ``None`` when nothing was tracked.
    share: float | None = None


class RecoveryReport(TrellisModel):
    """How many rejected writes were followed by an accepted one.

    Counted over **tracked** rejections only: ``WRITE_REJECTED`` rows that
    carry ``recovery_window_s``, written by a build that pairs rejections
    with later acceptances. A row from an older build has no such field and
    is left out of both sides, so ``recovered_share`` is ``None`` (not
    measured) rather than ``0.0`` (measured, none recovered) for a window
    holding only old rows. ``recovered`` is capped at ``tracked`` per tool,
    so a rejection just outside the window cannot push a share past 100%.
    The pairing is by tool within a short window, not by caller; read
    :attr:`note` for what that does to the number.
    """

    tracked_rejections: int = 0
    recovered_rejections: int = 0
    recovered_share: float | None = None
    window_seconds: int | None = None
    by_tool: dict[str, ToolRecovery] = Field(default_factory=dict)
    #: The approximation, stated beside the number it qualifies. Empty when
    #: nothing was tracked, so a caveat does not print over no figure.
    note: str = ""


class WriteHealthReport(TrellisModel):
    """Aggregated write-path health over one time window."""

    window_days: int
    accepted: int
    boundary_rejected: int
    executor_rejected: int
    attempts: int
    rejection_rate: float
    by_tool: dict[str, ToolWriteStats] = Field(default_factory=dict)
    #: Counts error **elements**, not attempts: one rejected call listing
    #: eight bad steps adds eight to ``steps[].result``. Read
    #: ``repeated_collisions[].count`` for attempts.
    boundary_kinds: dict[str, int] = Field(default_factory=dict)
    executor_reasons: dict[str, int] = Field(default_factory=dict)
    #: Rejections that recur on one tool: ``{kind, loc, tool, count,
    #: tool_attempts, rate}``. ``count`` is distinct rejected attempts, not
    #: error elements; ``rate`` is ``count / tool_attempts``. A file-condition
    #: kind carries ``None`` for the last two, since it is not rated.
    repeated_collisions: list[dict[str, Any]] = Field(default_factory=list)
    #: See :class:`RecoveryReport`. Absent on a report serialised before it
    #: existed, which reads as nothing tracked.
    recovery: RecoveryReport = Field(default_factory=RecoveryReport)
    #: Whether the EventLog reads behind these counts hit their cap, and
    #: what that excluded. A capped write-health report is a report whose
    #: window is shorter than ``window_days`` claims (#374).
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    status: str = "ok"
    reasons: list[str] = Field(default_factory=list)


class FailedStrategyReport(TrellisModel):
    """Packs in the window whose build continued after a strategy raised.

    Tallied from ``PACK_ASSEMBLED.strategy_failures`` over every scanned
    pack, a withheld one included, since it still ran its strategies.
    Counts and strategy names only, never the exception's class or message,
    which can carry a path or a DSN fragment.
    """

    #: Packs in the window whose build continued after at least one
    #: strategy raised.
    packs: int = 0
    #: Failures per strategy name. A strategy failing on more than one of
    #: ``packs`` contributes more than one count here.
    by_strategy: dict[str, int] = Field(default_factory=dict)
    #: ``occurred_at`` of the most recent failed-strategy pack in the
    #: window, or ``None`` when ``packs`` is zero.
    latest_at: datetime | None = None


class MalformedRowReport(TrellisModel):
    """Packs in the window where a strategy skipped a malformed stored row.

    Tallied from ``PACK_ASSEMBLED.malformed_row_skips`` over every scanned
    pack, a withheld one included, since it still ran its strategies. A skip
    is the writer-contract breach a missing-stamp ``ValueError`` used to
    surface through ``strategy_failures``, so it is counted here rather than
    left to a log line. Counts and strategy names only, never an item id or a
    value, which are caller-controlled.
    """

    #: Packs in the window where at least one strategy skipped a row.
    packs: int = 0
    #: Rows skipped per strategy name, summed over the window's packs.
    by_strategy: dict[str, int] = Field(default_factory=dict)
    #: ``occurred_at`` of the most recent such pack in the window, or ``None``
    #: when ``packs`` is zero.
    latest_at: datetime | None = None


class ServeAttributionReport(TrellisModel):
    """The serve-side coverage numbers the learning join lives or dies on.

    Deliberately narrow — deep pack telemetry already exists in
    ``trellis analyze pack-telemetry``. These are the two coverage rates
    whose silent collapse starved the loop for a month: packs carrying
    ``injected_items[]`` (without which the promote join has no rows) and
    feedback carrying item attribution (without which the demote half has
    no signal).

    ``attribution_rate`` divides by *every* feedback event (bar those
    naming a held-out pack, below), and that denominator mixes two
    populations with nothing in common (backlog A4).
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
    The one exclusion is the pack holdout
    (:mod:`trellis.core.pack_holdout`): a withheld pack in the window is
    left out of ``packs``, and the feedback naming a withheld pack is left
    out of ``feedback_events`` wherever the pack's row falls, before the
    window or past the scan cap included, because that pack served nothing
    to cite and counting it would read the experiment as a capture
    regression.

    **Citing is not joining, and the ``stray_*`` block is the gap between
    them** (#574). ``pack_attribution_rate`` counts a caller who cited
    *something*; it says nothing about whether the ids reached the pack's
    membership list. On the reference deployment 14 of 699 citations
    (2.0%) named an id the pack never served, concentrated in 6 packs —
    one of which lost 6 of its 15 — and every one of those verdicts was
    dropped by the learning join in silence. The block sits **beside**
    ``pack_attribution_rate`` and does not touch it: the older number
    keeps meaning exactly what it has always meant, because a metric that
    starts measuring something new under the same name is the failure
    this report already exists to expose once.
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

    # -- Stray citations (#574) ----------------------------------------
    #: Cited ids on feedback that joined to a pack whose served set is
    #: known. The denominator for ``stray_citation_rate`` — *not* every
    #: cited id in the window, because a pack with no recorded membership
    #: (a sectioned pack assembled before #958, or one outside the scan)
    #: cannot contribute a stray and must not dilute the rate. Counted per
    #: **(feedback event, cited id)** pair; ``analyze value``'s ``cited_ids_not_served``
    #: counts the same strays per (pack, distinct id), so on a pack graded
    #: twice the two can differ.
    cited_ids: int = 0
    #: Of those, the ids the pack never served — verdicts the learning
    #: join has nowhere to put, and drops without a word.
    stray_cited_ids: int = 0
    #: ``stray_cited_ids / cited_ids``. Zero on an empty window reads as
    #: "no evidence", not "no strays"; read it beside ``cited_ids``.
    stray_citation_rate: float = 0.0
    #: Packs contributing at least one stray. The loss concentrates, so
    #: the count of affected packs says more than the rate does.
    packs_with_stray_citations: int = 0
    #: Strays by the namespace prefix on the cited id.
    stray_citations_by_namespace: dict[str, int] = Field(default_factory=dict)
    #: Strays by :func:`~trellis.feedback.attribution.stray_shape` — how
    #: the id relates to what the pack *did* serve. This is the partition
    #: that says whether a loss is a caller-side id-spelling defect or a
    #: verdict on something the pack genuinely never carried.
    stray_citations_by_shape: dict[str, int] = Field(default_factory=dict)
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
    #: Coverage of the PACK_ASSEMBLED / FEEDBACK_RECORDED reads (#374).
    scan: ScanCoverage = Field(default_factory=ScanCoverage)

    #: See :class:`FailedStrategyReport`.
    failed_strategies: FailedStrategyReport = Field(
        default_factory=FailedStrategyReport
    )
    #: See :class:`MalformedRowReport`.
    malformed_rows: MalformedRowReport = Field(default_factory=MalformedRowReport)
    #: Which builds assembled the window's served packs, grouped by the
    #: ``write_provenance`` stamp. Counted over the same rows as ``packs``
    #: (the holdout dropped), so the groups sum to it. See
    #: :mod:`trellis.ops.serving_builds`.
    serving_builds: ServingBuildsReport = Field(default_factory=ServingBuildsReport)


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
    #: Whether ``save_memory`` extraction is running when its flag is on
    #: (#987). Defaulted, unlike ``capture``: ``unobserved`` is the honest
    #: reading of a log with no state event, and a deployment with the flag
    #: off is in that state by design. See
    #: :mod:`trellis.ops.memory_extraction_health`.
    memory_extraction: MemoryExtractionHealthReport = Field(
        default_factory=MemoryExtractionHealthReport
    )
    #: Reinstated precedents whose document or vector re-stamp did not land,
    #: read from the ``PRECEDENT_REINSTATED`` events the handler already
    #: writes. See :mod:`trellis.ops.precedent_sync_health`.
    precedent_sync: PrecedentSyncReport
    #: The merged coverage of every EventLog read behind this report. When
    #: ``truncated`` is set, ``status`` is ``warn`` and ``reasons`` names
    #: it: a health verdict computed over a silently shortened window is
    #: the failure mode #374 exists to end, so it must never present as a
    #: clean ``ok``.
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    status: str = "ok"
    reasons: list[str] = Field(default_factory=list)


@dataclass
class _BoundaryTally:
    """What one pass over ``WRITE_REJECTED`` events yields."""

    #: Error **elements** per ``kind@loc`` label.
    kinds: dict[str, int]
    #: Distinct rejected *attempts* per (surface, kind, loc): an event lists
    #: every error element, so a key is counted once per event however many
    #: elements share it.
    collision_attempts: dict[tuple[str, str, str], int]
    #: Rejections per surface written by a build that tracks recovery.
    tracked: dict[str, int]
    tracked_windows: list[int]


def _tally_boundary_rejections(
    events: Iterable[Event], stats: Callable[[str], ToolWriteStats]
) -> _BoundaryTally:
    """Count each ``WRITE_REJECTED`` event once per tool and once per key."""
    tally = _BoundaryTally(
        kinds={}, collision_attempts={}, tracked={}, tracked_windows=[]
    )
    for event in events:
        surface = surface_label(str(event.payload.get("tool") or "")) or "(unknown)"
        stats(surface).boundary_rejected += 1
        window = event.payload.get("recovery_window_s")
        if isinstance(window, int) and not isinstance(window, bool) and window > 0:
            tally.tracked[surface] = tally.tracked.get(surface, 0) + 1
            tally.tracked_windows.append(window)
        in_this_attempt: set[tuple[str, str]] = set()
        for row in event.payload.get("rejections") or []:
            kind = str(row.get("kind", "other"))
            # List indices are collapsed (``steps.0.action`` and
            # ``steps.1.action`` → ``steps[].action``) because they are the
            # same schema disagreement. Un-normalized, one mistake made in
            # eight steps of one payload presented as eight separate
            # collisions, each undercounting it.
            loc = normalize_loc(str(row.get("loc", "")))
            label = f"{kind}@{loc}" if loc else kind
            tally.kinds[label] = tally.kinds.get(label, 0) + 1
            in_this_attempt.add((kind, loc))
        for kind, loc in in_this_attempt:
            key = (surface, kind, loc)
            tally.collision_attempts[key] = tally.collision_attempts.get(key, 0) + 1
    return tally


def _tally_recoveries(events: Iterable[Event]) -> dict[str, int]:
    """Rejected attempts recovered, per surface, from ``WRITE_RECOVERED``."""
    recovered: dict[str, int] = {}
    for event in events:
        # One malformed event must not fail health: skip it.
        n = event.payload.get("rejected_attempts")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            continue
        surface = surface_label(str(event.payload.get("tool") or "")) or "(unknown)"
        recovered[surface] = recovered.get(surface, 0) + n
    return recovered


def _rate_clause(item: Mapping[str, Any]) -> str:
    """`` on <tool> (N% of M attempts)`` for a rated row; empty for a file kind."""
    if item["rate"] is None:
        return ""
    return (
        f" on {item['tool']} ({item['rate']:.0%} of {item['tool_attempts']} attempts)"
    )


def _call_attempts(surface: str, tool_stats: ToolWriteStats) -> int:
    """Attempts on ``surface``: accepted calls plus rejected ones.

    Accepted comes from :data:`_CALL_LEVEL_OPERATION` where the tool fans out,
    else from the ``accepted`` total (see :class:`ToolWriteStats`).
    """
    operation = _CALL_LEVEL_OPERATION.get(surface)
    accepted = (
        tool_stats.accepted_by_operation.get(operation, 0)
        if operation
        else tool_stats.accepted
    )
    return accepted + tool_stats.boundary_rejected + tool_stats.executor_rejected


def _repeated_collisions(
    collision_attempts: Mapping[tuple[str, str, str], int],
    by_tool: Mapping[str, ToolWriteStats],
) -> list[dict[str, Any]]:
    """The ``(surface, kind, loc)`` rejections that recur, rated where they can be.

    A file condition (:data:`_COLLISION_REASONS`) is warned on distinct
    attempts alone. Any other kind must also be a share of its tool's
    attempts above :data:`_COLLISION_RATE_WARN`, with at least
    :data:`_MIN_ATTEMPTS_FOR_COLLISION_RATE` attempts to rate. A surface
    that never reaches the executor (:data:`_EXECUTOR_BYPASS_SURFACES`) has
    no accepted count at all, so its rate would read 100% whatever it did;
    it is not rated and not warned here, and stays in ``by_tool`` and
    ``boundary_kinds``.
    """
    rows: list[dict[str, Any]] = []
    for (surface, kind, loc), count in collision_attempts.items():
        if count < _REPEAT_COLLISION_WARN:
            continue
        if kind in _COLLISION_REASONS:
            rows.append(
                {
                    "kind": kind,
                    "loc": loc,
                    "tool": surface,
                    "count": count,
                    "tool_attempts": None,
                    "rate": None,
                }
            )
            continue
        tool_stats = by_tool.get(surface)
        if tool_stats is None or surface in _EXECUTOR_BYPASS_SURFACES:
            continue
        tool_attempts = _call_attempts(surface, tool_stats)
        if tool_attempts < _MIN_ATTEMPTS_FOR_COLLISION_RATE:
            continue
        share = count / tool_attempts
        if share > _COLLISION_RATE_WARN:
            rows.append(
                {
                    "kind": kind,
                    "loc": loc,
                    "tool": surface,
                    "count": count,
                    "tool_attempts": tool_attempts,
                    "rate": round(share, 4),
                }
            )
    rows.sort(key=lambda r: (-r["count"], r["tool"], r["kind"], r["loc"]))
    return rows


def _recovery_report(
    tracked: Mapping[str, int],
    recovered: Mapping[str, int],
    windows: list[int],
) -> RecoveryReport:
    """Pair tracked rejections with the recoveries emitted for the same tool."""
    by_tool = {
        surface: ToolRecovery(
            tracked=count,
            recovered=min(recovered.get(surface, 0), count),
            share=round(min(recovered.get(surface, 0), count) / count, 4),
        )
        for surface, count in sorted(tracked.items())
    }
    total = sum(t.tracked for t in by_tool.values())
    done = sum(t.recovered for t in by_tool.values())
    window = max(windows) if windows else None
    return RecoveryReport(
        tracked_rejections=total,
        recovered_rejections=done,
        recovered_share=round(done / total, 4) if total else None,
        window_seconds=window,
        by_tool=by_tool,
        note=RECOVERY_APPROXIMATION_NOTE.format(window=window) if total else "",
    )


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
    ``ToolWriteStats.accepted_by_operation`` keys that same total by the
    ``operation`` name ``MUTATION_EXECUTED`` already carries, so a reader
    can recover the per-kind count the flat total hides.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)
    by_tool: dict[str, ToolWriteStats] = {}

    def stats(key: str) -> ToolWriteStats:
        return by_tool.setdefault(key or "(unknown)", ToolWriteStats())

    accepted_scan = scan_events(
        event_log, event_type=EventType.MUTATION_EXECUTED, since=since, limit=limit
    )
    for event in accepted_scan.events:
        tool_stats = stats(str(event.payload.get("requested_by") or ""))
        tool_stats.accepted += 1
        operation = str(event.payload.get("operation") or "(unknown)")
        tool_stats.accepted_by_operation[operation] = (
            tool_stats.accepted_by_operation.get(operation, 0) + 1
        )

    executor_reasons: dict[str, int] = {}
    rejected_scan = scan_events(
        event_log, event_type=EventType.MUTATION_REJECTED, since=since, limit=limit
    )
    for event in rejected_scan.events:
        stats(str(event.payload.get("requested_by") or "")).executor_rejected += 1
        reason = str(event.payload.get("reason") or "failed")
        executor_reasons[reason] = executor_reasons.get(reason, 0) + 1

    boundary_scan = scan_events(
        event_log, event_type=EventType.WRITE_REJECTED, since=since, limit=limit
    )
    boundary = _tally_boundary_rejections(boundary_scan.events, stats)
    recovered_scan = scan_events(
        event_log, event_type=EventType.WRITE_RECOVERED, since=since, limit=limit
    )
    recovered_by_surface = _tally_recoveries(recovered_scan.events)

    accepted = sum(s.accepted for s in by_tool.values())
    boundary_rejected = sum(s.boundary_rejected for s in by_tool.values())
    executor_rejected = sum(s.executor_rejected for s in by_tool.values())
    attempts = accepted + boundary_rejected + executor_rejected
    rate = (boundary_rejected + executor_rejected) / attempts if attempts else 0.0

    repeated = _repeated_collisions(boundary.collision_attempts, by_tool)
    recovery = _recovery_report(
        boundary.tracked, recovered_by_surface, boundary.tracked_windows
    )

    scan = merge_coverage(
        accepted_scan.coverage,
        rejected_scan.coverage,
        boundary_scan.coverage,
        recovered_scan.coverage,
    )

    reasons: list[str] = []
    if scan.truncated:
        # First reason, deliberately: every count below it was computed
        # over a shorter window than ``window_days`` names, so a reader
        # who stops at the first line still learns the right thing.
        reasons.append(scan.note)
    if attempts >= _MIN_ATTEMPTS_FOR_RATE and rate > _REJECTION_RATE_WARN:
        reasons.append(
            f"rejection rate {rate:.0%} over {attempts} attempts "
            f"(warn above {_REJECTION_RATE_WARN:.0%})"
        )
    for item in repeated:
        loc = str(item["loc"]) or "(payload)"
        # The explanation is the routing, so it follows the kind (#485).
        label, explanation = _COLLISION_REASONS.get(
            str(item["kind"]), _DEFAULT_COLLISION_REASON
        )
        reasons.append(
            f"repeated {label}: {item['kind']} at {loc} "
            f"x{item['count']}{_rate_clause(item)} — {explanation}"
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
        boundary_kinds=boundary.kinds,
        executor_reasons=executor_reasons,
        repeated_collisions=repeated,
        recovery=recovery,
        scan=scan,
        status="warn" if reasons else "ok",
        reasons=reasons,
    )


def _malformed_rows_report(events: Iterable[Event]) -> MalformedRowReport:
    """Tally ``PACK_ASSEMBLED.malformed_row_skips`` over ``events``.

    A payload with no key, an empty dict, or a value of any other shape counts
    nothing, and an entry that is not a positive integer count (a bool, a
    string, a negative) is dropped: one bad event must not fail health.
    """
    packs = 0
    by_strategy: dict[str, int] = {}
    latest_at: datetime | None = None
    for event in events:
        raw = event.payload.get("malformed_row_skips")
        skips = {
            str(name): rows
            for name, rows in (raw.items() if isinstance(raw, dict) else ())
            # ``bool`` is an ``int``; a count must be a real, positive one.
            if isinstance(rows, int) and not isinstance(rows, bool) and rows > 0
        }
        if not skips:
            continue
        packs += 1
        for name, rows in skips.items():
            by_strategy[name] = by_strategy.get(name, 0) + rows
        if latest_at is None or event.occurred_at > latest_at:
            latest_at = event.occurred_at
    return MalformedRowReport(packs=packs, by_strategy=by_strategy, latest_at=latest_at)


def summarize_serve_attribution(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> ServeAttributionReport:
    """Coverage of the two joins the learning loop depends on."""
    since = datetime.now(tz=UTC) - timedelta(days=days)

    packs = packs_with_items = 0
    served_by_pack: dict[str, set[str]] = {}
    pack_scan = scan_events(
        event_log, event_type=EventType.PACK_ASSEMBLED, since=since, limit=limit
    )
    feedback_scan = scan_events(
        event_log, event_type=EventType.FEEDBACK_RECORDED, since=since, limit=limit
    )
    # Every scanned pack, not the holdout-filtered population below: a
    # withheld pack still ran its strategies. A malformed entry is skipped
    # or counted as "unknown", because one bad event must not fail health.
    failed_packs = 0
    failures_by_strategy: dict[str, int] = {}
    latest_failure_at: datetime | None = None
    for event in pack_scan.events:
        failures = event.payload.get("strategy_failures")
        if not isinstance(failures, list) or not failures:
            continue
        failed_packs += 1
        for failure in failures:
            name = failure.get("strategy") if isinstance(failure, dict) else None
            strategy = str(name or "unknown")
            failures_by_strategy[strategy] = failures_by_strategy.get(strategy, 0) + 1
        if latest_failure_at is None or event.occurred_at > latest_failure_at:
            latest_failure_at = event.occurred_at

    # The served arm only: a withheld pack carries no injected items, and
    # it and the feedback naming it would read as a capture regression.
    # Feedback in the window can name a pack the scan lacks, one assembled
    # before the window opened or pushed past the cap, so each such pack is
    # looked up by id and a withheld one joins the drop.
    held_unscanned = [
        event
        for pack_id in unscanned_pack_ids(pack_scan.events, feedback_scan.events)
        for event in event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=1
        )
        if is_holdout(event.payload)
    ]
    pack_events, feedback_events = drop_holdout(
        [*pack_scan.events, *held_unscanned], feedback_scan.events
    )
    for event in pack_events:
        packs += 1
        if event.payload.get("injected_items"):
            packs_with_items += 1
        # ``entity_id`` is the pack id at both emit sites in
        # ``PackBuilder`` — the same key ``join_pack_feedback`` uses, and
        # the one verified against production (73 of 73 distinct feedback
        # pack_ids matched, 2026-09-16). The payload carries no
        # ``pack_id`` key at all, so joining on that would silently yield
        # nothing.
        if event.entity_id:
            served_by_pack[event.entity_id] = served_item_ids(event.payload)

    feedback = attributed = 0
    pack_targeted = pack_targeted_attributed = 0
    strays = StrayCitationTally()
    for event in feedback_events:
        feedback += 1
        # Both predicates come from ``trellis.feedback.attribution`` so the
        # health surface and the MCP boundary cannot drift on what
        # "attributed" or "pack-targeted" means in one deployment.
        is_attributed = payload_is_attributed(event.payload)
        if is_attributed:
            attributed += 1
        pack_id = payload_pack_id(event.payload)
        if pack_id:
            pack_targeted += 1
            if is_attributed:
                pack_targeted_attributed += 1
            # A pack absent from ``served_by_pack``, or one with no
            # recorded membership, is skipped whole by ``add`` — it can
            # never contribute a stray, so letting its citations into the
            # denominator would report a rate that falls as coverage falls.
            strays.add(event.payload, served_by_pack.get(pack_id, ()), pack_id=pack_id)

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
        cited_ids=strays.cited,
        stray_cited_ids=strays.stray,
        stray_citation_rate=strays.stray_rate,
        packs_with_stray_citations=len(strays.packs_with_stray),
        stray_citations_by_namespace=dict(strays.by_namespace),
        stray_citations_by_shape=dict(strays.by_shape),
        retrieval_availability_note=(
            RETRIEVAL_AVAILABILITY_ASSUMPTION if feedback - pack_targeted else ""
        ),
        scan=merge_coverage(pack_scan.coverage, feedback_scan.coverage),
        failed_strategies=FailedStrategyReport(
            packs=failed_packs,
            by_strategy=failures_by_strategy,
            latest_at=latest_failure_at,
        ),
        malformed_rows=_malformed_rows_report(pack_scan.events),
        serving_builds=summarize_serving_builds(pack_events),
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
    extraction = summarize_memory_extraction(event_log, days=days, limit=limit)
    precedent_sync = summarize_precedent_sync(event_log, days=days, limit=limit)

    scan = merge_coverage(
        write.scan, serve.scan, capture.scan, extraction.scan, precedent_sync.scan
    )

    # The write section states its own truncation so it reads correctly
    # standalone; here that is superseded by the merged note, which covers
    # the serve and capture reads too. Dropping the narrower line keeps one
    # truncation statement per report instead of two overlapping ones.
    reasons = [r for r in write.reasons if not r.startswith(_TRUNCATION_PREFIX)]
    if scan.truncated and scan.note:
        reasons.insert(0, scan.note)
    if serve.packs > 0 and serve.injected_coverage < _ATTRIBUTION_COVERAGE_WARN:
        reasons.append(
            f"only {serve.packs_with_injected_items}/{serve.packs} packs "
            "carry injected_items[] — the learning join is starved below "
            f"{_ATTRIBUTION_COVERAGE_WARN:.0%} coverage"
        )
    if serve.failed_strategies.packs > 0:
        by_strategy = ", ".join(
            f"{name}: {count}"
            for name, count in sorted(
                serve.failed_strategies.by_strategy.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        latest = serve.failed_strategies.latest_at
        latest_str = f"{latest:%Y-%m-%d %H:%M} UTC" if latest else "unknown"
        reasons.append(
            f"{serve.failed_strategies.packs} pack(s) in window hit a failed "
            f"retrieval strategy ({by_strategy}), latest {latest_str}"
        )
    if serve.malformed_rows.packs > 0:
        skipped_by_strategy = ", ".join(
            f"{name}: {count}"
            for name, count in sorted(
                serve.malformed_rows.by_strategy.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        latest_skip = serve.malformed_rows.latest_at
        latest_skip_str = (
            f"{latest_skip:%Y-%m-%d %H:%M} UTC" if latest_skip else "unknown"
        )
        reasons.append(
            f"{serve.malformed_rows.packs} pack(s) in window skipped malformed "
            f"retrieval rows ({skipped_by_strategy}), latest {latest_skip_str}"
        )
    mixed = mixed_builds_reason(serve.serving_builds, days=days)
    if mixed:
        reasons.append(mixed)
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

    if extraction.state == "unavailable":
        # The flag is on and the newest report says extraction is dead. Unlike
        # capture's "unobserved", this is a defect in *this* deployment. It is
        # worded as a report, not a fact about every save: several processes
        # share one event log (see ``memory_extraction_health``), and the row
        # names none of them.
        since = f"{extraction.since:%Y-%m-%d %H:%M} UTC" if extraction.since else "?"
        reasons.append(
            "memory extraction: the most recent extraction-state report says "
            f"unavailable ({extraction.reason or 'unknown'}), reported since "
            f"{since}; saves on that process are stored without entity/edge "
            "extraction"
        )

    reasons.extend(precedent_sync_reasons(precedent_sync))

    return BackendHealthReport(
        window_days=days,
        write=write,
        serve=serve,
        capture=capture,
        memory_extraction=extraction,
        precedent_sync=precedent_sync,
        scan=scan,
        status="warn" if reasons else "ok",
        reasons=reasons,
    )
