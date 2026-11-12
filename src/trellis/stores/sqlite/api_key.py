"""SQLiteApiKeyStore — scoped REST API credentials."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

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
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS trellis_api_keys (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    secret_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    defaults TEXT,
    expires_at TEXT
)"""

#: Columns added after the first release. An existing table lacks them; they
#: are NULL-default so every old row reads as "no defaults, no expiry".
_ADDED_COLUMNS = (("defaults", "TEXT"), ("expires_at", "TEXT"))

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
        for column, ddl in _ADDED_COLUMNS:
            present = {
                row[1] for row in cur.execute("PRAGMA table_info(trellis_api_keys)")
            }
            if column in present:
                continue
            try:
                cur.execute(f"ALTER TABLE trellis_api_keys ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                # A concurrent opener (host CLI beside a container) added the
                # column between the check and the ALTER. Anything else, or a
                # column that is still absent, is a real failure.
                recheck = {
                    row[1] for row in cur.execute("PRAGMA table_info(trellis_api_keys)")
                }
                if "duplicate column name" not in str(exc) or column not in recheck:
                    raise
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()

    # -- mutations -----------------------------------------------------------

    def create(self, record: ApiKeyRecord) -> ApiKeyRecord:
        conn = self._conn
        try:
            with conn:  # commits on success, rolls back on an exception
                conn.execute(
                    "INSERT INTO trellis_api_keys ("
                    "key_id, name, scopes, secret_hash, created_at, revoked_at,"
                    " defaults, expires_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.key_id,
                        record.name,
                        json.dumps(list(record.scopes)),
                        record.secret_hash,
                        record.created_at.isoformat(),
                        record.revoked_at.isoformat() if record.revoked_at else None,
                        _dump_defaults(record.defaults),
                        _utc_iso(record.expires_at) if record.expires_at else None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
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
        conn = self._conn
        with conn:  # commits on success, rolls back on an exception
            cur = conn.execute(
                "UPDATE trellis_api_keys SET revoked_at = ?"
                " WHERE key_id = ? AND revoked_at IS NULL",
                (utc_now().isoformat(), key_id),
            )
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

    def update(
        self,
        key_id: str,
        *,
        defaults: ApiKeyDefaults | Unset | None = UNSET,
        expires_at: datetime | Unset | None = UNSET,
        only_if_no_expiry: bool = False,
    ) -> bool:
        sets: list[str] = []
        params: list[str | None] = []
        if not isinstance(defaults, Unset):
            sets.append("defaults = ?")
            params.append(_dump_defaults(defaults))
        if not isinstance(expires_at, Unset):
            sets.append("expires_at = ?")
            params.append(_utc_iso(expires_at) if expires_at else None)
        if not sets:
            return self.get(key_id) is not None
        where = "key_id = ? AND revoked_at IS NULL"
        where_params: list[str | None] = [key_id]
        if not isinstance(expires_at, Unset):
            if only_if_no_expiry:
                # A rotation's retire: it wins only against a key nobody has
                # retired yet. Decided in this one statement, so two rotations
                # that both read the key as live cannot both write a window.
                where += " AND expires_at IS NULL"
            else:
                # An expired key's expiry is frozen: clearing or moving it
                # would revive a key that no longer verifies.
                where += " AND (expires_at IS NULL OR expires_at > ?)"
                where_params.append(_utc_iso(utc_now()))
        with self._conn as conn:  # commits on success, rolls back on an exception
            cur = conn.execute(
                f"UPDATE trellis_api_keys SET {', '.join(sets)} WHERE {where}",
                (*params, *where_params),
            )
        return cur.rowcount > 0

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
            defaults=_load_defaults(row["defaults"]),
            expires_at=(
                datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
            ),
        )


def _utc_iso(value: datetime) -> str:
    """The stored form of an instant: UTC, so comparing the text compares time.

    ``expires_at`` is compared in SQL as text. Offsets other than ``+00:00``
    would order by their clock reading, not by the instant they name (the way
    a Postgres ``TIMESTAMPTZ`` never does), so every value is normalised to UTC
    on the way in. A naive value names no instant and is refused rather than
    read as local time.
    """
    if value.utcoffset() is None:
        msg = "expires_at must be a timezone-aware datetime"
        raise ValueError(msg)
    return value.astimezone(UTC).isoformat()


def _dump_defaults(defaults: ApiKeyDefaults | None) -> str | None:
    """The stored form of a defaults block: ``None`` for no block at all."""
    if defaults is None or defaults.is_empty():
        return None
    return defaults.model_dump_json(exclude_none=True)


def _load_defaults(raw: str | None) -> ApiKeyDefaults | None:
    return ApiKeyDefaults.model_validate_json(raw) if raw else None
