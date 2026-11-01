"""A row of an event type this build does not know, read from ``SQLiteEventLog``.

A newer build can append an ``event_type`` an older one lacks. A read that met
one used to raise out of ``EventType(...)`` and fail whole, so each new type
imposed a deploy order on operators. The behaviour every backend shares is in
``contracts/event_log_contract.py``; this file holds what only SQLite can show:
the statements a read sends, the stored row after it, and a malformed field.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from tests.structlog_isolation import clear_cached_logger_proxies
from trellis.stores.base.event_log import (
    Event,
    EventType,
    warn_skipped_event_types,
)
from trellis.stores.sqlite.event_log import SQLiteEventLog

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_SKIPPED = "event_log.unknown_event_type_skipped"


def _future(
    event_type: str, offset: int, payload: dict[str, str] | None = None
) -> Event:
    """An event as a newer build wrote it (see the contract suite's twin)."""
    return Event.model_construct(
        event_type=event_type,
        source="newer",
        entity_id="entity-1",
        occurred_at=_BASE + timedelta(seconds=offset),
        recorded_at=_BASE + timedelta(seconds=offset),
        payload=payload or {},
    )


def _known(offset: int) -> Event:
    return Event(
        event_type=EventType.TRACE_INGESTED,
        source="a",
        occurred_at=_BASE + timedelta(seconds=offset),
    )


@pytest.fixture
def evicted_logger_cache():
    """``capture_logs`` sees nothing from a proxy a CLI test already used."""
    clear_cached_logger_proxies()
    yield
    clear_cached_logger_proxies()


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteEventLog(tmp_path / "events.db")
    yield s
    s.close()


def _raw(store: SQLiteEventLog) -> sqlite3.Connection:
    return store._conn


class TestWarning:
    def test_names_the_types_and_the_counts_and_no_payload(
        self, store: SQLiteEventLog, evicted_logger_cache: None
    ) -> None:
        store.append(_known(0))
        secret = {"note": "PAYLOAD-TEXT-SENTINEL"}
        store.append(_future("future.kind", 1, secret))
        store.append(_future("future.kind", 2, secret))
        store.append(_future("other.kind", 3, secret))

        with capture_logs() as logs:
            events = store.get_events()

        assert len(events) == 1
        warnings = [log for log in logs if log["event"] == _SKIPPED]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["log_level"] == "warning"
        assert warning["operation"] == "get_events"
        assert warning["rows_skipped"] == 3
        assert warning["distinct_types"] == 2
        assert warning["event_types"] == {"future.kind": 2, "other.kind": 1}
        assert "PAYLOAD-TEXT-SENTINEL" not in repr(logs)
        assert "entity-1" not in repr(logs)

    def test_a_read_with_nothing_to_skip_says_nothing(
        self, store: SQLiteEventLog, evicted_logger_cache: None
    ) -> None:
        store.append(_known(0))
        with capture_logs() as logs:
            store.get_events()
        assert [log for log in logs if log["event"] == _SKIPPED] == []

    def test_get_event_names_the_type(
        self, store: SQLiteEventLog, evicted_logger_cache: None
    ) -> None:
        newer = _future("future.kind", 1)
        store.append(newer)
        with capture_logs() as logs:
            assert store.get_event(newer.event_id) is None
        (warning,) = [log for log in logs if log["event"] == _SKIPPED]
        assert warning["operation"] == "get_event"
        assert warning["event_types"] == {"future.kind": 1}

    def test_a_stored_type_string_is_bounded_before_it_is_logged(
        self, evicted_logger_cache: None
    ) -> None:
        skipped = {f"t{i:02d}.{'x' * 200}": 1 for i in range(25)}
        with capture_logs() as logs:
            warn_skipped_event_types(skipped, operation="get_events")
        (warning,) = logs
        assert warning["rows_skipped"] == 25
        assert warning["distinct_types"] == 25
        assert len(warning["event_types"]) == 10
        assert all(len(t) <= 64 for t in warning["event_types"])


class TestSkipOnly:
    def test_no_row_is_deleted_or_rewritten(self, store: SQLiteEventLog) -> None:
        store.append(_known(0))
        newer = _future("future.kind", 1, {"k": "v"})
        store.append(newer)
        dump = "SELECT * FROM events ORDER BY rowid"
        before = _raw(store).execute(dump).fetchall()

        store.get_events()
        store.get_events(limit=1, order="desc")
        store.get_event(newer.event_id)
        store.count()

        after = _raw(store).execute(dump).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        assert len(after) == 2

    def test_a_readable_row_with_a_malformed_field_still_raises(
        self, store: SQLiteEventLog
    ) -> None:
        good = _known(0)
        bad = _known(1)
        store.append(good)
        store.append(bad)
        store.append(_future("future.kind", 2))
        with _raw(store) as conn:
            conn.execute(
                "UPDATE events SET payload_json = '{' WHERE event_id = ?",
                (bad.event_id,),
            )

        with pytest.raises(json.JSONDecodeError):
            store.get_events()
        with pytest.raises(json.JSONDecodeError):
            store.get_event(bad.event_id)

    def test_only_the_type_is_judged_on_a_row_that_is_skipped(
        self, store: SQLiteEventLog
    ) -> None:
        """A skipped row is not parsed, so nothing else about it can fail the read."""
        newer = _future("future.kind", 1)
        store.append(newer)
        with _raw(store) as conn:
            conn.execute(
                "UPDATE events SET payload_json = '{' WHERE event_id = ?",
                (newer.event_id,),
            )
        assert store.get_events() == []
        assert store.get_event(newer.event_id) is None


class TestStatementsSent:
    @staticmethod
    def _selects(store: SQLiteEventLog) -> list[str]:
        seen: list[str] = []
        _raw(store).set_trace_callback(
            lambda sql: (
                seen.append(sql) if sql.startswith("SELECT * FROM events") else None
            )
        )
        return seen

    def test_a_read_with_nothing_to_skip_sends_the_one_statement_it_always_did(
        self, store: SQLiteEventLog
    ) -> None:
        for i in range(3):
            store.append(_known(i))
        seen = self._selects(store)
        assert len(store.get_events(limit=3)) == 3
        assert len(seen) == 1
        assert "event_type IN" not in seen[0]

    def test_a_short_page_is_not_read_again(self, store: SQLiteEventLog) -> None:
        store.append(_known(0))
        store.append(_future("future.kind", 1))
        seen = self._selects(store)
        assert len(store.get_events(limit=10)) == 1
        assert len(seen) == 1

    def test_a_full_page_with_a_skipped_row_is_read_again_for_known_types(
        self, store: SQLiteEventLog
    ) -> None:
        store.append(_known(0))
        store.append(_known(1))
        store.append(_future("future.kind", 2))
        seen = self._selects(store)
        page = store.get_events(limit=2, order="desc")
        assert len(page) == 2
        assert len(seen) == 2
        assert "event_type IN" in seen[1]
        # Every type the build knows is bound; the unknown one never is.
        assert "future.kind" not in seen[1]
