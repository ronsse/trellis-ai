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

import json
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
    # "password must be set" stays clean. The optional quote after the
    # key name matches a JSON or repr'd mapping's shape, "api_key": "...",
    # where a closing quote sits between the key and the separator.
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)\b"
        r"['\"]?\s*[=:]\s*\S+"
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


def describe_os_error(exc: OSError) -> str:
    """Describe an ``OSError`` by ``strerror``, ``errno`` and ``filename``,
    never by ``str(exc)``.

    ``str(exc)`` already renders roughly this shape ("[Errno 2] No such
    file or directory: 'path'"), but as one opaque sentence whose
    punctuation varies by platform and by whether ``filename`` is set at
    all; describing the fields separately gives call sites one stable
    shape to build a sentence around. ``filename`` is the path Trellis
    itself resolved and asked the OS to open — safe to surface per the
    storage and display rule's "paths Trellis itself resolved" row —
    never a value read from inside that file.
    """
    parts: list[str] = []
    if exc.strerror:
        parts.append(exc.strerror)
    if exc.errno is not None:
        parts.append(f"errno {exc.errno}")
    if exc.filename is not None:
        parts.append(f"path {exc.filename!s}")
    return ", ".join(parts) if parts else type(exc).__name__


def describe_json_error(exc: json.JSONDecodeError) -> str:
    """Describe a ``json.JSONDecodeError`` by ``msg``, ``lineno`` and
    ``colno``, never by ``str(exc)``.

    ``str(exc)`` is ``f"{msg}: line {lineno} column {colno} (char
    {pos})"`` — already just these three fields plus ``pos``, which this
    drops as redundant with line/column. Never quotes the document: a
    JSON parse failure here is reading a Trellis-owned file (a policy or
    fingerprint file), and the document can hold row data or a secret
    the file stored in the clear.
    """
    return f"{exc.msg} (line {exc.lineno}, column {exc.colno})"


def describe_validation_error(exc: BaseException) -> str:
    """Describe a pydantic ``ValidationError`` by each failed field's
    location, error type and pydantic's own ``msg``, never by the
    exception's rendered ``str()``.

    pydantic's own ``str(exc)`` composes ``input_value=<the caller's
    value>`` into every line, by design (it is meant for a human
    debugging their own call) — exactly the content the storage and
    display rule forbids in an audit event or a caller-facing reply.
    ``errors(include_input=False)`` gives the same failures without that
    clause; this keeps ``loc`` (dotted), ``type`` and ``msg``. ``msg`` is
    a separate field from ``input_value``: for every error type pydantic
    raises itself (a missing field, a type mismatch, an enum/literal
    miss), ``msg`` names only the *expectation*, never the caller's
    actual value, so a field's own schema-authored sentence — "metric_value
    must be a finite number" — survives without reintroducing what the
    caller sent. For ``value_error``/``assertion_error``, ``msg`` is a
    custom ``@field_validator``'s own exception text, which *could* embed
    a value if that validator chooses to interpolate one in (none of this
    module's current callers' validators do); since this function cannot
    inspect a validator's source to know whether it did, ``msg`` is passed
    through :func:`sanitize_error_message` as a defense-in-depth measure
    before it is used.

    Falls back to the exception's type name when *exc* has no
    ``errors(include_input=...)`` method — not every exception a broad
    ``except Exception`` catches around a ``model_validate`` call is a
    pydantic ``ValidationError``.
    """
    errors_method = getattr(exc, "errors", None)
    if not callable(errors_method):
        return type(exc).__name__
    try:
        raw_errors = errors_method(include_input=False)
    except TypeError:
        return type(exc).__name__
    pairs = []
    for err in raw_errors:
        loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        err_type = err.get("type", "?")
        msg = err.get("msg")
        if msg:
            pairs.append(f"{loc}: {err_type}: {sanitize_error_message(str(msg))}")
        else:
            pairs.append(f"{loc}: {err_type}")
    return "; ".join(pairs) if pairs else type(exc).__name__


def describe_import_error(exc: ImportError) -> str:
    """Describe an ``ImportError`` by its own ``name`` field, never by
    ``str(exc)``.

    A dotted import path can fail on a *transitive* import inside the
    target module rather than on the target itself; ``exc.name`` names
    whichever module actually raised, which ``str(exc)`` folds into one
    sentence that can also carry an unrelated third-party package's own
    error text. The two ``ImportError`` shapes say different things and
    must not share a sentence: ``ModuleNotFoundError`` (a subclass of
    ``ImportError``) means the named module itself does not exist, while
    a plain ``ImportError`` with a ``name`` means that module *was*
    found and imported, but ``from <name> import <missing attr>`` failed
    inside it — the earlier "module is not importable" wording claimed
    the former for both, sending an operator to reinstall a package that
    already works.
    """
    if not exc.name:
        return type(exc).__name__
    if isinstance(exc, ModuleNotFoundError):
        return f"module {exc.name!r} was not found"
    return f"an import from module {exc.name!r} failed"


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

    A ``TrellisError`` keeps its own text, on the convention that Trellis
    composes it without raw driver text (a ``StoreError`` names its cause
    by type). Any other exception's text can be a driver's, such as a
    Postgres DETAIL line, so it goes through :func:`sanitize_error_message`:
    a clean message (a timeout) still reads, and a leak-shaped one becomes
    the static marker. The sanitizer is a deny-list, so a leak in a shape
    it does not know still passes. Log the full text on an operator
    channel first; this decides only what the caller sees.
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


def _driver_error_code(exc: BaseException) -> str | None:
    """Pick a structured error code off a caught exception, if one exists.

    Checked in order, first match wins: a psycopg ``sqlstate`` (the
    five-character Postgres SQLSTATE), a sqlite3 ``sqlite_errorname``
    (e.g. ``"SQLITE_READONLY"``, Python 3.11+), a ``code`` attribute —
    set independently by neo4j's driver exceptions (a dotted status such
    as ``"Neo.ClientError.Schema.ConstraintValidationFailed"``) and by
    Trellis's own :class:`~trellis.errors.TrellisError` family, so one
    check covers both — and finally an ``OSError``'s numeric ``errno``.
    Each of these is a stable, caller-defined identifier rather than
    free text, so none needs :func:`sanitize_error_message`. Returns
    ``None`` when the exception carries none of them.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return sqlstate
    sqlite_errorname = getattr(exc, "sqlite_errorname", None)
    if isinstance(sqlite_errorname, str) and sqlite_errorname:
        return sqlite_errorname
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        return str(errno)
    return None


def _foreign_exception_summary(exc: BaseException) -> dict[str, Any]:
    """``error_type``/``error_code``/``message``/``constraint`` for an
    exception Trellis did not compose itself.

    Shared by :func:`summarize_exception`'s own top-level branch for a
    non-:class:`~trellis.errors.TrellisError` and by its ``cause`` branch
    (a :class:`TrellisError` wraps one with ``raise ... from exc``): the
    same rule applies to the type Trellis never spoke for, whether it
    reached the caller directly or is chained one level down. ``message``
    is the *first line* of ``str(exc)`` — a multi-line driver error's
    ``DETAIL:``/``CONTEXT:`` continuation lines are the most likely place
    for a quoted row value, and are dropped outright rather than merely
    masked — then :func:`_mask_quoted` (so a same-line quoted value, such
    as a Postgres ``Key (email)=('a@b.com')`` row, does not survive as
    scannable text), then :func:`sanitize_error_message` as a second
    layer over anything the mask regex does not recognize as quoted.
    ``constraint`` is psycopg's ``diag.constraint_name``, only when the
    driver names one; the key is omitted (not ``None``) otherwise, so a
    reader can test for its presence rather than its value.
    """
    text = str(exc)
    first_line = text.splitlines()[0] if text else ""
    summary: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error_code": _driver_error_code(exc),
        "message": sanitize_error_message(_mask_quoted(first_line)),
    }
    constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
    if constraint:
        summary["constraint"] = constraint
    return summary


def summarize_exception(exc: BaseException) -> dict[str, Any]:
    """Build the audit-safe structured summary of a caught exception.

    This is the shape Trellis's audit and telemetry events store for a
    caught error — distinct from :func:`render_exception_detail` (a
    caller-facing message) and :func:`sanitized_error_payload` (a CLI/API
    error envelope): an immutable event is read back by a reader who
    cannot ask a follow-up question, so it gets a structured summary
    rather than one opaque sentence.

    Returns a dict with:

    * ``error_type`` — the exception's class name. Always safe: a type
      name never contains payload data.
    * ``error_code`` — see :func:`_driver_error_code`; ``None`` when the
      exception carries none.
    * ``message`` — for a :class:`~trellis.errors.TrellisError`,
      :func:`sanitize_error_message` run on the error's own ``.message``
      (Trellis composes that text itself without driver internals by
      convention; the sanitizer still runs as defense-in-depth in case a
      handler interpolated caller input into it). For any other
      exception, see :func:`_foreign_exception_summary`.
    * ``constraint`` — psycopg's ``diag.constraint_name``, only when the
      driver names one; the key is omitted (not ``None``) otherwise, so
      a reader can test for its presence rather than its value.
    * ``cause`` — present only when ``exc.__cause__`` is set, one level
      only (``__context__`` is never read): :func:`_foreign_exception_summary`
      of that cause. Production wraps a driver error as ``StoreError(f"...
      failed: {type(exc).__name__}") from exc`` precisely so the type-only
      text stays clear of the driver's own text, leaving the SQLSTATE, the
      driver's message and the violated constraint reachable only off
      ``__cause__`` — this is what reads them back out, so a wrapped
      Postgres deadlock records ``error_code: "40P01"`` and
      ``message: "deadlock detected"`` on ``cause`` rather than only
      ``DeadlockDetected`` on the wrapper's own ``error_type``.
    """
    if isinstance(exc, TrellisError):
        summary: dict[str, Any] = {
            "error_type": type(exc).__name__,
            "error_code": _driver_error_code(exc),
            "message": sanitize_error_message(exc.message),
        }
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
        if constraint:
            summary["constraint"] = constraint
    else:
        summary = _foreign_exception_summary(exc)
    cause = exc.__cause__
    if cause is not None:
        summary["cause"] = _foreign_exception_summary(cause)
    return summary
