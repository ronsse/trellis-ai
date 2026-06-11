"""SQLiteApiKeyStore — scoped REST API credentials."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import structlog

from trellis.core.base import utc_now
from trellis.errors import StoreError
from trellis.stores.base.api_key import ApiKeyRecord, ApiKeyStore
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS trellis_api_keys (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    secret_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_api_keys_created ON trellis_api_keys(created_at)",
]


class SQLiteApiKeyStore(SQLiteStoreBase, ApiKeyStore):
    """SQLite-backed :class:`ApiKeyRecord` store.

    Rows are append-then-revoke; ``revoke`` is the only mutation after
    ``create`` and it only ever moves ``revoked_at`` from NULL to a
    timestamp.
    """

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()

    # -- mutations -----------------------------------------------------------

    def create(self, record: ApiKeyRecord) -> ApiKeyRecord:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "INSERT INTO trellis_api_keys ("
                "key_id, name, scopes, secret_hash, created_at, revoked_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.key_id,
                    record.name,
                    json.dumps(list(record.scopes)),
                    record.secret_hash,
                    record.created_at.isoformat(),
                    record.revoked_at.isoformat() if record.revoked_at else None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            msg = f"API key already exists: {record.key_id}"
            raise StoreError(msg, store="api_key") from exc
        self._conn.commit()
        logger.info(
            "api_key.created",
            key_id=record.key_id,
            name=record.name,
            scopes=list(record.scopes),
        )
        return record

    def revoke(self, key_id: str) -> bool:
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE trellis_api_keys SET revoked_at = ?"
            " WHERE key_id = ? AND revoked_at IS NULL",
            (utc_now().isoformat(), key_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            # Loud on misuse: distinguish unknown vs already-revoked in
            # the log so the operator knows which mistake they made.
            existing = self.get(key_id)
            logger.warning(
                "api_key.revoke_noop",
                key_id=key_id,
                reason="already_revoked" if existing else "unknown_key_id",
            )
            return False
        logger.info("api_key.revoked", key_id=key_id)
        return True

    # -- queries -------------------------------------------------------------

    def get(self, key_id: str) -> ApiKeyRecord | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM trellis_api_keys WHERE key_id = ?",
            (key_id,),
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list(self) -> list[ApiKeyRecord]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM trellis_api_keys ORDER BY created_at DESC, key_id")
        return [self._row_to_record(row) for row in cur.fetchall()]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ApiKeyRecord:
        return ApiKeyRecord(
            key_id=row["key_id"],
            name=row["name"],
            scopes=tuple(json.loads(row["scopes"])),
            secret_hash=row["secret_hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
            revoked_at=(
                datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None
            ),
        )
