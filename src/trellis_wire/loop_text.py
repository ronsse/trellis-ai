"""The retrieve / record / grade loop as text: one source, many renderings.

The loop is told to agents in several places that were written one at a time
and drifted apart: the MCP server instructions, the packaged system prompt, the
CLAUDE.md section, the five-line agent rule, the ``trellis-loop`` skill, and the
``record_feedback`` call every pack footer shows (core and SDK each had a copy).
This module holds that text as named fragments and renders every surface from
them. The surfaces were not unified by this change: each still reads exactly as
it did, and ``tests/unit/mcp/test_loop_text_surfaces.py`` pins every one against
a golden captured before the module existed. Wording is shared only where it
was already identical; where two surfaces had drifted apart they keep two
fragments, side by side here, so unifying them later is an edit to this file.

It is pure (standard library only) so the HTTP-only SDK, which may not import
``trellis``, can read the same footer text core does. The files the package
ships (``trellis_cli/prompts/*.md`` and the ``trellis-loop`` skill) are written
from these renderers by ``scripts/generate_loop_text.py``, and
``tests/unit/test_loop_text_generated.py`` fails when one differs from what the
script produces.
"""

from __future__ import annotations

import re
import textwrap
from typing import TypeGuard

#: Column the shipped markdown is wrapped at.
_WRAP_WIDTH = 75

# ---------------------------------------------------------------------------
# Fragments shared verbatim by more than one surface.
# ---------------------------------------------------------------------------

#: Said by the system prompt and the CLAUDE.md section alike.
NAME_ITEM_IDS = "Feedback that names no item ids does not reach the learning loop."

#: The rule a dispatched subagent must see in its own definition. The
#: ``agent-rule`` prompt variant and the ``trellis-loop`` skill carry it.
AGENT_RULE = (
    "Before reading files, call get_context(intent=<your task>, session_id=<your "
    "agent id or the task id>). Before you finish, grade the pack with "
    "record_feedback(pack_id=..., rating=..., helpful_item_ids=[...], "
    "unhelpful_item_ids=[...]). This is part of every task, not scope creep. An "
    "empty pack is a valid answer: say so, continue, and grade it only if a "
    "pack_id was shown."
)


def _wrap(text: str, *, marker: str = "") -> str:
    """``text`` filled to the shipped width, with a hanging indent under ``marker``."""
    return textwrap.fill(
        text,
        width=_WRAP_WIDTH,
        initial_indent=marker,
        subsequent_indent=" " * len(marker),
        break_long_words=False,
        break_on_hyphens=False,
    )


# ---------------------------------------------------------------------------
# MCP server instructions (``initialize`` response). Unwrapped paragraphs.
# ---------------------------------------------------------------------------

MCP_INSTRUCTION_PARAGRAPHS: tuple[str, ...] = (
    (
        "Trellis — persistent memory for AI agents. Retrieval responses are concise "
        "markdown sized for LLM context windows."
    ),
    "Use it as a loop, not a lookup table:",
    (
        "1. BEFORE non-trivial work, call `get_context(intent=...)`. Prior "
        "traces and precedents are cheap; re-deriving them is not. Pass "
        "`session_id` to dedup across calls in one conversation. `domain=` is "
        "safe to pass — it scopes with default-pass semantics and never "
        "hard-excludes memories that carry no domain. The pack header carries "
        "its `pack_id`, and each item's id is in backticks. An empty pack is a "
        "real answer: it means greenfield, so say so; a `**Withheld:**` line, "
        "when present, counts what matched but was not served."
    ),
    (
        "2. AFTER meaningful work — a fix, a discovery, an instructive failure "
        "— call `save_experience`. Each step's `result` is an object, never a "
        "bare string. Put failures in the step's `error` field; a workaround "
        "the next agent would otherwise rediscover is the most reusable thing "
        "you can leave behind."
    ),
    (
        "3. GRADE what you were served: `record_feedback(pack_id=..., "
        "rating=0.0-1.0)` naming `helpful_item_ids` and `unhelpful_item_ids` "
        "(and `followed_advisory_ids` for advisories you acted on). Feedback "
        "without item ids cannot join to the pack and is invisible to the "
        "learning loop, so a reflexive success=true teaches nothing. An honest "
        "low `rating` is worth more than a polite high one."
    ),
    (
        "To save a long reference document, call `save_memory(content=<full "
        "text>, summary=<your own short summary>, uri=<link to the original>)`: "
        "only the summary is embedded and shown in a pack, and `get_items` "
        "returns the full text."
    ),
    (
        "The common failure is retrieval that never happens, not retrieval "
        "that returns nothing."
    ),
)


def mcp_instructions() -> str:
    """The text the MCP server returns in its ``initialize`` response."""
    return "\n\n".join(MCP_INSTRUCTION_PARAGRAPHS)


# ---------------------------------------------------------------------------
# Packaged prompts (``trellis admin show-prompt``).
# ---------------------------------------------------------------------------

#: Hand-wrapped when it last changed and never refilled, so it is kept as
#: shipped rather than filled: no single width reproduces its line breaks.
_SYSTEM_STEP_SAVE = (
    "2. After the work, call save_experience(trace_json=<a JSON string>) with a\n"
    '   trace: source "agent", intent, context {domain}, 2-6 steps (each has a\n'
    "   step_type and a name; a step's `result` is a JSON object, never a bare\n"
    "   string, and a failure goes in the step's `error` as a string), and an outcome\n"
    "   {status: success|failure|partial, summary}. Record what the next agent\n"
    "   would otherwise have to rediscover: the workaround, the wrong\n"
    "   assumption, the command that worked."
)


def system_prompt() -> str:
    """The system prompt for a client that gets no skills (``system``)."""
    blocks = (
        "You have Trellis memory tools. Use them as a loop, not a lookup.",
        _wrap(
            "Before any non-trivial task, call get_context(intent=<one "
            "sentence: what you are about to do and why>, "
            "session_id=<a stable id for this conversation>, "
            "domain=<area, if you know it>). Read the pack before you read "
            'anything else. Its header carries the pack_id on a "**pack_id:**" '
            'line, and each item is headed "## [type] `item_id`": keep both. '
            'If the response says "No context found", that is a real answer: '
            "proceed, and skip step 3 unless a pack_id was shown.",
            marker="1. ",
        ),
        _SYSTEM_STEP_SAVE,
        _wrap(
            (
                "Grade the pack you were served: record_feedback(pack_id=<from "
                "step 1>, rating=<0.0-1.0>, helpful_item_ids=[ids you used], "
                "unhelpful_item_ids=[ids that cost you time or were wrong]). "
                f"{NAME_ITEM_IDS} If the pack listed advisories and you acted on "
                "one, add followed_advisory_ids=[...]. An honest low rating is "
                "worth more than a polite high one."
            ),
            marker="3. ",
        ),
        _wrap(
            "When you learn a durable fact about a named thing (a service, a "
            "host, a dataset), call save_knowledge(name=..., content=<the "
            'fact and where it lives>, properties={"domain": ...}). It '
            "always creates a new node, so call search(query=<name>) first "
            "if the node may already exist.",
            marker="4. ",
        ),
        _wrap(
            "Retrieval that never happens is the common failure, not "
            "retrieval that returns nothing."
        ),
    )
    return "\n\n".join(blocks) + "\n"


def claude_md_section() -> str:
    """A section for CLAUDE.md or AGENTS.md (``claude-md``)."""
    bullets = (
        (
            "Before non-trivial work, call get_context(intent=<what you are "
            "about to do and why>, session_id=<a stable id for this session>). "
            "Keep the pack_id from the pack header and the item ids in "
            'backticks. "No context found" is a real answer: proceed.'
        ),
        (
            "After meaningful work, call save_experience with a trace. A step's "
            "`result` is a JSON object, never a bare string; a failure goes in "
            "the step's `error` as a string."
        ),
        (
            "Grade the pack: record_feedback(pack_id=..., rating=0.0-1.0, "
            f"helpful_item_ids=[...], unhelpful_item_ids=[...]). {NAME_ITEM_IDS} "
            "Skip it only if no pack_id was shown."
        ),
        (
            "This applies to every dispatched subagent. Retrieval and grading "
            "are part of the task, not extra scope."
        ),
    )
    body = "\n".join(_wrap(bullet, marker="- ") for bullet in bullets)
    return (
        "## Trellis memory\n\n"
        "Trellis memory tools are connected. Use them as a loop, not a "
        "lookup.\n\n" + body + "\n"
    )


def agent_rule() -> str:
    """The short rule for an agent definition (``agent-rule``)."""
    return _wrap(AGENT_RULE) + "\n"


# ---------------------------------------------------------------------------
# The ``trellis-loop`` skill.
# ---------------------------------------------------------------------------

_LOOP_SKILL_DESCRIPTION = (
    "Make Trellis retrieval and grading part of every task, including each "
    "dispatched subagent's. Call get_context before reading files, grade the "
    "pack by item id before finishing."
)

_LOOP_SKILL_TEMPLATE = """---
name: trellis-loop
description: {description}
version: 1.0.0
---

# Trellis Loop

A dispatched subagent loads CLAUDE.md and its agent definition, not skills.
If the instruction to retrieve lives only in a skill, the subagent never sees
it, and an agent told to "do exactly the task in your prompt" treats retrieval
as extra scope. This is the rule that closes that gap:

```
{agent_rule}
```

Add that paragraph to every agent definition you dispatch and to CLAUDE.md, so
that dispatched subagents see it.

For a client that gets no skills at all (a model called with an API key, Cursor
rules, a framework system message), `trellis admin show-prompt` prints the full
system prompt. `--variant claude-md` prints a section for CLAUDE.md or
AGENTS.md, and `--variant agent-rule` prints the paragraph above.
"""


def loop_skill() -> str:
    """The ``trellis-loop`` SKILL.md, carrying :data:`AGENT_RULE` as shipped."""
    return _LOOP_SKILL_TEMPLATE.replace(
        "{description}", _LOOP_SKILL_DESCRIPTION
    ).replace("{agent_rule}", agent_rule().rstrip("\n"))


# ---------------------------------------------------------------------------
# The ``record_feedback`` call a reply shows.
# ---------------------------------------------------------------------------


def feedback_call(pack_id: str) -> str:
    """The grading call core's pack footers show, pack id filled in.

    Leads with ``rating`` and names every field the learning loop joins on, so
    a footer-only reader (an agent that never saw the tool's description) is
    steered to the call the description teaches.
    """
    return (
        'record_feedback(pack_id="' + pack_id + '", rating=..., '
        "helpful_item_ids=[...], unhelpful_item_ids=[...], "
        "followed_advisory_ids=[...])"
    )


def sdk_feedback_call(pack_id: str) -> str:
    """The same call as the HTTP-only SDK's footer prints it, advisories left out."""
    return (
        'record_feedback(pack_id="' + pack_id + '", '
        "rating=..., helpful_item_ids=[...], unhelpful_item_ids=[...])"
    )


# ---------------------------------------------------------------------------
# The reply trailer (``TRELLIS_GRADING_TRAILER``, default off).
# ---------------------------------------------------------------------------

#: Packs are minted with a ULID. The trailer echoes the id only when it has that
#: shape, so no caller text can ride into a reply through it (``get_items``
#: takes the id from the caller).
_PACK_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def is_pack_id(value: str | None) -> TypeGuard[str]:
    """Whether ``value`` has the shape of a pack id the builder mints (a ULID).

    The one test for "safe to echo": a value that passes carries no newline,
    backtick or link syntax. ``get_items`` and the trailers take the id from
    the caller and use this before printing it.
    """
    return value is not None and _PACK_ID.fullmatch(value) is not None


def grading_trailer(pack_id: str | None) -> str | None:
    """One line telling the reader how to grade the pack, or ``None``.

    Names the pack and the minimal grading call: ``pack_id``, ``rating`` and
    the item ids that helped and those that did not. ``rating`` is named
    because a call without it has the server derive one from the cited ids
    (the helpful share), a weaker grade than the one the reader holds.
    ``None`` for anything that is not a well-formed pack id, so a
    caller-supplied value is never echoed.
    """
    if not is_pack_id(pack_id):
        return None
    return (
        f'*Grade this pack: `record_feedback(pack_id="{pack_id}", rating=..., '
        "helpful_item_ids=[...], unhelpful_item_ids=[...])`.*"
    )


# ---------------------------------------------------------------------------
# What the ``trellis-hook`` commands say (``trellis_cli.hooks``).
# ---------------------------------------------------------------------------
#
# The hooks are the loop told to an agent by its harness rather than by its
# prompt, so their sentences live here with the rest of the loop text. They are
# new fragments: none of the surfaces above reads them, and the goldens that pin
# those surfaces are untouched.

#: Added to the context of a session (or a dispatched subagent) that has not
#: retrieved yet.
HOOK_PREWORK_NUDGE = (
    "Trellis memory: this session has not called get_context yet. Before you "
    "read files or edit anything, call get_context(intent=<what you are about "
    "to do and why>, session_id=<your agent id or the task id>). An empty pack "
    "is a valid answer: say so and continue."
)

#: The reason a first edit is refused once, in a session that has not retrieved.
HOOK_EDIT_DENY_REASON = (
    "Trellis memory: this session has not called get_context yet. Call "
    "get_context(intent=<what you are about to do and why>, session_id=<your "
    "agent id or the task id>) first, then retry this edit."
)

#: The trace half of the closing ask, for a session that saved none.
HOOK_TRACE_ASK = (
    "Also record the work with save_experience(trace_json=<a JSON string>), so "
    "the next agent can retrieve it; a step's `result` is a JSON object, never "
    "a bare string."
)

#: Closes the ask, so a session with nothing to grade can say so and stop.
HOOK_NOT_WARRANTED = (
    "If a step is not warranted here, say why in one line, then finish."
)


def hook_grading_ask(pack_id: str | None, *, ask_trace: bool = False) -> str | None:
    """The one message that asks a session to grade the pack it was served.

    Names the pack and the call that attributes feedback to items. With
    ``ask_trace`` it also asks for a trace, so a session is asked once and not
    twice. ``None`` for anything that is not a well-formed pack id, so no
    transcript text is ever echoed into a reply.
    """
    if not is_pack_id(pack_id):
        return None
    parts = [
        (
            f"Trellis memory: you were served pack {pack_id} and have not graded "
            f"it. Grade it with {feedback_call(pack_id)}, naming only item ids you "
            "read: the ones that helped and the ones that cost you time or were "
            f"wrong. {NAME_ITEM_IDS} An honest low rating is worth more than a "
            "polite high one."
        )
    ]
    if ask_trace:
        parts.append(HOOK_TRACE_ASK)
    parts.append(HOOK_NOT_WARRANTED)
    return " ".join(parts)


__all__ = [
    "AGENT_RULE",
    "HOOK_EDIT_DENY_REASON",
    "HOOK_NOT_WARRANTED",
    "HOOK_PREWORK_NUDGE",
    "HOOK_TRACE_ASK",
    "MCP_INSTRUCTION_PARAGRAPHS",
    "NAME_ITEM_IDS",
    "agent_rule",
    "claude_md_section",
    "feedback_call",
    "grading_trailer",
    "hook_grading_ask",
    "is_pack_id",
    "loop_skill",
    "mcp_instructions",
    "sdk_feedback_call",
    "system_prompt",
]
