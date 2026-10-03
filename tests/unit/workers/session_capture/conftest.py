"""Shared fixtures for session-capture tests.

Everything is fully synthetic (this repo is public): fake transcripts, fake
tool output, fake tokens. Nothing is copied from a real machine, a real
CLAUDE.md, or a real transcript.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trellis.llm.types import LLMResponse


class FakeLLMClient:
    """A deterministic stand-in for the local distillation/reconcile model.

    ``responses`` is a list of raw strings returned in order; once exhausted
    the last one repeats. ``calls`` records every prompt for assertions.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls: list[list[Any]] = []

    async def generate(
        self,
        *,
        messages: list[Any],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return LLMResponse(content=self._responses[idx], model="fake-local")


class BrokenLLMClient:
    """A client whose model is 'down' — every call raises."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        msg = "simulated model outage"
        raise RuntimeError(msg)


def candidates_json(*candidates: dict[str, Any]) -> str:
    """Serialize distiller candidate dicts to the model's JSON-array shape."""
    return json.dumps(list(candidates))


def good_candidate(**overrides: Any) -> dict[str, Any]:
    """A synthetic candidate that clears the worthiness gate."""
    base: dict[str, Any] = {
        "title": "Widget deploy needs the migrate flag first",
        "memory": (
            "The frobnicator service must run its schema migration before the "
            "web tier boots, or the boot probe fails with a missing-table "
            "error. Run the migrate step first in the deploy playbook."
        ),
        "memory_type": "procedural",
        "signal": "failure",
        "evidence": "deploy/playbook.yml step order; observed boot-probe error",
        "non_derivable": True,
        "durable": True,
        "actionable": True,
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


def write_transcript(path: Path, records: list[dict[str, Any] | str]) -> None:
    """Write JSONL records to *path*. A ``str`` entry is written verbatim
    (used to inject a deliberately malformed line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        record if isinstance(record, str) else json.dumps(record) for record in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def user_turn(text: str, session_id: str = "sess-fake-0001") -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": "u-fake",
        "sessionId": session_id,
        "message": {"role": "user", "content": text},
    }


def assistant_turn(
    text: str,
    tool_name: str | None = None,
    session_id: str = "sess-fake-0001",
    *,
    use_id: str = "t-fake",
) -> dict[str, Any]:
    """One assistant record, optionally carrying a single ``tool_use``.

    ``use_id`` is a parameter rather than a constant because the error join
    is keyed on it: a fixture set where every call shares one id cannot
    distinguish a join that resolves correctly from one that marks
    everything, which is #447's "pool too uniform to see the field under
    test" in its transcript form.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if tool_name is not None:
        content.append(
            {"type": "tool_use", "id": use_id, "name": tool_name, "input": {}}
        )
    return {
        "type": "assistant",
        "uuid": "a-fake",
        "sessionId": session_id,
        "message": {"role": "assistant", "content": content},
    }


def assistant_tools(
    *calls: tuple[str, str], session_id: str = "sess-fake-0001"
) -> dict[str, Any]:
    """An assistant record carrying several ``tool_use`` blocks.

    Each entry is ``(tool_name, use_id)``. Claude Code emits parallel tool
    calls in one assistant message, so a per-call rollup has to survive it.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": "working"}]
    content.extend(
        {"type": "tool_use", "id": use_id, "name": name, "input": {}}
        for name, use_id in calls
    )
    return {
        "type": "assistant",
        "uuid": "a-fake-multi",
        "sessionId": session_id,
        "message": {"role": "assistant", "content": content},
    }


def tool_result_turn(
    *,
    is_error: bool,
    session_id: str = "sess-fake-0001",
    tool_use_id: str | None = "t-fake",
) -> dict[str, Any]:
    """A user record carrying a tool_result content array (F8 trap).

    ``tool_use_id=None`` omits the key entirely, which is the shape a
    compaction boundary or a truncated write leaves behind: an errored
    result whose call this file never held.
    """
    block: dict[str, Any] = {
        "type": "tool_result",
        "content": [{"type": "text", "text": "raw tool output here"}],
        "is_error": is_error,
    }
    if tool_use_id is not None:
        block["tool_use_id"] = tool_use_id
    return {
        "type": "user",
        "uuid": "u-fake-tr",
        "sessionId": session_id,
        "message": {"role": "user", "content": [block]},
    }


#: Made-up pack ids in the shape the retrieval formatters print: 26
#: characters of Crockford base32. Distinct in their last character, so an
#: assertion naming which ids a session recorded cannot pass on the wrong one.
PACK_IDS = tuple(f"01FAKEPACK000000000000000{suffix}" for suffix in "ABCDEFGH")


def pack_markdown(
    pack_id: str | None,
    *,
    title: str = "# Context for: fake intent about the widget deploy",
    banner: bool = False,
) -> str:
    """A retrieval tool's markdown, laid out as the formatters lay it out.

    Title line, then the ``**pack_id:**`` header when there is a pack, a
    blank line, then the items. ``banner`` prepends the capture-health
    warning the MCP server puts above a pack while capture is failing — a
    blockquote paragraph, never a ``#`` line. Every word of it is synthetic.
    """
    lines = [title]
    if pack_id is not None:
        lines.append(f"**pack_id:** `{pack_id}`")
    lines += ["", "## Precedents", "- fake item: the frobnicator boots after migrate"]
    text = "\n".join(lines)
    if banner:
        text = "> **WARNING: memory capture is failing.** Fake banner text.\n\n" + text
    return text


def mcp_envelope(markdown: str) -> str:
    """The string Claude Code records for a FastMCP tool's result."""
    return json.dumps({"result": markdown})


def pack_result_turn(
    tool_use_id: str,
    content: Any,
    *,
    is_error: bool = False,
    session_id: str = "sess-fake-0001",
) -> dict[str, Any]:
    """A user record answering one tool call with *content* as given."""
    return {
        "type": "user",
        "uuid": f"u-fake-{tool_use_id}",
        "sessionId": session_id,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }
