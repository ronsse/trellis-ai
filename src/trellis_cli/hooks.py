"""Claude Code hooks that keep the Trellis retrieve / grade loop closed.

An agent retrieves from Trellis only if its harness tells it to, and a prompt
alone does not always do that. ``trellis admin install-hooks`` writes three
hook entries into a Claude Code ``settings.json``; each runs ``trellis-hook
<name>``, which is :func:`main` here:

``prework`` (``UserPromptSubmit``, ``SubagentStart``)
    Adds one short line to the context of a session that has not called a
    pack-returning retrieval tool, once per session (once per subagent).
``edit-guard`` (``PreToolUse`` on the file-editing tools)
    Refuses the first edit of a session that has not retrieved, once, with a
    reason that says to call ``get_context`` and retry. It is soft: the retry
    passes, so it cannot deadlock. By default it applies to dispatched
    subagents only.
``grade`` (``Stop``, ``SubagentStop``)
    Blocks once, when a pack was served, no ``record_feedback`` has named item
    ids for it and the session made enough tool calls, with one message that
    asks for the grade (and a trace, when none was saved). It never asks about
    a pack that was not served: asking for a grade on a pack that does not
    exist trains the habit of inventing one.

Every sentence a hook prints is in :mod:`trellis_wire.loop_text`. A hook decides
from the transcript Claude Code names on stdin and nothing else: no store, no
network, no Trellis config. It reads that file as a bounded stream, and it
fails open. Any exception, unreadable input or oversized transcript exits 0
with no output, because a hook that breaks a session is worse than a loop that
stays open. (Exit code 2 blocks in Claude Code, so the argument parser never
exits 2 either.) One case is not unknown: a transcript path that is named but
whose file does not exist yet, at ``UserPromptSubmit``, is a session that has
retrieved nothing, so ``prework`` nudges. A file that exists but cannot be read
stays unknown.

Hook input fields used, from the Claude Code hooks reference: ``session_id``,
``transcript_path``, ``hook_event_name``, ``tool_name`` (``PreToolUse``),
``agent_id`` (present when the hook fires inside a subagent; absent in the main
session), ``stop_hook_active`` (``Stop``, ``SubagentStop``) and
``agent_transcript_path`` (``SubagentStop``; the subagent's own transcript, in
a ``subagents/`` folder beside the session's). Output: ``additionalContext``
under ``hookSpecificOutput`` (``UserPromptSubmit``, ``SubagentStart``),
``permissionDecision`` ``deny`` plus ``permissionDecisionReason`` (``PreToolUse``)
and ``decision`` ``block`` plus ``reason`` (``Stop``, ``SubagentStop``).

One-time state (the "already said it" markers) is a small empty file per
session under a temp directory, ``$TRELLIS_HOOK_STATE_DIR`` when set; nothing
is written under ``~/.trellis``. ``TRELLIS_HOOKS_DISABLED=1`` turns every hook
into a no-op.

This module imports only the standard library (and :mod:`trellis_wire.loop_text`
at the moment it speaks), so a hook that stays silent starts in milliseconds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

#: The executable the installed settings entries run.
COMMAND = "trellis-hook"

HOOK_PREWORK = "prework"
HOOK_EDIT_GUARD = "edit-guard"
HOOK_GRADE = "grade"
HOOK_NAMES: tuple[str, ...] = (HOOK_PREWORK, HOOK_EDIT_GUARD, HOOK_GRADE)

#: Tool-use count at which a served, ungraded pack is asked about.
DEFAULT_GRADING_THRESHOLD = 10

#: ``subagents``: the edit guard applies to dispatched subagents only.
#: ``all``: it also applies to the main session.
EDIT_DENY_SCOPES: tuple[str, ...] = ("subagents", "all")
DEFAULT_EDIT_SCOPE = "subagents"

#: The file-editing tools the edit guard watches, and the ``PreToolUse``
#: matcher that selects them.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"

#: Trellis tools whose reply can carry a ``**pack_id:**`` line: the header of a
#: pack, the footer ``get_lessons`` prints behind ``TRELLIS_GET_LESSONS_PACK_EVENT``
#: and the pack id ``get_items`` echoes. A literal copy, because this module must
#: start without importing the MCP server (and its dependencies); the server
#: marks each such tool with ``trellis.mcp.app.PACK_TOOL_TAG`` and
#: ``tests/unit/mcp/test_hook_text_claims.py`` fails when this set and the tagged
#: tools differ, so a new pack-returning tool cannot be missed here unnoticed.
PACK_TOOLS = frozenset(
    {
        "get_context",
        "search",
        "get_objective_context",
        "get_task_context",
        "get_sectioned_context",
        "get_lessons",
        "get_items",
    }
)
_FEEDBACK_TOOL = "record_feedback"
_TRACE_TOOL = "save_experience"

#: The ``record_feedback`` fields that attribute an outcome to specific items.
#: ``ignored_item_ids`` and ``followed_advisory_ids`` do not: they contribute no
#: per-item row to the learning join.
_ATTRIBUTION_KEYS = ("helpful_item_ids", "unhelpful_item_ids")

#: A transcript larger than this is not scanned (the hook stays silent), and a
#: scan that runs past the deadline gives up the same way.
MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
SCAN_DEADLINE_SECONDS = 0.8
#: A line longer than this is skipped unparsed and counted in
#: :attr:`TranscriptFacts.skipped_lines`. Without it a single line (a tool result
#: that dumped a large file, say) is read and parsed whole, and the deadline,
#: checked between lines, cannot interrupt that. The lines the hooks read are a
#: ``tool_use`` of a Trellis tool and the reply to a retrieval, both well under
#: this.
MAX_LINE_BYTES = 1024 * 1024
#: The clock is read at least this often by bytes, as well as every 256 lines, so
#: a run of long lines cannot outrun the deadline.
_CLOCK_BYTES = 4 * 1024 * 1024
_CLOCK_LINES = 256
_MAX_STDIN_BYTES = 4 * 1024 * 1024

#: The pack header every retrieval reply prints. A pack id is a ULID.
_PACK_HEADER = re.compile(r"\*\*pack_id:\*\* `([0-9A-HJKMNP-TV-Z]{26})`")
#: ``get_lessons`` prints the same label as a footer on a line of its own, so a
#: reply longer than the header window is also read from its end.
_PACK_FOOTER = re.compile(r"^\*\*pack_id:\*\* `([0-9A-HJKMNP-TV-Z]{26})`", re.MULTILINE)
_HEADER_SEARCH_CHARS = 16384
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: Open flag that keeps ``open`` from blocking on a named pipe; absent on Windows.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_DISABLE_ENV = "TRELLIS_HOOKS_DISABLED"
_STATE_DIR_ENV = "TRELLIS_HOOK_STATE_DIR"


# ---------------------------------------------------------------------------
# Transcript scan
# ---------------------------------------------------------------------------


@dataclass
class TranscriptFacts:
    """What one transcript says about the Trellis loop."""

    #: Every ``tool_use`` block, each counted once.
    tool_uses: int = 0
    #: A pack-returning retrieval tool was called (whatever it returned).
    retrieval_called: bool = False
    #: ``save_experience`` was called and did not error.
    saved_trace: bool = False
    #: Pack ids served and not yet named by attributed feedback, oldest first.
    ungraded: list[str] = field(default_factory=list)
    #: Lines longer than :data:`MAX_LINE_BYTES`, skipped without being parsed.
    skipped_lines: int = 0


#: ``mcp``, the server, the tool: the three segments of an MCP tool name.
_MCP_NAME_PARTS = 3


def _trellis_tool(name: Any) -> str | None:
    """The bare tool name of an MCP tool served by a Trellis server, else ``None``.

    ``mcp__trellis__get_context``, a plugin-scoped
    ``mcp__plugin_x_trellis__get_context`` and a connector's
    ``mcp__claude_ai_Trellis__get_context`` all give ``get_context``. The
    server's registered name must contain ``trellis`` (any case); a Trellis
    server registered as ``memory`` is not recognised.
    """
    if not isinstance(name, str):
        return None
    parts = name.split("__")
    if (
        len(parts) >= _MCP_NAME_PARTS
        and parts[0] == "mcp"
        and "trellis" in parts[1].lower()
    ):
        return parts[-1]
    return None


def _has_attribution(tool_input: Any) -> bool:
    """True when a ``record_feedback`` input names at least one specific item."""
    if not isinstance(tool_input, dict):
        return False
    for key in _ATTRIBUTION_KEYS:
        value = tool_input.get(key)
        if isinstance(value, list) and any(
            isinstance(item, str) and item.strip() for item in value
        ):
            return True
    return False


def _unwrap(text: str) -> str:
    """The markdown inside the ``{"result": "..."}`` envelope MCP replies carry."""
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
        except ValueError:
            return text
        result = decoded.get("result") if isinstance(decoded, dict) else None
        if isinstance(result, str):
            return result
    return text


def _result_text(content: Any) -> str:
    """A ``tool_result`` block's text, however Claude Code shaped its content."""
    if isinstance(content, str):
        return _unwrap(content)
    if isinstance(content, list):
        return "\n".join(
            _unwrap(block["text"])
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _header_pack_id(content: Any) -> str | None:
    """The pack id a retrieval reply names, or ``None`` (an empty pack).

    A pack prints it in a header near the top. ``get_lessons`` prints it in a
    footer, so a reply too long for the header window is also read from its end,
    where only a line that starts with the label counts.
    """
    text = _result_text(content)
    found = _PACK_HEADER.search(text[:_HEADER_SEARCH_CHARS])
    if found:
        return found.group(1)
    if len(text) > _HEADER_SEARCH_CHARS:
        footers = list(_PACK_FOOTER.finditer(text[-_HEADER_SEARCH_CHARS:]))
        if footers:
            return footers[-1].group(1)
    return None


class _Scan:
    """Accumulates :class:`TranscriptFacts` while a transcript streams past."""

    def __init__(self, *, stop_at_retrieval: bool) -> None:
        self.facts = TranscriptFacts()
        self.stop_at_retrieval = stop_at_retrieval
        self._seen_use_ids: set[str] = set()
        #: tool_use id -> (kind, detail), for calls whose result has not arrived.
        self._pending: dict[str, tuple[str, Any]] = {}
        self._served: set[str] = set()

    @property
    def pending_ids(self) -> list[bytes]:
        return [use_id.encode() for use_id in self._pending]

    def block(self, block: dict[str, Any]) -> bool:
        """Fold in one content block. ``True`` when the scan can stop early."""
        kind = block.get("type")
        if kind == "tool_use":
            return self._tool_use(block)
        if kind == "tool_result":
            self._tool_result(block)
        return False

    def _tool_use(self, block: dict[str, Any]) -> bool:
        use_id = block.get("id")
        if not isinstance(use_id, str) or use_id in self._seen_use_ids:
            return False
        self._seen_use_ids.add(use_id)
        self.facts.tool_uses += 1
        tool = _trellis_tool(block.get("name"))
        if tool in PACK_TOOLS:
            self.facts.retrieval_called = True
            self._pending[use_id] = ("pack", None)
            return self.stop_at_retrieval
        if tool == _FEEDBACK_TOOL:
            tool_input = block.get("input")
            pack_arg = (
                tool_input.get("pack_id") if isinstance(tool_input, dict) else None
            )
            self._pending[use_id] = (
                "feedback",
                (pack_arg, _has_attribution(tool_input)),
            )
        elif tool == _TRACE_TOOL:
            self._pending[use_id] = ("trace", None)
        return False

    def _tool_result(self, block: dict[str, Any]) -> None:
        use_id = block.get("tool_use_id")
        pending = self._pending.pop(use_id, None) if isinstance(use_id, str) else None
        if pending is None or block.get("is_error") is True:
            return
        kind, detail = pending
        if kind == "pack":
            pack_id = _header_pack_id(block.get("content"))
            if pack_id is not None and pack_id not in self._served:
                self._served.add(pack_id)
                self.facts.ungraded.append(pack_id)
        else:
            self._apply(kind, detail)

    def _apply(self, kind: str, detail: Any) -> None:
        if kind == "trace":
            self.facts.saved_trace = True
        elif kind == "feedback":
            pack_arg, attributed = detail
            if not attributed:
                return
            if isinstance(pack_arg, str) and pack_arg:
                if pack_arg in self.facts.ungraded:
                    self.facts.ungraded.remove(pack_arg)
            else:
                # Attributed feedback that names no pack grades what was served.
                self.facts.ungraded.clear()

    def finish(self) -> TranscriptFacts:
        """Settle calls whose result never reached the file (still in flight)."""
        for kind, detail in self._pending.values():
            if kind != "pack":
                self._apply(kind, detail)
        self._pending.clear()
        return self.facts


def _open_nonblocking(path: str | os.PathLike[str], flags: int) -> int:
    """``open``'s opener: never block on a file swapped for a pipe after ``stat``."""
    return os.open(path, flags | _O_NONBLOCK)


def _skip_rest_of_line(handle: Any, started: float, deadline_seconds: float) -> bool:
    """Read past the end of an over-long line without keeping it.

    Returns ``False`` when the deadline passes first. Each read is at most
    :data:`MAX_LINE_BYTES`, so memory stays bounded however long the line is.
    """
    while True:
        chunk = handle.readline(MAX_LINE_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return True
        if time.monotonic() - started > deadline_seconds:
            return False


def scan_transcript(  # noqa: PLR0911, PLR0912 - one return per fail-open guard
    path: str | os.PathLike[str] | None,
    *,
    stop_at_retrieval: bool = False,
    include_sidechain: bool = False,
    deadline_seconds: float = SCAN_DEADLINE_SECONDS,
    missing_is_empty: bool = False,
) -> TranscriptFacts | None:
    """Stream a transcript JSONL file and report what it says about the loop.

    Returns ``None`` when the file is missing, unreadable, not a regular file
    (a named pipe would block the read), larger than
    :data:`MAX_TRANSCRIPT_BYTES` or slower to read than ``deadline_seconds``:
    the answer is then unknown, and an unknown answer never produces output. A
    line that is not valid JSON (a truncated last line, say) is skipped, and so
    is a line longer than :data:`MAX_LINE_BYTES`, which is never parsed and is
    counted in :attr:`TranscriptFacts.skipped_lines`.

    ``missing_is_empty`` reads a path that is named but has no file (``ENOENT``
    only) as an empty transcript, facts with nothing in them, instead of
    unknown. Claude Code may not have written a fresh session's file when its
    first prompt arrives; a file that is there but cannot be read (permissions,
    a loop, a parent that is a file, I/O) stays unknown. No path at all is
    unknown either way.

    ``include_sidechain`` keeps entries marked ``isSidechain``. A main-session
    scan leaves them out, so a subagent's retrieval cannot satisfy its parent;
    a subagent's own transcript is all sidechain, so its scan keeps them.

    Lines are filtered on raw bytes before they are parsed, so a large
    transcript costs one substring test per line, not one ``json.loads``.
    """
    if not path:
        return None
    scan = _Scan(stop_at_retrieval=stop_at_retrieval)
    started = time.monotonic()
    try:
        source = Path(path)
        info = source.stat()
        # A FIFO reports size 0 and ``open`` blocks until a writer appears, long
        # past the deadline, which is only checked between lines.
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TRANSCRIPT_BYTES:
            return None
        with open(source, "rb", opener=_open_nonblocking) as handle:
            lines_seen = 0
            unclocked = 0
            while True:
                # One byte past the cap tells a line of exactly the cap from a
                # longer one without reading the longer one whole.
                line = handle.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if lines_seen % _CLOCK_LINES == 0 or unclocked >= _CLOCK_BYTES:
                    unclocked = 0
                    if time.monotonic() - started > deadline_seconds:
                        return None
                lines_seen += 1
                unclocked += len(line)
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    scan.facts.skipped_lines += 1
                    if not _skip_rest_of_line(handle, started, deadline_seconds):
                        return None
                    continue
                wanted = b'"tool_use"' in line or (
                    b'"tool_result"' in line
                    and any(use_id in line for use_id in scan.pending_ids)
                )
                if not wanted:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("isSidechain") is True and not include_sidechain:
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and scan.block(block):
                        return scan.finish()
    except FileNotFoundError:
        return TranscriptFacts() if missing_is_empty else None
    except OSError:
        return None
    return scan.finish()


# ---------------------------------------------------------------------------
# One-time state
# ---------------------------------------------------------------------------


class HookState:
    """Empty marker files, one per thing a hook has already said.

    ``claim`` creates a marker atomically and reports whether this call made
    it, so two hooks racing on the same key cannot both speak. Any failure to
    create one (a read-only temp dir, a directory owned by someone else) reads
    as "already said": a hook that cannot remember that it spoke stays quiet
    rather than repeating itself every turn.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else default_state_dir()

    def _marker(self, key: Sequence[str]) -> Path:
        digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:32]
        return self.root / f"{key[0]}-{digest}"

    def seen(self, *key: str) -> bool:
        return os.path.lexists(self._marker(key))

    def claim(self, *key: str) -> bool:
        marker = self._marker(key)
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if self.root.is_symlink() or (
                hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                return False
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False
        os.close(descriptor)
        return True


def default_state_dir() -> Path:
    """``$TRELLIS_HOOK_STATE_DIR``, else a per-user directory under the temp dir."""
    configured = os.environ.get(_STATE_DIR_ENV)
    if configured:
        return Path(configured)
    owner = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"trellis-hooks-{owner}"


# ---------------------------------------------------------------------------
# The three decisions. Each takes the hook's stdin JSON and returns the JSON the
# hook prints, or ``None`` to stay silent.
# ---------------------------------------------------------------------------


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _agent_id(payload: dict[str, Any]) -> str | None:
    """The subagent id, when the hook fires inside a subagent."""
    value = _text(payload, "agent_id")
    return value if value is not None and _SAFE_ID.fullmatch(value) else None


def _subagent_transcripts(payload: dict[str, Any], agent_id: str) -> list[str]:
    """The paths a subagent's own transcript may be at, in the order to try.

    ``SubagentStop`` names it as ``agent_transcript_path``. A ``PreToolUse``
    inside a subagent does not, so it is derived from the session transcript
    the way Claude Code lays it out: ``<session>/subagents/agent-<id>.jsonl``
    beside ``<session>.jsonl``. An id that already starts ``agent-`` (the hooks
    reference shows one) is also tried as the file name itself, so a build that
    sends the prefix still finds its file. A path that is not there reads as
    unknown.
    """
    named = _text(payload, "agent_transcript_path")
    if named is not None:
        return [named]
    session_transcript = _text(payload, "transcript_path")
    if session_transcript is None:
        return []
    folder = Path(session_transcript).with_suffix("") / "subagents"
    names = [f"agent-{agent_id}.jsonl"]
    if agent_id.startswith("agent-"):
        names.append(f"{agent_id}.jsonl")
    return [str(folder / name) for name in names]


def decide_prework(  # noqa: PLR0911 - one return per fail-open guard
    payload: dict[str, Any], state: HookState
) -> dict[str, Any] | None:
    """The retrieval nudge: once per session, only while nothing was retrieved."""
    from trellis_wire.loop_text import HOOK_PREWORK_NUDGE  # noqa: PLC0415

    event = payload.get("hook_event_name")
    session = _text(payload, "session_id")
    if session is None:
        return None
    if event == "SubagentStart":
        agent = _agent_id(payload)
        if agent is None or state.seen("prework", session, agent):
            return None
        # A subagent has just started, so it has retrieved nothing.
        if not state.claim("prework", session, agent):
            return None
        return _context_output("SubagentStart", HOOK_PREWORK_NUDGE)
    if event != "UserPromptSubmit" or _agent_id(payload) is not None:
        return None
    if state.seen("prework", session):
        return None
    # A named transcript that is not there yet is a session with no retrieval:
    # the first prompt of a fresh session is nudged, not left to prompt two.
    facts = scan_transcript(
        _text(payload, "transcript_path"),
        stop_at_retrieval=True,
        missing_is_empty=True,
    )
    if facts is None or facts.retrieval_called:
        return None
    if not state.claim("prework", session):
        return None
    return _context_output("UserPromptSubmit", HOOK_PREWORK_NUDGE)


def decide_edit_guard(
    payload: dict[str, Any], state: HookState, *, scope: str = DEFAULT_EDIT_SCOPE
) -> dict[str, Any] | None:
    """The soft deny: the first edit of a session that has not retrieved, once."""
    from trellis_wire.loop_text import HOOK_EDIT_DENY_REASON  # noqa: PLC0415

    session = _text(payload, "session_id")
    if (
        session is None
        or payload.get("hook_event_name") != "PreToolUse"
        or payload.get("tool_name") not in EDIT_TOOLS
    ):
        return None
    agent = _agent_id(payload)
    if agent is None and scope != "all":
        return None
    key = (session, agent or "")
    if state.seen("edit-guard", *key):
        return None
    paths: Sequence[str | None] = (
        _subagent_transcripts(payload, agent)
        if agent is not None
        else [_text(payload, "transcript_path")]
    )
    facts = None
    for path in paths:
        facts = scan_transcript(
            path, stop_at_retrieval=True, include_sidechain=agent is not None
        )
        if facts is not None:
            break
    if facts is None or facts.retrieval_called:
        return None
    if not state.claim("edit-guard", *key):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": HOOK_EDIT_DENY_REASON,
        }
    }


def _stop_hook_active(payload: dict[str, Any]) -> bool:
    """True when this ``Stop`` follows a block from a stop hook.

    Claude Code sends a bool. The string ``"true"`` (any case) counts too, and
    that is the fail-safe reading: mistaking a second pass for a first would
    block the model twice in a row, a loop, while mistaking a first pass for a
    second skips one ask. Every other value (``1``, ``"yes"``, ``None``, an
    absent key) reads as not active, as before.
    """
    value = payload.get("stop_hook_active")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def decide_grade(  # noqa: PLR0911 - one return per fail-open guard
    payload: dict[str, Any],
    state: HookState,
    *,
    threshold: int = DEFAULT_GRADING_THRESHOLD,
) -> dict[str, Any] | None:
    """The grading ask: a pack served, no item-attributed feedback, enough work."""
    from trellis_wire.loop_text import hook_grading_ask  # noqa: PLC0415

    # A second pass: the model already saw the ask. Never block twice in a row.
    if _stop_hook_active(payload):
        return None
    session = _text(payload, "session_id")
    event = payload.get("hook_event_name")
    if session is None or event not in ("Stop", "SubagentStop"):
        return None
    if event == "SubagentStop":
        # The subagent's own transcript; ``transcript_path`` is its parent's.
        agent = _agent_id(payload)
        if agent is None:
            return None
        path = _text(payload, "agent_transcript_path")
    else:
        agent = ""
        path = _text(payload, "transcript_path")
    facts = scan_transcript(path, include_sidechain=event == "SubagentStop")
    if facts is None or facts.tool_uses < threshold or not facts.ungraded:
        return None
    pack_id = facts.ungraded[-1]
    if state.seen("grade", session, agent, pack_id):
        return None
    if not state.claim("grade", session, agent, pack_id):
        return None
    # One ask covers every pack still ungraded, so a later turn does not ask
    # again about an older one.
    for older in facts.ungraded[:-1]:
        state.claim("grade", session, agent, older)
    # A dispatched subagent is asked to retrieve and to grade, not to write a
    # trace: its parent session records the task.
    ask_trace = event == "Stop" and not facts.saved_trace
    reason = hook_grading_ask(pack_id, ask_trace=ask_trace)
    if reason is None:
        return None
    return {"decision": "block", "reason": reason}


def _context_output(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """Raises on a bad command line instead of exiting 2, which would block."""

    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=COMMAND,
        description=(
            "Claude Code hook for the Trellis loop. Reads the hook's JSON on stdin, "
            "prints the hook's JSON reply (or nothing), and always exits 0."
        ),
    )
    commands = parser.add_subparsers(dest="hook", required=True)
    commands.add_parser(HOOK_PREWORK, help="nudge a session that has not retrieved")
    guard = commands.add_parser(HOOK_EDIT_GUARD, help="soft-deny the first edit")
    guard.add_argument("--scope", choices=EDIT_DENY_SCOPES, default=DEFAULT_EDIT_SCOPE)
    grade = commands.add_parser(HOOK_GRADE, help="ask for a grade on a served pack")
    grade.add_argument(
        "--threshold", type=int, default=DEFAULT_GRADING_THRESHOLD, metavar="N"
    )
    return parser


def run_hook(
    argv: Sequence[str], stdin_text: str, state: HookState | None = None
) -> dict[str, Any] | None:
    """Decide one hook invocation. Raises on anything it cannot make sense of."""
    args = _build_parser().parse_args(list(argv))
    payload = json.loads(stdin_text)
    if not isinstance(payload, dict):
        return None
    state = state if state is not None else HookState()
    if args.hook == HOOK_PREWORK:
        return decide_prework(payload, state)
    if args.hook == HOOK_EDIT_GUARD:
        return decide_edit_guard(payload, state, scope=args.scope)
    return decide_grade(payload, state, threshold=args.threshold)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one hook. Always returns 0: a hook never breaks a session."""
    try:
        if os.environ.get(_DISABLE_ENV) == "1":
            return 0
        arguments = list(sys.argv[1:] if argv is None else argv)
        if sys.stdin is None or sys.stdin.isatty():
            sys.stderr.write(_build_parser().format_usage())
            return 0
        reply = run_hook(arguments, sys.stdin.read(_MAX_STDIN_BYTES))
        if reply is not None:
            sys.stdout.write(json.dumps(reply))
            sys.stdout.flush()
    # GRACEFUL-DEGRADATION: a hook must never break a session. Exit code 2 and
    # a traceback both reach the model or the user, so every failure (bad
    # arguments, garbage stdin, an unreadable transcript) is a silent exit 0.
    except (Exception, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
