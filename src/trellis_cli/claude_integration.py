"""Claude Code settings integration utilities."""

from __future__ import annotations

import json
import shutil
from importlib.resources import as_file, files
from pathlib import Path

import structlog

from trellis_cli.skills import SKILL_NAMES

_logger = structlog.get_logger(__name__)


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
    The missing-file branch is documented graceful-degradation —
    the initial ``trellis admin init`` call is expected to bootstrap
    from no file. We log at ``debug`` so the create-from-empty path
    is recoverable from structured logs.
    """
    try:
        text = path.read_text().strip()
    # GRACEFUL-DEGRADATION: ``trellis admin init`` bootstraps from no
    # file — see docstring; empty-dict return is the expected branch.
    except FileNotFoundError:
        _logger.debug("claude_settings_not_found", path=str(path))
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


# ---------------------------------------------------------------------------
# Skill installation
# ---------------------------------------------------------------------------


def get_skills_target_dir(scope: str, project_dir: Path | None = None) -> Path:
    """Return the Claude Code skills directory for the given scope.

    Args:
        scope: "user" for ``~/.claude/skills/``, "project" for
               ``<project_dir>/.claude/skills/``.
        project_dir: Required when scope is "project". Defaults to the
            current working directory when omitted.

    Raises:
        ValueError: If ``scope`` is not "user" or "project".
    """
    if scope == "user":
        return Path.home() / ".claude" / "skills"
    if scope == "project":
        base = project_dir if project_dir is not None else Path.cwd()
        return base / ".claude" / "skills"
    msg = f"unknown skills scope {scope!r} (expected 'user' or 'project')"
    raise ValueError(msg)


def install_skills(target_dir: Path, *, force: bool = False) -> list[dict[str, str]]:
    """Copy the bundled skill templates into ``target_dir``.

    Reads the canonical skill directories from the ``trellis_cli.skills``
    package via :mod:`importlib.resources`, so this works from an
    installed wheel as well as a repo checkout. Idempotent: a skill whose
    destination directory already exists is skipped unless ``force`` is
    set, in which case it is replaced.

    Args:
        target_dir: Destination skills directory (e.g.
            ``~/.claude/skills``). Created if missing.
        force: Overwrite skill directories that already exist.

    Returns:
        One result dict per skill, each with ``name`` and ``status``
        (``"installed"``, ``"overwritten"``, ``"skipped"``, or
        ``"failed"`` with an ``error`` field). A copy failure on one
        skill (disk full, permissions) is captured per-skill rather
        than raised, so the report always covers every skill and the
        caller's structured output stays accurate on partial installs.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, str]] = []
    skills_root = files("trellis_cli.skills")
    for name in SKILL_NAMES:
        dest = target_dir / name
        if dest.exists() and not force:
            _logger.debug("skill_install_skipped", skill=name, dest=str(dest))
            results.append({"name": name, "status": "skipped"})
            continue
        status = "overwritten" if dest.exists() else "installed"
        try:
            if dest.exists():
                shutil.rmtree(dest)
            # ``as_file`` materializes the packaged resource as a real
            # path (a no-op for filesystem-backed installs, an
            # extraction for zipped wheels), which ``copytree`` needs.
            with as_file(skills_root / name) as src:
                shutil.copytree(src, dest)
        except OSError as exc:
            _logger.warning("skill_install_failed", skill=name, error=str(exc))
            results.append({"name": name, "status": "failed", "error": str(exc)})
            continue
        _logger.debug("skill_installed", skill=name, dest=str(dest), force=force)
        results.append({"name": name, "status": status})
    return results
