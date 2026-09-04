"""Typed, strict parsing for JSON-shaped LLM responses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum


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


_OPENING_FENCE = re.compile(r"^```(?:json)?\s*$")


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
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONParseResult(
            outcome=JSONParseOutcome.MALFORMED,
            error=f"{type(exc).__name__}: {exc}",
        )
    if value == []:
        return JSONParseResult(outcome=JSONParseOutcome.EMPTY, value=value)
    return JSONParseResult(outcome=JSONParseOutcome.VALUE, value=value)


__all__ = [
    "JSONParseOutcome",
    "JSONParseResult",
    "parse_json_response",
    "strip_code_fence",
]
