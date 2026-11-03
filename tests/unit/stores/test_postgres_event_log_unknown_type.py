"""``PostgresEventLog`` skips a row of an event type this build does not know.

No database is needed to see what is sent and what is returned, so this runs
wherever psycopg is installed. It cannot show that the statements are valid SQL
on a server; the shared behaviour is in ``contracts/event_log_contract.py``,
which the live suite runs against Postgres.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from tests.structlog_isolation import clear_cached_logger_proxies
from trellis.stores.base.event_log import KNOWN_EVENT_TYPE_VALUES
from trellis.stores.postgres.event_log import PostgresEventLog

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_SKIPPED = "event_log.unknown_event_type_skipped"


def _row(event_type: str, offset: int) -> tuple[Any, ...]:
    at = _BASE + timedelta(seconds=offset)
    return (f"id-{offset}", event_type, "src", None, None, at, at, {}, {}, "1.0")


class _Cursor:
    def __init__(self, calls: list[tuple[str, Any]], pages: list[list[Any]]) -> None:
        self._calls = calls
        self._pages = pages

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append((sql, params))

    def fetchall(self) -> list[Any]:
        return self._pages.pop(0)

    def fetchone(self) -> Any:
        page = self._pages.pop(0)
        return page[0] if page else None


class _Connection:
    def __init__(self, calls: list[tuple[str, Any]], pages: list[list[Any]]) -> None:
        self._calls = calls
        self._pages = pages

    def cursor(self) -> _Cursor:
        return _Cursor(self._calls, self._pages)


@pytest.fixture
def evicted_logger_cache():
    clear_cached_logger_proxies()
    yield
    clear_cached_logger_proxies()


@pytest.fixture
def calls() -> list[tuple[str, Any]]:
    return []


@pytest.fixture
def pages() -> list[list[Any]]:
    """What each successive statement answers, set per test."""
    return []


@pytest.fixture
def store(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, Any]],
    pages: list[list[Any]],
) -> PostgresEventLog:
    @contextmanager
    def _conn(self: PostgresEventLog):
        yield _Connection(calls, pages)

    monkeypatch.setattr(PostgresEventLog, "_conn", _conn)
    # ``__init__`` opens a pool and creates the schema; neither is under test.
    return PostgresEventLog.__new__(PostgresEventLog)


def test_a_read_with_nothing_to_skip_sends_one_statement(
    store: PostgresEventLog, calls: list[tuple[str, Any]], pages: list[list[Any]]
) -> None:
    pages.append([_row("trace.ingested", 0), _row("trace.ingested", 1)])
    assert [e.event_id for e in store.get_events(limit=2)] == ["id-0", "id-1"]
    assert len(calls) == 1
    assert "event_type IN" not in calls[0][0]


def test_a_short_page_with_a_skipped_row_is_not_read_again(
    store: PostgresEventLog,
    calls: list[tuple[str, Any]],
    pages: list[list[Any]],
    evicted_logger_cache: None,
) -> None:
    pages.append([_row("trace.ingested", 0), _row("future.kind", 1)])
    with capture_logs() as logs:
        events = store.get_events(limit=5)
    assert [e.event_id for e in events] == ["id-0"]
    assert len(calls) == 1
    (warning,) = [log for log in logs if log["event"] == _SKIPPED]
    assert warning["rows_skipped"] == 1
    assert warning["event_types"] == {"future.kind": 1}


def test_a_full_page_with_a_skipped_row_is_read_again_for_known_types(
    store: PostgresEventLog,
    calls: list[tuple[str, Any]],
    pages: list[list[Any]],
    evicted_logger_cache: None,
) -> None:
    pages.append([_row("future.kind", 3), _row("trace.ingested", 2)])
    pages.append([_row("trace.ingested", 2), _row("trace.ingested", 1)])
    with capture_logs() as logs:
        events = store.get_events(source="src", limit=2, order="desc")
    assert [e.event_id for e in events] == ["id-2", "id-1"]
    assert len(calls) == 2
    first_sql, first_params = calls[0]
    again_sql, again_params = calls[1]
    assert "event_type IN" not in first_sql
    assert "source = %s AND event_type IN (" in again_sql
    assert again_sql.count("%s") == 1 + len(KNOWN_EVENT_TYPE_VALUES) + 1
    # The filter's value, then every known type, then the limit, in placeholder order.
    assert again_params == ["src", *KNOWN_EVENT_TYPE_VALUES, 2]
    assert first_params == ["src", 2]
    assert len([log for log in logs if log["event"] == _SKIPPED]) == 1


def test_get_event_reads_an_unknown_type_as_absent(
    store: PostgresEventLog,
    pages: list[list[Any]],
    evicted_logger_cache: None,
) -> None:
    pages.append([_row("future.kind", 1)])
    with capture_logs() as logs:
        assert store.get_event("id-1") is None
    (warning,) = [log for log in logs if log["event"] == _SKIPPED]
    assert warning["operation"] == "get_event"
    pages.append([_row("trace.ingested", 2)])
    found = store.get_event("id-2")
    assert found is not None
    assert found.event_id == "id-2"


def test_a_readable_row_with_a_malformed_field_still_raises(
    store: PostgresEventLog, pages: list[list[Any]]
) -> None:
    bad = list(_row("trace.ingested", 1))
    bad[7] = "{"
    pages.append([tuple(bad), _row("future.kind", 2)])
    with pytest.raises(json.JSONDecodeError):
        store.get_events()
