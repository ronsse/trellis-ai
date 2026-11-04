"""One read-only weekly series over the leading indicators, split at build boundaries.

Every other analysis surface is a point read (``analyze value``, ``health``,
``judged-outcomes``). A regression that unfolds over a week is invisible to a
point read: once, subagent retrieval adoption fell from about 83% to about 16%
inside a week and no surface warned. The question "is it improving over time"
needs a series, and a series is only legible when a build change reads as a
segment boundary and not as noise.

**Composition, not a second implementation.** Each weekly figure comes from the
reader that already owns it, called with that week as its window:

* ``pack_value`` - :func:`~trellis.retrieve.pack_value.summarize_pack_value`
* ``serve_attribution`` -
  :func:`~trellis.ops.write_health.summarize_serve_attribution`
* ``judged_outcomes`` -
  :func:`~trellis.learning.judged_outcomes.summarize_judged_outcomes`
* ``write_health`` (the capture rejection rate) -
  :func:`~trellis.ops.write_health.summarize_write_health`, filtered to capture
  surfaces with :func:`~trellis.ops.capture_health.is_capture_surface`
* ``retrieval_adoption`` -
  :func:`~trellis.ops.retrieval_adoption.summarize_retrieval_adoption`
* builds - :func:`~trellis.ops.serving_builds.summarize_serving_builds`

Two scans exist only here (follow-up retrieval, precedents served), and both
read the served packs of the week that
:func:`~trellis.learning.pack_observations.join_pack_events_with_coverage`
already returns, so they see the population every other metric sees.

**Not included: loop health.** ``summarize_loop_health`` reports each loop's
*latest* run and the tuner's live pending-proposal count. Neither is a quantity
of a window: a past week's "last run" and its backlog cannot be rebuilt from
the event log, and the reader takes no window. It is listed in
``TrendReport.omitted`` rather than approximated.

**The holdout's outcome is sealed and never read here.** This module imports
nothing from :mod:`trellis.analyze.holdout`, reads no ``outcome`` block of a
``CAPTURE_SESSION_PACKS`` event and splits nothing by arm. Withheld packs are
dropped (:func:`~trellis.core.pack_holdout.drop_holdout`, via the shared join)
before every metric that reads packs, so the page cannot move with the draw.
``tests/unit/analyze/test_trend.py`` pins the import and the field.

**A truncated scan is stated.** Any week whose composed reads hit their cap
carries ``truncated: true`` and the notes; the report's ``status`` is then
``truncated``.

**Edge effects, stated once.** Weeks are ISO weeks in UTC and each reader sees
only events inside its week: feedback that grades a pack from the previous week
joins to nothing in this one, and a session spanning a boundary is counted in
both. The graph-shape row is the graph as of ``generated_at``; the graph store
keeps no per-week snapshot, so it does not follow ``until``.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.feedback.attribution import served_item_ids
from trellis.learning.judged_outcomes import summarize_judged_outcomes
from trellis.learning.pack_observations import join_pack_events_with_coverage
from trellis.ops.capture_health import is_capture_surface
from trellis.ops.retrieval_adoption import summarize_retrieval_adoption
from trellis.ops.serving_builds import (
    build_label,
    stamp_fields,
    summarize_serving_builds,
)
from trellis.ops.write_health import (
    summarize_serve_attribution,
    summarize_write_health,
)
from trellis.retrieve.pack_value import collect_pack_verdicts, summarize_pack_value
from trellis.schemas import well_known
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    Event,
    ScanCoverage,
)

if TYPE_CHECKING:
    from trellis.analyze.graph_shape import GraphShapeReport
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

TREND_DEFAULT_WEEKS: Final = 8
TREND_MIN_WEEKS: Final = 1
TREND_MAX_WEEKS: Final = 26

#: Item ids under this prefix are promoted precedents.
PRECEDENT_ID_PREFIX: Final = "precedent://learning/"

#: ``PACK_ASSEMBLED.retrieval_phase`` when the caller did not state one.
PHASE_UNSET: Final = "unset"

#: Edge kinds that record where something came from or what was measured about
#: it, not what it means. The graph-shape row counts every edge kind *not* in
#: this set as semantic, so a kind added later reads as semantic until it is
#: named here: the error is toward more connectedness, and it is visible in
#: ``provenance_kinds`` beside the share.
PROVENANCE_EDGE_KINDS: Final[frozenset[str]] = frozenset(
    {
        well_known.USED,
        well_known.WAS_GENERATED_BY,
        well_known.WAS_INFORMED_BY,
        well_known.WAS_DERIVED_FROM,
        well_known.WAS_ATTRIBUTED_TO,
        well_known.WAS_ASSOCIATED_WITH,
        well_known.HAS_OBSERVATION,
        well_known.HAS_MEASUREMENT,
    }
)

#: Metrics deliberately absent from the series, with the reason, so a reader
#: asking "where is X" gets an answer on the page.
OMITTED_METRICS: Final[tuple[tuple[str, str], ...]] = (
    (
        "loop_health",
        (
            "point-in-time: it reports each loop's latest run and the tuner's "
            "live pending-proposal count, neither of which can be rebuilt for a "
            "past week, and the reader takes no window"
        ),
    ),
)

STATUS_OK: Final = "ok"
STATUS_TRUNCATED: Final = "truncated"

_WEEK = timedelta(days=7)


class TrendError(ValueError):
    """A request the report refuses: weeks out of bounds, an unparseable date."""


class TrendPackValue(TrellisModel):
    """``summarize_pack_value`` for the week."""

    packs: int = 0
    attributed_packs: int = 0
    injected_tokens: int = 0
    useful_token_fraction: float | None = None
    unhelpful_token_fraction: float | None = None
    unjudged_token_fraction: float | None = None
    suppressed: bool = False
    suppressed_reason: str = ""


class TrendServeAttribution(TrellisModel):
    """``summarize_serve_attribution`` for the week; a rate over nothing is ``None``."""

    packs: int = 0
    packs_with_injected_items: int = 0
    injected_coverage: float | None = None
    feedback_events: int = 0
    attribution_rate: float | None = None
    pack_targeted_feedback: int = 0
    pack_attribution_rate: float | None = None


class TrendJudged(TrellisModel):
    """The ``judged_outcomes`` rungs for the week (they partition ``judged``)."""

    judged: int = 0
    unservable: int = 0
    never_served: int = 0
    served_ungraded: int = 0
    graded_uncited: int = 0
    cited_unhelpful: int = 0
    cited_helpful: int = 0
    cited_share: float | None = None


class TrendWrite(TrellisModel):
    """``summarize_write_health`` for the week, whole and for capture surfaces."""

    attempts: int = 0
    rejected: int = 0
    rejection_rate: float | None = None
    capture_attempts: int = 0
    capture_rejected: int = 0
    capture_rejection_rate: float | None = None


class TrendAdoption(TrellisModel):
    """``summarize_retrieval_adoption`` for the week."""

    sessions: int = 0
    sessions_with_packs: int = 0
    adoption: float | None = None
    state: str = "insufficient_data"


class TrendFollowUp(TrellisModel):
    """Sessions that retrieved again, and when in the task the packs were asked for."""

    sessions: int = 0
    sessions_with_follow_up: int = 0
    follow_up_rate: float | None = None
    #: Served packs whose row names no session; counted here, never folded
    #: into ``sessions`` and never dropped.
    packs_without_session: int = 0
    retrieval_phase: dict[str, int] = Field(default_factory=dict)


class TrendPrecedents(TrellisModel):
    """Promoted precedents served, and how many of those servings were cited helpful.

    The counts are real zeros when nothing was served; ``helpful_share`` is the
    only field that is ``None`` for want of a denominator.
    """

    packs: int = 0
    packs_with_precedent: int = 0
    precedent_items_served: int = 0
    precedent_items_helpful: int = 0
    helpful_share: float | None = None


class TrendWeek(TrellisModel):
    """One ISO week, every metric family, and the builds that served in it."""

    week: str
    start: datetime
    end: datetime
    partial: bool = False
    truncated: bool = False
    truncation_notes: list[str] = Field(default_factory=list)
    #: Distinct commits that assembled a served pack this week. ``None`` is a
    #: build that stamped no commit, never a guessed one.
    commits: list[str | None] = Field(default_factory=list)
    packs: int = 0
    pack_value: TrendPackValue = Field(default_factory=TrendPackValue)
    serve_attribution: TrendServeAttribution = Field(
        default_factory=TrendServeAttribution
    )
    judged_outcomes: TrendJudged = Field(default_factory=TrendJudged)
    write_health: TrendWrite = Field(default_factory=TrendWrite)
    retrieval_adoption: TrendAdoption = Field(default_factory=TrendAdoption)
    follow_up: TrendFollowUp = Field(default_factory=TrendFollowUp)
    precedents: TrendPrecedents = Field(default_factory=TrendPrecedents)


class TrendBuild(TrellisModel):
    """One build segment across the whole window."""

    version: str | None = None
    commit: str | None = None
    label: str
    packs: int
    first_seen: datetime
    last_seen: datetime
    #: The ISO week that build first served in: the segment boundary.
    first_seen_week: str


class TrendGraphShape(TrellisModel):
    """Monthly-scale graph shape, read once; not a weekly series."""

    as_of: datetime
    status: str = STATUS_OK
    nodes: int = 0
    edges: int = 0
    semantic_edges: int = 0
    semantic_edge_share: float | None = None
    isolated_nodes: int = 0
    isolated_node_share: float | None = None
    provenance_kinds: list[str] = Field(default_factory=list)
    note: str = ""


class TrendOmission(TrellisModel):
    """A metric the report does not carry, and why."""

    metric: str
    reason: str


class TrendWindow(TrellisModel):
    weeks: int
    start: datetime
    end: datetime


class TrendReport(TrellisModel):
    """The series: ``weeks`` oldest first, ``builds`` across all of them."""

    status: str = STATUS_OK
    generated_at: datetime
    window: TrendWindow
    weeks: list[TrendWeek] = Field(default_factory=list)
    builds: list[TrendBuild] = Field(default_factory=list)
    graph_shape: TrendGraphShape | None = None
    omitted: list[TrendOmission] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def parse_until(value: str | None) -> datetime | None:
    """``--until`` as an aware UTC datetime; a date or naive time is read as UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"until must be an ISO-8601 date or time, got {value!r}."
        raise TrendError(msg) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _week_label(start: datetime) -> str:
    iso = start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _monday(moment: datetime) -> datetime:
    day = moment.astimezone(UTC).date()
    monday = day - timedelta(days=day.weekday())
    return datetime.combine(monday, datetime.min.time(), tzinfo=UTC)


def week_bounds(
    weeks: int, end: datetime
) -> list[tuple[str, datetime, datetime, bool]]:
    """``(label, start, end, partial)`` per ISO week, oldest first, ending at ``end``.

    ``end`` is exclusive, so a window ending exactly on a Monday closes the
    previous week. The last week is partial when ``end`` falls inside it.
    The event log's ``until`` is inclusive, so ``_week_row`` reads each week
    up to one microsecond before its ``end``.
    """
    last_start = _monday(end - timedelta(microseconds=1))
    rows: list[tuple[str, datetime, datetime, bool]] = []
    for offset in range(weeks - 1, -1, -1):
        start = last_start - offset * _WEEK
        stop = min(start + _WEEK, end)
        rows.append((_week_label(start), start, stop, start + _WEEK > end))
    return rows


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _commit_key(commit: str | None) -> tuple[int, str]:
    return (1, "") if commit is None else (0, commit)


def _follow_up(pack_events: list[Event]) -> TrendFollowUp:
    per_session: Counter[str] = Counter()
    phases: Counter[str] = Counter()
    sessionless = 0
    for event in pack_events:
        payload = event.payload or {}
        session = payload.get("session_id")
        if isinstance(session, str) and session.strip():
            per_session[session.strip()] += 1
        else:
            sessionless += 1
        phase = payload.get("retrieval_phase")
        phases[phase if isinstance(phase, str) and phase else PHASE_UNSET] += 1
    follow = sum(1 for count in per_session.values() if count >= 2)  # noqa: PLR2004
    return TrendFollowUp(
        sessions=len(per_session),
        sessions_with_follow_up=follow,
        follow_up_rate=_ratio(follow, len(per_session)),
        packs_without_session=sessionless,
        retrieval_phase=dict(sorted(phases.items())),
    )


def _precedents(
    pack_events: dict[str, Event], feedback_events: list[Event]
) -> TrendPrecedents:
    served_by_pack = {
        pack_id: {
            item
            for item in served_item_ids(event.payload or {})
            if item.startswith(PRECEDENT_ID_PREFIX)
        }
        for pack_id, event in pack_events.items()
    }
    verdicts = collect_pack_verdicts(
        feedback_events,
        {pack_id: event.payload or {} for pack_id, event in pack_events.items()},
    )
    helpful_by_pack = verdicts["helpful"]
    served = sum(len(items) for items in served_by_pack.values())
    helpful = sum(
        len(items & set(helpful_by_pack.get(pack_id, ())))
        for pack_id, items in served_by_pack.items()
    )
    return TrendPrecedents(
        packs=len(pack_events),
        packs_with_precedent=sum(1 for items in served_by_pack.values() if items),
        precedent_items_served=served,
        precedent_items_helpful=helpful,
        helpful_share=_ratio(helpful, served),
    )


def _week_row(
    event_log: EventLog,
    label: str,
    start: datetime,
    end: datetime,
    partial: bool,
    limit: int,
) -> tuple[TrendWeek, list[Event]]:
    # The event log's ``until`` is inclusive and this week's ``end`` is the
    # next week's ``start``, so read one microsecond short of it: an event
    # stamped exactly on the boundary counts in the later week only.
    read_until = end - timedelta(microseconds=1)
    window: dict[str, Any] = {"since": start, "until": read_until}
    feedback, pack_events, _, join_scan = join_pack_events_with_coverage(
        event_log, since=start, until=read_until, limit=limit
    )
    value = summarize_pack_value(event_log, limit=limit, **window)
    serve = summarize_serve_attribution(event_log, limit=limit, **window)
    judged = summarize_judged_outcomes(event_log, limit=limit, **window)
    write = summarize_write_health(event_log, limit=limit, **window)
    adoption = summarize_retrieval_adoption(event_log, limit=limit, **window)

    totals = judged.totals
    capture = [
        stats for surface, stats in write.by_tool.items() if is_capture_surface(surface)
    ]
    capture_attempts = sum(stats.attempts for stats in capture)
    capture_rejected = sum(
        stats.boundary_rejected + stats.executor_rejected for stats in capture
    )
    scans: list[ScanCoverage] = [
        join_scan,
        value.scan,
        serve.scan,
        judged.scan,
        write.scan,
        adoption.scan,
    ]
    notes = sorted({scan.note for scan in scans if scan.truncated and scan.note})
    served_events = list(pack_events.values())
    row = TrendWeek(
        week=label,
        start=start,
        end=end,
        partial=partial,
        truncated=any(scan.truncated for scan in scans),
        truncation_notes=notes,
        commits=sorted(
            {stamp_fields(event)[1] for event in served_events}, key=_commit_key
        ),
        packs=len(served_events),
        pack_value=TrendPackValue(
            packs=value.packs,
            attributed_packs=value.attributed_packs,
            injected_tokens=value.injected_tokens,
            useful_token_fraction=value.useful_token_fraction,
            unhelpful_token_fraction=value.unhelpful_token_fraction,
            unjudged_token_fraction=value.unjudged_token_fraction,
            suppressed=value.suppressed,
            suppressed_reason=value.suppressed_reason,
        ),
        serve_attribution=TrendServeAttribution(
            packs=serve.packs,
            packs_with_injected_items=serve.packs_with_injected_items,
            injected_coverage=_ratio(serve.packs_with_injected_items, serve.packs),
            feedback_events=serve.feedback_events,
            attribution_rate=_ratio(serve.feedback_attributed, serve.feedback_events),
            pack_targeted_feedback=serve.pack_targeted_feedback,
            pack_attribution_rate=_ratio(
                serve.pack_targeted_attributed, serve.pack_targeted_feedback
            ),
        ),
        judged_outcomes=TrendJudged(
            judged=totals.judged,
            unservable=totals.unservable,
            never_served=totals.never_served,
            served_ungraded=totals.served_ungraded,
            graded_uncited=totals.graded_uncited,
            cited_unhelpful=totals.cited_unhelpful,
            cited_helpful=totals.cited_helpful,
            cited_share=_ratio(totals.cited, totals.judged),
        ),
        write_health=TrendWrite(
            attempts=write.attempts,
            rejected=write.boundary_rejected + write.executor_rejected,
            rejection_rate=_ratio(
                write.boundary_rejected + write.executor_rejected, write.attempts
            ),
            capture_attempts=capture_attempts,
            capture_rejected=capture_rejected,
            capture_rejection_rate=_ratio(capture_rejected, capture_attempts),
        ),
        retrieval_adoption=TrendAdoption(
            sessions=adoption.sessions,
            sessions_with_packs=adoption.sessions_with_packs,
            adoption=adoption.adoption,
            state=adoption.state,
        ),
        follow_up=_follow_up(served_events),
        precedents=_precedents(pack_events, feedback),
    )
    return row, served_events


def _graph_row(report: GraphShapeReport, as_of: datetime) -> TrendGraphShape:
    semantic = sum(
        count
        for kind, count in report.edge_types.items()
        if kind not in PROVENANCE_EDGE_KINDS
    )
    return TrendGraphShape(
        as_of=as_of,
        status=report.status,
        nodes=report.nodes,
        edges=report.edges,
        semantic_edges=semantic,
        semantic_edge_share=_ratio(semantic, report.edges),
        isolated_nodes=report.isolation.isolated,
        isolated_node_share=_ratio(report.isolation.isolated, report.isolation.nodes),
        provenance_kinds=sorted(PROVENANCE_EDGE_KINDS),
        note=report.scan.note,
    )


def summarize_trend(
    event_log: EventLog,
    graph_store: GraphStore | None = None,
    *,
    weeks: int = TREND_DEFAULT_WEEKS,
    until: datetime | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> TrendReport:
    """The weekly series ending at ``until`` (default now), oldest week first.

    Read-only: it emits no event, writes no store and caches nothing.
    ``graph_store=None`` omits the graph-shape row and says so.
    """
    if not TREND_MIN_WEEKS <= weeks <= TREND_MAX_WEEKS:
        msg = (
            f"weeks must be between {TREND_MIN_WEEKS} and {TREND_MAX_WEEKS}, "
            f"got {weeks}."
        )
        raise TrendError(msg)
    generated_at = datetime.now(tz=UTC)
    end = _as_utc(until) if until is not None else generated_at
    bounds = week_bounds(weeks, end)

    rows: list[TrendWeek] = []
    served: list[tuple[str, Event]] = []
    for label, start, stop, partial in bounds:
        row, events = _week_row(event_log, label, start, stop, partial, limit)
        rows.append(row)
        served.extend((label, event) for event in events)

    first_week: dict[tuple[str | None, str | None], str] = {}
    for label, event in served:  # weeks arrive oldest first
        key = stamp_fields(event)[:2]
        first_week.setdefault(key, label)
    groups = summarize_serving_builds(event for _, event in served).groups
    builds = [
        TrendBuild(
            version=group.version,
            commit=group.commit,
            label=build_label(group.version, group.commit),
            packs=group.packs,
            first_seen=group.first_seen,
            last_seen=group.last_seen,
            first_seen_week=first_week[(group.version, group.commit)],
        )
        for group in sorted(groups, key=lambda g: (g.first_seen, g.label))
    ]

    graph: TrendGraphShape | None = None
    notes: list[str] = []
    if graph_store is not None:
        from trellis.analyze.graph_shape import analyze_graph_shape  # noqa: PLC0415

        graph = _graph_row(analyze_graph_shape(graph_store), generated_at)
        notes.append(
            "graph shape is the graph as of generated_at; the store keeps no "
            "per-week snapshot, so it does not follow until"
        )
    else:
        notes.append("graph shape omitted: no graph store was supplied")

    truncated = any(row.truncated for row in rows) or (
        graph is not None and graph.status == STATUS_TRUNCATED
    )
    logger.debug("trend_summarized", weeks=weeks, truncated=truncated)
    return TrendReport(
        status=STATUS_TRUNCATED if truncated else STATUS_OK,
        generated_at=generated_at,
        window=TrendWindow(weeks=weeks, start=bounds[0][1], end=end),
        weeks=rows,
        builds=builds,
        graph_shape=graph,
        omitted=[TrendOmission(metric=m, reason=r) for m, r in OMITTED_METRICS],
        notes=notes,
    )


_MARKDOWN_HEADER = (
    "The pack holdout's outcome is sealed until its pre-registered horizon and is "
    "not read here. This page shows leading indicators only: what was served, "
    "cited, retrieved again and rejected, week by week, split at build "
    "boundaries. A leading indicator can move without the outcome moving, so a "
    "change on this page is a reason to look, not a result."
)


def _cell(value: object) -> str:
    """One GFM table cell: ``-`` for a missing value, pipes and newlines escaped."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1%}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return [*lines, ""]


def _week_name(week: TrendWeek) -> str:
    marks = (" (partial)" if week.partial else "") + (
        " (truncated)" if week.truncated else ""
    )
    return f"{week.week}{marks}"


def render_markdown(report: TrendReport) -> str:
    """The report as a plain GFM page with YAML frontmatter, no HTML, no links."""
    first = report.weeks[0].start.date() if report.weeks else date.today()  # noqa: DTZ011
    last = report.window.end.date()
    lines = [
        "---",
        f"title: Trellis weekly trend, {first.isoformat()} to {last.isoformat()}",
        "---",
        "",
        _MARKDOWN_HEADER,
        "",
        (
            "Weeks are ISO weeks in UTC, oldest first. Generated "
            f"{report.generated_at:%Y-%m-%d %H:%M} UTC. Status: {report.status}."
        ),
        "",
    ]
    weeks = report.weeks

    def section(title: str, headers: list[str], pick: Any) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(
            _table(["Week", *headers], [[_week_name(w), *pick(w)] for w in weeks])
        )

    section(
        "Retrieval adoption and follow-up",
        [
            "Sessions",
            "With a pack",
            "Adoption",
            "Sessions (pack rows)",
            "Follow-up",
            "Follow-up rate",
            "Packs without session",
        ],
        lambda w: [
            w.retrieval_adoption.sessions,
            w.retrieval_adoption.sessions_with_packs,
            w.retrieval_adoption.adoption,
            w.follow_up.sessions,
            w.follow_up.sessions_with_follow_up,
            w.follow_up.follow_up_rate,
            w.follow_up.packs_without_session,
        ],
    )
    phases = sorted({p for w in weeks for p in w.follow_up.retrieval_phase})
    if phases:
        lines.append("Retrieval phase of served packs:")
        lines.append("")
        lines.extend(
            _table(
                ["Week", *phases],
                [
                    [
                        _week_name(w),
                        *(w.follow_up.retrieval_phase.get(p, 0) for p in phases),
                    ]
                    for w in weeks
                ],
            )
        )
    section(
        "Pack value",
        ["Packs", "Attributed", "Injected tokens", "Useful", "Unhelpful", "No verdict"],
        lambda w: [
            w.pack_value.packs,
            w.pack_value.attributed_packs,
            w.pack_value.injected_tokens,
            w.pack_value.useful_token_fraction,
            w.pack_value.unhelpful_token_fraction,
            w.pack_value.unjudged_token_fraction,
        ],
    )
    section(
        "Attribution and grading coverage",
        [
            "Packs",
            "With items",
            "Item coverage",
            "Feedback",
            "Attributed",
            "Pack-targeted",
            "Pack attributed",
        ],
        lambda w: [
            w.serve_attribution.packs,
            w.serve_attribution.packs_with_injected_items,
            w.serve_attribution.injected_coverage,
            w.serve_attribution.feedback_events,
            w.serve_attribution.attribution_rate,
            w.serve_attribution.pack_targeted_feedback,
            w.serve_attribution.pack_attribution_rate,
        ],
    )
    section(
        "Judged outcomes",
        [
            "Judged",
            "Unservable",
            "Never served",
            "Served, ungraded",
            "Graded, uncited",
            "Cited unhelpful",
            "Cited helpful",
            "Cited share",
        ],
        lambda w: [
            w.judged_outcomes.judged,
            w.judged_outcomes.unservable,
            w.judged_outcomes.never_served,
            w.judged_outcomes.served_ungraded,
            w.judged_outcomes.graded_uncited,
            w.judged_outcomes.cited_unhelpful,
            w.judged_outcomes.cited_helpful,
            w.judged_outcomes.cited_share,
        ],
    )
    section(
        "Write rejection",
        [
            "Attempts",
            "Rejected",
            "Rate",
            "Capture attempts",
            "Capture rejected",
            "Capture rate",
        ],
        lambda w: [
            w.write_health.attempts,
            w.write_health.rejected,
            w.write_health.rejection_rate,
            w.write_health.capture_attempts,
            w.write_health.capture_rejected,
            w.write_health.capture_rejection_rate,
        ],
    )
    section(
        "Precedents served",
        [
            "Packs",
            "With a precedent",
            "Precedent items served",
            "Cited helpful",
            "Helpful share",
        ],
        lambda w: [
            w.precedents.packs,
            w.precedents.packs_with_precedent,
            w.precedents.precedent_items_served,
            w.precedents.precedent_items_helpful,
            w.precedents.helpful_share,
        ],
    )
    lines.append("## Builds")
    lines.append("")
    lines.extend(
        _table(
            ["Build", "Packs", "First seen", "Last seen", "Boundary (first week)"],
            [
                [
                    b.label,
                    b.packs,
                    f"{b.first_seen:%Y-%m-%d %H:%M}",
                    f"{b.last_seen:%Y-%m-%d %H:%M}",
                    b.first_seen_week,
                ]
                for b in report.builds
            ],
        )
    )
    lines.extend(
        _table(
            ["Week", "Commits that served"],
            [
                [
                    _week_name(w),
                    ", ".join(c or "unidentified build" for c in w.commits) or "-",
                ]
                for w in weeks
            ],
        )
    )
    if report.graph_shape is not None:
        g = report.graph_shape
        lines.append("## Graph shape")
        lines.append("")
        lines.extend(
            _table(
                [
                    "As of",
                    "Nodes",
                    "Edges",
                    "Semantic edge share",
                    "Isolated node share",
                    "Status",
                ],
                [
                    [
                        f"{g.as_of:%Y-%m-%d}",
                        g.nodes,
                        g.edges,
                        g.semantic_edge_share,
                        g.isolated_node_share,
                        g.status,
                    ]
                ],
            )
        )
        lines.append(
            "Provenance edge kinds, not counted as semantic: "
            f"{', '.join(g.provenance_kinds)}."
        )
        lines.append("")
    notes = [n for w in weeks for n in w.truncation_notes]
    if notes or report.notes or report.omitted:
        lines.append("## Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in dict.fromkeys(notes))
        lines.extend(f"- {n}" for n in report.notes)
        lines.extend(f"- Not included, {o.metric}: {o.reason}." for o in report.omitted)
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "OMITTED_METRICS",
    "PRECEDENT_ID_PREFIX",
    "PROVENANCE_EDGE_KINDS",
    "STATUS_OK",
    "STATUS_TRUNCATED",
    "TREND_DEFAULT_WEEKS",
    "TREND_MAX_WEEKS",
    "TREND_MIN_WEEKS",
    "TrendError",
    "TrendReport",
    "TrendWeek",
    "parse_until",
    "render_markdown",
    "summarize_trend",
    "week_bounds",
]
