"""AGENTS.md is loaded into every agent's context, so its size is a per-call cost.

Keep it to rules and pointers; reasoning and history go in
docs/design/claude-md-rationale.md (see AGENTS.md § "Editing this file").

``AGENTS.md`` is the instruction file of record: Codex, Cursor and Grok Build
read it by name. Claude Code reads ``CLAUDE.md``, which is a one-line stub
importing it. The stub is a regular file rather than a symlink, and holds
nothing else, because a harness that reads both names would otherwise load
the rules twice.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS_MD = REPO / "AGENTS.md"
CLAUDE_MD = REPO / "CLAUDE.md"

# Raise only after making room: move reasoning to the rationale doc first.
MAX_BYTES = 24_000

# Codex stops reading a project instruction file here (``project_doc_max_bytes``),
# so a budget above it would let the tail of the file go unread without a failure.
CODEX_READ_LIMIT = 32 * 1024


def test_agents_md_stays_within_budget() -> None:
    size = len(AGENTS_MD.read_bytes())
    assert size <= MAX_BYTES, (
        f"AGENTS.md is {size} bytes, over the {MAX_BYTES}-byte budget. "
        "Move reasoning, measurements and history to "
        "docs/design/claude-md-rationale.md and keep one line here "
        '(AGENTS.md § "Editing this file").'
    )


def test_budget_stays_below_the_codex_read_limit() -> None:
    assert MAX_BYTES < CODEX_READ_LIMIT


def test_claude_md_is_only_the_import_stub() -> None:
    assert not CLAUDE_MD.is_symlink(), (
        "CLAUDE.md must be a regular file: as a symlink to AGENTS.md, a "
        "harness that reads both names loads the rules twice."
    )
    assert CLAUDE_MD.read_text(encoding="utf-8") == "@AGENTS.md\n", (
        "CLAUDE.md is a one-line stub that imports AGENTS.md. Put the rule "
        "in AGENTS.md; content added here is loaded only by Claude Code."
    )
