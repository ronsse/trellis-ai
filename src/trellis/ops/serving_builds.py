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
changed.**  Let L be the newest build: the group whose ``first_seen`` is
latest, ties falling to pack count, then label.  It is not the group that
served the last pack, because a stale session can serve that one, and it
would then name the old build as the newest.  The flag is set when some
other group has a pack at or after L's ``first_seen``: an older build was
still assembling packs once the newer one had started.  A clean redeploy,
where the old build's last pack precedes the new build's first, is not a
mix however many days of both sit in the window, so a redeploy does not
warn for the rest of the window.  A genuine overlap does stay flagged until
its packs age out of the window.  An unidentified group (no stamp, null
commit) follows the same rule as any other.  Groups are told apart by their
``(version, commit)`` key, never by label, because two different keys can
share one label.  ``packs_from_other_builds`` counts only those concurrent
packs, the other groups' packs at or after L's ``first_seen``, so it is zero
exactly when the flag is clear.  A tie at that instant counts as concurrent.
Timestamps are normalised to aware UTC before any comparison, because
backends differ on whether a stored ``occurred_at`` is naive.

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
from trellis.stores.base.event_log import Event, _as_utc

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

    #: Newest build first (latest ``first_seen``); ties fall to pack count,
    #: then label. ``groups[0]`` is the build ``latest_build`` names.
    groups: list[BuildGroup] = Field(default_factory=list)
    packs: int = 0
    #: Another group assembled a pack at or after the first pack of the
    #: newest build, so two builds served concurrently.
    mixed_builds: bool = False
    #: Label of the newest build (L, the latest ``first_seen``), or ``None``
    #: with no packs.
    latest_build: str | None = None
    #: The other groups' packs at or after L's ``first_seen``. Zero exactly
    #: when ``mixed_builds`` is clear; not the packs outside L.
    packs_from_other_builds: int = 0


def _text(value: Any) -> str | None:
    """A stamp field as a non-empty string, or ``None`` for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def stamp_fields(event: Event) -> tuple[str | None, str | None, bool]:
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
        version, commit, stamp_stale = stamp_fields(event)
        key = (version, commit)
        seen.setdefault(key, []).append(_as_utc(event.occurred_at))
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
    if not groups:
        return ServingBuildsReport()
    # Newest build first. The version and commit break a tie between two keys
    # that share a label, so the order never depends on arrival order.
    groups.sort(
        key=lambda g: (g.first_seen, g.packs, g.label, g.version or "", g.commit or ""),
        reverse=True,
    )
    newest = groups[0]
    newest_key = (newest.version, newest.commit)
    concurrent = sum(
        1
        for key, times in seen.items()
        if key != newest_key
        for when in times
        if when >= newest.first_seen
    )
    return ServingBuildsReport(
        groups=groups,
        packs=sum(g.packs for g in groups),
        mixed_builds=concurrent > 0,
        latest_build=newest.label,
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
        f"newest build ({report.latest_build}) after it began at "
        f"{began:%Y-%m-%d %H:%M} UTC — a long-lived client may still be "
        "serving from an old build"
    )


__all__ = [
    "NO_PROVENANCE_LABEL",
    "BuildGroup",
    "ServingBuildsReport",
    "build_label",
    "mixed_builds_reason",
    "stamp_fields",
    "summarize_serving_builds",
]
