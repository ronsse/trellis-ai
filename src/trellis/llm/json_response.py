"""Typed, strict parsing for JSON-shaped LLM responses."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class JSONParseOutcome(StrEnum):
    """Result categories from the shared JSON parsing seam."""

    VALUE = "value"
    EMPTY = "empty"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class JSONParseResult:
    """A decoded value or a concrete malformed-response outcome."""

    outcome: JSONParseOutcome
    value: object | None = None
    error: str | None = None


_OPENING_FENCE = re.compile(r"^```(?:json)?\s*$", re.IGNORECASE)


def strip_code_fence(raw: str) -> str:
    """Remove at most one leading and one trailing markdown fence line."""
    lines = raw.strip().splitlines()
    if lines and _OPENING_FENCE.fullmatch(lines[0].strip()):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_response(raw: str) -> JSONParseResult:
    """Strip an outer code fence and decode JSON without content salvage."""
    text = strip_code_fence(raw)
    try:
        value = json.loads(text)
    # RecursionError: a reply nested past the interpreter's recursion limit
    # (about 1,000 deep on 3.11, about 10,000 on 3.12/3.13) is malformed too.
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        return JSONParseResult(
            outcome=JSONParseOutcome.MALFORMED,
            error=f"{type(exc).__name__}: {exc}",
        )
    if value == []:
        return JSONParseResult(outcome=JSONParseOutcome.EMPTY, value=value)
    return JSONParseResult(outcome=JSONParseOutcome.VALUE, value=value)


def coerce_finite_float(value: Any) -> float | None:
    """Read a decoded JSON value as a finite float, or ``None``.

    ``None`` for a non-numeric value, and also for NaN, ±Infinity and a
    number too large for a float (``json.loads`` accepts all three), which
    each caller maps to its existing non-numeric default. ``bool`` is
    accepted (it is an ``int``): each site applies its own bool policy.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "JSONParseOutcome",
    "JSONParseResult",
    "coerce_finite_float",
    "parse_json_response",
    "strip_code_fence",
]
