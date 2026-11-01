"""The per-key access log: what did this API key read, and when did it last run?

One ``access.key_read`` event per authenticated read by an API key, written at
the transport boundary (``trellis_api.access_log`` for REST,
``trellis.mcp.access_log`` for MCP) and never by retrieval. Nothing here may
touch ``PACK_ASSEMBLED``, a pack, or ``src/trellis/retrieve/``: the log records
what a response carried, it does not shape one.

* **Off by default.** ``TRELLIS_ACCESS_LOG`` (``write_config.access_log``) must
  be set to a truthy spelling. Rows have **no retention path**: the event log
  has no prune or delete, so every row stays until the database is dealt with
  by hand (about 1.6 KB a row on SQLite).
* **What a row holds.** The key id, the surface (``rest`` / ``mcp``), the route
  or tool, the item count (:func:`build_payload` says how each surface counts)
  and the first :data:`MAX_LOGGED_ITEM_IDS` item ids with ``item_ids_truncated``
  beside them. No document text, no query or intent text, no secret. A route or
  tool that does not name its items records ``item_count: null`` and no ids,
  never a guess.
* **Who is logged.** A call with a key id. The legacy shared secret, auth mode
  ``off`` and an anonymous call carry no key id and record nothing.
* **Off the request path.** The transport hands one entry to
  :class:`AccessLogWriter`: a bounded in-process queue drained by one dedicated
  daemon thread. Submitting never blocks and never raises; a full queue drops
  the entry and counts it. The emit, and any parsing of a REST response body,
  happen on that thread, never on the event loop and never on anyio's worker
  pool. The one exception is :func:`note_printed_items`, a substring search of
  a pack tool's answer on the tool's own worker thread. The queue holds at most
  :data:`MAX_QUEUE_ENTRIES` entries and :data:`MAX_QUEUE_BYTES` of held response
  bodies (16 MiB). The store is safe for a writer thread: SQLite opens one
  connection per thread and Postgres draws from its pool.
* **Advisory, wrapped whole.** Every step catches every exception, as
  :func:`trellis.core.version.resolve_stamp_staleness` does, because one
  escaped exception would fail a read that had already succeeded. A read that
  could not be recorded (dropped, or its emit failed) is counted in process,
  and the next ``access.log_failed`` row that the store accepts carries the
  counts. That row is rate limited to one per minute per process, so a row is
  a signal, not a tally of reads.
* **Shutdown** drains for at most :data:`SHUTDOWN_DRAIN_S` and never blocks
  exit: the writer is a daemon thread.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import threading
import time
from collections import deque
from dataclasses import dataclass
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
#: Entries waiting for the writer thread, and the response bytes they hold.
MAX_QUEUE_ENTRIES = 1024
MAX_QUEUE_BYTES = 16 * 1024 * 1024
#: Bound on a shutdown drain; a stuck store cannot hold the process past it.
SHUTDOWN_DRAIN_S = 2.0
#: Failure signal rate limit: a store that is down fails every read, and the
#: signal must not double that load.
_FAILURE_SIGNAL_INTERVAL_S = 60.0
#: Fixed cost charged to the byte bound for an entry, on top of its body.
ENTRY_OVERHEAD_BYTES = 512
#: Cap on rows one report scan reads.
_REPORT_SCAN_LIMIT = 5000

#: A read tool fills this holder with the ids it chose to serve (MCP). A
#: ``ContextVar`` holds a reference to one mutable object, so a tool that runs
#: on a worker thread (which gets a *copy* of the context) still fills the
#: holder the middleware reads.
_served: contextvars.ContextVar[ServedItems | None] = contextvars.ContextVar(
    "access_log_served_items", default=None
)


class ServedItems:
    """The ids one read tool served: a count, and the first few ids."""

    __slots__ = ("count", "filled", "ids")

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.count = 0
        #: ``False`` until a tool names its items; an unfilled holder means
        #: "this tool did not say", recorded as ``item_count: null``.
        self.filled = False

    def add(self, item_ids: Iterable[str]) -> None:
        self.filled = True
        for item_id in item_ids:
            self.count += 1
            if len(self.ids) < MAX_LOGGED_ITEM_IDS:
                self.ids.append(str(item_id)[:_MAX_ID_CHARS])

    def result(self) -> tuple[list[str], int | None]:
        return (list(self.ids), self.count if self.filled else None)


def begin_served_items() -> tuple[ServedItems, contextvars.Token[ServedItems | None]]:
    """Open a holder for the current context (the MCP middleware calls this)."""
    holder = ServedItems()
    return holder, _served.set(holder)


def end_served_items(token: contextvars.Token[ServedItems | None]) -> None:
    """Close the holder opened by :func:`begin_served_items`. Never raises."""
    try:
        _served.reset(token)
    except Exception:  # advisory; a stale token must not fail a read
        return


def served_ids_in(text: str, item_ids: Iterable[str]) -> list[str]:
    """The ids in ``item_ids`` that ``text`` prints as `` `id` ``, each once, in order.

    The ids are the caller's own (a pack's items); ``text`` is only searched
    for them, never read for ids. A formatter stops listing items when its token
    budget runs out, so this is the set the reader actually received.

    Known bound: ``text`` is the whole answer, so an id quoted in backticks by an
    item's body, the caller's intent or a section name counts as printed even if
    its own entry was cut.
    """
    return [i for i in dict.fromkeys(item_ids) if i and f"`{i}`" in text]


def note_printed_items(text: str, item_ids: Iterable[str]) -> None:
    """A read tool names the pack ids its finished answer printed. No-op, no holder.

    ``item_ids`` are ids the tool already holds as structured data; only those
    that ``text`` prints in backticks are recorded (:func:`served_ids_in`). Never
    raises, and does no work when no holder is open.
    """
    try:
        holder = _served.get()
        if holder is not None:
            holder.add(served_ids_in(text, item_ids))
    except Exception:  # advisory; a tool must not fail on its own log
        return


def note_served_items(item_ids: Iterable[str]) -> None:
    """A read tool names the ids it served. A no-op with no holder open.

    For a tool whose formatter reports which ids it served (``get_items``), and
    for an empty answer. Called with ids the tool already holds as structured
    data, never parsed out of what it printed, so no document text can reach a
    row. A pack tool uses :func:`note_printed_items`, because its formatter can
    drop items for budget after they were chosen. Never raises.
    """
    try:
        holder = _served.get()
        if holder is not None:
            holder.add(item_ids)
    except Exception:  # advisory; a tool must not fail on its own log
        return


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
    """The ``access.key_read`` payload; ``item_count`` is the caller's count.

    REST counts the items in the answer's body; MCP counts the pack ids the
    answer printed.
    """
    kept = clip_item_ids(item_ids)
    return {
        "key_id": key_id,
        "surface": surface,
        "route": route,
        "item_count": item_count,
        "item_ids": kept,
        "item_ids_truncated": item_count is not None and item_count > len(kept),
    }


def access_log_enabled(registry: StoreRegistry | None) -> bool:
    """The knob, resolved env > settings > default. Never raises; off on doubt."""
    try:
        return registry is not None and resolve_write_behaviour(registry).access_log
    except Exception:  # advisory
        return False


# ---------------------------------------------------------------------------
# Unrecorded reads and the failure signal
# ---------------------------------------------------------------------------


class _FailureState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.dropped = 0
        self.failed = 0
        self.last_signal: float | None = None
        self.last_warning: float | None = None


_state = _FailureState()


def reset_failure_state() -> None:
    """Forget counted reads and the rate limit (tests)."""
    global _state  # noqa: PLW0603
    _state = _FailureState()


def count_unrecorded(*, dropped: int = 0, failed: int = 0) -> None:
    """Count reads the log could not record, to be surfaced with the next signal."""
    with _state.lock:
        _state.dropped += dropped
        _state.failed += failed


def pending_unrecorded() -> tuple[int, int]:
    """``(dropped, failed)`` reads counted and not yet surfaced."""
    with _state.lock:
        return _state.dropped, _state.failed


def surface_pending(
    registry: StoreRegistry | None, surface: str, error_type: str
) -> None:
    """Emit one ``access.log_failed`` row carrying the counted reads. Never raises.

    Rate limited to one row per minute per process. The counts are taken off
    only after the store accepted the row, so a store that is down keeps them
    for the next signal it can write. With no registry there is nowhere to
    write, so nothing is consumed (not the counts, not the rate limit).
    """
    try:
        state = _state
        with state.lock:
            dropped, failed = state.dropped, state.failed
        if not (dropped or failed):
            return
        now = time.monotonic()
        if registry is None:
            with state.lock:
                warn = (
                    state.last_warning is None
                    or now - state.last_warning >= _FAILURE_SIGNAL_INTERVAL_S
                )
                if warn:
                    state.last_warning = now
            if warn:
                logger.warning(
                    "access_log.record_failed", surface=surface, error_type=error_type
                )
            return
        with state.lock:
            if (
                state.last_signal is not None
                and now - state.last_signal < _FAILURE_SIGNAL_INTERVAL_S
            ):
                return
            state.last_signal = now
        # ``warning`` is visible on the default CLI; the class name only, since
        # a message can carry a DSN.
        logger.warning(
            "access_log.record_failed",
            surface=surface,
            error_type=error_type,
            dropped_reads=dropped,
            failed_reads=failed,
        )
        registry.operational.event_log.emit(
            EventType.ACCESS_LOG_FAILED,
            ACCESS_LOG_SOURCE,
            payload={
                "surface": surface,
                "error_type": error_type,
                "dropped_reads": dropped,
                "failed_reads": failed,
            },
        )
        with state.lock:
            state.dropped -= dropped
            state.failed -= failed
    except Exception:  # the signal is itself advisory
        return


def signal_failure(
    registry: StoreRegistry | None, surface: str, exc: BaseException
) -> None:
    """Count one read the log failed to record and signal it. Never raises."""
    try:
        count_unrecorded(failed=1)
        surface_pending(registry, surface, type(exc).__name__)
    except Exception:  # the signal is itself advisory
        return


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class KeyReadEntry:
    """One authenticated read waiting for the writer thread."""

    registry: StoreRegistry
    key_id: str
    surface: str
    route: str
    #: Runs on the writer thread: ``(item_ids, item_count)``. Parsing a body
    #: belongs here, not on the caller.
    items: Callable[[], tuple[Iterable[str], int | None]]
    #: Bytes this entry holds (a response body plus a fixed overhead).
    size: int = ENTRY_OVERHEAD_BYTES


class AccessLogWriter:
    """A bounded queue and one daemon thread that writes ``access.key_read`` rows."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_QUEUE_ENTRIES,
        max_bytes: int = MAX_QUEUE_BYTES,
    ) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._cond = threading.Condition()
        self._queue: deque[KeyReadEntry] = deque()
        self._bytes = 0
        self._busy = False
        self._stopping = False
        self._deadline: float | None = None
        self._thread: threading.Thread | None = None
        self._last: tuple[StoreRegistry, str] | None = None

    # -- caller side ------------------------------------------------------

    def submit(self, entry: KeyReadEntry) -> bool:
        """Queue ``entry`` without blocking. ``False`` (and counted) if dropped."""
        try:
            with self._cond:
                if (
                    self._stopping
                    or len(self._queue) >= self._max_entries
                    or self._bytes + entry.size > self._max_bytes
                ):
                    dropped = True
                else:
                    dropped = False
                    self._queue.append(entry)
                    self._bytes += entry.size
                    self._ensure_thread()
                    self._cond.notify()
            if dropped:
                count_unrecorded(dropped=1)
                return False
        except Exception:  # advisory; a read must never fail on its own log
            with contextlib.suppress(Exception):
                count_unrecorded(dropped=1)
            return False
        return True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until every queued entry is written (tests, shutdown)."""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._queue and not self._busy, timeout
            )

    def shutdown(self, timeout: float = SHUTDOWN_DRAIN_S) -> None:
        """Drain for at most ``timeout`` seconds, then give up. Never raises."""
        try:
            with self._cond:
                self._stopping = True
                self._deadline = time.monotonic() + timeout
                self._cond.notify_all()
                thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout)
        except Exception:  # advisory; shutdown must never block exit
            return

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    # -- writer thread ----------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="access-log-writer", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    if self._stopping:
                        self._cond.notify_all()
                        return
                    if not self._cond.wait(timeout=_FAILURE_SIGNAL_INTERVAL_S):
                        break
                if (
                    self._stopping
                    and self._deadline is not None
                    and time.monotonic() >= self._deadline
                ):
                    return
                entry = self._queue.popleft() if self._queue else None
                if entry is not None:
                    self._bytes -= entry.size
                    self._busy = True
            try:
                if entry is not None:
                    self._write(entry)
                elif self._last is not None:
                    surface_pending(self._last[0], self._last[1], "queue_full")
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def _write(self, entry: KeyReadEntry) -> None:
        self._last = (entry.registry, entry.surface)
        try:
            item_ids, item_count = entry.items()
            entry.registry.operational.event_log.emit(
                EventType.KEY_READ,
                ACCESS_LOG_SOURCE,
                entity_id=entry.key_id,
                entity_type="api_key",
                payload=build_payload(
                    key_id=entry.key_id,
                    surface=entry.surface,
                    route=entry.route,
                    item_ids=list(item_ids),
                    item_count=item_count,
                ),
            )
        except Exception as exc:  # advisory step; never fail the read
            signal_failure(entry.registry, entry.surface, exc)
            return
        # The store accepted a row, so it can take the signal for any reads
        # dropped while it was slow or full.
        surface_pending(entry.registry, entry.surface, "queue_full")


_writer: AccessLogWriter | None = None
_writer_lock = threading.Lock()


def get_writer() -> AccessLogWriter:
    """The process-wide writer, built on first use."""
    global _writer  # noqa: PLW0603
    with _writer_lock:
        if _writer is None:
            _writer = AccessLogWriter()
            atexit.register(_writer.shutdown)
        return _writer


def enqueue_key_read(
    registry: StoreRegistry | None,
    *,
    key_id: str | None,
    surface: str,
    route: str,
    items: Callable[[], tuple[Iterable[str], int | None]],
    size: int = ENTRY_OVERHEAD_BYTES,
) -> bool:
    """Hand one authenticated read to the writer. Never blocks, never raises.

    A call with no key id, or no registry, records nothing. The caller has
    already checked the knob (:func:`access_log_enabled`), before it buffered
    anything.
    """
    try:
        if not key_id or registry is None:
            return False
        return get_writer().submit(
            KeyReadEntry(
                registry=registry,
                key_id=key_id,
                surface=surface,
                route=route,
                items=items,
                size=size,
            )
        )
    except Exception as exc:  # advisory step; never fail the read
        signal_failure(registry, surface, exc)
        return False


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class KeyReadSummary(TrellisModel):
    """One key's reads within a window."""

    key_id: str
    #: The key's name today, from the key store; ``None`` when it has no row.
    name: str | None = None
    #: ``True`` when the key store says the key is revoked; ``None`` unknown.
    revoked: bool | None = None
    reads: int
    #: Sum of the true ``item_count`` over the key's reads (a route that does
    #: not name its items adds nothing).
    items_returned: int
    #: ISO-8601 time of the key's latest read inside the window.
    last_read_at: str
    by_surface: dict[str, int]


class KeyReadReport(TrellisModel):
    """Per-key last-used time and read counts over a window."""

    since: str
    window_hours: float
    keys: list[KeyReadSummary]
    #: ``access.log_failed`` rows in the window. Each is a rate-limited signal
    #: (one a minute per process at most), so this is not a count of reads.
    failure_signals: int
    #: Reads the writer's queue refused because it was full, summed over the
    #: signals' own counts. A lower bound: a process that died first never
    #: wrote its count.
    dropped_reads: int
    #: Reads whose emit raised, summed the same way, with the same bound.
    failed_reads: int
    #: ``True`` when the scan cap bit, so older reads in the window are missing.
    truncated: bool
    covered_since: str
    #: Non-empty when ``truncated``: what the cap means for this answer.
    note: str = ""


def _signal_counts(event_log: EventLog, since: datetime) -> tuple[int, int, int]:
    scan = scan_events(event_log, event_type=EventType.ACCESS_LOG_FAILED, since=since)
    dropped = failed = 0
    for event in scan.events:
        for name in ("dropped_reads", "failed_reads"):
            value = event.payload.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                if name == "dropped_reads":
                    dropped += value
                else:
                    failed += value
    return len(scan.events), dropped, failed


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
    key's current name and revoked flag, and nothing is written to it. A
    ``key_id`` filter is applied by the store (rows carry the key id as their
    ``entity_id``), so a quiet key is not hidden behind a noisy one.
    """
    since = (now or datetime.now(tz=UTC)) - timedelta(hours=window_hours)
    if key_id:
        events = event_log.get_events(
            event_type=EventType.KEY_READ,
            since=since,
            entity_id=key_id,
            limit=_REPORT_SCAN_LIMIT,
            order="desc",
        )
        truncated = len(events) >= _REPORT_SCAN_LIMIT
        oldest = min((e.occurred_at for e in events), default=None)
        covered_since = oldest.isoformat() if truncated and oldest else ""
        events.reverse()
        note = (
            "The scan hit its cap, so this key's counts cover only the newest "
            f"{_REPORT_SCAN_LIMIT} of its reads, back to {covered_since}."
            if truncated
            else ""
        )
    else:
        scan = scan_events(event_log, event_type=EventType.KEY_READ, since=since)
        events = scan.events
        truncated = scan.coverage.truncated
        covered_since = scan.coverage.covered_since
        note = (
            "The scan hit its cap, so only the newest reads are counted (back "
            f"to {covered_since}). A key missing from this list may still have "
            "been used in the window; look one key up with its key id."
            if truncated
            else ""
        )
    reads: dict[str, dict[str, Any]] = {}
    for event in events:
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
    signals, dropped, failed = _signal_counts(event_log, since)
    return KeyReadReport(
        since=since.isoformat(),
        window_hours=window_hours,
        keys=summaries,
        failure_signals=signals,
        dropped_reads=dropped,
        failed_reads=failed,
        truncated=truncated,
        covered_since=covered_since,
        note=note,
    )
