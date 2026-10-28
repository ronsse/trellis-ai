"""Enforcement: a setup doc that teaches ``record_feedback`` must teach item ids.

An MCP client with no skills is steered by whatever setup page its owner
copied a snippet from. ``record_feedback`` with no ``helpful_item_ids`` /
``unhelpful_item_ids`` is accepted and then joins to nothing: the learning
loop attributes outcomes to *items*, and a bare rating names none. A snippet
that says "call ``record_feedback``" and stops therefore trains every client
that copies it to send feedback the loop cannot use.

The rule is textual, on purpose. The failure is in prose, so there is no AST
to walk: **any ``.md`` file under ``docs/getting-started/`` or ``examples/``
that mentions ``record_feedback`` must also mention ``helpful_item_ids``.**

It is scanned from the directory tree, never from a list of known files, so a
new setup page is covered the day it lands.

``_PENDING`` held one page while its rewrite was owned by another change. It
is empty now and may only stay empty: ``test_every_exemption_is_still_needed``
fails on any entry that is new or no longer needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("docs/getting-started", "examples")
TOOL = "record_feedback"
REQUIRED = "helpful_item_ids"

#: Pages exempt until their own rewrite lands. Empty, and shrink-only.
_PENDING: frozenset[str] = frozenset()


def _scan(root: Path) -> list[Path]:
    """Every markdown file under the scanned roots, relative to ``root``."""
    found: list[Path] = []
    for scanned in SCANNED_ROOTS:
        base = root / scanned
        if base.is_dir():
            found.extend(sorted(p for p in base.rglob("*.md") if p.is_file()))
    return found


def violations(root: Path, pending: frozenset[str] = frozenset()) -> list[str]:
    """Files that mention the tool without the item-id parameter."""
    bad: list[str] = []
    for path in _scan(root):
        rel = path.relative_to(root).as_posix()
        if rel in pending:
            continue
        text = path.read_text(encoding="utf-8")
        if TOOL in text and REQUIRED not in text:
            bad.append(rel)
    return bad


def test_every_feedback_snippet_names_item_ids() -> None:
    assert violations(REPO_ROOT, _PENDING) == [], (
        f"these pages mention `{TOOL}` without `{REQUIRED}`; a rating with "
        "no item ids does not join to the pack and never reaches the loop"
    )


def test_the_scan_sees_the_pages_it_guards() -> None:
    """Population floor, from a hand read: five setup pages carry a snippet."""
    mentioning = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in _scan(REPO_ROOT)
        if TOOL in p.read_text(encoding="utf-8")
    }
    hand_read = {
        "docs/getting-started/mcp-claude-desktop.md",
        "docs/getting-started/mcp-cursor.md",
        "docs/getting-started/mcp-claude-code.md",
        "examples/mcp_claude_code.md",
        "examples/integrations/openclaw/SKILL.md",
    }
    assert hand_read <= mentioning


def test_every_exemption_is_still_needed() -> None:
    # Shrink-only: a new exemption is a new non-compliant page, not a pending one.
    allowed: frozenset[str] = frozenset()
    assert _PENDING.issubset(allowed), sorted(_PENDING)
    stale = [rel for rel in sorted(_PENDING) if rel not in violations(REPO_ROOT)]
    assert stale == [], f"remove from _PENDING, the page now complies: {stale}"


def test_a_defective_copy_is_caught(tmp_path: Path) -> None:
    """Prove the guard against a subject that breaks the rule."""
    docs = tmp_path / "docs" / "getting-started"
    docs.mkdir(parents=True)
    (docs / "good.md").write_text(
        "call `record_feedback(pack_id=..., helpful_item_ids=[...])`\n"
    )
    (docs / "mentions-nothing.md").write_text("no feedback here\n")
    (docs / "bad.md").write_text("Mark outcomes with `record_feedback`.\n")
    examples = tmp_path / "examples" / "nested"
    examples.mkdir(parents=True)
    (examples / "bad-too.md").write_text(
        "record_feedback(trace_id=..., success=true)\n"
    )

    assert violations(tmp_path) == [
        "docs/getting-started/bad.md",
        "examples/nested/bad-too.md",
    ]
    assert violations(tmp_path, frozenset({"docs/getting-started/bad.md"})) == [
        "examples/nested/bad-too.md"
    ]


@pytest.mark.parametrize("root", SCANNED_ROOTS)
def test_scanned_roots_exist(root: str) -> None:
    assert (REPO_ROOT / root).is_dir()
