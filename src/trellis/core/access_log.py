"""The per-key access log: what did this API key read, and when did it last run?

One ``access.key_read`` event per authenticated read by an API key, written at
the transport boundary (``trellis_api.access_log`` for REST,
``trellis.mcp.access_log`` for MCP) and never by retrieval. Nothing here may
touch ``PACK_ASSEMBLED``, a pack, or ``src/trellis/retrieve/``: the log records
what a response carried, it does not shape one.

* **What a row holds.** The key id, the surface (``rest`` / ``mcp``), the route
  or tool, the true count of items returned, and the first
  :data:`MAX_LOGGED_ITEM_IDS` item ids with ``item_ids_truncated`` beside them.
  No document text, no query or intent text, no secret.
* **Who is logged.** A call with a key id. The legacy shared secret, auth mode
  ``off`` and an anonymous call carry no key id and record nothing.
* **Advisory, wrapped whole.** :func:`record_key_read` catches every exception,
  as :func:`trellis.core.version.resolve_stamp_staleness` does, because one
  escaped exception would fail a read that had already succeeded. A failure is
  not lost: it emits an ``access.log_failed`` event (the repo's health signal is
  an event, not a probe; an ``info`` line is invisible on the default CLI) and a
  ``warning`` line, both at most once a minute per process, and
  :func:`summarize_key_reads` reports the count beside the per-key numbers.
* **Off means no row.** ``TRELLIS_ACCESS_LOG`` (``write_config.access_log``,
  default on) is read first; off returns before anything else is touched.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.base import TrellisModel
from trellis.core.write_config import resolve_write_behaviour
from trellis.stores.base.event_log import EventType, scan_events

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from trellis.stores.base.api_key import ApiKeyStore
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

SURFACE_REST = "rest"
SURFACE_MCP = "mcp"

#: Item ids kept on one row. The true count rides beside them, so a cut is
#: visible, and a pack-sized answer cannot make a row grow without bound.
MAX_LOGGED_ITEM_IDS = 25
#: One id is clipped to this many characters (an id is a short identifier;
#: the clip bounds a hostile one).
_MAX_ID_CHARS = 200
#: ``source`` of every row this module writes.
ACCESS_LOG_SOURCE = "access_log"
#: Failure signal rate limit: a store that is down fails every read, and the
#: signal must not double that load.
_FAILURE_SIGNAL_INTERVAL_S = 60.0

_last_failure_signal: list[float] = []


def clip_item_ids(item_ids: Iterable[str]) -> list[str]:
    """The ids a row keeps: the first :data:`MAX_LOGGED_ITEM_IDS`, each clipped."""
    kept: list[str] = []
    for item_id in item_ids:
        if len(kept) >= MAX_LOGGED_ITEM_IDS:
            break
        kept.append(str(item_id)[:_MAX_ID_CHARS])
    return kept


def build_payload(
    *,
    key_id: str,
    surface: str,
    route: str,
    item_ids: list[str],
    item_count: int | None,
) -> dict[str, Any]:
    """The ``access.key_read`` payload. ``item_count`` is the true count."""
    kept = clip_item_ids(item_ids)
    return {
        "key_id": key_id,
        "surface": surface,
        "route": route,
        "item_count": item_count,
        "item_ids": kept,
        "item_ids_truncated": item_count is not None and item_count > len(kept),
    }


def record_key_read(
    registry: StoreRegistry | None,
    *,
    key_id: str | None,
    surface: str,
    route: str,
    items: Callable[[], tuple[Iterable[str], int | None]],
) -> None:
    """Record one authenticated read. Never raises; a keyless call records nothing.

    ``items`` returns ``(item_ids, item_count)`` and is called only after the
    knob and the key id have passed, inside the wrapped step, so reading the
    response is part of the advisory work: off costs nothing, and a response
    that cannot be read cannot fail the read.
    """
    try:
        if not key_id or registry is None:
            return
        if not resolve_write_behaviour(registry).access_log:
            return
        item_ids, item_count = items()
        registry.operational.event_log.emit(
            EventType.KEY_READ,
            ACCESS_LOG_SOURCE,
            entity_id=key_id,
            entity_type="api_key",
            payload=build_payload(
                key_id=key_id,
                surface=surface,
                route=route,
                item_ids=list(item_ids),
                item_count=item_count,
            ),
        )
    except Exception as exc:  # advisory step; never fail the read
        signal_failure(registry, surface, exc)


def signal_failure(
    registry: StoreRegistry | None, surface: str, exc: BaseException
) -> None:
    """Leave an operator-visible trace of a failed access-log step. Never raises."""
    try:
        now = time.monotonic()
        if (
            _last_failure_signal
            and now - _last_failure_signal[0] < _FAILURE_SIGNAL_INTERVAL_S
        ):
            return
        _last_failure_signal[:] = [now]
        error_type = type(exc).__name__
        # ``warning`` is visible on the default CLI; the class name only, since
        # a message can carry a DSN.
        logger.warning(
            "access_log.record_failed", surface=surface, error_type=error_type
        )
        if registry is None:
            return
        registry.operational.event_log.emit(
            EventType.ACCESS_LOG_FAILED,
            ACCESS_LOG_SOURCE,
            payload={"surface": surface, "error_type": error_type},
        )
    except Exception:  # the signal is itself advisory
        return


class KeyReadSummary(TrellisModel):
    """One key's reads within a window."""

    key_id: str
    #: The key's name today, from the key store; ``None`` when it has no row.
    name: str | None = None
    #: ``True`` when the key store says the key is revoked; ``None`` unknown.
    revoked: bool | None = None
    reads: int
    #: Sum of the true ``item_count`` over the key's reads (an uncountable
    #: route adds nothing).
    items_returned: int
    #: ISO-8601 time of the key's latest read inside the window.
    last_read_at: str
    by_surface: dict[str, int]


class KeyReadReport(TrellisModel):
    """Per-key last-used time and read counts over a window."""

    since: str
    window_hours: float
    keys: list[KeyReadSummary]
    #: ``access.log_failed`` rows in the window: reads that went unrecorded.
    log_failures: int
    #: ``True`` when the scan cap bit, so older reads in the window are missing.
    truncated: bool
    covered_since: str


def summarize_key_reads(
    event_log: EventLog,
    *,
    window_hours: float,
    key_id: str | None = None,
    key_store: ApiKeyStore | None = None,
    now: datetime | None = None,
) -> KeyReadReport:
    """Aggregate ``access.key_read`` events into one row per key.

    Derived from the events alone; the key store is read only to attach each
    key's current name and revoked flag, and nothing is written to it.
    """
    since = (now or datetime.now(tz=UTC)) - timedelta(hours=window_hours)
    scan = scan_events(event_log, event_type=EventType.KEY_READ, since=since)
    reads: dict[str, dict[str, Any]] = {}
    for event in scan.events:
        row_key = event.payload.get("key_id")
        if not isinstance(row_key, str) or (key_id and row_key != key_id):
            continue
        agg = reads.setdefault(
            row_key, {"reads": 0, "items": 0, "last": event.occurred_at, "surf": {}}
        )
        agg["reads"] += 1
        count = event.payload.get("item_count")
        if isinstance(count, int) and not isinstance(count, bool):
            agg["items"] += count
        agg["last"] = max(agg["last"], event.occurred_at)
        surface = str(event.payload.get("surface", "unknown"))
        agg["surf"][surface] = agg["surf"].get(surface, 0) + 1

    known = {}
    if key_store is not None:
        known = {record.key_id: record for record in key_store.list()}
    summaries = []
    for row_key, agg in reads.items():
        record = known.get(row_key)
        summaries.append(
            KeyReadSummary(
                key_id=row_key,
                name=record.name if record is not None else None,
                revoked=record.revoked_at is not None if record is not None else None,
                reads=agg["reads"],
                items_returned=agg["items"],
                last_read_at=agg["last"].isoformat(),
                by_surface=dict(sorted(agg["surf"].items())),
            )
        )
    summaries.sort(key=lambda s: s.last_read_at, reverse=True)
    return KeyReadReport(
        since=since.isoformat(),
        window_hours=window_hours,
        keys=summaries,
        log_failures=event_log.count(
            event_type=EventType.ACCESS_LOG_FAILED, since=since
        ),
        truncated=scan.coverage.truncated,
        covered_since=scan.coverage.covered_since,
    )
