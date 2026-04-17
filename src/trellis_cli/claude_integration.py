"""Claude Code settings integration utilities."""

from __future__ import annotations

import json
from pathlib import Path


def get_claude_settings_path(scope: str, project_dir: Path | None = None) -> Path:
    """Return the path to the Claude Code settings file for the given scope.

    Args:
        scope: "root" for ~/.claude/settings.json, "project" for
               <project_dir>/.claude/settings.local.json.
        project_dir: Required when scope is "project".
    """
    if scope == "project":
        if project_dir is None:
            msg = "project_dir is required for project scope"
            raise ValueError(msg)
        return project_dir / ".claude" / "settings.local.json"
    return Path.home() / ".claude" / "settings.json"


def read_claude_settings(path: Path) -> dict:
    """Read and parse a Claude Code settings file.

    Returns an empty dict if the file does not exist or is empty.
    """
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return {}
    if not text:
        return {}
    result: dict = json.loads(text)
    return result


def merge_mcp_server(
    settings: dict, name: str, entry: dict, *, force: bool = False
) -> tuple[dict, bool]:
    """Merge an MCP server entry into settings, returning (updated, changed).

    Args:
        settings: Existing parsed settings dict (mutated in place).
        name: Server name key (e.g. "trellis").
        entry: Server config dict (command, args, env, etc.).
        force: Overwrite if name already present.

    Returns:
        Tuple of (settings dict, whether a change was made).
    """
    servers = settings.setdefault("mcpServers", {})
    if name in servers and not force:
        return settings, False
    servers[name] = entry
    return settings, True


def write_claude_settings(path: Path, settings: dict) -> None:
    """Write settings dict as formatted JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
