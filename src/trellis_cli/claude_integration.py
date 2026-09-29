"""Install the bundled Trellis skills into a Claude Code skills directory.

Nothing here writes Claude Code's MCP config: ``trellis admin quickstart``
prints the ``claude mcp add`` command for the user to run instead.
"""

from __future__ import annotations

import shutil
from importlib.resources import as_file, files
from pathlib import Path

import structlog

from trellis.core.error_sanitize import sanitize_error_message
from trellis_cli.skills import SKILL_NAMES

_logger = structlog.get_logger(__name__)


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
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": sanitize_error_message(str(exc)),
                }
            )
            continue
        _logger.debug("skill_installed", skill=name, dest=str(dest), force=force)
        results.append({"name": name, "status": status})
    return results
