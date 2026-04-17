"""Local filesystem BlobStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from trellis.stores.base.blob import BlobStore

logger = structlog.get_logger(__name__)


class LocalBlobStore(BlobStore):
    """Filesystem-backed blob store."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._meta_dir = self._root / ".meta"
        self._meta_dir.mkdir(exist_ok=True)
        logger.info("local_blob_store_initialized", root=str(self._root))

    def put(self, key: str, data: bytes, metadata: dict[str, Any] | None = None) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if metadata:
            meta_path = self._meta_dir / f"{key}.json"
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(metadata))
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

    def close(self) -> None:
        logger.info("local_blob_store_closed")
