"""The Trellis loop as text, shipped as package data.

A skill reaches an agent that loads skills. Two kinds of consumer load none:
a model called with an API key plus Trellis tools (Messages API, OpenAI API,
Cursor rules, a framework's system message), and a dispatched subagent, which
loads CLAUDE.md and its own agent definition. This package holds the text
those consumers are given, in three variants of one loop:

* ``system``: the system prompt for an API-key client;
* ``claude-md``: a section for CLAUDE.md or AGENTS.md;
* ``agent-rule``: the five-line rule for an agent definition.

The files are read through :mod:`importlib.resources`, so they come from an
installed wheel as well as a checkout. They sit beside ``trellis_cli.skills``
rather than inside it: that package is one directory per installable skill,
and ``trellis admin install-skills`` copies each of them into a skills
directory. Printed by ``trellis admin show-prompt``; the agent rule is also
printed by ``install-skills`` and carried by the ``trellis-loop`` skill.
``tests/unit/cli/test_prompts.py`` pins every tool and field name in the text
to the schema it names.
"""

from __future__ import annotations

from importlib.resources import files

#: Variant names, in the order ``show-prompt`` documents them.
PROMPT_VARIANTS: tuple[str, ...] = ("system", "claude-md", "agent-rule")

#: What ``trellis admin show-prompt`` prints without ``--variant``.
DEFAULT_VARIANT = "system"


def load_prompt(variant: str) -> str:
    """Return the text of ``variant``, exactly as shipped.

    The file's own trailing newline is part of the text, so printing it with
    ``end=""`` reproduces the file byte for byte.

    Raises:
        ValueError: If ``variant`` is not one of :data:`PROMPT_VARIANTS`.
    """
    if variant not in PROMPT_VARIANTS:
        msg = f"unknown prompt variant {variant!r} (expected one of {PROMPT_VARIANTS})"
        raise ValueError(msg)
    return (files("trellis_cli.prompts") / f"{variant}.md").read_text(encoding="utf-8")


def agent_rule_text() -> str:
    """The agent rule without its trailing newline, for splicing into a brief."""
    return load_prompt("agent-rule").rstrip("\n")
