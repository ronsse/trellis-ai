"""Claude Code PostToolUse hook — auto-lint files Claude just edited.

Runs after every successful Edit/Write/MultiEdit tool call. Reads the
tool's input from stdin (JSON), extracts the edited file path, and runs
ruff format + ruff check --fix on it if it's a Python file.

Silent on success. On lint error, prints the ruff output so Claude sees
it in the next turn and can fix it before committing.

Wired up in .claude/settings.json under hooks.PostToolUse.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_LINTED_EXTS = {".py"}
# Extend here when you want the hook to also format e.g. toml/yaml.
# ruff currently only formats Python; for other types, add another tool.


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed hook input — don't block the agent, just no-op.
        return 0

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return 0

    path = Path(file_path)
    if path.suffix.lower() not in _LINTED_EXTS:
        return 0
    if not path.exists():
        return 0

    # Run ruff format then ruff check --fix. Both are safe — they only
    # make in-place changes that the project has already approved via
    # pyproject.toml config.
    for cmd in (
        ["ruff", "format", str(path)],
        ["ruff", "check", "--fix", str(path)],
    ):
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Surface the output so Claude sees it in the next turn.
            sys.stderr.write(
                f"\n[claude_postedit_lint] {' '.join(cmd)} failed:\n"
                f"{result.stdout}{result.stderr}\n"
            )

    # Never block the agent — this is advisory, not gating.
    return 0


if __name__ == "__main__":
    sys.exit(main())
