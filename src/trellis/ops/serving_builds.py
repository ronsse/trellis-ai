"""Serving builds — which builds assembled the packs in a window.

Every event carries a ``write_provenance`` stamp
(:mod:`trellis.core.write_provenance`), and a ``PACK_ASSEMBLED`` row is
stamped by the process that built the pack.  A long-lived client, a stdio
MCP child that outlived a redeploy for one, keeps assembling packs from the
build it started on, and nothing in a pack or in the pack counts says so.
This module groups a window's pack rows by ``(version, commit)`` so the
mix is a field in ``trellis analyze health`` rather than a hand-grouped
query.

**An unidentified build is a group, never a drop.**  A row with no stamp,
a malformed one, or one whose ``commit`` is ``null`` (an image built
without ``make docker-build`` stamps ``fallback-version`` and a null
commit) cannot be placed on a commit, but it did serve a pack.  Each
distinct ``(version, commit)`` pair, ``None`` parts included, is its own
group, and :func:`build_label` names it as unidentified instead of
folding it into a known build.

**``mixed_builds`` means two builds served concurrently, not that a build
changed.**  Let L be the group with the latest ``last_seen``.  The flag is
set when some other group has a pack at or after L's ``first_seen``: an
older build was still assembling packs once the newer one had started.  A
clean redeploy, where the old build's last pack precedes the new build's
first, is not a mix however many days of both sit in the window, so a
redeploy does not warn for the rest of the window.  A genuine overlap does
stay flagged until its packs age out of the window.  An unidentified group
(no stamp, null commit) follows the same rule as any other.
``packs_from_other_builds`` counts only those concurrent packs, the other
groups' packs at or after L's ``first_seen``, so it is zero exactly when the
flag is clear.  A tie at that instant counts as concurrent.

**The commit is only as good as the stamp.**  ``commit`` is frozen at
install time, so an editable install off a moved working tree reports a
specific, wrong one.  When the staleness probe caught that, the stamp
carries ``stamp_stale`` and the group says so (``stamp_stale``), and its
commit should be read as what the install metadata claimed rather than as
what ran.  :mod:`trellis.ops.capture_coverage` is deliberately not keyed
on this stamp for that reason; this report is, because a stamp is the only
record a build leaves on a pack, and it names the one case it knows to be
wrong.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.core.write_provenance import WRITE_PROVENANCE_KEY
from trellis.stores.base.event_log import Event

#: Label for rows that carry no usable stamp at all.
NO_PROVENANCE_LABEL = "no provenance"


def build_label(version: str | None, commit: str | None) -> str:
    """Name a ``(version, commit)`` group, saying so when part is unknown."""
    if version is None and commit is None:
        return NO_PROVENANCE_LABEL
    return f"{version or 'unknown version'} @ {commit or 'commit unknown'}"


class BuildGroup(TrellisModel):
    """The packs one ``(version, commit)`` pair assembled in the window."""

    version: str | None = None
    commit: str | None = None
    #: :func:`build_label`; what the human view prints.
    label: str
    packs: int
    first_seen: datetime
    last_seen: datetime
    #: Any row in the group carried ``stamp_stale``: ``commit`` is what the
    #: install metadata said, and the working tree had moved on.
    stamp_stale: bool = False


class ServingBuildsReport(TrellisModel):
    """Which builds assembled the window's packs, and whether it was more than one."""

    #: Newest ``last_seen`` first; ties fall to pack count, then label.
    groups: list[BuildGroup] = Field(default_factory=list)
    packs: int = 0
    #: Another group assembled a pack at or after the first pack of the
    #: group seen most recently, so two builds served concurrently.
    mixed_builds: bool = False
    #: Label of the group seen most recently (L), or ``None`` with no packs.
    latest_build: str | None = None
    #: The other groups' packs at or after L's ``first_seen``. Zero exactly
    #: when ``mixed_builds`` is clear; not the packs outside L.
    packs_from_other_builds: int = 0


def _text(value: Any) -> str | None:
    """A stamp field as a non-empty string, or ``None`` for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _stamp_fields(event: Event) -> tuple[str | None, str | None, bool]:
    """``(version, commit, stamp_stale)`` off one row, never raising.

    A row whose metadata or stamp is not the shape the writer emits reads as
    unidentified: one malformed row must not fail the health read.
    """
    stamp = (event.metadata or {}).get(WRITE_PROVENANCE_KEY)
    if not isinstance(stamp, dict):
        return None, None, False
    return (
        _text(stamp.get("version")),
        _text(stamp.get("commit")),
        stamp.get("stamp_stale") is True,
    )


def summarize_serving_builds(pack_events: Iterable[Event]) -> ServingBuildsReport:
    """Group ``PACK_ASSEMBLED`` rows by the build that stamped them.

    The caller decides which rows count: ``analyze health`` passes the
    served arm, the rows ``ServeAttributionReport.packs`` counts, so the
    groups sum to that number.
    """
    seen: dict[tuple[str | None, str | None], list[datetime]] = {}
    stale: set[tuple[str | None, str | None]] = set()
    for event in pack_events:
        version, commit, stamp_stale = _stamp_fields(event)
        key = (version, commit)
        seen.setdefault(key, []).append(event.occurred_at)
        if stamp_stale:
            stale.add(key)

    groups = [
        BuildGroup(
            version=key[0],
            commit=key[1],
            label=build_label(*key),
            packs=len(times),
            first_seen=min(times),
            last_seen=max(times),
            stamp_stale=key in stale,
        )
        for key, times in seen.items()
    ]
    groups.sort(key=lambda g: (g.last_seen, g.packs, g.label), reverse=True)
    total = sum(g.packs for g in groups)
    if not groups:
        return ServingBuildsReport()
    latest = groups[0]
    concurrent = sum(
        1
        for key, times in seen.items()
        if build_label(*key) != latest.label
        for when in times
        if when >= latest.first_seen
    )
    return ServingBuildsReport(
        groups=groups,
        packs=total,
        mixed_builds=concurrent > 0,
        latest_build=latest.label,
        packs_from_other_builds=concurrent,
    )


def mixed_builds_reason(report: ServingBuildsReport, *, days: int) -> str | None:
    """The warning line for a mixed window, or ``None`` when it was one build."""
    if not report.mixed_builds:
        return None
    began = report.groups[0].first_seen
    return (
        f"mixed builds: {report.packs_from_other_builds} of {report.packs} "
        f"pack(s) in the last {days} day(s) came from builds other than the "
        f"latest seen ({report.latest_build}) after it began at "
        f"{began:%Y-%m-%d %H:%M} UTC — a long-lived client may still be "
        "serving from an old build"
    )


__all__ = [
    "NO_PROVENANCE_LABEL",
    "BuildGroup",
    "ServingBuildsReport",
    "build_label",
    "mixed_builds_reason",
    "summarize_serving_builds",
]
