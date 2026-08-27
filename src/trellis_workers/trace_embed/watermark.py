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

1. **It advances only through a contiguous prefix of confirmed successes.**
   The driver walks candidates in ascending ``created_at`` order and stops
   advancing at the first trace that did not end with a vector row — while
   still processing the rest of the batch. A failure in the middle of a pass
   therefore pins the cursor behind it, and the next pass re-reaches it.

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
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = ["TraceCursor", "TraceEmbedWatermark"]

#: On-disk format version. Bumped only when the payload shape changes; an
#: unreadable or unknown-version file degrades to "no cursor" (full re-scan),
#: which is safe because the store-state check is the authority.
WATERMARK_VERSION = 1


@dataclass(frozen=True, order=True)
class TraceCursor:
    """A position in the trace stream: ``(created_at, trace_id)``."""

    created_at: datetime
    trace_id: str

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
        self._dirty = False
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def cursor(self) -> TraceCursor | None:
        """The highest position every earlier trace is confirmed embedded at."""
        return self._cursor

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
        if not isinstance(raw, dict) or raw.get("version") != WATERMARK_VERSION:
            logger.warning(
                "trace_embed_watermark_version_mismatch",
                path=str(self._path),
                found=raw.get("version") if isinstance(raw, dict) else None,
                expected=WATERMARK_VERSION,
            )
            return
        self._cursor = TraceCursor.from_json(raw.get("cursor"))

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

    def reset(self) -> None:
        """Forget the cursor, so the next pass re-scans from the beginning."""
        if self._cursor is None:
            return
        self._cursor = None
        self._dirty = True

    def save(self) -> None:
        """Atomically persist the cursor (temp file + rename)."""
        if not self._dirty:
            return
        payload: dict[str, Any] = {
            "version": WATERMARK_VERSION,
            "cursor": self._cursor.to_json() if self._cursor is not None else None,
        }
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
