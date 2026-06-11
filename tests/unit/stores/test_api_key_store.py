"""Tests for the ApiKeyStore backends (sqlite always; postgres env-gated).

The Postgres section mirrors the gating in ``test_postgres_stores.py``:
``@pytest.mark.postgres`` + skip unless ``TRELLIS_TEST_PG_DSN`` is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

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


# ======================================================================
# Postgres (env-gated, mirrors test_postgres_stores.py)
# ======================================================================

PG_DSN = os.environ.get("TRELLIS_TEST_PG_DSN")


@pytest.mark.postgres
@pytest.mark.skipif(PG_DSN is None, reason="TRELLIS_TEST_PG_DSN not set")
class TestPostgresApiKeyStore:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        psycopg = pytest.importorskip("psycopg")
        assert PG_DSN is not None
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
