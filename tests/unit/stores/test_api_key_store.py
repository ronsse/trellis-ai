"""Tests for the ApiKeyStore backends (sqlite always; postgres env-gated).

The Postgres section mirrors the gating in ``test_postgres_stores.py``:
``@pytest.mark.postgres`` + skip unless ``TRELLIS_TEST_PG_DSN`` is set.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tests.pg_scratch import configured_dsn, require_scratch_database
from tests.unit.stores.sqlite_write_lock import committed_rows, write_at_once
from trellis.errors import StoreError
from trellis.stores.base.api_key import ApiKeyRecord
from trellis.stores.sqlite.api_key import SQLiteApiKeyStore


def _record(key_id: str = "abcdef123456", **overrides) -> ApiKeyRecord:
    defaults = {
        "key_id": key_id,
        "name": "test-key",
        "scopes": ("read", "ingest"),
        "secret_hash": "a" * 64,
    }
    defaults.update(overrides)
    return ApiKeyRecord(**defaults)


# ======================================================================
# SQLite
# ======================================================================


class TestSQLiteApiKeyStore:
    @pytest.fixture
    def store(self, tmp_path):
        s = SQLiteApiKeyStore(tmp_path / "api_keys.db")
        yield s
        s.close()

    def test_create_and_get(self, store: SQLiteApiKeyStore) -> None:
        record = _record()
        stored = store.create(record)
        assert stored == record

        fetched = store.get("abcdef123456")
        assert fetched is not None
        assert fetched.key_id == record.key_id
        assert fetched.name == "test-key"
        assert fetched.scopes == ("read", "ingest")
        assert fetched.secret_hash == "a" * 64
        assert fetched.revoked_at is None

    def test_get_missing_returns_none(self, store: SQLiteApiKeyStore) -> None:
        assert store.get("000000000000") is None

    def test_create_duplicate_raises(self, store: SQLiteApiKeyStore) -> None:
        store.create(_record())
        with pytest.raises(StoreError, match="already exists"):
            store.create(_record())

    def test_a_create_commits_at_once(self, store, tmp_path) -> None:
        store.create(_record("synkey000001"))

        rows = committed_rows(
            tmp_path / "api_keys.db", "SELECT key_id FROM trellis_api_keys"
        )
        assert rows == [("synkey000001",)]

    def test_a_failed_create_holds_no_write_lock(self, store, tmp_path) -> None:
        """A duplicate create is rolled back, not left holding the write lock."""
        store.create(_record("synkey000001"))

        with pytest.raises(
            StoreError, match=r"^API key already exists: synkey000001$"
        ) as err:
            store.create(_record("synkey000001", name="syn-duplicate"))

        assert err.value.store == "api_key"
        assert isinstance(err.value.__cause__, sqlite3.IntegrityError)
        assert store._conn.in_transaction is False
        write_at_once(
            tmp_path / "api_keys.db",
            "INSERT INTO trellis_api_keys (key_id, name, secret_hash, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("synkey000002", "syn-other", "b" * 64, "2026-01-01T00:00:00+00:00"),
        )
        rows = committed_rows(
            tmp_path / "api_keys.db",
            "SELECT key_id FROM trellis_api_keys ORDER BY key_id",
        )
        assert rows == [("synkey000001",), ("synkey000002",)]

    def test_list_newest_first(self, store: SQLiteApiKeyStore) -> None:
        store.create(
            _record(
                "aaaaaaaaaaaa",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        store.create(
            _record(
                "bbbbbbbbbbbb",
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        rows = store.list()
        assert [r.key_id for r in rows] == ["bbbbbbbbbbbb", "aaaaaaaaaaaa"]

    def test_list_empty(self, store: SQLiteApiKeyStore) -> None:
        assert store.list() == []

    def test_revoke_live_key(self, store: SQLiteApiKeyStore) -> None:
        store.create(_record())
        assert store.revoke("abcdef123456") is True
        fetched = store.get("abcdef123456")
        assert fetched is not None
        assert fetched.revoked_at is not None

    def test_revoke_unknown_returns_false(self, store: SQLiteApiKeyStore) -> None:
        assert store.revoke("000000000000") is False

    def test_revoke_twice_returns_false(self, store: SQLiteApiKeyStore) -> None:
        store.create(_record())
        assert store.revoke("abcdef123456") is True
        assert store.revoke("abcdef123456") is False

    def test_revoked_key_still_listed(self, store: SQLiteApiKeyStore) -> None:
        store.create(_record())
        store.revoke("abcdef123456")
        rows = store.list()
        assert len(rows) == 1
        assert rows[0].revoked_at is not None

    def test_scopes_round_trip_tuple(self, store: SQLiteApiKeyStore) -> None:
        store.create(_record(scopes=("admin",)))
        fetched = store.get("abcdef123456")
        assert fetched is not None
        assert fetched.scopes == ("admin",)

    def test_close_idempotent(self, tmp_path) -> None:
        s = SQLiteApiKeyStore(tmp_path / "api_keys.db")
        s.close()
        s.close()

    # -- expiry is compared as an instant, whatever offset it arrived in --------

    @pytest.mark.parametrize(
        ("offset_hours", "minutes_from_now", "clearing_is_allowed"),
        [
            # 5 minutes ago, written at +14:00: expired, so its expiry is frozen.
            # Compared as text, "...T00:05+14:00" sorts after "...T10:05+00:00".
            (14, -5, False),
            # 5 minutes from now, written at -12:00: still live, so it may move.
            # Compared as text it sorts before now and would read as expired.
            (-12, 5, True),
        ],
    )
    def test_an_expiry_at_a_non_utc_offset_is_compared_as_an_instant(
        self,
        store: SQLiteApiKeyStore,
        offset_hours: int,
        minutes_from_now: int,
        clearing_is_allowed: bool,
    ) -> None:
        store.create(_record())
        when = (datetime.now(UTC) + timedelta(minutes=minutes_from_now)).astimezone(
            timezone(timedelta(hours=offset_hours))
        )
        assert store.update("abcdef123456", expires_at=when) is True
        assert store.update("abcdef123456", expires_at=None) is clearing_is_allowed
        stored = store.get("abcdef123456")
        assert stored is not None
        assert (stored.expires_at is None) is clearing_is_allowed
        if not clearing_is_allowed:
            assert stored.expires_at == when

    def test_an_expiry_is_stored_in_utc_on_create_and_on_update(
        self, store: SQLiteApiKeyStore, tmp_path
    ) -> None:
        east = timezone(timedelta(hours=14))
        created = datetime(2999, 1, 1, 12, 0, tzinfo=east)
        store.create(_record("created00001", expires_at=created))
        store.create(_record("updated00001"))
        assert store.update("updated00001", expires_at=created)
        rows = committed_rows(
            tmp_path / "api_keys.db",
            "SELECT key_id, expires_at FROM trellis_api_keys ORDER BY key_id",
        )
        assert rows == [
            ("created00001", "2998-12-31T22:00:00+00:00"),
            ("updated00001", "2998-12-31T22:00:00+00:00"),
        ]

    def test_a_naive_expiry_is_refused_not_read_as_local_time(
        self, store: SQLiteApiKeyStore
    ) -> None:
        store.create(_record())
        naive = datetime.fromisoformat("2999-01-01T00:00:00")
        with pytest.raises(ValueError, match="timezone-aware"):
            store.update("abcdef123456", expires_at=naive)
        assert store.get("abcdef123456").expires_at is None  # type: ignore[union-attr]

    # -- a retire is conditional on the key having no expiry yet ----------------

    def test_an_expiry_set_only_if_none_is_set_once(
        self, store: SQLiteApiKeyStore
    ) -> None:
        store.create(_record())
        first = datetime.now(UTC) + timedelta(hours=2)
        second = datetime.now(UTC) + timedelta(days=30)
        assert (
            store.update("abcdef123456", expires_at=first, only_if_no_expiry=True)
            is True
        )
        assert (
            store.update("abcdef123456", expires_at=second, only_if_no_expiry=True)
            is False
        )
        assert store.get("abcdef123456").expires_at == first  # type: ignore[union-attr]
        # The condition is the caller's to ask for: a plain update still moves it.
        assert store.update("abcdef123456", expires_at=second) is True
        assert store.get("abcdef123456").expires_at == second  # type: ignore[union-attr]

    def test_a_conditional_expiry_is_refused_on_a_revoked_key(
        self, store: SQLiteApiKeyStore
    ) -> None:
        store.create(_record())
        store.revoke("abcdef123456")
        when = datetime.now(UTC) + timedelta(hours=1)
        assert (
            store.update("abcdef123456", expires_at=when, only_if_no_expiry=True)
            is False
        )


# ======================================================================
# Postgres (env-gated, mirrors test_postgres_stores.py)
# ======================================================================

PG_DSN = configured_dsn() or None


@pytest.mark.postgres
@pytest.mark.skipif(PG_DSN is None, reason="TRELLIS_TEST_PG_DSN not set")
class TestPostgresApiKeyStore:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        psycopg = pytest.importorskip("psycopg")
        assert PG_DSN is not None
        require_scratch_database(PG_DSN)
        conn = psycopg.connect(PG_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS trellis_api_keys CASCADE")
        conn.close()

    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.api_key import PostgresApiKeyStore

        assert PG_DSN is not None
        s = PostgresApiKeyStore(PG_DSN)
        yield s
        s.close()

    def test_create_get_list_revoke(self, store) -> None:
        record = _record()
        store.create(record)

        fetched = store.get("abcdef123456")
        assert fetched is not None
        assert fetched.name == "test-key"
        assert fetched.scopes == ("read", "ingest")
        assert fetched.revoked_at is None

        assert [r.key_id for r in store.list()] == ["abcdef123456"]

        assert store.revoke("abcdef123456") is True
        refetched = store.get("abcdef123456")
        assert refetched is not None
        assert refetched.revoked_at is not None

    def test_create_duplicate_raises(self, store) -> None:
        store.create(_record())
        with pytest.raises(StoreError, match="already exists"):
            store.create(_record())

    def test_revoke_semantics(self, store) -> None:
        assert store.revoke("000000000000") is False
        store.create(_record())
        assert store.revoke("abcdef123456") is True
        assert store.revoke("abcdef123456") is False

    def test_an_expiry_set_only_if_none_is_set_once(self, store) -> None:
        store.create(_record())
        first = datetime.now(UTC) + timedelta(hours=2)
        second = datetime.now(UTC) + timedelta(days=30)
        assert store.update("abcdef123456", expires_at=first, only_if_no_expiry=True)
        assert not store.update(
            "abcdef123456", expires_at=second, only_if_no_expiry=True
        )
        assert store.get("abcdef123456").expires_at == first
        store.revoke("abcdef123456")
        assert not store.update(
            "abcdef123456", expires_at=second, only_if_no_expiry=True
        )


class _FakeCursor:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log
        self.rowcount = 1

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((" ".join(sql.split()), tuple(params)))


class _FakeConnection:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._log)


class TestPostgresUpdateStatement:
    """The statement the Postgres store sends, read without a database."""

    @pytest.fixture
    def sent(self) -> list[tuple[str, tuple]]:
        pytest.importorskip("psycopg_pool")
        return []

    @pytest.fixture
    def store(self, sent: list[tuple[str, tuple]]):
        from trellis.stores.postgres.api_key import PostgresApiKeyStore

        s = PostgresApiKeyStore.__new__(PostgresApiKeyStore)
        s._conn = lambda: _FakeConnection(sent)  # type: ignore[method-assign]
        return s

    def test_a_retire_is_conditional_on_no_expiry_and_not_revoked(
        self, store, sent
    ) -> None:
        when = datetime.now(UTC) + timedelta(hours=1)
        store.update("abcdef123456", expires_at=when, only_if_no_expiry=True)
        sql, params = sent[-1]
        assert "WHERE key_id = %s AND revoked_at IS NULL AND expires_at IS NULL" in sql
        assert params == (when, "abcdef123456")

    def test_a_plain_expiry_update_keeps_the_frozen_expiry_guard(
        self, store, sent
    ) -> None:
        when = datetime.now(UTC) + timedelta(hours=1)
        store.update("abcdef123456", expires_at=when)
        sql, _ = sent[-1]
        assert "AND (expires_at IS NULL OR expires_at > %s)" in sql
        assert "revoked_at IS NULL AND expires_at IS NULL" not in sql
