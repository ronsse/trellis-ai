"""CLAUDE.md is loaded into every agent's context, so its size is a per-call cost.

Keep it to rules and pointers; reasoning and history go in
docs/design/claude-md-rationale.md (see CLAUDE.md § "Editing this file").
"""

from __future__ import annotations

from pathlib import Path

CLAUDE_MD = Path(__file__).resolve().parents[2] / "CLAUDE.md"

# Raise only after making room: move reasoning to the rationale doc first.
MAX_BYTES = 24_000


def test_claude_md_stays_within_budget() -> None:
    size = len(CLAUDE_MD.read_bytes())
    assert size <= MAX_BYTES, (
        f"CLAUDE.md is {size} bytes, over the {MAX_BYTES}-byte budget. "
        "Move reasoning, measurements and history to "
        "docs/design/claude-md-rationale.md and keep one line here "
        '(CLAUDE.md § "Editing this file").'
    )
