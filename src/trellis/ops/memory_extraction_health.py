"""Memory-extraction availability — the health signal behind #987.

``TRELLIS_ENABLE_MEMORY_EXTRACTION=1`` asks ``save_memory`` to mine entity
and edge drafts out of what it stores, through an LLM client. When no
client can be built the MCP server serves the save and extracts nothing, and
the only record used to be an ``info`` log line that no shipped
configuration prints. A deployment with the flag on and extraction dead is
indistinguishable, from the outside, from one that never turned it on.

The signal is one event, :attr:`EventType.MEMORY_EXTRACTION_STATE`, emitted
on a **state change** and never per write (:func:`emit_memory_extraction_state`
is the writer; the state machine that decides *when* lives next to the
extractor cache in :mod:`trellis.mcp.server`). Two states are written:

* ``unavailable`` — the flag is on and no extractor could be built.
  ``reason`` is a closed slug (:data:`UNAVAILABLE_REASONS`).
* ``available`` — an extractor was built. Written on the first successful
  build in a process as well as on recovery, so a restart that fixes the
  problem leaves positive evidence and the warning can clear.

A deployment with the flag off writes nothing, and a healthy one writes a
single ``available`` row per process start.

:func:`summarize_memory_extraction` is the reader. It reports the state the
newest event in the window says, and since when. Three limits are deliberate:

* **Newest event wins across processes.** The stdio MCP children and the HTTP
  server each write their own transitions to one log, so the reader sees the
  interleaving and has no way to attribute a row to a process. A deployment
  split between a working and a broken process reads as whichever wrote last.
* **A restart with the flag off does not clear an old ``unavailable``.** Such a
  process writes nothing, so the row ages out of the window instead.
* **A window with no event is ``unobserved``, not healthy.** The writer
  refreshes a persistent ``unavailable`` (either reason) once a day
  (``trellis.mcp.server._MEMORY_EXTRACTOR_REFRESH_SECONDS``) so a broken
  process older than the window does not vanish from the report. There is no
  background timer: the refresh is written by a save that arrives after the
  retry interval, so a broken process that stops receiving saves stops
  refreshing and its last row ages out of the window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    _as_utc,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

__all__ = [
    "REASON_BUILD_FAILED",
    "REASON_NO_LLM_CLIENT",
    "STATE_AVAILABLE",
    "STATE_UNAVAILABLE",
    "MemoryExtractionHealthReport",
    "emit_memory_extraction_state",
    "summarize_memory_extraction",
]

#: Surface label on the event's ``source`` column.
MEMORY_EXTRACTION_SURFACE = "mcp:memory_extraction"

STATE_AVAILABLE: Final = "available"
STATE_UNAVAILABLE: Final = "unavailable"

#: Why an extractor could not be built. Closed, so a group-by is exhaustive.
#: ``no_llm_client`` is the transient-capable one (a route outage, a config
#: read race); ``build_failed`` is a construction error (a client or the
#: extractor raised). The server retries both on the same interval and never
#: raises either at a save.
REASON_NO_LLM_CLIENT: Final = "no_llm_client"
REASON_BUILD_FAILED: Final = "build_failed"
UNAVAILABLE_REASONS: Final = frozenset({REASON_NO_LLM_CLIENT, REASON_BUILD_FAILED})

#: Rows the reader walks. State-change events are rare by construction, so
#: this is a ceiling a healthy log never reaches; ``scan.truncated`` says
#: when it was.
_EVENT_LIMIT = DEFAULT_SCAN_LIMIT


def emit_memory_extraction_state(
    event_log: EventLog,
    *,
    state: str,
    reason: str | None = None,
    error_type: str | None = None,
) -> bool:
    """Emit one ``MEMORY_EXTRACTION_STATE`` row. Returns whether it landed.

    Advisory and wrapped whole: it runs on the ``save_memory`` write path,
    where one escaped exception fails the write, so no exception type is
    singled out. ``False`` tells the caller the row did not land and the
    transition is still unreported. ``error_type`` is a class name only,
    never a message, which can carry a path or a DSN fragment.
    """
    try:
        event_log.emit(
            EventType.MEMORY_EXTRACTION_STATE,
            source=MEMORY_EXTRACTION_SURFACE,
            payload={"state": state, "reason": reason, "error_type": error_type},
        )
    except Exception:
        logger.warning(
            "memory_extraction_state_emit_failed", state=state, exc_info=True
        )
        return False
    return True


class MemoryExtractionHealthReport(TrellisModel):
    """What the event log says about whether memory extraction is running."""

    #: ``unobserved`` when the window holds no state event — a deployment
    #: with the flag off, one that has never saved a memory, or a state
    #: older than the window. Not a claim that extraction works.
    state: Literal["unobserved", "available", "unavailable"] = "unobserved"
    #: Why, for ``unavailable``; ``None`` otherwise.
    reason: str | None = None
    #: When the newest run of identical states began, so "dark since".
    since: datetime | None = None
    #: ``unavailable`` rows in the window, refreshes included. A count of
    #: events, not of failed saves.
    unavailable_events: int = 0
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


def summarize_memory_extraction(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _EVENT_LIMIT,
) -> MemoryExtractionHealthReport:
    """Read the extraction state over the trailing ``days``."""
    since = datetime.now(tz=UTC) - timedelta(days=days)
    scan = scan_events(
        event_log,
        event_type=EventType.MEMORY_EXTRACTION_STATE,
        since=since,
        limit=limit,
    )
    state: str | None = None
    reason: str | None = None
    state_since: datetime | None = None
    unavailable = 0
    for event in scan.events:  # ascending
        payload: dict[str, Any] = event.payload or {}
        row_state = payload.get("state")
        if row_state not in (STATE_AVAILABLE, STATE_UNAVAILABLE):
            continue
        if row_state == STATE_UNAVAILABLE:
            unavailable += 1
        if row_state != state:
            state_since = _as_utc(event.occurred_at)
        state = row_state
        reason = payload.get("reason") if row_state == STATE_UNAVAILABLE else None
    if state is None:
        return MemoryExtractionHealthReport(scan=scan.coverage)
    return MemoryExtractionHealthReport(
        state=state,  # type: ignore[arg-type]
        reason=reason if isinstance(reason, str) else None,
        since=state_since,
        unavailable_events=unavailable,
        scan=scan.coverage,
    )
