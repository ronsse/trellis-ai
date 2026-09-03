"""Read-side capture-health check for pack-serving surfaces.

``trellis analyze health`` (#297) reports write-boundary rejections —
but only when the operator runs it. The dogfood history behind #309
(13 invisible write rejections, 27 dark sessions) shows the operator
does not: capture fails silently exactly where nobody is looking. The
fix, borrowed from claude-mem's observer-health ledger, is to surface
the warning where the operator already looks — prepended to every
served pack — until capture recovers.

:func:`check_capture_health` is the threshold check, and the rule is
**per surface**, not global. A surface warns when it has at least
``threshold`` rejected writes in the trailing window and *zero* accepted
ones. Rejections count boundary ``WRITE_REJECTED`` plus executor
``MUTATION_REJECTED``, because a write dying at the policy gate leaves
capture exactly as dark as one dying at the boundary.

**A warning must be able to clear, and only a capture surface should
raise one** (#461). Both halves of that were broken. Accepts were read as
``MUTATION_EXECUTED`` alone, which is emitted from exactly one place, so
any surface whose success path is not a governed mutation was
*structurally unclearable* — ``mcp:save_memory``, the flagship capture
tool, clears now via ``MEMORY_STORED`` (:data:`_EXTRA_ACCEPT_EVENTS`,
:func:`accept_events_for`). And ``mcp:record_feedback`` is a *grading*
surface: it captures nothing, so its rejections are counted by ``trellis
analyze health`` but never headlined as lost experience
(:data:`NON_CAPTURE_SURFACES`, :func:`is_capture_surface`).

Per-surface is load-bearing. The incident this exists to catch — every
MCP ``save_*`` call rejected while a nightly ``trellis ingest corpus``
run keeps writing — has successful writes in the same window by
construction, so a global "zero accepted anywhere" rule would stay
silent through exactly the outage it was built for. Scoping the accept
test to the surface that is failing keeps the false-positive discipline
(a banner on *every* retrieval call must not cry wolf) without blinding
the check to the motivating case.

One label is deliberately **not** per-surface. A policy file that will
not load raises at gate-build time, before a Command exists, so it fails
every governed write on every surface at once and belongs to none of them
(#425). It is recorded under ``config:policy_file``, and because no
accepted write can ever carry that ``requested_by``, it gets its own
recovery rule: an accepted write from *any* surface, newer than its last
rejection. See :data:`_GLOBAL_SURFACE_PREFIX` — without that rule the
banner would be unable to clear, which is a worse failure than not firing.

Idempotency replays are excluded: the executor emits
``MUTATION_REJECTED`` with ``reason="idempotency_replay"`` for a command
whose write already landed, which is a duplicate submission, not dark
capture.

The knobs are read-side — they shape what retrieval serves, not what
ingest writes — and so deliberately live here, not in
:mod:`trellis.core.write_config`:

* ``TRELLIS_CAPTURE_WARN_THRESHOLD`` — rejections one surface needs to
  warn (default 3, mirroring claude-mem's three-consecutive-failures
  rule; ``0`` disables the check entirely).
* ``TRELLIS_CAPTURE_WARN_WINDOW_HOURS`` — trailing window (default 24).

Callers must treat the check as advisory telemetry and wrap it in the
same GRACEFUL-DEGRADATION posture as ``track_token_usage`` — a
health-check failure can never block or corrupt retrieval.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import EventLog, EventType, scan_events

logger = structlog.get_logger(__name__)

_THRESHOLD_ENV = "TRELLIS_CAPTURE_WARN_THRESHOLD"
_WINDOW_ENV = "TRELLIS_CAPTURE_WARN_WINDOW_HOURS"

DEFAULT_THRESHOLD = 3
DEFAULT_WINDOW_HOURS = 24

#: Cap on the detail fetch once the counts have crossed the threshold —
#: enough to attribute surfaces and date the outage without pulling an
#: unbounded window.
#:
#: The fetch is **newest-first** (#374). It used to be ascending, on the
#: reasoning that keeping the earliest rejection inside the cap keeps
#: ``since`` truthful. That trade was backwards for this check: the
#: per-surface counts are computed *from this slice*, so a surface whose
#: rejections are all newer than the cap boundary contributes zero and
#: never warns — the banner going silent through a fresh outage while a
#: noisy older one fills the slice. Under ``desc`` the cost is inverted
#: and much smaller: a truncated slice can only *understate* how long a
#: surface has been dark, and :attr:`CaptureHealthWarning.truncated` says
#: when that is possible. Missing an outage is worse than dating one
#: conservatively.
_DETAIL_LIMIT = 500

#: Surfaces named in the rendered banner; the model keeps the full list.
_MAX_NAMED_SURFACES = 3

#: Label for rejections that name no surface. Deliberately not a valid
#: ``requested_by`` value, so it can never match an accepted write.
_UNKNOWN_SURFACE = "(unknown)"

#: Executor reason for a duplicate submission of a write that already
#: landed — not a capture failure.
_REPLAY_REASON = "idempotency_replay"

#: Surfaces that record write-boundary rejections but do **not** capture
#: experience, and so are excluded from the banner (#461).
#:
#: The banner's headline is *"New experience from this session is NOT being
#: saved"*. For ``mcp:record_feedback`` that sentence is false in both
#: halves: grading a pack stores no new experience, and its success path is
#: :func:`trellis.feedback.recording.record_feedback` →
#: ``FEEDBACK_RECORDED``, which no ``MUTATION_EXECUTED`` ever accompanies.
#: The label was therefore **structurally unclearable** — three malformed
#: ratings pinned a false alarm on every retrieval call for a full window,
#: on a deployment whose capture was healthy. A banner that cries wolf is
#: one the reader learns to skip, which destroys the signal at exactly the
#: moment it is telling the truth.
#:
#: Excluded from the *banner*, not from measurement: ``trellis analyze
#: health`` (:mod:`trellis.ops.write_health`) counts these rejections
#: exactly as before, and a grading surface that is failing is still worth
#: fixing — just not under a capture headline.
#:
#: The list is a **deny-list, deliberately**. Presuming a surface non-capture
#: is a silent false negative — a write surface that goes unwatched — which
#: is the failure direction this check exists to prevent; presuming it
#: capture is merely noisy, and noisy is visible. So a surface is watched
#: unless it is named here. ``tests/unit/mcp/test_capture_surface_roster.py``
#: enumerates every ``_record_boundary_rejection`` call site and fails if its
#: tool is neither named here nor able to demonstrate an accept event, so the
#: roster is checkable rather than declared (#443's failure shape).
#:
#: ``config:advisory_file`` (#448) is the second entry, and it is here
#: against the instinct the prefix creates. A ``config:`` label *looks* like
#: ``config:policy_file``, which belongs in the banner because a policy file
#: that will not load raises at gate-build time and fails **every governed
#: write on every surface at once**. A refused *advisory* write blocks
#: nothing: ``save_memory``, ``save_experience``, ``save_knowledge``, the
#: session-capture sweep and every ingest path keep working, and the only
#: thing that did not land is a derived artefact the next curate cycle
#: regenerates. So the headline — *"New experience from this session is NOT
#: being saved"* — is false in both halves, exactly as it was for
#: ``mcp:record_feedback``; without this entry three nights of a degraded
#: ``advisories.json`` pin that sentence to the top of every pack on a
#: deployment whose capture is perfectly healthy. Two labels sharing a
#: prefix is not what earns a banner; stopping experience from being
#: written is.
#:
#: The literal is spelled out rather than imported from
#: :data:`~trellis.stores.advisory_source.ADVISORY_WRITER_SURFACE`, for the
#: same reason ``mcp:save_memory`` is below: this module runs on every
#: retrieval call, and importing ``advisory_source`` would drag
#: ``AdvisoryStore`` and its schemas onto that path to learn a string. The
#: duplication is pinned by execution rather than by eye —
#: ``tests/unit/mcp/test_capture_surface_roster.py`` matches the two
#: spellings and drives the real surface to prove no banner is raised.
NON_CAPTURE_SURFACES: frozenset[str] = frozenset(
    {"mcp:record_feedback", "config:advisory_file"}
)

#: Accept events that clear a capture surface **in addition to** the
#: executor's ``MUTATION_EXECUTED``, keyed by surface label (#461).
#:
#: ``MUTATION_EXECUTED`` is emitted from exactly one place
#: (``mutate/executor.py``), so a surface whose success path is not a
#: governed mutation cannot clear under that rule alone. ``mcp:save_memory``
#: is the case in point: its only ``MUTATION_EXECUTED`` comes from
#: ``_run_memory_extraction``, gated on ``TRELLIS_ENABLE_MEMORY_EXTRACTION``
#: which defaults to ``False``, and which returns early emitting nothing when
#: extraction yields no drafts. Its unconditional success signal is
#: ``MEMORY_STORED`` — emitted by every ``save_memory`` path that persists a
#: doc, and described in that emitter as "a hard requirement".
#:
#: The event must carry a ``requested_by`` naming the surface, because
#: ``Event.source`` is too coarse to attribute one: ``MEMORY_STORED`` has
#: three emitters and matching them by ``source`` is the looseness #458
#: refused. Adding the key to the MCP emitter is what made this entry
#: possible.
#:
#: The label is spelled out here rather than imported from
#: ``trellis.mcp.server.SAVE_MEMORY_SURFACE``: ``ops`` must not depend on
#: the MCP layer, and an ops module importing a tool server to learn a
#: string is a worse coupling than a duplicated literal. The duplication is
#: pinned by *execution* rather than by eye —
#: ``tests/unit/mcp/test_capture_surface_roster.py`` calls the real
#: ``save_memory`` and requires the banner to clear, so the two spellings
#: drifting apart fails a test instead of silently unclearing a surface.
_EXTRA_ACCEPT_EVENTS: dict[str, tuple[EventType, ...]] = {
    "mcp:save_memory": (EventType.MEMORY_STORED,),
}

#: Prefix marking a surface whose failure is **global** rather than
#: per-surface: the deployment's own configuration would not load, so no
#: write on any surface could have been attempted. Today that is
#: ``config:policy_file`` (#425) — a policy file that raises at gate-build
#: time, before a Command exists.
#:
#: The prefix decides the *recovery rule*, not whether a banner is raised at
#: all: ``config:advisory_file`` (#448) shares it and is nonetheless in
#: :data:`NON_CAPTURE_SURFACES`, because a refused advisory write stops no
#: capture path. A label reaches this rule only after
#: :func:`is_capture_surface` has already let it through.
#:
#: It needs its own recovery rule. The per-surface rule asks whether *this*
#: label landed an accepted write, and no ``MUTATION_EXECUTED`` is ever
#: attributed to a config label, so under that rule the banner could never
#: clear: a one-character fix would leave it crying wolf for a full window
#: on a deployment that was writing normally again. The evidence that a
#: global condition is over is an accepted write **newer than the last
#: rejection**, anywhere — which is exactly what a loaded gate produces and
#: a broken one cannot.
#:
#: "Newer than the last rejection", not "anywhere in the window": on a
#: deployment with steady traffic there is nearly always an accept
#: somewhere in a 24h window, so the looser test would silence the banner
#: through the outage instead of after it.
_GLOBAL_SURFACE_PREFIX = "config:"


class CaptureHealthWarning(TrellisModel):
    """Capture-failure state for the surfaces that are failing outright.

    ``failing_surfaces`` are executor-style labels (``mcp:save_experience``),
    ordered most-rejected first; each one has ``threshold``-or-more
    rejections and no accepted write in the window. ``rejected`` counts
    only those surfaces' rejections. ``since`` is their earliest rejection
    *within the scanned slice* — the "dark since when" an operator needs,
    and exact unless :attr:`truncated` is set.
    """

    window_hours: int
    rejected: int
    #: Always 0 by construction (an accepted write clears the surface it
    #: belongs to); kept explicit so the payload documents the rule.
    accepted: int = 0
    failing_surfaces: list[str] = Field(default_factory=list)
    since: datetime
    #: The detail fetch hit :data:`_DETAIL_LIMIT`, so ``rejected`` is a
    #: floor and ``since`` may be later than the true onset. Never affects
    #: whether the banner fires — the threshold gate is a ``count()`` over
    #: the whole window and is not capped.
    truncated: bool = False


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int knob; malformed values fall back loudly."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("capture_health.bad_env_int", var=name, value=raw)
        return default
    return value if value >= 0 else default


def _as_utc(moment: datetime) -> datetime:
    """Normalise a stored timestamp to aware UTC.

    Backends differ: SQLite round-trips the UTC ISO string it was given,
    while Postgres hands back a ``timestamptz`` in the session timezone.
    Comparing or formatting those without normalising either raises on
    the naive/aware mix or renders a local wall clock labelled ``UTC``.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _surface_label(name: str, payload_key: str) -> str:
    # Boundary events store the bare tool name; executor events already
    # carry ``mcp:<tool>`` in ``requested_by``. Normalise so both stages
    # aggregate under one surface label (same rule as write_health).
    if payload_key == "tool" and name != _UNKNOWN_SURFACE and ":" not in name:
        return f"mcp:{name}"
    return name


def _rejections_by_surface(
    event_log: EventLog, since: datetime
) -> tuple[dict[str, int], dict[str, datetime], dict[str, datetime], bool]:
    """Count rejections per surface, date each surface's first and last, flag caps.

    The *latest* is only read for a global surface, where recovery is "an
    accepted write after the last rejection" (see
    :data:`_GLOBAL_SURFACE_PREFIX`); it is collected for every label anyway
    because it costs nothing over a slice already being walked, and a
    per-label special case in the scan is one more thing to keep in sync.

    Unlike ``earliest``, ``latest`` is **exact under truncation**: the
    detail fetch is newest-first (#374), so a capped slice keeps the newest
    rejections and drops the oldest. That is what makes the global recovery
    rule safe — an understated ``latest`` would compare accepts against a
    rejection older than the real last one and could clear the banner
    early.
    """
    counts: dict[str, int] = {}
    earliest: dict[str, datetime] = {}
    latest: dict[str, datetime] = {}
    truncated = False
    for event_type, payload_key in (
        (EventType.WRITE_REJECTED, "tool"),
        (EventType.MUTATION_REJECTED, "requested_by"),
    ):
        scan = scan_events(
            event_log, event_type=event_type, since=since, limit=_DETAIL_LIMIT
        )
        truncated = truncated or scan.coverage.truncated
        for event in scan.events:
            if str(event.payload.get("reason") or "") == _REPLAY_REASON:
                continue
            name = str(event.payload.get(payload_key) or "") or _UNKNOWN_SURFACE
            label = _surface_label(name, payload_key)
            counts[label] = counts.get(label, 0) + 1
            occurred = _as_utc(event.occurred_at)
            if label not in earliest or occurred < earliest[label]:
                earliest[label] = occurred
            if label not in latest or occurred > latest[label]:
                latest[label] = occurred
    return counts, earliest, latest, truncated


def is_capture_surface(label: str) -> bool:
    """Does a rejection on ``label`` mean new experience is being lost?

    True unless the label is named in :data:`NON_CAPTURE_SURFACES` — see
    that constant for why the default is *watched*.
    """
    return label not in NON_CAPTURE_SURFACES


def accept_events_for(label: str) -> tuple[EventType, ...]:
    """Event types whose presence clears ``label``'s rejections.

    ``MUTATION_EXECUTED`` always counts — it is what the governed pipeline
    emits for a write attributed to this surface — plus whatever
    :data:`_EXTRA_ACCEPT_EVENTS` adds for a surface whose success path is
    not a governed mutation. Empty for a non-capture surface: nothing needs
    to clear a banner it can never raise.

    Public because the roster guard reads it. A declared event type that
    :func:`_surface_has_accepts` would not honour is caught there, by
    executing the clear rather than trusting the declaration.

    The empty return for a non-capture surface is safe only because
    :func:`check_capture_health` filters those labels out before any accept
    lookup — read literally through :func:`_surface_has_accepts` it would
    say "no accepts", i.e. dark. Keep that filter upstream of the lookup.
    """
    if not is_capture_surface(label):
        return ()
    return (EventType.MUTATION_EXECUTED, *_EXTRA_ACCEPT_EVENTS.get(label, ()))


def _surface_has_accepts(
    event_log: EventLog,
    label: str,
    since: datetime,
    *,
    last_rejection: datetime | None = None,
) -> bool:
    """Has this surface landed any write that clears its rejections?

    Pushed into the backend with ``limit=1``: the question is existence,
    and the accept volume of a healthy surface is exactly what must not be
    pulled into a retrieval call.

    Two rules, because there are two kinds of surface. A normal one is
    cleared by an accepted write **of its own** anywhere in the window. A
    global one (:data:`_GLOBAL_SURFACE_PREFIX`) is cleared by an accepted
    write from **any** surface after its last rejection — it can never have
    an accept of its own, and its failure mode blocks every surface at
    once, so any write landing after it proves the condition is over.

    "An accepted write of its own" is not one event type but
    :func:`accept_events_for`: a surface that does not write through the
    governed pipeline has no ``MUTATION_EXECUTED`` to find, and reading only
    that one is what made ``mcp:save_memory`` unclearable (#461).
    """
    if label == _UNKNOWN_SURFACE:
        # No surface to attribute an accept to; the rejections stand.
        return False
    if label.startswith(_GLOBAL_SURFACE_PREFIX):
        return bool(
            event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                since=last_rejection or since,
                limit=1,
            )
        )
    return any(
        event_log.get_events(
            event_type=event_type,
            since=since,
            limit=1,
            payload_filters={"requested_by": label},
        )
        for event_type in accept_events_for(label)
    )


def check_capture_health(
    event_log: EventLog,
    *,
    threshold: int | None = None,
    window_hours: int | None = None,
) -> CaptureHealthWarning | None:
    """Return the capture warning iff some surface has gone dark.

    A surface warns when it is a **capture** surface
    (:func:`is_capture_surface`), the trailing ``window_hours`` hold at
    least ``threshold`` of its rejected writes (``WRITE_REJECTED`` +
    ``MUTATION_REJECTED``, idempotency replays excluded), and *none* of its
    accept events (:func:`accept_events_for`) is attributed to it; returns
    ``None`` when no surface qualifies. Explicit kwargs win over the env
    knobs, which win over the defaults.

    The healthy path costs two ``count()`` queries: the window's total
    rejections bound every per-surface count, so falling short of the
    threshold rules out every surface at once and no event rows are
    fetched. This runs on every retrieval call.

    Because that gate is a ``count()`` over the whole window rather than a
    capped read, the detail fetch's cap can never *silence* the banner —
    only understate how many rejections a surface has and how long it has
    been dark, which :attr:`CaptureHealthWarning.truncated` discloses.
    """
    resolved_threshold = (
        threshold
        if threshold is not None
        else _env_int(_THRESHOLD_ENV, DEFAULT_THRESHOLD)
    )
    if resolved_threshold <= 0:
        return None
    resolved_window = (
        window_hours
        if window_hours is not None
        else _env_int(_WINDOW_ENV, DEFAULT_WINDOW_HOURS)
    )
    since = datetime.now(tz=UTC) - timedelta(hours=resolved_window)

    total_rejected = event_log.count(
        event_type=EventType.WRITE_REJECTED, since=since
    ) + event_log.count(event_type=EventType.MUTATION_REJECTED, since=since)
    if total_rejected < resolved_threshold:
        return None

    counts, earliest, latest, truncated = _rejections_by_surface(event_log, since)
    # Drop non-capture surfaces here rather than in the comprehension below,
    # so the rule lives in exactly one place: everything downstream — the
    # accept lookup, ``rejected``, ``since`` — then sees only labels whose
    # failure means lost experience, and no later edit can reach the accept
    # test without passing this filter first (#461).
    counts = {
        label: count for label, count in counts.items() if is_capture_surface(label)
    }
    failing = [
        (label, count)
        for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= resolved_threshold
        and not _surface_has_accepts(
            event_log, label, since, last_rejection=latest.get(label)
        )
    ]
    if not failing:
        return None

    return CaptureHealthWarning(
        window_hours=resolved_window,
        rejected=sum(count for _, count in failing),
        failing_surfaces=[label for label, _ in failing],
        since=min(earliest[label] for label, _ in failing),
        truncated=truncated,
    )


def format_capture_warning(warning: CaptureHealthWarning) -> str:
    """Render the warning as the markdown banner packs prepend.

    Blockquoted so it reads as a banner distinct from pack headings, and
    single-block short — it rides inside a token-budgeted response on
    every retrieval call while capture stays down. Deliberately *outside*
    the caller's ``max_tokens``: the pack is budgeted first and the
    banner prepended after, because a warning that the budget can evict
    is a warning that disappears on exactly the small packs a dark
    capture path produces.
    """
    named = ", ".join(warning.failing_surfaces[:_MAX_NAMED_SURFACES]) or (
        _UNKNOWN_SURFACE
    )
    overflow = len(warning.failing_surfaces) - _MAX_NAMED_SURFACES
    if overflow > 0:
        named += f" (+{overflow} more)"
    since = _as_utc(warning.since).strftime("%Y-%m-%d %H:%M UTC")
    at_least = "at least " if warning.truncated else ""
    since_clause = (
        f"since at or before {since}" if warning.truncated else f"since {since}"
    )
    return (
        "> **WARNING: memory capture is failing.** "
        f"{at_least}{warning.rejected} write attempt(s) rejected and 0 accepted "
        f"in the last {warning.window_hours}h from: {named} ({since_clause}). "
        "New experience from this session is NOT being saved. "
        "Diagnose with `trellis analyze health`."
    )
