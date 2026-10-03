"""Shared fixtures for session-capture tests.

Everything is fully synthetic (this repo is public): fake transcripts, fake
tool output, fake tokens. Nothing is copied from a real machine, a real
CLAUDE.md, or a real transcript.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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


# --- Real-shape records, for the session outcome -------------------------------
#
# The builders above write the minimum the turn and pack parsers read. The
# outcome also reads what Claude Code writes around a message: ``timestamp``
# on every record, ``message.id`` and ``usage`` on an assistant record, and
# one record per content block. These write that shape, still fully fake.

#: The start of the fake clock every real-shape record is stamped from.
_CLOCK_START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


def stamp(seconds: float) -> str:
    """A record ``timestamp`` *seconds* after a fixed start, in the
    millisecond ``Z`` form Claude Code writes."""
    moment = _CLOCK_START + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def usage_block(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> dict[str, Any]:
    """An assistant message's ``usage``, keyed as the API keys it."""
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "service_tier": "standard",
    }


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use_block(use_id: str, name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}


def bash_block(use_id: str, command: str) -> dict[str, Any]:
    """A ``Bash`` call running *command*, a fake command line."""
    return tool_use_block(use_id, "Bash", command=command, description="fake step")


def result_block(use_id: str, output: Any, *, is_error: bool = False) -> dict[str, Any]:
    """A ``tool_result`` block answering *use_id* with *output* as given."""
    return {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": output,
        "is_error": is_error,
    }


def _record(
    record_type: str,
    at: float | None,
    session_id: str,
    *,
    sidechain: bool,
    **fields: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "parentUuid": None,
        "isSidechain": sidechain,
        "userType": "external",
        "cwd": "/fake/project",
        "sessionId": session_id,
        "version": "2.0.0",
        "gitBranch": "fake-branch",
        "type": record_type,
        "uuid": f"{record_type}-fake",
        **fields,
    }
    if at is not None:
        record["timestamp"] = stamp(at)
    return record


def api_message(
    message_id: str,
    *blocks: dict[str, Any],
    at: float | None,
    usage: dict[str, Any] | None = None,
    session_id: str = "sess-fake-0001",
    sidechain: bool = False,
    api_error: bool = False,
    model: str = "fake-model",
) -> list[dict[str, Any]]:
    """One assistant API message as Claude Code writes it: a record per block.

    Every record carries the same ``message.id`` and its own ``usage``
    snapshot. Input and cache counts repeat on each record and only the last
    record's ``output_tokens`` is the message's total; the earlier ones stand
    at 1 here, as a streaming snapshot does. So a reader that sums usage per
    record counts a two-block message's input twice. ``usage=None`` writes no
    usage at all, the shape of a transcript that did not record it.
    ``api_error`` writes the record Claude Code writes itself when the API
    call failed, which names ``<synthetic>`` as its model; *model* alone set
    to that is the other record it writes in the model's place.
    """
    records = []
    for index, block in enumerate(blocks):
        message: dict[str, Any] = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": "<synthetic>" if api_error else model,
            "content": [block],
            "stop_reason": None,
            "stop_sequence": None,
        }
        if usage is not None:
            snapshot = dict(usage)
            if index < len(blocks) - 1:
                snapshot["output_tokens"] = 1
            message["usage"] = snapshot
        record = _record(
            "assistant",
            at,
            session_id,
            sidechain=sidechain,
            message=message,
            requestId="req-fake",
        )
        if api_error:
            record["isApiErrorMessage"] = True
        records.append(record)
    return records


def prompt(
    text: str,
    *,
    at: float | None,
    session_id: str = "sess-fake-0001",
    sidechain: bool = False,
    **flags: Any,
) -> dict[str, Any]:
    """A user record carrying text. ``isMeta=True`` or ``isCompactSummary=True``
    makes it the harness record of that name instead of a person's prompt."""
    return _record(
        "user",
        at,
        session_id,
        sidechain=sidechain,
        message={"role": "user", "content": text},
        **flags,
    )


def tool_results(
    *blocks: dict[str, Any],
    at: float | None,
    session_id: str = "sess-fake-0001",
    sidechain: bool = False,
    tool_use_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A user record carrying ``tool_result`` blocks, and optionally the
    ``toolUseResult`` Claude Code writes beside them."""
    record = _record(
        "user",
        at,
        session_id,
        sidechain=sidechain,
        message={"role": "user", "content": list(blocks)},
    )
    if tool_use_result is not None:
        record["toolUseResult"] = tool_use_result
    return record


def system_note(*, at: float | None, session_id: str = "sess-fake-0001") -> dict[str, Any]:
    """A non-conversational record that still carries a timestamp."""
    return _record(
        "system",
        at,
        session_id,
        sidechain=False,
        subtype="fake_note",
        content="fake system note",
        level="info",
    )


#: The fake repository every real-shape PR URL points into.
FAKE_PR_URL = "https://github.com/fake-owner/fake-repo/pull/{}"


def worked_session(session_id: str = "sess-worked-0001") -> list[dict[str, Any]]:
    """A session with a known count of everything the outcome records.

    :data:`WORKED_OUTCOME` is the outcome it must parse to. Each line says
    what it adds.
    """
    s = {"session_id": session_id}
    return [
        prompt("Fake task: ship the fake migration.", at=0, **s),  # user turn 1
        prompt("fake caveat", at=1, isMeta=True, **s),  # harness, not a turn
        # Out of order on purpose: the span runs from the earliest stamp.
        system_note(at=-4, **s),
        *api_message(  # turn 1, split over two records
            "msg_fake_01",
            text_block("Running the fake tests."),
            bash_block("toolu_fake_01", "pytest tests/unit -q"),
            at=2,
            usage=usage_block(10, 20, cache_read=100, cache_creation=5),
            **s,
        ),
        tool_results(result_block("toolu_fake_01", "1 failed", is_error=True), at=5, **s),
        *api_message(  # turn 2: a commit that succeeds
            "msg_fake_02",
            bash_block("toolu_fake_02", 'git add -A && git commit -m "fake change"'),
            at=6,
            usage=usage_block(12, 30, cache_read=110),
            **s,
        ),
        tool_results(result_block("toolu_fake_02", "[fake 0000000] fake change"), at=8, **s),
        *api_message(  # turn 3: a commit that fails
            "msg_fake_03",
            bash_block("toolu_fake_03", "git -c user.name=fake commit --amend --no-edit"),
            at=9,
            usage=usage_block(3, 4, cache_read=120),
            **s,
        ),
        tool_results(
            result_block("toolu_fake_03", "fatal: fake failure", is_error=True),
            at=10,
            **s,
        ),
        *api_message(  # turn 4: a PR opened, its URL printed
            "msg_fake_04",
            bash_block(
                "toolu_fake_04",
                "git push -u origin fake && gh pr create --title Fake --body-file /fake/b.md",
            ),
            at=12,
            usage=usage_block(4, 40, cache_read=130, cache_creation=2),
            **s,
        ),
        tool_results(
            result_block("toolu_fake_04", FAKE_PR_URL.format(101) + "\n"), at=15, **s
        ),
        *api_message(  # turn 5: two parallel calls, one a merge of the same PR
            "msg_fake_05",
            bash_block("toolu_fake_05", "gh pr merge 101 --squash"),
            tool_use_block("toolu_fake_06", "Read", file_path="/fake/notes.md"),
            at=16,
            usage=usage_block(6, 50, cache_read=140),
            **s,
        ),
        tool_results(
            result_block(
                "toolu_fake_05", [text_block("Merged " + FAKE_PR_URL.format(101))]
            ),
            at=18,
            **s,
        ),
        tool_results(result_block("toolu_fake_06", "fake file body"), at=18, **s),
        prompt("Thanks, fake follow-up.", at=20, **s),  # user turn 2
        prompt("Fake compaction summary.", at=21, isCompactSummary=True, **s),
        *api_message(  # turn 6
            "msg_fake_06",
            text_block("Done."),
            at=30,
            usage=usage_block(5, 7, cache_read=150),
            **s,
        ),
        {"type": "summary", "summary": "fake summary", "leafUuid": "fake-leaf"},
    ]


#: What :func:`worked_session` parses to. Tokens are each message's own total
#: once: summing every record would add messages 1 and 5's input and cache
#: counts a second time.
WORKED_OUTCOME: dict[str, Any] = {
    "tool_calls": 6,
    "tool_errors": 2,
    "assistant_turns": 6,
    "assistant_turns_with_usage": 6,
    "user_turns": 2,
    "input_tokens": 40,
    "output_tokens": 151,
    "cache_read_input_tokens": 750,
    "cache_creation_input_tokens": 7,
    "wall_clock_seconds": 34.0,
    "commits": 1,
    "prs_created": 1,
    "prs_merged": 1,
    "pr_urls": 1,
    "ended_on_error": False,
    "ended_interrupted": False,
}


def delegating_session(
    parent_id: str = "sess-parent-0001",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A parent session that hands a PR to a sub-agent, and that sub-agent.

    The parent's ``Agent`` result reports the child's totals in
    ``toolUseResult`` and prints the PR URL the child opened. Both belong to
    the child's own transcript, which records them itself.
    """
    p = {"session_id": parent_id}
    c = {"session_id": parent_id, "sidechain": True}
    report = [text_block("Opened " + FAKE_PR_URL.format(202))]
    parent = [
        prompt("Fake task: delegate the fake PR.", at=0, **p),
        *api_message(
            "msg_fake_p1",
            text_block("Delegating."),
            tool_use_block(
                "toolu_fake_agent",
                "Agent",
                description="fake",
                prompt="fake brief",
                subagent_type="general-purpose",
            ),
            at=1,
            usage=usage_block(8, 9, cache_read=10, cache_creation=1),
            **p,
        ),
        tool_results(
            result_block("toolu_fake_agent", report),
            at=40,
            tool_use_result={
                "status": "completed",
                "prompt": "fake brief",
                "agentId": "fake1",
                "content": report,
                "totalDurationMs": 39000,
                "totalTokens": 987654,
                "totalToolUseCount": 77,
                "usage": usage_block(1000, 2000, cache_read=3000, cache_creation=400),
            },
            **p,
        ),
        *api_message(
            "msg_fake_p2",
            text_block("The sub-agent opened the fake PR."),
            at=41,
            usage=usage_block(2, 3, cache_read=20),
            **p,
        ),
    ]
    child = [
        prompt("fake brief", at=2, **c),
        *api_message(
            "msg_fake_c1",
            bash_block("toolu_fake_c1", "git commit -am fake"),
            at=3,
            usage=usage_block(100, 200, cache_read=300, cache_creation=40),
            **c,
        ),
        tool_results(result_block("toolu_fake_c1", "[fake 0000001] fake"), at=5, **c),
        *api_message(
            "msg_fake_c2",
            bash_block("toolu_fake_c2", "gh pr create --fill"),
            at=6,
            usage=usage_block(100, 300, cache_read=400),
            **c,
        ),
        tool_results(result_block("toolu_fake_c2", FAKE_PR_URL.format(202)), at=9, **c),
        *api_message(
            "msg_fake_c3",
            text_block("Opened the fake PR."),
            at=10,
            usage=usage_block(100, 50, cache_read=500),
            **c,
        ),
    ]
    return parent, child


#: What :func:`delegating_session` parses to, parent and child apart.
PARENT_OUTCOME: dict[str, Any] = {
    "tool_calls": 1,
    "tool_errors": 0,
    "assistant_turns": 2,
    "assistant_turns_with_usage": 2,
    "user_turns": 1,
    "input_tokens": 10,
    "output_tokens": 12,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 1,
    "wall_clock_seconds": 41.0,
    "commits": 0,
    "prs_created": 0,
    "prs_merged": 0,
    "pr_urls": 0,
    "ended_on_error": False,
    "ended_interrupted": False,
}
CHILD_OUTCOME: dict[str, Any] = {
    "tool_calls": 2,
    "tool_errors": 0,
    "assistant_turns": 3,
    "assistant_turns_with_usage": 3,
    "user_turns": 1,
    "input_tokens": 300,
    "output_tokens": 550,
    "cache_read_input_tokens": 1200,
    "cache_creation_input_tokens": 40,
    "wall_clock_seconds": 8.0,
    "commits": 1,
    "prs_created": 1,
    "prs_merged": 0,
    "pr_urls": 1,
    "ended_on_error": False,
    "ended_interrupted": False,
}
