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

from trellis.errors import TrellisError

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
    # bolt://u:p@..., https://token@localhost/... . A password
    # separator and a dotted host are both optional.
    re.compile(r"\w+://[^\s/@?#]+@"),
    # Secret-shaped assignment: password=..., token: ...,
    # Authorization: Bearer ... . Word-bounded so prose like
    # "password must be set" stays clean.
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)\b"
        r"\s*[=:]\s*\S+"
    ),
    # Raw SQL statement shape. Curator/scout errors quoting warehouse
    # SQL must not put statement text into artifacts.
    re.compile(
        r"(?i)\b(select\s+.+?\s+from\s|insert\s+into\s|update\s+\S+\s+set\s"
        r"|delete\s+from\s|drop\s+(table|database)\s)"
    ),
    # PostgreSQL row values, quoted by a constraint violation's DETAIL
    # line: "Key (name)=(value) already exists." (unique, foreign key and
    # exclusion; the column list can nest parentheses) and "Failing row
    # contains (...)." (NOT NULL and CHECK). English wording only: a server
    # with another lc_messages translates both.
    re.compile(r"\bKey \(.*?\)=\(|\bFailing row contains \("),
    # Neo4j's uniqueness violation quotes the value: "Node(<n>) already
    # exists with label `<Label>` and property `<prop>` = '<value>'".
    re.compile(r"\balready exists with label `[^`]*` and property `[^`]*` = '"),
    # ArcadeDB's, over Bolt: "Duplicated key [<value>] found on index
    # '<Label>[<prop>]' ...". The prefix alone, because the value can hold
    # its own "]": an alias claim key is a JSON array.
    re.compile(r"\bDuplicated key \["),
    # Neo4j's *constraint-creation* violation quotes the value too, raised
    # when the stores' startup schema DDL (``CREATE CONSTRAINT ... IS
    # UNIQUE``) runs over rows that already duplicate it: "Both Node(<n>)
    # and Node(<n>) have the label `<Label>` and property `<prop>` =
    # '<value>'". Distinct wording from the write-time violation above.
    re.compile(r"\bhave the label `[^`]*` and property `[^`]*` = '"),
)

_LONG_TOKEN_RUN = re.compile(
    r"(?<![A-Za-z0-9+_-])[A-Za-z0-9+_-]{40,}(?![A-Za-z0-9+_-])"
)
_PATH_SEPARATORS = frozenset("/\\")

#: Characters scanned past ``max_len``. Only ``text[:max_len]`` reaches the
#: output, but a secret starting there can complete its pattern past the
#: cut: a long-opaque-token run needs 40 characters, and the email,
#: credential-URL and SQL patterns end on the ``@`` or ``from`` that follows
#: the sensitive text. 500 covers most DSN userinfo and column lists; the
#: patterns' worst-case backtracking over a 1000-character window is ~3 ms.
_SCAN_MARGIN = 500


def _is_repeated_character_path_component(text: str, match: re.Match[str]) -> bool:
    """Recognize the low-entropy component used by long pytest basetemps."""
    token = match.group()
    start = match.start()
    if start == 0 or text[start - 1] not in _PATH_SEPARATORS:
        return False
    if len(set(token)) != 1:
        return False
    whitespace_start = max(text.rfind(" ", 0, start), text.rfind("\t", 0, start))
    return "://" not in text[whitespace_start + 1 : start]


def _has_long_opaque_token(text: str) -> bool:
    return any(
        not _is_repeated_character_path_component(text, match)
        for match in _LONG_TOKEN_RUN.finditer(text)
    )


def sanitize_error_message(text: str, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """Return ``text`` bounded to ``max_len``, or a static marker if it
    trips a leak heuristic.

    Clean text passes through so operator-authored Trellis error
    messages ("entity_type 'precedent' not registered") stay useful in
    JSON output. Text containing an email, an inline-credential URL, a
    secret-shaped assignment, a long token-shaped run, raw SQL, a
    PostgreSQL row value, or a Neo4j or ArcadeDB duplicate-constraint row
    value is replaced wholesale with :data:`SUPPRESSED_MARKER` — partial
    redaction is not attempted because any transform of the original
    text risks leaving a recoverable fragment. The heuristics scan only
    ``text[:max_len + _SCAN_MARGIN]``, so their cost does not grow with
    the input. A match that cannot complete inside that window is missed:
    a leak wholly past it no longer suppresses the visible prefix, and a
    secret straddling the cut by more than the margin shows its start.

    Raises:
        ValueError: if ``max_len`` is negative — ``text[:max_len]`` then
            keeps everything but the last few characters, which can run
            past the scanned window and return an unscanned secret.
    """
    if max_len < 0:
        msg = f"max_len must be >= 0; got {max_len!r}"
        raise ValueError(msg)
    window = text[: max_len + _SCAN_MARGIN]
    if any(pattern.search(window) for pattern in _LEAK_PATTERNS):
        return SUPPRESSED_MARKER
    if _has_long_opaque_token(window):
        return SUPPRESSED_MARKER
    if len(text) > max_len:
        return text[:max_len] + "…[truncated]"
    return text


# PyYAML quotes with ``%r``: one character (``':'``, ``'\t'``), a parser token
# id (``'<block end>'``), or document text (an alias, anchor, tag or tag
# handle). Only the first two survive; document text becomes ``'...'``.
_QUOTED = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
_KEPT_QUOTED = re.compile(r".|\\(?:x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8}|.)|<[a-z ]+>")


def _mask_quoted(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        quoted = match.group()
        if _KEPT_QUOTED.fullmatch(quoted[1:-1]):
            return quoted
        return f"{quoted[0]}...{quoted[0]}"

    return _QUOTED.sub(_replace, text)


def describe_yaml_error(exc: BaseException) -> str:
    """Describe a failed ``yaml.safe_load`` without quoting the document.

    ``str(exc)`` of a PyYAML error prints the offending line, and a config
    line can hold a DSN or a password. The description is rebuilt from the
    structured fields instead: ``context`` and ``problem``, each with its
    1-based line and column, with quoted document text masked. A
    ``ReaderError`` (a non-printable character) gives its code point and
    position. Anything else ``safe_load`` raises (``!!int`` and
    ``!!float`` raise ``ValueError``, ``!!bool`` ``KeyError``, each naming
    the value) is described by its type alone. ``str(exc)`` is never used.
    """
    parts: list[str] = []
    for text, mark in (
        (getattr(exc, "context", None), getattr(exc, "context_mark", None)),
        (getattr(exc, "problem", None), getattr(exc, "problem_mark", None)),
    ):
        if not isinstance(text, str):
            continue
        piece = _mask_quoted(text)
        if mark is not None:
            piece += f" (line {mark.line + 1}, column {mark.column + 1})"
        parts.append(piece)
    if parts:
        return "; ".join(parts)
    character = getattr(exc, "character", None)
    position = getattr(exc, "position", None)
    if isinstance(character, int) and isinstance(position, int):
        return f"unacceptable character #x{character:04x} at position {position}"
    return (
        f"a value could not be constructed ({type(exc).__name__}); check"
        " explicit tags such as !!int, !!float or !!bool, and dates"
    )


def render_exception_detail(exc: BaseException) -> str:
    """Render a caught exception's text for a caller-facing message.

    A ``TrellisError`` keeps its own text: Trellis wrote it, and every
    ``StoreError`` across ``src/trellis/stores/`` already names its cause
    by type alone rather than embedding raw driver text, so there is
    nothing left to strip. Any other exception's text can be a driver's —
    a Postgres DETAIL line, a Neo4j constraint message — and is rendered
    through :func:`sanitize_error_message` instead of discarded outright,
    so a clean message (a timeout, a connection refusal) still reaches the
    caller while a leak-shaped one comes back as the sanitizer's static
    marker. Callers that also log keep the full text on an operator
    channel first (``logger.exception`` or equivalent); this function
    only decides what a caller-facing message gets. The sanitizer is a
    deny-list, so a leak in a shape it does not know still passes
    (trellis-ai#748).

    Shared by ``trellis.mcp.server``'s ``_exception_detail`` and
    ``trellis.mcp.supersession``'s stamp functions so the two render a
    caught exception the same way rather than keeping two copies of this
    rule (trellis-ai#793 follow-up).
    """
    if isinstance(exc, TrellisError):
        return str(exc)
    return sanitize_error_message(str(exc))


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
