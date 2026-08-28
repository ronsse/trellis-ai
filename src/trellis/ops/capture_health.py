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
capture exactly as dark as one dying at the boundary; accepts are
``MUTATION_EXECUTED``.

Per-surface is load-bearing. The incident this exists to catch — every
MCP ``save_*`` call rejected while a nightly ``trellis ingest corpus``
run keeps writing — has successful writes in the same window by
construction, so a global "zero accepted anywhere" rule would stay
silent through exactly the outage it was built for. Scoping the accept
test to the surface that is failing keeps the false-positive discipline
(a banner on *every* retrieval call must not cry wolf) without blinding
the check to the motivating case.

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
) -> tuple[dict[str, int], dict[str, datetime], bool]:
    """Count rejections per surface, date each surface's earliest, flag caps."""
    counts: dict[str, int] = {}
    earliest: dict[str, datetime] = {}
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
    return counts, earliest, truncated


def _surface_has_accepts(event_log: EventLog, label: str, since: datetime) -> bool:
    """Has this surface landed any write in the window?

    Pushed into the backend as a payload filter with ``limit=1``: the
    question is existence, and the accept volume of a healthy surface is
    exactly what must not be pulled into a retrieval call.
    """
    if label == _UNKNOWN_SURFACE:
        # No surface to attribute an accept to; the rejections stand.
        return False
    return bool(
        event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED,
            since=since,
            limit=1,
            payload_filters={"requested_by": label},
        )
    )


def check_capture_health(
    event_log: EventLog,
    *,
    threshold: int | None = None,
    window_hours: int | None = None,
) -> CaptureHealthWarning | None:
    """Return the capture warning iff some surface has gone dark.

    A surface warns when the trailing ``window_hours`` hold at least
    ``threshold`` of its rejected writes (``WRITE_REJECTED`` +
    ``MUTATION_REJECTED``, idempotency replays excluded) and *no*
    accepted write (``MUTATION_EXECUTED``) attributed to it; returns
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

    counts, earliest, truncated = _rejections_by_surface(event_log, since)
    failing = [
        (label, count)
        for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= resolved_threshold
        and not _surface_has_accepts(event_log, label, since)
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
