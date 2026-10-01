"""Which project a session's packs and traces belong to.

Claude Code spawns one stdio MCP server per session, in the session's
directory, so the server's working directory names the project the agent
is working on.  :func:`resolve_project` turns that directory into a name:

1. ``TRELLIS_PROJECT``, when set to a non-blank value;
2. else the git repository containing the working directory.  A linked
   worktree reports its *main* repository, not its own directory, because
   ``<anywhere>/feature-x`` belongs to the repository it was added from;
3. else ``None`` — a session started in ``~`` or ``/`` has no project.

The repository is read from ``.git`` directly rather than through a
``git`` subprocess, so a host without git, or with an unexpected one,
resolves the same way.  Only a missing ``.git`` lets the walk move to the
parent directory (the :mod:`trellis.core.path_presence` doctrine): a
``.git`` file that is unreadable, malformed or dangling answers ``None``
rather than naming whatever repository happens to enclose it, and a
``.git`` directory names its repository without being read.

Not part of ``write_provenance``, which every process writes: a cwd names
a caller's project only for the per-session stdio server, and event
metadata is invisible to ``get_events(payload_filters=...)``.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

#: Environment variable that overrides the derived project name.
PROJECT_ENV = "TRELLIS_PROJECT"


def project_override() -> str | None:
    """Return ``TRELLIS_PROJECT``, stripped; ``None`` when unset or blank."""
    return os.environ.get(PROJECT_ENV, "").strip() or None


def _repo_name(start: Path) -> str | None:
    """Name the git repository containing ``start``, or ``None``.

    A ``.git`` file is a linked worktree's pointer (``gitdir: <path>``);
    its gitdir's ``commondir`` leads to the main repository's git
    directory, which is ``<main>/.git`` for a normal repository and
    ``<name>.git`` for a bare one.  A gitfile without a ``commondir`` (a
    submodule, or a ``--separate-git-dir`` checkout) is not a linked
    worktree and answers ``None``.
    """
    for directory in (start, *start.parents):
        dotgit = directory / ".git"
        try:
            if dotgit.is_dir():
                return directory.name or None
            line = dotgit.read_text(encoding="utf-8").partition("\n")[0]
        except FileNotFoundError:
            continue
        if not line.startswith("gitdir:"):
            return None
        gitdir = directory / line.removeprefix("gitdir:").strip()
        try:
            commondir = (gitdir / "commondir").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        common = Path(os.path.normpath(gitdir / commondir.strip()))
        if common.name == ".git":
            return common.parent.name or None
        return common.name.removesuffix(".git") or None
    return None


@functools.lru_cache(maxsize=1)
def resolve_project() -> str | None:
    """Return this process's project name, resolved once; ``None`` if none.

    Never raises: any failure (a deleted working directory, an unreadable
    ``.git`` file) answers ``None``.
    """
    try:
        return project_override() or _repo_name(Path.cwd())
    except Exception:  # advisory probe; never fail a pack or a write
        return None


__all__ = ["PROJECT_ENV", "project_override", "resolve_project"]
