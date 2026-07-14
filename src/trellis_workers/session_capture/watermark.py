"""Per-file watermark cursor — incremental sweeps over a large corpus.

The operator's transcript directory is large (~190 MB) and grows daily; the
sweep must never re-read the whole corpus. The watermark stores an
``(mtime, size)`` cursor per transcript file, client-side, so an unchanged
session is skipped before it is even opened. Session identity is the JSONL
file stem (the Claude Code session UUID).

This is the first of two idempotency layers: the watermark skips unchanged
*files*; :func:`~trellis.ingest_corpus.sync.sync_records`' content-hash
short-circuit skips unchanged *memories* when a file is re-processed anyway
(e.g. after a watermark reset).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class WatermarkStore:
    """A JSON-backed ``{path: {mtime, size}}`` cursor over transcript files."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cursors: dict[str, dict[str, float]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # A corrupt watermark degrades to a full re-scan (safe, just
            # slower) rather than crashing the sweep.
            logger.warning("watermark_unreadable_resetting", path=str(self._path))
            return
        cursors = raw.get("cursors") if isinstance(raw, dict) else None
        if isinstance(cursors, dict):
            self._cursors = {
                str(key): value
                for key, value in cursors.items()
                if isinstance(value, dict)
            }

    def is_unchanged(self, file_path: Path) -> bool:
        """``True`` iff *file_path* has the same size and mtime as recorded."""
        cursor = self._cursors.get(str(file_path))
        if cursor is None:
            return False
        try:
            stat = file_path.stat()
        except OSError:
            return False
        return (
            cursor.get("size") == stat.st_size
            and cursor.get("mtime") == stat.st_mtime
        )

    def record(self, file_path: Path, stat: os.stat_result | None = None) -> None:
        """Stamp *file_path*'s ``(mtime, size)`` into the cursor.

        Callers that read the file MUST pass a ``stat`` taken **before**
        reading (the append-during-sweep race): a fresh post-read stat can
        claim bytes appended between read-EOF and the stat call, permanently
        skipping that tail. Recording the pre-read snapshot instead means an
        appended tail makes the file compare as changed, so the session is
        re-processed next sweep (safe — the write path is idempotent).

        The fresh-stat fallback (``stat=None``) is only for callers that do
        not read the file between the watermark check and the record.
        """
        if stat is None:
            try:
                stat = file_path.stat()
            except OSError:
                return
        self._cursors[str(file_path)] = {
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        self._dirty = True

    def save(self) -> None:
        """Atomically persist the cursor map (temp file + rename)."""
        if not self._dirty:
            return
        payload: dict[str, Any] = {"version": 1, "cursors": self._cursors}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            Path(tmp_name).replace(self._path)
        except OSError:
            logger.exception("watermark_save_failed", path=str(self._path))
            Path(tmp_name).unlink(missing_ok=True)
            return
        self._dirty = False
