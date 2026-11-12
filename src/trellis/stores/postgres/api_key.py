"""PostgresApiKeyStore — scoped REST API credentials."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.errors import StoreError
from trellis.stores.base.api_key import (
    UNSET,
    ApiKeyDefaults,
    ApiKeyRecord,
    ApiKeyStore,
    Unset,
)
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS trellis_api_keys (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]',
    secret_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    defaults JSONB,
    expires_at TIMESTAMPTZ
)"""

#: Columns added after the first release; an existing table gets them on open.
_ADDED_COLUMNS = (
    "ALTER TABLE trellis_api_keys ADD COLUMN IF NOT EXISTS defaults JSONB",
    "ALTER TABLE trellis_api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
)

_COLUMNS = (
    "key_id, name, scopes, secret_hash, created_at, revoked_at, defaults, expires_at"
)

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_api_keys_created ON trellis_api_keys(created_at)",
]


class PostgresApiKeyStore(PostgresStoreBase, ApiKeyStore):
    """Postgres-backed :class:`ApiKeyRecord` store using psycopg v3 (sync)."""

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            for alter_sql in _ADDED_COLUMNS:
                cur.execute(alter_sql)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)

    # -- mutations -----------------------------------------------------------

    def create(self, record: ApiKeyRecord) -> ApiKeyRecord:
        import psycopg  # noqa: PLC0415

        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trellis_api_keys ("
                    "key_id, name, scopes, secret_hash, created_at, revoked_at,"
                    " defaults, expires_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.key_id,
                        record.name,
                        json.dumps(list(record.scopes)),
                        record.secret_hash,
                        record.created_at,
                        record.revoked_at,
                        _dump_defaults(record.defaults),
                        record.expires_at,
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

    def update(
        self,
        key_id: str,
        *,
        defaults: ApiKeyDefaults | Unset | None = UNSET,
        expires_at: datetime | Unset | None = UNSET,
        only_if_no_expiry: bool = False,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if not isinstance(defaults, Unset):
            sets.append("defaults = %s")
            params.append(_dump_defaults(defaults))
        if not isinstance(expires_at, Unset):
            sets.append("expires_at = %s")
            params.append(expires_at)
        if not sets:
            return self.get(key_id) is not None
        where = "key_id = %s AND revoked_at IS NULL"
        where_params: list[Any] = [key_id]
        if not isinstance(expires_at, Unset):
            if only_if_no_expiry:
                # A rotation's retire: it wins only against a key nobody has
                # retired yet, decided in this one statement.
                where += " AND expires_at IS NULL"
            else:
                # An expired key's expiry is frozen: clearing or moving it
                # would revive a key that no longer verifies.
                where += " AND (expires_at IS NULL OR expires_at > %s)"
                where_params.append(utc_now())
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE trellis_api_keys SET {', '.join(sets)} WHERE {where}",
                (*params, *where_params),
            )
            rowcount: int = cur.rowcount
            return rowcount > 0

    # -- queries -------------------------------------------------------------

    def get(self, key_id: str) -> ApiKeyRecord | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM trellis_api_keys WHERE key_id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list(self) -> list[ApiKeyRecord]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM trellis_api_keys"
                " ORDER BY created_at DESC, key_id"
            )
            rows = cur.fetchall()
        return [self._row_to_record(row) for row in rows]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> ApiKeyRecord:
        (
            key_id,
            name,
            scopes,
            secret_hash,
            created_at,
            revoked_at,
            defaults,
            expires_at,
        ) = row
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
            defaults=_load_defaults(defaults),
            expires_at=expires_at,
        )


def _dump_defaults(defaults: ApiKeyDefaults | None) -> str | None:
    """The stored (JSONB) form of a defaults block: ``None`` for no block."""
    if defaults is None or defaults.is_empty():
        return None
    return defaults.model_dump_json(exclude_none=True)


def _load_defaults(raw: Any) -> ApiKeyDefaults | None:
    if raw is None:
        return None
    # psycopg decodes JSONB to a dict; tolerate the raw-string shape.
    return ApiKeyDefaults.model_validate(
        json.loads(raw) if isinstance(raw, str) else raw
    )
