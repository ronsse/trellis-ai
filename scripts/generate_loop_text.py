#!/usr/bin/env python3
"""Write the shipped loop-text files from ``trellis_wire.loop_text``.

    python scripts/generate_loop_text.py          # rewrite the files
    python scripts/generate_loop_text.py --check  # exit 1 if any differs

The files are package data that ``trellis admin show-prompt`` and
``trellis admin install-skills`` read, so they stay checked in; this script is
the only way they change. ``tests/unit/test_loop_text_generated.py`` runs the
``--check`` comparison, the way ``openapi-check`` pins the OpenAPI spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

from trellis_wire import loop_text

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "trellis_cli"


def generated_files() -> dict[Path, str]:
    """Path -> the text the single source produces for it."""
    return {
        PACKAGE / "prompts" / "system.md": loop_text.system_prompt(),
        PACKAGE / "prompts" / "claude-md.md": loop_text.claude_md_section(),
        PACKAGE / "prompts" / "agent-rule.md": loop_text.agent_rule(),
        PACKAGE / "skills" / "trellis-loop" / "SKILL.md": loop_text.loop_skill(),
    }


def stale_files() -> list[Path]:
    """The generated files whose checked-in bytes differ from the source."""
    return [
        path
        for path, text in generated_files().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]


def _shown(path: Path) -> Path:
    """``path`` relative to the repo when it lives there, else as given."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def main(argv: list[str]) -> int:
    """Rewrite the files, or with ``--check`` only report the stale ones."""
    stale = stale_files()
    if "--check" in argv:
        for path in stale:
            sys.stderr.write(f"stale: {_shown(path)}\n")
        return 1 if stale else 0
    for path, text in generated_files().items():
        if path in stale:
            path.write_text(text, encoding="utf-8")
            sys.stdout.write(f"wrote {_shown(path)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
