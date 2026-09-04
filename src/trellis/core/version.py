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
  signal that the install is stale.  :func:`resolve_stamp_staleness`
  raises that signal instead of leaving it for a human to notice, by
  reading the source tree's live ``HEAD`` and comparing.
* **Container image** — this repo's ``Dockerfile`` builds the wheel from a
  context with no ``.git`` in it (``.dockerignore`` excludes it, and the
  builder stage copies only ``pyproject.toml README.md LICENSE src/``), so
  ``hatch-vcs`` has nothing to read and falls through to
  ``[tool.hatch.version] fallback-version`` — the same
  :data:`FALLBACK_VERSION` string for every image ever built.  A build must
  therefore *tell* the image what it is: ``make docker-build`` passes the
  working tree's git-derived version as the ``TRELLIS_BUILD_VERSION`` build
  arg, which the Dockerfile forwards to ``SETUPTOOLS_SCM_PRETEND_VERSION``.
  Do that and the image carries the sha it was built from, immutable for
  its life.  Skip it and resolution reports ``source="fallback-version"``
  with ``commit=None`` — "I cannot identify this build", which is the truth
  and is greppable, rather than a plausible-looking ``0.2.0``.

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
import json
import re
from dataclasses import dataclass
from typing import Any

#: Installed distribution name, as declared in ``pyproject.toml``.
DISTRIBUTION_NAME = "trellis-ai"

#: Returned when neither the distribution metadata nor a generated
#: ``_version`` module can name the running build (e.g. running straight
#: from a source tree that was never installed).
UNKNOWN_VERSION = "0.0.0+unknown"

#: ``[tool.hatch.version] fallback-version`` from ``pyproject.toml``.  A
#: build with no git history resolves to exactly this, which means it says
#: nothing about *which* build it is — so resolution reports it as
#: :data:`FALLBACK_SOURCE`, not as a successfully identified version.
#: ``tests/unit/core/test_version.py`` asserts the two stay in sync.
FALLBACK_VERSION = "0.2.0"

#: ``CodeVersion.source`` for the case above.
FALLBACK_SOURCE = "fallback-version"

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
    if raw == FALLBACK_VERSION:
        return CodeVersion(version=raw, source=FALLBACK_SOURCE)
    commit, dirty = _split_local_segment(raw)
    return CodeVersion(version=raw, source=source, commit=commit, dirty=dirty)


#: :attr:`StampStaleness.state` — no comparison was attempted.  Either the
#: version did not come from installed metadata (a container image, a
#: generated module, an unidentifiable build), or the metadata carries no
#: sha to compare, or the distribution is not an editable install off a
#: directory.  None of those can drift the way an editable install does:
#: for a wheel or an image, code and metadata were frozen together.
STALENESS_NOT_CHECKED = "not-checked"

#: A comparison *was* attempted and the source tree's ``HEAD`` could not
#: be read — git is not installed, the tree is gone, the call timed out.
#: Reported as its own state rather than folded into "fresh", because
#: "nothing is wrong" and "I could not look" are different facts.
STALENESS_UNRESOLVED = "unresolved"

#: The source tree's ``HEAD`` is the sha the installed metadata carries.
STALENESS_FRESH = "fresh"

#: The source tree has moved on from the sha the metadata carries — every
#: write this process makes is attributed to code it is no longer running.
STALENESS_STALE = "stale"

#: Seconds to wait for the ``git rev-parse`` probe.  Generous for a local
#: repository and still bounded: the probe is advisory, and a stamp that
#: blocks a write has cost more than the drift it reports.
_GIT_PROBE_TIMEOUT_S = 5.0

#: A resolved git object name.
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class StampStaleness:
    """Whether the installed metadata still describes the running code.

    Scoped narrowly, because the neighbouring :attr:`CodeVersion.dirty`
    is a *different clock* and the two are easy to conflate:

    * :attr:`CodeVersion.dirty` — was the tree dirty **at install time**,
      parsed from the PEP 440 local segment.  A property of the metadata.
    * :attr:`state` — has the source tree's ``HEAD`` moved **since** the
      install.  A property of the tree, read now.

    What this deliberately does **not** answer: whether the tree's files
    differ from its own ``HEAD`` right now.  This compares commits, not
    contents, so a tree with uncommitted edits at the stamped sha reports
    :data:`STALENESS_FRESH`.  Answering that would need a
    ``git status`` walk of the whole worktree on a path that runs before
    the process's first write; the drift this exists for — ``git pull``
    with no re-install — is a commit move and is what the cheap probe
    sees.
    """

    state: str
    source_tree_commit: str | None = None

    @property
    def is_stale(self) -> bool:
        """True only for :data:`STALENESS_STALE`."""
        return self.state == STALENESS_STALE

    def as_stamp_fields(self) -> dict[str, Any]:
        """Fields to merge into an event stamp — empty unless stale.

        Silence is the healthy answer.  The stamp rides
        :attr:`~trellis.stores.base.event_log.Event.metadata` on *every*
        emitted event, so a fresh editable install and every container
        produce a byte-identical stamp to the one they produced before
        this probe existed; only a deployment with something to report
        pays any bytes for it.  An operator who needs to tell "checked
        and fine" from "never checked" asks ``trellis admin
        write-config``, which reports :attr:`state` in full.
        """
        if not self.is_stale:
            return {}
        return {"stamp_stale": True, "source_tree_commit": self.source_tree_commit}


def _direct_url_record() -> dict[str, Any]:
    """Parsed ``direct_url.json``, or ``{}`` when there is nothing to read.

    Absent for a wheel installed from an index, which is the shape this
    returns ``{}`` for; unparseable is treated the same way, since a
    stamp is not the place to raise about a malformed installer artefact.
    *Unreadable* is treated the same way too, and by a blanket guard
    rather than a list of exception types: ``read_text`` decodes as
    UTF-8, so a corrupt file raises :exc:`UnicodeDecodeError` — a
    :exc:`ValueError`, not the :exc:`OSError` an enumeration reaches
    for.  Naming the ways an installer artefact is allowed to fail is
    how this guarantee gets lost.
    """
    from importlib.metadata import distribution  # noqa: PLC0415

    try:
        raw = distribution(DISTRIBUTION_NAME).read_text("direct_url.json")
    except Exception:  # advisory probe; never fail a write
        return {}
    if not raw:
        return {}
    try:
        record = json.loads(raw)
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def _editable_source_tree() -> str | None:
    """Directory an editable install points at, or ``None``.

    Reads PEP 610 ``direct_url.json``, which the installer writes beside
    the distribution metadata and which answers both halves of the
    question in one file: ``dir_info.editable`` says the install is
    editable, ``url`` says which directory.  ``Distribution.origin``
    exposes the same file as an object but only from Python 3.13, and
    this package supports 3.11 — so the file is read directly.
    ``read_text`` returns ``None`` (it does not raise) when the file is
    absent, which is the normal case for a wheel installed from an index.
    """
    from urllib.parse import unquote, urlparse  # noqa: PLC0415

    record = _direct_url_record()
    dir_info = record.get("dir_info")
    if not isinstance(dir_info, dict) or dir_info.get("editable") is not True:
        return None
    url = record.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file" or not parsed.path:
        return None
    return unquote(parsed.path)


def _git_head(tree: str) -> str | None:
    """``HEAD`` of the repository containing ``tree``, or ``None``.

    Every failure is swallowed — a missing git binary
    (``FileNotFoundError``), a timeout, a tree that is not a checkout, a
    permission error.  This runs on the way to a write and must never be
    the reason one fails.

    The repository is addressed with ``git -C`` rather than by changing
    the process's working directory, which would be a global side effect
    on a path that can run from any thread.

    No check that ``tree`` is the repository *root*: if the metadata
    carries a sha at all, ``hatch-vcs`` resolved it from git in this same
    directory at install time using the same upward discovery, so the
    repository found now is the one that produced the stamp.
    """
    import subprocess  # noqa: PLC0415

    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "-C", tree, "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT_S,
            check=False,
        )
    except Exception:  # advisory probe; never fail a write
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip()
    return head if _FULL_SHA.match(head) else None


@functools.lru_cache(maxsize=1)
def resolve_stamp_staleness() -> StampStaleness:
    """Has the source tree moved on from the installed metadata's sha?

    Cached for the life of the process, like
    :func:`resolve_code_version`: the answer feeds the event hot path via
    :mod:`trellis.core.write_provenance`, so this is one ``git rev-parse``
    per process, taken lazily on the first stamp — never at import time.
    A tree that moves *under* a running process keeps reporting the sha
    it had at first use, which is the same freeze
    :func:`resolve_code_version` already applies to the version itself.

    Advisory throughout: every failure resolves to a state, never to an
    exception — and that is held *structurally*, by the blanket guard
    here, not by each step having enumerated what it may raise.  The
    stamp rides ``EventLog.emit``, so an escape does not cost one probe:
    it fails **every write for the life of the process**, with a
    traceback out of a version module.  An enumeration is one unforeseen
    exception type away from that, which is why this is a seam and not a
    list.  See :func:`_direct_url_record` for the type that got through
    a list.

    ``resolve_stamp_staleness.cache_clear()`` re-reads — test-facing, and
    the autouse fixture in ``tests/conftest.py`` calls it.
    """
    try:
        return _resolve_stamp_staleness()
    except Exception:  # advisory probe; never fail a write
        return StampStaleness(state=STALENESS_UNRESOLVED)


def _resolve_stamp_staleness() -> StampStaleness:
    """The verdict itself; :func:`resolve_stamp_staleness` guards it."""
    version = resolve_code_version()
    if version.source != "dist-metadata" or version.commit is None:
        return StampStaleness(state=STALENESS_NOT_CHECKED)
    tree = _editable_source_tree()
    if tree is None:
        return StampStaleness(state=STALENESS_NOT_CHECKED)
    head = _git_head(tree)
    if head is None:
        return StampStaleness(state=STALENESS_UNRESOLVED)
    if head.startswith(version.commit):
        return StampStaleness(state=STALENESS_FRESH, source_tree_commit=head)
    return StampStaleness(state=STALENESS_STALE, source_tree_commit=head)


__all__ = [
    "DISTRIBUTION_NAME",
    "FALLBACK_SOURCE",
    "FALLBACK_VERSION",
    "STALENESS_FRESH",
    "STALENESS_NOT_CHECKED",
    "STALENESS_STALE",
    "STALENESS_UNRESOLVED",
    "UNKNOWN_VERSION",
    "CodeVersion",
    "StampStaleness",
    "resolve_code_version",
    "resolve_stamp_staleness",
]
