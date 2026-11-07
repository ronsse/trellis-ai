"""The trace-embed cursor — an optimisation that is never load-bearing.

A watermark over an immutable, append-only store is the obvious way to keep a
pass from rescanning the corpus. It is also the obvious way to lose rows: the
moment "have I done this?" is answered by a bookkeeping file instead of by the
store, a file that advanced one row too far skips that row **permanently and
silently**, and nothing in the summary says so. Traces cannot carry an
embedded-state stamp — they are immutable — so the tracking has to live
outside them, which is exactly the situation that failure mode likes.

So this cursor is deliberately not the authority. Correctness comes from
:func:`~trellis_workers.trace_embed.worker.trace_is_embedded`, which asks the
*vector store* whether the row exists; the cursor only decides how far back a
pass bothers to look. The two guarantees that make that safe:

1. **It advances only through a contiguous prefix of traces that are done,
   permanently refused, or on the retry list.** The driver walks candidates in
   ascending ``created_at`` order and stops advancing at the first trace that
   did not end with a vector row and that the retry list had no room for —
   while still processing the rest of the batch. So a failure when the list is
   full pins the cursor behind it, and the next pass re-reaches it.

   The retry list is why a held trace no longer starves the traces after it.
   A trace that could not be embedded *yet* (a store that could not answer for
   a cited document, a failed write) goes on a bounded list beside the cursor,
   which then moves past it; every pass re-attempts the whole list first. A
   full list holds the cursor, as before: nothing is dropped to make room.

2. **Losing it costs time, not rows.** Deleting the file (or
   ``--reset-watermark``) re-scans from the beginning; every already-embedded
   trace is skipped by the store-state check, nothing is re-embedded, and the
   cursor rebuilds itself. That is the property to reach for when the cursor is
   ever suspected of lying.

The cursor is ``(created_at, trace_id)`` rather than ``created_at`` alone
because two traces can share a timestamp, and ``since=`` on
:meth:`~trellis.stores.base.trace.TraceStore.query` is inclusive — the tie-break
keeps a same-instant sibling from being stepped over.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from trellis.stores.base.trace import as_utc

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger(__name__)

__all__ = ["TraceCursor", "TraceEmbedWatermark"]

#: On-disk format version. Bumped only when the payload shape changes; an
#: unreadable or unknown-version file degrades to "no cursor" (full re-scan),
#: which is safe because the store-state check is the authority.
WATERMARK_VERSION = 1

#: The version of a file that carries a non-empty retry list. A build that
#: predates the list would read the cursor and ignore the list, and so skip the
#: listed traces for good; reading the file as an unknown version instead makes
#: it re-scan, which is slower and never wrong. A file with an empty list is
#: written as ``WATERMARK_VERSION``, so only a watermark that holds something
#: back is unreadable to that older build.
WATERMARK_VERSION_WITH_RETRY = 2


@dataclass(frozen=True, order=True)
class TraceCursor:
    """A position in the trace stream: ``(created_at, trace_id)``."""

    created_at: datetime
    trace_id: str

    def __post_init__(self) -> None:
        # Aware UTC always, so two cursors compare by instant. A cursor file an
        # earlier build wrote from a naive trace holds a naive stamp, and it
        # would otherwise raise against the aware one a newer pass computes.
        object.__setattr__(self, "created_at", as_utc(self.created_at))

    def to_json(self) -> dict[str, str]:
        return {"created_at": self.created_at.isoformat(), "trace_id": self.trace_id}

    @classmethod
    def from_json(cls, raw: Any) -> TraceCursor | None:
        """Parse a stored cursor, or ``None`` when it is unusable."""
        if not isinstance(raw, dict):
            return None
        created_at = raw.get("created_at")
        trace_id = raw.get("trace_id")
        if not isinstance(created_at, str) or not isinstance(trace_id, str):
            return None
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError:
            return None
        return cls(created_at=parsed, trace_id=trace_id)


class TraceEmbedWatermark:
    """A JSON-backed :class:`TraceCursor`, written atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cursor: TraceCursor | None = None
        #: When the last restamp sweep finished, clean or not
        #: (:mod:`~trellis_workers.trace_embed.restamp`). Bookkeeping like the
        #: cursor, and as expendable: losing it runs one sweep early.
        self._restamped_at: datetime | None = None
        #: Traces the cursor has moved past that did not end with a vector row,
        #: by trace id. Every pass re-attempts all of them before it looks at
        #: anything newer, so each one is either embedded, permanently refused,
        #: or still here. Bounded by the caller, never trimmed by this class.
        self._retry: dict[str, TraceCursor] = {}
        self._dirty = False
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def cursor(self) -> TraceCursor | None:
        """The highest position every earlier trace is confirmed embedded at."""
        return self._cursor

    @property
    def retry(self) -> tuple[TraceCursor, ...]:
        """The retry list, oldest first."""
        return tuple(sorted(self._retry.values()))

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # A corrupt cursor degrades to a full re-scan: slower, never wrong,
            # because the store-state check decides what actually gets written.
            logger.warning("trace_embed_watermark_unreadable", path=str(self._path))
            return
        if not isinstance(raw, dict) or raw.get("version") not in (
            WATERMARK_VERSION,
            WATERMARK_VERSION_WITH_RETRY,
        ):
            logger.warning(
                "trace_embed_watermark_version_mismatch",
                path=str(self._path),
                found=raw.get("version") if isinstance(raw, dict) else None,
                expected=WATERMARK_VERSION_WITH_RETRY,
            )
            return
        retry = self._parse_retry(raw.get("retry"))
        if retry is None:
            # A list this build cannot read is a list of traces it cannot name.
            # Keeping the cursor without it would skip them for good, so the
            # whole file degrades to a re-scan, as a corrupt one does.
            logger.warning(
                "trace_embed_watermark_retry_unreadable", path=str(self._path)
            )
            return
        self._retry = retry
        self._cursor = TraceCursor.from_json(raw.get("cursor"))
        stamp = raw.get("restamped_at")
        if isinstance(stamp, str):
            try:
                self._restamped_at = as_utc(datetime.fromisoformat(stamp))
            except ValueError:
                self._restamped_at = None

    @staticmethod
    def _parse_retry(raw: Any) -> dict[str, TraceCursor] | None:
        """The stored retry list, empty when absent, ``None`` when unreadable."""
        if raw is None:
            # A file from before the list existed.
            return {}
        if not isinstance(raw, list):
            return None
        parsed: dict[str, TraceCursor] = {}
        for item in raw:
            entry = TraceCursor.from_json(item)
            if entry is None:
                return None
            parsed[entry.trace_id] = entry
        return parsed

    def restamp_due(self, now: datetime, interval: timedelta) -> bool:
        """Whether no restamp sweep has finished within ``interval``."""
        if self._restamped_at is None:
            return True
        return as_utc(now) - self._restamped_at >= interval

    def mark_restamped(self, now: datetime) -> None:
        """Record that a restamp sweep finished at ``now``.

        An unclean sweep is marked too, so a citation that stays unreadable is
        retried once an interval rather than on every pass.
        """
        self._restamped_at = as_utc(now)
        self._dirty = True

    def advance_to(self, cursor: TraceCursor) -> bool:
        """Move the cursor forward to *cursor*.

        Monotonic: a cursor at or behind the current position is ignored and
        returns ``False``. Two passes racing on the same file cannot rewind
        each other, and a caller that computed its contiguous prefix wrongly
        can only fail to advance, never regress.
        """
        if self._cursor is not None and cursor <= self._cursor:
            return False
        self._cursor = cursor
        self._dirty = True
        return True

    def set_retry(self, entries: Iterable[TraceCursor]) -> bool:
        """Replace the retry list with *entries*; ``True`` when that changed it.

        The caller owns the bound and the policy: this stores what it is given,
        so a list is never trimmed behind the caller's back.
        """
        replacement = {entry.trace_id: entry for entry in entries}
        if replacement == self._retry:
            return False
        self._retry = replacement
        self._dirty = True
        return True

    def reset(self) -> None:
        """Forget the cursor, so the next pass re-scans from the beginning.

        Forgets the last restamp sweep too, so the same pass runs one, and the
        retry list: the re-scan reaches every trace on it again.
        """
        if self._cursor is None and self._restamped_at is None and not self._retry:
            return
        self._cursor = None
        self._restamped_at = None
        self._retry = {}
        self._dirty = True

    def save(self) -> None:
        """Atomically persist the cursor (temp file + rename)."""
        if not self._dirty:
            return
        payload: dict[str, Any] = {
            "version": (
                WATERMARK_VERSION_WITH_RETRY if self._retry else WATERMARK_VERSION
            ),
            "cursor": self._cursor.to_json() if self._cursor is not None else None,
            "restamped_at": (
                self._restamped_at.isoformat() if self._restamped_at else None
            ),
        }
        if self._retry:
            payload["retry"] = [entry.to_json() for entry in self.retry]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            Path(tmp_name).replace(self._path)
        except OSError:
            logger.exception("trace_embed_watermark_save_failed", path=str(self._path))
            Path(tmp_name).unlink(missing_ok=True)
            return
        self._dirty = False
