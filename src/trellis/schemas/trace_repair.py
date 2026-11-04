"""Deterministic repair of the two commonest trace-shape mistakes.

Trace validation is all-or-nothing and ``extra="forbid"``: one unknown key
rejects the whole trace and nothing is recorded. Two mistakes are common
enough to repair, and each has exactly one unambiguous fix:

* ``steps[i].result`` given as a bare string. The canonical shape is an
  object, so the string becomes ``{"summary": <the string>}``.
* ``context.repo``. ``TraceContext`` has no such field, and top-level
  ``metadata`` is where free-form run data belongs, so the value moves to
  ``metadata["repo"]``.

The repair runs on the parsed payload **before** validation, at every trace
write surface (MCP ``save_experience``, ``POST /traces``, ``trellis ingest
trace``), through :func:`validate_trace_payload` / :func:`validate_trace_json`.
The stored shape stays canonical: no field is added to ``Trace``,
``TraceStep`` or ``TraceContext``, and ``extra="forbid"`` is untouched.

Nothing else is repaired, and nothing is guessed. A ``result`` that is any
other non-object (a number, a list, ``null``) is left for validation to
reject, as is a ``context.repo`` whose value would overwrite a different
``metadata["repo"]``, or land in a ``metadata`` that is not an object. Each
rule is judged on its own, so a payload with one repairable and one
unrepairable mistake is rejected for the unrepairable one only.

Every repair is reported as ``{"path", "rule"}``: where it applied and which
rule, never the value. A write surface that accepts a repaired trace tells
the caller how to send it next time and records one ``WRITE_COERCED`` event
(:func:`trellis.ops.write_health.record_trace_coercion`), so a repaired write
is neither silent nor counted as a rejection.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any, Final, TypedDict

from trellis.schemas.trace import Trace, TraceContext

__all__ = [
    "REPAIR_RULES",
    "RULE_MOVED_TO_METADATA",
    "RULE_STR_TO_SUMMARY",
    "TraceRepair",
    "describe_repairs",
    "repair_trace_payload",
    "validate_trace_json",
    "validate_trace_payload",
]

#: ``steps[i].result`` was a string; it became ``{"summary": <string>}``.
RULE_STR_TO_SUMMARY: Final = "str_to_summary"
#: ``context.repo`` was moved to top-level ``metadata["repo"]``.
RULE_MOVED_TO_METADATA: Final = "moved_to_metadata"

#: Every rule this module can apply: the closed set a ``WRITE_COERCED`` row
#: and a health report key on.
REPAIR_RULES: Final = (RULE_STR_TO_SUMMARY, RULE_MOVED_TO_METADATA)

#: What to send next time, per rule, for the reply to the caller.
_NEXT_TIME: Final = {
    RULE_STR_TO_SUMMARY: 'send a step result as an object, e.g. {"summary": "<text>"}',
    RULE_MOVED_TO_METADATA: "put repo in top-level metadata, not in context",
}

#: Distinguishes a missing ``metadata`` key from one whose value is ``None``.
_ABSENT: Final = object()


class TraceRepair(TypedDict):
    """One repair applied to a payload: where, and by which rule. No content."""

    path: str
    rule: str


def _same_value(a: object, b: object) -> bool:
    """Equal and of the same type, so ``1`` and ``True`` do not agree."""
    return type(a) is type(b) and a == b


def _repair_steps(data: dict[str, Any], repairs: list[TraceRepair]) -> Any:
    """``data["steps"]`` with string results wrapped, or the original object."""
    steps = data.get("steps")
    if not isinstance(steps, list):
        return steps
    repaired: list[Any] | None = None
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("result"), str):
            continue
        if repaired is None:
            repaired = list(steps)
        repaired[index] = {**step, "result": {"summary": step["result"]}}
        repairs.append({"path": f"steps[{index}].result", "rule": RULE_STR_TO_SUMMARY})
    return steps if repaired is None else repaired


def repair_trace_payload(data: Any) -> tuple[Any, list[TraceRepair]]:
    """Apply the two repairs to a parsed trace payload.

    Returns ``(payload, repairs)``. ``payload`` is ``data`` itself, untouched,
    when nothing applied; otherwise a copy with only the repaired containers
    replaced. ``data`` is never mutated, so a caller keeps the original to
    describe a rejection by.
    """
    if not isinstance(data, dict):
        return data, []
    repairs: list[TraceRepair] = []
    out = dict(data)

    steps = _repair_steps(data, repairs)
    if steps is not data.get("steps"):
        out["steps"] = steps

    context = data.get("context")
    # A ``repo`` field added to ``TraceContext`` later makes the key valid,
    # and moving a valid field would be a silent rewrite.
    if (
        "repo" not in TraceContext.model_fields
        and isinstance(context, dict)
        and "repo" in context
    ):
        metadata = data.get("metadata", _ABSENT)
        target: dict[str, Any] | None = None
        if metadata is _ABSENT:
            target = {}
        elif isinstance(metadata, dict) and (
            "repo" not in metadata or _same_value(metadata["repo"], context["repo"])
        ):
            target = dict(metadata)
        if target is not None:
            target["repo"] = context["repo"]
            out["metadata"] = target
            out["context"] = {k: v for k, v in context.items() if k != "repo"}
            repairs.append({"path": "context.repo", "rule": RULE_MOVED_TO_METADATA})

    return (out, repairs) if repairs else (data, [])


def validate_trace_payload(data: Any) -> tuple[Trace, list[TraceRepair]]:
    """Repair, then validate, a parsed trace payload.

    Raises the ``ValidationError`` ``Trace.model_validate`` would, naming only
    what the repair could not fix. Returns the canonical ``Trace`` and the
    repairs applied.
    """
    payload, repairs = repair_trace_payload(data)
    return Trace.model_validate(payload), repairs


def validate_trace_json(text: str) -> tuple[Trace, list[TraceRepair]]:
    """:func:`validate_trace_payload` for a JSON string.

    Text that does not parse as a JSON object takes the exact path it always
    did (``Trace.model_validate_json``), so its error is unchanged. A payload
    that needs no repair is validated from the original text for the same
    reason.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
    payload, repairs = repair_trace_payload(parsed)
    if not repairs:
        return Trace.model_validate_json(text), []
    return Trace.model_validate_json(json.dumps(payload)), repairs


def describe_repairs(repairs: Sequence[TraceRepair]) -> str:
    """One line for the caller: what was repaired and how to send it next time.

    Paths are the schema's own, with list indices collapsed and counted, so
    thirty bad steps are one clause. Empty for no repairs. Carries no value.
    """
    if not repairs:
        return ""
    counts = Counter(
        (
            str(r["rule"]),
            "steps[].result" if r["rule"] == RULE_STR_TO_SUMMARY else str(r["path"]),
        )
        for r in repairs
    )
    where = ", ".join(
        path if n == 1 else f"{path} x{n}" for (_, path), n in counts.items()
    )
    rules = list(dict.fromkeys(str(r["rule"]) for r in repairs))
    advice = "; ".join(_NEXT_TIME[rule] for rule in rules if rule in _NEXT_TIME)
    noun = "field" if len(repairs) == 1 else "fields"
    return f"repaired {len(repairs)} {noun} ({where}); next time {advice}"
