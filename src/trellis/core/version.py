"""Code-version resolution — one answer per process.

The same database is written by several builds at once: the host CLI and
the stdio MCP server run an editable install off a working tree, while
the ``trellis-api`` / ``trellis-mcp`` containers run whatever image was
last built.  Nothing a write left behind used to record *which* build
produced it, so "was this row written before or after the fix?" was
unanswerable.  This module is the single place that answers "what code
is this process?".

**Where the answer comes from.**  ``hatch-vcs`` computes the version from
git at *install / build* time and bakes it into the installed
distribution metadata.  Reading it back with :mod:`importlib.metadata`
therefore works in both deployment shapes:

* **Editable install off a working tree** (``uv pip install -e .``) —
  yields the PEP 440 version computed when the editable install was
  made, e.g. ``0.9.1.dev156+gd7c3e7ace``.  The local segment carries the
  git sha, so :attr:`CodeVersion.commit` is populated.  Caveat worth
  knowing: this is frozen at *install* time, so a working tree that has
  moved on (``git pull`` with no re-install) reports the older sha.  That
  is still strictly better than nothing — and the mismatch is itself the
  signal that the install is stale.
* **Container image** (wheel built from a checkout, then installed) —
  yields the version computed at build time, i.e. the sha the image was
  built from.  Immutable for the life of the image, which is exactly the
  attribution an image-versus-host drift investigation needs.

No build system is invented here: both shapes read the metadata that the
existing ``[tool.hatch.version] source = "vcs"`` config already produces.

The fallback chain is metadata → :mod:`trellis._version` (the module a
``hatch-vcs`` build hook would write, if one is ever enabled) →
:data:`UNKNOWN_VERSION`.  Resolution is cached for the life of the
process: a git-derived version cannot change under a running interpreter,
and the caller is the event hot path.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any

#: Installed distribution name, as declared in ``pyproject.toml``.
DISTRIBUTION_NAME = "trellis-ai"

#: Returned when neither the distribution metadata nor a generated
#: ``_version`` module can name the running build (e.g. running straight
#: from a source tree that was never installed).
UNKNOWN_VERSION = "0.0.0+unknown"

#: A ``+g<sha>`` token in a PEP 440 local segment — how ``hatch-vcs``
#: (via ``setuptools-scm``'s ``node-and-date`` scheme) encodes the commit.
_SHA_TOKEN = re.compile(r"^g(?P<sha>[0-9a-f]{7,40})$")

#: A ``d<YYYYMMDD>`` token in the same local segment — emitted only when
#: the working tree was dirty at install/build time.
_DIRTY_DATE_TOKEN = re.compile(r"^d\d{8}$")


@dataclass(frozen=True, slots=True)
class CodeVersion:
    """The build identity of the running process.

    ``source`` names which mechanism produced ``version`` so an operator
    reading a stamped event can tell "resolved from installed metadata"
    apart from "could not resolve" without guessing at the string shape.
    """

    version: str
    source: str
    commit: str | None = None
    dirty: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for embedding in an event stamp."""
        return {
            "version": self.version,
            "version_source": self.source,
            "commit": self.commit,
            "dirty": self.dirty,
        }


def _split_local_segment(version: str) -> tuple[str | None, bool]:
    """Pull ``(commit, dirty)`` out of a PEP 440 local version segment.

    Returns ``(None, False)`` for a clean release version like ``0.9.1``
    — a tagged build has no local segment and therefore no sha to carry.
    """
    _, _, local = version.partition("+")
    if not local:
        return None, False
    commit: str | None = None
    dirty = False
    for part in local.split("."):
        match = _SHA_TOKEN.match(part)
        if match is not None:
            commit = match.group("sha")
        elif part == "dirty" or _DIRTY_DATE_TOKEN.match(part):
            dirty = True
    return commit, dirty


def _version_from_metadata() -> str | None:
    """Installed distribution version, or ``None`` when not installed."""
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return None


def _version_from_module() -> str | None:
    """Version from a generated ``trellis._version``, or ``None``."""
    try:
        from trellis._version import (  # noqa: PLC0415
            __version__,  # type: ignore[import-not-found]
        )
    except ImportError:
        return None
    else:
        return str(__version__)


@functools.lru_cache(maxsize=1)
def resolve_code_version() -> CodeVersion:
    """Resolve the running build's identity — once per process.

    Cached deliberately: the answer cannot change under a live
    interpreter, and every emitted event asks for it.
    """
    raw = _version_from_metadata()
    source = "dist-metadata"
    if raw is None:
        raw = _version_from_module()
        source = "generated-module"
    if raw is None:
        return CodeVersion(version=UNKNOWN_VERSION, source="unknown")
    commit, dirty = _split_local_segment(raw)
    return CodeVersion(version=raw, source=source, commit=commit, dirty=dirty)


__all__ = [
    "DISTRIBUTION_NAME",
    "UNKNOWN_VERSION",
    "CodeVersion",
    "resolve_code_version",
]
