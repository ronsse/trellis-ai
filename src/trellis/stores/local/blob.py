"""Local filesystem BlobStore."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.schemas.blob import BlobGCReport
from trellis.stores.base.blob import BLOB_EXPIRES_AT_KEY, BlobStore
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)


class LocalBlobStore(BlobStore):
    """Filesystem-backed blob store."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._meta_dir = self._root / ".meta"
        self._meta_dir.mkdir(exist_ok=True)
        logger.info("local_blob_store_initialized", root=str(self._root))

    def put(
        self,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
        *,
        expires_at: datetime | None = None,
    ) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        merged_meta: dict[str, Any] | None = None
        if metadata or expires_at is not None:
            merged_meta = dict(metadata or {})
            if expires_at is not None:
                merged_meta[BLOB_EXPIRES_AT_KEY] = expires_at.isoformat()
        if merged_meta:
            meta_path = self._meta_dir / f"{key}.json"
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(merged_meta))
        logger.debug("blob_stored", key=key)
        return self.get_uri(key)

    def get(self, key: str) -> bytes | None:
        path = self._root / key
        if not path.exists():
            return None
        return path.read_bytes()

    def delete(self, key: str) -> bool:
        path = self._root / key
        meta_path = self._meta_dir / f"{key}.json"
        existed = path.exists()
        if existed:
            path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        return existed

    def exists(self, key: str) -> bool:
        return (self._root / key).exists()

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._root / prefix if prefix else self._root
        if not base.exists():
            return []
        keys = [
            str(p.relative_to(self._root))
            for p in base.rglob("*")
            if p.is_file() and ".meta" not in p.parts
        ]
        return sorted(keys)

    def get_uri(self, key: str) -> str:
        return f"file://{(self._root / key).resolve()}"

    def sweep_expired(
        self,
        before: datetime | None = None,
        *,
        prefix: str = "",
        dry_run: bool = False,
        event_log: EventLog | None = None,
    ) -> BlobGCReport:
        cutoff = before or utc_now()
        start_ns = time.monotonic_ns()
        swept = 0
        skipped_no_ttl = 0
        skipped_not_yet_expired = 0
        errors = 0

        for key in self.list_keys(prefix=prefix):
            meta_path = self._meta_dir / f"{key}.json"
            if not meta_path.exists():
                skipped_no_ttl += 1
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                errors += 1
                logger.exception("blob_meta_read_failed", key=key)
                continue
            raw = meta.get(BLOB_EXPIRES_AT_KEY)
            if raw is None:
                skipped_no_ttl += 1
                continue
            try:
                expires_at = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                errors += 1
                logger.warning("blob_expires_at_parse_failed", key=key, value=raw)
                continue
            if expires_at >= cutoff:
                skipped_not_yet_expired += 1
                continue
            swept += 1
            if not dry_run:
                try:
                    self.delete(key)
                except OSError:
                    errors += 1
                    swept -= 1  # decrement — the delete failed
                    logger.exception("blob_delete_failed", key=key)

        report = BlobGCReport(
            before=cutoff,
            swept=swept,
            skipped_no_ttl=skipped_no_ttl,
            skipped_not_yet_expired=skipped_not_yet_expired,
            errors=errors,
            dry_run=dry_run,
            duration_ms=max((time.monotonic_ns() - start_ns) // 1_000_000, 0),
        )
        logger.info(
            "blob_gc_swept",
            before=cutoff.isoformat(),
            dry_run=dry_run,
            swept=swept,
            skipped_no_ttl=skipped_no_ttl,
            skipped_not_yet_expired=skipped_not_yet_expired,
            errors=errors,
            duration_ms=report.duration_ms,
        )
        if event_log is not None:
            event_log.emit(
                EventType.BLOB_GC_SWEPT,
                source="blob_store",
                payload=report.model_dump(mode="json") | {"prefix": prefix},
            )
        return report

    def close(self) -> None:
        logger.info("local_blob_store_closed")
