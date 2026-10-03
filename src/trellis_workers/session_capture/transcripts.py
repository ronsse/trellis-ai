"""F8-safe discovery and parsing of Claude Code transcript JSONL.

Claude Code writes one JSONL file per session under
``<root>/<project>/<session-uuid>.jsonl``. The schema has several traps the
#255 guide calls out (verified live):

* **Malformed lines** — a partially flushed final line, a truncated write.
  A single bad line must never abort the parse: it is skipped and counted.
* **Unknown record types** — the format churns (new ``type`` values appear).
  The parser tolerates them (counted as ``unknown_records``), never crashes.
* **Sidechains** — ``isSidechain: true`` records are sub-agent (Task) threads.
  When they are *interleaved* into a main session's file, their turns are
  excluded so the digest does not assume one linear conversation. When the
  whole file is sidechain — Claude Code writes each sub-agent thread to its
  own ``agent-*.jsonl`` — that file *is* the sub-agent's conversation and is
  kept, flagged ``is_subagent`` so nothing downstream mistakes the
  orchestrator's prompt for a person's. The blanket skip discarded 61% of a
  real corpus to guard against a mixed shape that occurred zero times
  (#332). Resolved by
  :meth:`~trellis_workers.session_capture.models.SessionDigest.resolve_thread`.
* **Summaries / compaction** — ``type: "summary"`` records and compaction
  boundaries are structural artifacts, not turns; counted and skipped.
* **``tool_result`` content arrays** — a tool result's ``content`` may be a
  bare string *or* a list of typed blocks. Either way it is raw tool output
  (``op read`` results, env dumps) and is **never** copied into the digest.
  What survives is its ``is_error`` flag, its ``tool_use_id`` and, for a
  Trellis retrieval tool's result alone, the pack id printed in its header.
  The use id is an opaque correlation handle, read because it attributes
  the failure to the call that produced it, which is the difference between
  "this session errored" and "``Bash`` errored 38 times out of 412" (#306).
  The pack id is a generated identifier, kept only once it matches the
  26-character alphabet it is generated in, so nothing else in the result
  rides along with it. See :func:`_collect_pack_ids`.

The output is a :class:`~trellis_workers.session_capture.models.SessionDigest`
that carries only natural-language turns, tool *names* with per-call error
flags, the pack ids the session was served, and structural signals.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from trellis_workers.session_capture.gating import (
    detect_correction,
    detect_error_markers,
)
from trellis_workers.session_capture.models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    SessionDigest,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

#: Transcript file glob, relative to the projects root.
_TRANSCRIPT_GLOB = "**/*.jsonl"

#: Project-directory names denoting a session whose working directory was a
#: system temp root. Claude Code flattens the cwd's separators into the
#: directory name, so ``/tmp/x`` is ``-tmp-x``. See
#: :func:`is_ephemeral_project`.
_EPHEMERAL_PROJECT_ROOTS = ("-tmp", "-var-tmp", "-private-tmp", "-private-var-tmp")

#: Record ``type`` values the parser understands. Anything else is counted as
#: ``unknown_records`` (forward compatibility) rather than treated as an error.
_TYPE_USER = "user"
_TYPE_ASSISTANT = "assistant"
_TYPE_SUMMARY = "summary"

#: The Trellis MCP tools whose result prints a ``**pack_id:**`` header on
#: the line after its title (``trellis.mcp.formatters``). ``get_items``
#: echoes the id of the pack it expands, so its header names a pack the
#: session was already served. ``get_file_context`` prints no header.
_PACK_TOOLS = frozenset(
    {
        "get_context",
        "search",
        "get_objective_context",
        "get_task_context",
        "get_sectioned_context",
        "get_items",
    }
)

#: The header's label. A result that mentions it but yields no pack id is
#: counted as unparsed rather than read as "served no pack".
_PACK_LABEL = "**pack_id:**"

#: The whole header line. A pack id is a ULID: 26 characters of Crockford
#: base32, which has no ``I``, ``L``, ``O`` or ``U``.
_PACK_HEADER = re.compile(r"\*\*pack_id:\*\* `([0-9A-HJKMNP-TV-Z]{26})`")


def discover_sessions(root: Path) -> list[Path]:
    """Return every transcript file under *root*, sorted for stable order.

    A missing root yields an empty list — the sweep is a no-op on a machine
    with no Claude Code history yet, never an error.

    Discovery is deliberately unfiltered: :func:`is_ephemeral_project` is
    applied by the sweep so the skip lands in the report as its own count
    rather than disappearing here. A capture gap must not be reportable as a
    sampling decision.
    """
    if not root.exists():
        return []
    return sorted(root.glob(_TRANSCRIPT_GLOB))


def is_ephemeral_project(path: Path, root: Path) -> bool:
    """Whether a transcript belongs to a session run in a throwaway directory.

    Claude Code names each *project* directory after the session's working
    directory with the separators flattened, so ``/tmp/tmpa1b2c3`` becomes
    ``-tmp-tmpa1b2c3``. A session whose cwd was a temp directory has no
    durable project for a memory to be *about*: the directory is gone, and
    nothing a future agent does will return to it.

    This is not hypothetical tidiness. Tooling that shells out to Claude in a
    scratch directory produces transcripts whose subject matter is whatever
    was pasted in — measured on a real corpus, every memory distilled from
    these directories was third-party document content rather than anything
    about the operator's own systems, and they were 29% of a first capture
    run.

    Resolved against *root* rather than reading ``path.parent``: a main
    session sits directly in its project directory, but a sub-agent
    transcript is nested at
    ``<project>/<parent-session>/subagents/[workflows/<wf>/]agent-*.jsonl``,
    where the immediate parent is ``subagents`` and carries no cwd
    information at all. Reading the parent would silently exempt every
    sub-agent transcript from this rule (#332).

    Matched on the temp *root* rather than the ``tmpXXXXXXXX`` name shape:
    the point is that the work had no durable home, which is equally true of
    a hand-named directory under ``/tmp``.
    """
    try:
        project = path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        # Outside the sweep root, or the root itself — no project to judge.
        return False
    return any(
        project == temp_root or project.startswith(f"{temp_root}-")
        for temp_root in _EPHEMERAL_PROJECT_ROOTS
    )


def parent_session_id(path: Path, root: Path) -> str | None:
    """The session a sub-agent transcript belongs to, or ``None``.

    Claude Code nests a sub-agent's transcript at
    ``<project>/<parent-session>/subagents/[workflows/<wf>/]agent-*.jsonl``,
    so the parent is the directory that holds ``subagents``. A main session,
    a path outside *root* or any other layout has none.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    match parts:
        case (_, parent, "subagents", *_, _):
            return parent
        case _:
            return None


def _extract_text(content: Any) -> list[str]:
    """Pull natural-language text out of a message ``content`` field.

    Handles the two shapes Claude Code emits — a bare string, or a list of
    typed blocks — and returns only ``text`` blocks. ``tool_use`` and
    ``tool_result`` blocks are intentionally dropped: tool inputs and outputs
    are exactly where secrets live.
    """
    if isinstance(content, str):
        stripped = content.strip()
        return [stripped] if stripped else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


def _errored_result_ids(content: Any) -> list[str | None]:
    """``tool_use_id``s of every errored ``tool_result`` in a user message.

    One entry per errored result, so the count is the number of failures and
    not the number of distinct tools. ``None`` marks an errored result whose
    id is absent or unusable — it still counts as a failure, it just cannot
    be attributed to a call.

    Only the boolean ``is_error`` flag and the id are read — the result
    ``content`` (raw tool output) is never touched.
    """
    if not isinstance(content, list):
        return []
    ids: list[str | None] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and bool(block.get("is_error"))
        ):
            use_id = block.get("tool_use_id")
            ids.append(use_id if isinstance(use_id, str) and use_id else None)
    return ids


def _is_pack_tool(name: str) -> bool:
    """Whether *name* is a Trellis retrieval tool that prints a pack id.

    MCP tools are named ``mcp__<server>__<tool>``. The local stdio server
    is ``trellis`` and the claude.ai connector carries ``trellis`` inside a
    longer server name, so the server is matched on containing it. A
    same-named tool on any other server served no Trellis pack.
    """
    if not name.startswith("mcp__"):
        return False
    server, _, tool = name.removeprefix("mcp__").rpartition("__")
    return "trellis" in server.lower() and tool in _PACK_TOOLS


def _unwrap(text: str) -> str:
    """The markdown inside a FastMCP ``{"result": ...}`` envelope, else *text*.

    A truncated envelope does not decode and comes back as it is, so the
    caller sees the label without a header it can read.
    """
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        return text
    result = decoded.get("result") if isinstance(decoded, dict) else None
    return result if isinstance(result, str) else text


def _result_text(content: Any) -> str | None:
    """A tool result's text, unwrapped; ``None`` for a shape with no text."""
    if isinstance(content, str):
        return _unwrap(content)
    if isinstance(content, list):
        texts = [
            _unwrap(block["text"])
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "\n".join(texts) if texts else None
    return None


def _header_pack_id(text: str) -> str | None:
    """The pack id in the header of a retrieval result's title paragraph.

    The formatters print the title, the ``**pack_id:**`` line, an optional
    note, then a blank line before any item. So the header is looked for
    from the first ``# `` line to the first blank line, which steps over
    the capture banner (a blockquote above the title) and an intent that
    wraps the title onto a second line. A header-shaped line further down
    sits inside an item, written by some other session, and is not read.
    """
    lines = text.split("\n")
    title = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if title is None:
        return None
    for line in lines[title + 1 :]:
        if not line.strip():
            return None
        found = _PACK_HEADER.fullmatch(line)
        if found:
            return found.group(1)
    return None


def _collect_pack_ids(digest: SessionDigest, content: Any) -> None:
    """Record the pack id each Trellis retrieval result in *content* names.

    A result is attributed to its call through ``tool_use_id``, so only a
    call this file holds can be read. Every retrieval result is counted,
    errored ones separately; a result that should have named a pack and did
    not parse is counted as unparsed, never mistaken for an empty pack. The
    id is the only text kept, and only once it matches :data:`_PACK_HEADER`.
    """
    if not isinstance(content, list):
        return
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        use_id = block.get("tool_use_id")
        name = digest.tool_name(use_id if isinstance(use_id, str) else None)
        if name is None or not _is_pack_tool(name):
            continue
        digest.retrieval_results += 1
        if block.get("is_error"):
            digest.retrieval_errors += 1
            continue
        text = _result_text(block.get("content"))
        pack_id = _header_pack_id(text) if text is not None else None
        if pack_id is not None:
            if pack_id not in digest.pack_ids:
                digest.pack_ids.append(pack_id)
        elif text is None or _PACK_LABEL in text:
            digest.pack_ids_unparsed += 1


def _add_turns(
    digest: SessionDigest, role: str, content: Any, *, sidechain: bool = False
) -> None:
    """Append every natural-language block of *content* as a turn, in order."""
    for text in _extract_text(content):
        digest.add_turn(role, text, sidechain=sidechain)


def _collect_tool_names(digest: SessionDigest, content: Any) -> None:
    """Record tool *names* from an assistant message. Never their inputs.

    The block's ``id`` rides along so the ``tool_result`` answering this
    call can be attributed back to it. An id is an opaque correlation
    handle, not payload.
    """
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                use_id = block.get("id")
                digest.record_tool_use(
                    name, use_id if isinstance(use_id, str) else None
                )


def _handle_record(record: dict[str, Any], digest: SessionDigest) -> None:
    """Fold one parsed record into *digest*. Never raises on shape."""
    record_type = record.get("type")

    if record_type == _TYPE_SUMMARY:
        digest.summary_records += 1
        return

    sidechain = bool(record.get("isSidechain"))
    if sidechain:
        digest.sidechain_records += 1

    message = record.get("message")
    if not isinstance(message, dict):
        if record_type not in (_TYPE_USER, _TYPE_ASSISTANT):
            digest.unknown_records += 1
        return
    content = message.get("content")

    if record_type == _TYPE_USER:
        _add_turns(digest, ROLE_USER, content, sidechain=sidechain)
        errored_ids = _errored_result_ids(content)
        if errored_ids:
            # Unconditional, and deliberately not derived from the join
            # below: an errored result whose call this file never held
            # still means the session hit an error. See
            # :meth:`SessionDigest.mark_tool_result`.
            digest.has_error = True
        for use_id in errored_ids:
            digest.mark_tool_result(use_id, errored=True)
        _collect_pack_ids(digest, content)
    elif record_type == _TYPE_ASSISTANT:
        _add_turns(digest, ROLE_ASSISTANT, content, sidechain=sidechain)
        _collect_tool_names(digest, content)
    else:
        digest.unknown_records += 1


def parse_session(path: Path) -> SessionDigest:
    """Parse one transcript file into a secret-free :class:`SessionDigest`.

    Robust by construction: an unreadable file yields an empty digest with a
    single malformed-line marker; a bad JSON line or an unexpected record
    shape is skipped and counted, never fatal.
    """
    digest = SessionDigest(session_id=path.stem, source_path=str(path))
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        logger.warning("transcript_unreadable", path=str(path))
        digest.malformed_lines += 1
        return digest

    with handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            digest.record_count += 1
            try:
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    digest.malformed_lines += 1
                    continue
                _handle_record(record, digest)
            except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                # SKIP + COUNT: one bad line never aborts a session parse.
                digest.malformed_lines += 1

    # Decide which turns are this transcript's conversation before the signal
    # detectors read them: a mixed file keeps its main thread, a dedicated
    # sub-agent file keeps its own (#332).
    digest.resolve_thread()

    if detect_correction(digest.user_texts):
        digest.has_correction = True
    if not digest.has_error and detect_error_markers(
        [*digest.user_texts, *digest.assistant_texts]
    ):
        digest.has_error = True
    return digest
