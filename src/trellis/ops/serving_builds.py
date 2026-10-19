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

**``mixed_builds`` is window-relative.**  Every group is a group because a
pack landed inside the window, so the flag reads "more than one build
assembled a pack in this window".  After a redeploy it holds until the old
build's last pack ages out of the window; ``--days 1`` asks whether
anything is *still* serving from it.  ``packs_from_other_builds`` counts
the packs outside the group seen most recently.

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
    #: More than one group assembled a pack in the window.
    mixed_builds: bool = False
    #: Label of the group seen most recently, or ``None`` with no packs.
    latest_build: str | None = None
    #: Packs from every group but the latest. Zero unless ``mixed_builds``.
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
    counts: dict[tuple[str | None, str | None], int] = {}
    first: dict[tuple[str | None, str | None], datetime] = {}
    last: dict[tuple[str | None, str | None], datetime] = {}
    stale: set[tuple[str | None, str | None]] = set()
    for event in pack_events:
        version, commit, stamp_stale = _stamp_fields(event)
        key = (version, commit)
        counts[key] = counts.get(key, 0) + 1
        if key not in first or event.occurred_at < first[key]:
            first[key] = event.occurred_at
        if key not in last or event.occurred_at > last[key]:
            last[key] = event.occurred_at
        if stamp_stale:
            stale.add(key)

    groups = [
        BuildGroup(
            version=key[0],
            commit=key[1],
            label=build_label(*key),
            packs=counts[key],
            first_seen=first[key],
            last_seen=last[key],
            stamp_stale=key in stale,
        )
        for key in counts
    ]
    groups.sort(key=lambda g: (g.last_seen, g.packs, g.label), reverse=True)
    total = sum(g.packs for g in groups)
    latest = groups[0] if groups else None
    return ServingBuildsReport(
        groups=groups,
        packs=total,
        mixed_builds=len(groups) > 1,
        latest_build=latest.label if latest else None,
        packs_from_other_builds=total - latest.packs if latest else 0,
    )


def mixed_builds_reason(report: ServingBuildsReport, *, days: int) -> str | None:
    """The warning line for a mixed window, or ``None`` when it was one build."""
    if not report.mixed_builds:
        return None
    return (
        f"mixed builds: {len(report.groups)} builds assembled packs in the last "
        f"{days} day(s), and {report.packs_from_other_builds} of {report.packs} "
        f"pack(s) came from builds other than the latest seen "
        f"({report.latest_build}) — a long-lived client may still be serving "
        "from an old build"
    )


__all__ = [
    "NO_PROVENANCE_LABEL",
    "BuildGroup",
    "ServingBuildsReport",
    "build_label",
    "mixed_builds_reason",
    "summarize_serving_builds",
]
