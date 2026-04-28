"""Tests for :class:`LocalBlobStore` — TTL + GC (Gap 4.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.stores.base.blob import BLOB_EXPIRES_AT_KEY
from trellis.stores.base.event_log import EventType
from trellis.stores.local.blob import LocalBlobStore
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def store(tmp_path: Path):
    s = LocalBlobStore(tmp_path / "blobs")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Basic put / get / delete (coverage that did not exist before this gap)
# ---------------------------------------------------------------------------


class TestPutGet:
    def test_put_and_get(self, store: LocalBlobStore):
        store.put("file.bin", b"hello")
        assert store.get("file.bin") == b"hello"

    def test_put_with_metadata(self, store: LocalBlobStore, tmp_path: Path):
        store.put("file.bin", b"x", metadata={"author": "agent"})
        meta = json.loads((tmp_path / "blobs" / ".meta" / "file.bin.json").read_text())
        assert meta == {"author": "agent"}

    def test_get_missing_returns_none(self, store: LocalBlobStore):
        assert store.get("missing.bin") is None


# ---------------------------------------------------------------------------
# TTL storage + sweep
# ---------------------------------------------------------------------------


class TestTTL:
    def test_put_stores_expires_at_in_meta(self, store: LocalBlobStore, tmp_path: Path):
        expires = datetime.now(UTC) + timedelta(days=1)
        store.put("file.bin", b"x", expires_at=expires)
        meta = json.loads((tmp_path / "blobs" / ".meta" / "file.bin.json").read_text())
        assert meta[BLOB_EXPIRES_AT_KEY] == expires.isoformat()

    def test_put_merges_metadata_and_expires_at(
        self, store: LocalBlobStore, tmp_path: Path
    ):
        expires = datetime.now(UTC) + timedelta(hours=1)
        store.put(
            "file.bin",
            b"x",
            metadata={"author": "agent"},
            expires_at=expires,
        )
        meta = json.loads((tmp_path / "blobs" / ".meta" / "file.bin.json").read_text())
        assert meta["author"] == "agent"
        assert meta[BLOB_EXPIRES_AT_KEY] == expires.isoformat()


class TestSweepExpired:
    def test_sweeps_expired_blobs(self, store: LocalBlobStore):
        past = datetime.now(UTC) - timedelta(days=1)
        future = datetime.now(UTC) + timedelta(days=1)
        store.put("old.bin", b"old", expires_at=past)
        store.put("new.bin", b"new", expires_at=future)
        store.put("no-ttl.bin", b"forever")  # no expires_at → skipped

        report = store.sweep_expired()
        assert report.swept == 1
        assert report.skipped_not_yet_expired == 1
        assert report.skipped_no_ttl == 1
        assert report.errors == 0
        assert report.dry_run is False

        assert store.get("old.bin") is None
        assert store.get("new.bin") == b"new"
        assert store.get("no-ttl.bin") == b"forever"

    def test_dry_run_does_not_delete(self, store: LocalBlobStore):
        past = datetime.now(UTC) - timedelta(days=1)
        store.put("old.bin", b"old", expires_at=past)

        report = store.sweep_expired(dry_run=True)
        assert report.dry_run is True
        assert report.swept == 1
        # File still present.
        assert store.get("old.bin") == b"old"

    def test_before_override(self, store: LocalBlobStore):
        # All TTLs are in the future, but the override cutoff is further
        # in the future still — so they count as expired.
        t_plus_1 = datetime.now(UTC) + timedelta(days=1)
        t_plus_10 = datetime.now(UTC) + timedelta(days=10)
        store.put("a.bin", b"x", expires_at=t_plus_1)

        report = store.sweep_expired(before=t_plus_10)
        assert report.swept == 1

    def test_prefix_limits_scope(self, store: LocalBlobStore):
        past = datetime.now(UTC) - timedelta(days=1)
        store.put("docs/a.bin", b"a", expires_at=past)
        store.put("uploads/b.bin", b"b", expires_at=past)

        report = store.sweep_expired(prefix="docs/")
        assert report.swept == 1
        assert store.get("docs/a.bin") is None
        assert store.get("uploads/b.bin") == b"b"

    def test_malformed_ttl_counts_as_error(self, store: LocalBlobStore, tmp_path: Path):
        # Seed a blob + meta file with an invalid ISO timestamp.
        store.put("broken.bin", b"x", metadata={"author": "agent"})
        meta_path = tmp_path / "blobs" / ".meta" / "broken.bin.json"
        meta_path.write_text(json.dumps({BLOB_EXPIRES_AT_KEY: "not-a-timestamp"}))
        report = store.sweep_expired()
        assert report.errors == 1
        assert report.swept == 0
        # Original blob preserved — sweep is fail-soft.
        assert store.get("broken.bin") == b"x"

    def test_emits_event_when_event_log_provided(
        self, store: LocalBlobStore, tmp_path: Path
    ):
        past = datetime.now(UTC) - timedelta(days=1)
        store.put("old.bin", b"x", expires_at=past)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            store.sweep_expired(event_log=event_log)
            events = event_log.get_events(event_type=EventType.BLOB_GC_SWEPT)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["swept"] == 1
            assert payload["dry_run"] is False
        finally:
            event_log.close()

    def test_dry_run_still_emits_event(self, store: LocalBlobStore, tmp_path: Path):
        past = datetime.now(UTC) - timedelta(days=1)
        store.put("old.bin", b"x", expires_at=past)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            store.sweep_expired(dry_run=True, event_log=event_log)
            events = event_log.get_events(event_type=EventType.BLOB_GC_SWEPT)
            assert len(events) == 1
            assert events[0].payload["dry_run"] is True
        finally:
            event_log.close()

    def test_empty_store_returns_zero_report(self, store: LocalBlobStore):
        report = store.sweep_expired()
        assert report.swept == 0
        assert report.skipped_no_ttl == 0
        assert report.skipped_not_yet_expired == 0
        assert report.errors == 0


# ---------------------------------------------------------------------------
# Base-class opt-in check (coverage for the NotImplementedError default)
# ---------------------------------------------------------------------------


def test_base_sweep_not_implemented():
    from trellis.stores.base.blob import BlobStore

    class _StubBlob(BlobStore):
        def put(self, *a, **k):  # type: ignore[override]
            pass

        def get(self, *a, **k):  # type: ignore[override]
            return None

        def delete(self, *a, **k):  # type: ignore[override]
            return False

        def exists(self, *a, **k):  # type: ignore[override]
            return False

        def list_keys(self, *a, **k):  # type: ignore[override]
            return []

        def get_uri(self, *a, **k):  # type: ignore[override]
            return ""

        def close(self):  # type: ignore[override]
            pass

    with pytest.raises(NotImplementedError, match="sweep_expired"):
        _StubBlob().sweep_expired()
