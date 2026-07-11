"""Deterministic directory walker for corpus ingestion.

Yields files in sorted relative-path order so a sync run's plan, report
and doc-id assignment are reproducible run-over-run. Dot-directories
and dot-files (``.obsidian/``, ``.git/``, ``.DS_Store``) are skipped —
they are tool state, not corpus content.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path


def _matches_include(relpath: str, include: tuple[str, ...]) -> bool:
    """``True`` when *relpath* passes the ``--include`` glob filter.

    An empty filter passes everything. Globs match against the full
    POSIX relative path and, for convenience, the bare filename — so
    ``--include '*.md'`` works without a ``**/`` prefix.
    """
    if not include:
        return True
    name = relpath.rsplit("/", 1)[-1]
    return any(fnmatch(relpath, pat) or fnmatch(name, pat) for pat in include)


def walk_corpus(
    root: Path,
    *,
    include: tuple[str, ...] = (),
    extensions: tuple[str, ...] = (),
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Enumerate corpus files under *root* (a directory or single file).

    Args:
        root: Directory to walk, or a single file to ingest alone.
        include: Optional glob patterns; empty means all files.
        extensions: Lower-case extensions with a registered handler.

    Returns:
        ``(supported, unsupported)`` — ``supported`` is a sorted list of
        ``(relpath, absolute_path)`` pairs ready for a handler;
        ``unsupported`` is the sorted relpaths that passed the include
        filter but have no handler (reported, never silently dropped).
    """
    candidates: list[tuple[str, Path]] = []
    if root.is_file():
        candidates.append((root.name, root))
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            base = Path(dirpath)
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                path = base / filename
                candidates.append((path.relative_to(root).as_posix(), path))

    supported: list[tuple[str, Path]] = []
    unsupported: list[str] = []
    for relpath, path in candidates:
        if not _matches_include(relpath, include):
            continue
        dot = relpath.rfind(".")
        extension = relpath[dot:].lower() if dot != -1 else ""
        if extension in extensions:
            supported.append((relpath, path))
        else:
            unsupported.append(relpath)

    supported.sort(key=lambda pair: pair[0])
    unsupported.sort()
    return supported, unsupported
