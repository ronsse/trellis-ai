"""PostgresApiKeyStore — scoped REST API credentials."""

from __future__ import annotations

import json
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.errors import StoreError
from trellis.stores.base.api_key import ApiKeyRecord, ApiKeyStore
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS trellis_api_keys (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]',
    secret_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_api_keys_created ON trellis_api_keys(created_at)",
]


class PostgresApiKeyStore(PostgresStoreBase, ApiKeyStore):
    """Postgres-backed :class:`ApiKeyRecord` store using psycopg v3 (sync)."""

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)

    # -- mutations -----------------------------------------------------------

    def create(self, record: ApiKeyRecord) -> ApiKeyRecord:
        import psycopg  # noqa: PLC0415

        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trellis_api_keys ("
                    "key_id, name, scopes, secret_hash, created_at, revoked_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        record.key_id,
                        record.name,
                        json.dumps(list(record.scopes)),
                        record.secret_hash,
                        record.created_at,
                        record.revoked_at,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            msg = f"API key already exists: {record.key_id}"
            raise StoreError(msg, store="api_key") from exc
        logger.info(
            "api_key.created",
            key_id=record.key_id,
            name=record.name,
            scopes=list(record.scopes),
        )
        return record

    def revoke(self, key_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE trellis_api_keys SET revoked_at = %s"
                " WHERE key_id = %s AND revoked_at IS NULL",
                (utc_now(), key_id),
            )
            updated = cur.rowcount
        if updated == 0:
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
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key_id, name, scopes, secret_hash, created_at, revoked_at"
                " FROM trellis_api_keys WHERE key_id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list(self) -> list[ApiKeyRecord]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key_id, name, scopes, secret_hash, created_at, revoked_at"
                " FROM trellis_api_keys ORDER BY created_at DESC, key_id"
            )
            rows = cur.fetchall()
        return [self._row_to_record(row) for row in rows]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> ApiKeyRecord:
        key_id, name, scopes, secret_hash, created_at, revoked_at = row
        # psycopg decodes JSONB to a Python list already; tolerate the
        # raw-string shape for drivers configured without JSON loaders.
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        return ApiKeyRecord(
            key_id=key_id,
            name=name,
            scopes=tuple(scopes),
            secret_hash=secret_hash,
            created_at=created_at,
            revoked_at=revoked_at,
        )
