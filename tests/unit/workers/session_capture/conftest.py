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
        record if isinstance(record, str) else json.dumps(record)
        for record in records
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
    text: str, tool_name: str | None = None, session_id: str = "sess-fake-0001"
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if tool_name is not None:
        content.append(
            {"type": "tool_use", "id": "t-fake", "name": tool_name, "input": {}}
        )
    return {
        "type": "assistant",
        "uuid": "a-fake",
        "sessionId": session_id,
        "message": {"role": "assistant", "content": content},
    }


def tool_result_turn(
    *, is_error: bool, session_id: str = "sess-fake-0001"
) -> dict[str, Any]:
    """A user record carrying a tool_result content array (F8 trap)."""
    return {
        "type": "user",
        "uuid": "u-fake-tr",
        "sessionId": session_id,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t-fake",
                    "content": [{"type": "text", "text": "raw tool output here"}],
                    "is_error": is_error,
                }
            ],
        },
    }
