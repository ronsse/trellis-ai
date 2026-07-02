"""Leak-safe error text for machine-readable output surfaces (issue #206).

CLI ``--format json`` payloads and API response bodies are frequently
captured into artifacts (scout runs, CI logs, review bundles). Success
paths are shaped deliberately, but error paths that embed raw
``str(exc)`` inherit whatever an external system put in the exception
message — a psycopg connection error can echo a DSN with credentials, a
cloud SDK can echo request payloads, an LLM client can echo prompt
fragments. This module is the shared guard for those surfaces:

* :func:`sanitize_error_message` passes clean text through (bounded),
  and replaces text that trips a leak heuristic with a static marker.
* :func:`sanitized_error_payload` builds the standard JSON error shape
  — ``status`` / ``error_type`` / sanitized ``message`` plus caller
  context — so CLI commands emit one consistent envelope.

The heuristics are deliberately conservative *toward suppression*: a
false positive costs an operator a trip to the logs (the full exception
should still be logged via ``structlog`` on an operator channel); a
false negative leaks a credential into an artifact. Full detail never
belongs in the machine payload — that is what the log stream is for.
"""

from __future__ import annotations

import re
from typing import Any

#: Upper bound for a passed-through message. Exception text beyond this
#: is almost always a wrapped stack dump or an echoed payload; the
#: interesting part (the leading error statement) survives truncation.
DEFAULT_MAX_LEN = 500

#: Replacement used when a leak heuristic trips. Static on purpose —
#: anything derived from the original text could itself leak.
SUPPRESSED_MARKER = "[error detail suppressed: potentially sensitive content]"

# Leak heuristics. Each pattern flags content that has no business in a
# machine-readable artifact, per the #206 finding:
_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Email address (user identifier).
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # URL with inline credentials — postgres://user:pass@host,
    # bolt://u:p@..., https://token@... . Connection errors from
    # drivers routinely echo the DSN they failed to reach.
    re.compile(r"\w+://[^/\s@:]+:[^@\s]+@"),
    # Secret-shaped assignment: password=..., token: ...,
    # Authorization: Bearer ... . Word-bounded so prose like
    # "password must be set" stays clean.
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)\b"
        r"\s*[=:]\s*\S+"
    ),
    # Long unbroken token-shaped run (API keys, JWTs, hex digests).
    # 40+ chars clears ULIDs (26) and short ids; dots and slashes break
    # runs, so file paths and dotted module paths stay clean.
    re.compile(r"[A-Za-z0-9+_-]{40,}"),
    # Raw SQL statement shape. Curator/scout errors quoting warehouse
    # SQL must not put statement text into artifacts.
    re.compile(
        r"(?i)\b(select\s+.+?\s+from\s|insert\s+into\s|update\s+\S+\s+set\s"
        r"|delete\s+from\s|drop\s+(table|database)\s)"
    ),
)


def sanitize_error_message(text: str, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """Return ``text`` bounded to ``max_len``, or a static marker if it
    trips a leak heuristic.

    Clean text passes through so operator-authored Trellis error
    messages ("entity_type 'precedent' not registered") stay useful in
    JSON output. Text containing an email, an inline-credential URL, a
    secret-shaped assignment, a long token-shaped run, or raw SQL is
    replaced wholesale with :data:`SUPPRESSED_MARKER` — partial
    redaction is not attempted because any transform of the original
    text risks leaving a recoverable fragment.
    """
    if any(pattern.search(text) for pattern in _LEAK_PATTERNS):
        return SUPPRESSED_MARKER
    if len(text) > max_len:
        return text[:max_len] + "…[truncated]"
    return text


def sanitized_error_payload(exc: BaseException, **context: Any) -> dict[str, Any]:
    """Build the standard leak-safe JSON error envelope for an exception.

    Always carries ``status="error"`` and the exception class name as
    ``error_type`` (safe: a type name never contains payload data), a
    sanitized ``message``, and any caller-supplied context fields
    (command name, config identifiers — caller-authored values, not
    exception content). Context keys must not collide with the three
    reserved keys.
    """
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "message": sanitize_error_message(str(exc)),
        **context,
    }
