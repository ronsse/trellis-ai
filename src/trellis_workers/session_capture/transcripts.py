"""F8-safe discovery and parsing of Claude Code transcript JSONL.

Claude Code writes one JSONL file per session under
``<root>/<project>/<session-uuid>.jsonl``. The schema has several traps the
#255 guide calls out (verified live):

* **Malformed lines** — a partially flushed final line, a truncated write.
  A single bad line must never abort the parse: it is skipped and counted.
* **Unknown record types** — the format churns (new ``type`` values appear).
  The parser tolerates them (counted as ``unknown_records``), never crashes.
* **Sidechains** — ``isSidechain: true`` records are sub-agent (Task) threads
  interleaved into the file; their turns are counted but excluded from the
  main-thread salient text so the digest does not assume one linear
  conversation.
* **Summaries / compaction** — ``type: "summary"`` records and compaction
  boundaries are structural artifacts, not turns; counted and skipped.
* **``tool_result`` content arrays** — a tool result's ``content`` may be a
  bare string *or* a list of typed blocks. Either way it is raw tool output
  (``op read`` results, env dumps) and is **never** copied into the digest;
  only its ``is_error`` flag survives.

The output is a :class:`~trellis_workers.session_capture.models.SessionDigest`
that carries only natural-language turns, tool *names*, and structural signals.
"""

from __future__ import annotations

import json
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
    ToolCall,
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


def is_ephemeral_project(path: Path) -> bool:
    """Whether a transcript belongs to a session run in a throwaway directory.

    Claude Code names each project directory after the session's working
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

    Matched on the temp *root* rather than the ``tmpXXXXXXXX`` name shape:
    the point is that the work had no durable home, which is equally true of
    a hand-named directory under ``/tmp``.
    """
    project = path.parent.name
    return any(
        project == root or project.startswith(f"{root}-")
        for root in _EPHEMERAL_PROJECT_ROOTS
    )


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


def _tool_result_errored(content: Any) -> bool:
    """Whether a user message carries a ``tool_result`` block flagged errored.

    Only the boolean ``is_error`` flag is read — the result ``content`` (raw
    tool output) is never touched.
    """
    if not isinstance(content, list):
        return False
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and bool(block.get("is_error"))
        ):
            return True
    return False


def _add_turns(digest: SessionDigest, role: str, content: Any) -> None:
    """Append every natural-language block of *content* as a turn, in order."""
    for text in _extract_text(content):
        digest.add_turn(role, text)


def _collect_tool_names(digest: SessionDigest, content: Any) -> None:
    """Record tool *names* from an assistant message. Never their inputs."""
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                digest.tool_calls.append(ToolCall(name=name))


def _handle_record(record: dict[str, Any], digest: SessionDigest) -> None:
    """Fold one parsed record into *digest*. Never raises on shape."""
    record_type = record.get("type")

    if record_type == _TYPE_SUMMARY:
        digest.summary_records += 1
        return

    if record.get("isSidechain"):
        # Sub-agent thread: count it, but keep its turns out of the
        # main-thread salient text (no continuity assumption).
        digest.sidechain_records += 1
        return

    message = record.get("message")
    if not isinstance(message, dict):
        if record_type not in (_TYPE_USER, _TYPE_ASSISTANT):
            digest.unknown_records += 1
        return
    content = message.get("content")

    if record_type == _TYPE_USER:
        _add_turns(digest, ROLE_USER, content)
        if _tool_result_errored(content):
            digest.has_error = True
    elif record_type == _TYPE_ASSISTANT:
        _add_turns(digest, ROLE_ASSISTANT, content)
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

    if detect_correction(digest.user_texts):
        digest.has_correction = True
    if not digest.has_error and detect_error_markers(
        [*digest.user_texts, *digest.assistant_texts]
    ):
        digest.has_error = True
    return digest
