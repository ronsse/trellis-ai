"""``PostgresEventLog`` binds a naive timestamp as UTC and an aware one untouched.

``events.occurred_at`` is ``TIMESTAMPTZ``. psycopg binds a naive ``datetime`` as
``timestamp``, which Postgres converts to ``timestamptz`` in the *session's*
time zone, while the SQLite log and the trace store read a naive stamp as UTC.
Under a UTC session the two agree; under any other the same naive event, or the
same naive bound, names an instant hours away. Binding it as UTC makes the
answer independent of the session. An aware value has no such ambiguity, so it
must reach the driver as the very object it was: a deployment whose callers
pass aware datetimes sees no change.

No database is needed to see what is bound, so this runs wherever psycopg is
installed. The live contract suite runs these cases against a real server, but
only under that server's default session.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from trellis.stores.base.event_log import Event, EventType
from trellis.stores.postgres.event_log import PostgresEventLog

_NAIVE = datetime(2026, 3, 4, 5, 6, 7)  # noqa: DTZ001
_AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
_BEHIND = timezone(timedelta(hours=-11))
_AHEAD = timezone(timedelta(hours=14))


class _Cursor:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append((sql, params))

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> None:
        return None


class _Connection:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _Cursor:
        return _Cursor(self._calls)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """The statements a store sends, with no pool behind it."""
    recorded: list[tuple[str, Any]] = []

    @contextmanager
    def _conn(self: PostgresEventLog):
        yield _Connection(recorded)

    monkeypatch.setattr(PostgresEventLog, "_conn", _conn)
    return recorded


@pytest.fixture
def store(calls: list[tuple[str, Any]]) -> PostgresEventLog:
    # ``__init__`` opens a pool and creates the schema; neither is under test.
    return PostgresEventLog.__new__(PostgresEventLog)


def _event(occurred_at: datetime, recorded_at: datetime | None = None) -> Event:
    return Event(
        event_type=EventType.TRACE_INGESTED,
        source="binding",
        occurred_at=occurred_at,
        recorded_at=recorded_at or occurred_at,
    )


def _bound(calls: list[tuple[str, Any]]) -> list[datetime]:
    return [p for p in calls[-1][1] if isinstance(p, datetime)]


def _assert_utc(moment: datetime) -> None:
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_append_binds_a_naive_occurred_at_and_recorded_at_as_utc(
    store: PostgresEventLog, calls: list[tuple[str, Any]]
) -> None:
    store.append(_event(_NAIVE, _NAIVE + timedelta(seconds=1)))
    occurred_at, recorded_at = _bound(calls)
    _assert_utc(occurred_at)
    _assert_utc(recorded_at)
    assert occurred_at == _AT
    assert recorded_at == _AT + timedelta(seconds=1)


@pytest.mark.parametrize("zone", [UTC, _BEHIND, _AHEAD], ids=["utc", "-11", "+14"])
def test_append_binds_an_aware_stamp_as_the_object_it_was(
    store: PostgresEventLog, calls: list[tuple[str, Any]], zone: timezone
) -> None:
    occurred_at = _AT.astimezone(zone)
    recorded_at = occurred_at + timedelta(seconds=1)
    store.append(_event(occurred_at, recorded_at))
    bound_occurred, bound_recorded = _bound(calls)
    assert bound_occurred is occurred_at
    assert bound_recorded is recorded_at


def test_get_events_binds_naive_bounds_as_utc(
    store: PostgresEventLog, calls: list[tuple[str, Any]]
) -> None:
    store.get_events(since=_NAIVE, until=_NAIVE + timedelta(hours=1))
    since, until = _bound(calls)
    _assert_utc(since)
    _assert_utc(until)
    assert (since, until) == (_AT, _AT + timedelta(hours=1))


@pytest.mark.parametrize("zone", [UTC, _BEHIND, _AHEAD], ids=["utc", "-11", "+14"])
def test_get_events_binds_aware_bounds_as_the_objects_they_were(
    store: PostgresEventLog, calls: list[tuple[str, Any]], zone: timezone
) -> None:
    since = _AT.astimezone(zone)
    until = since + timedelta(hours=1)
    store.get_events(since=since, until=until)
    bound_since, bound_until = _bound(calls)
    assert bound_since is since
    assert bound_until is until


def test_count_binds_a_naive_since_as_utc(
    store: PostgresEventLog, calls: list[tuple[str, Any]]
) -> None:
    store.count(since=_NAIVE)
    (since,) = _bound(calls)
    _assert_utc(since)
    assert since == _AT


@pytest.mark.parametrize("zone", [UTC, _BEHIND, _AHEAD], ids=["utc", "-11", "+14"])
def test_count_binds_an_aware_since_as_the_object_it_was(
    store: PostgresEventLog, calls: list[tuple[str, Any]], zone: timezone
) -> None:
    since = _AT.astimezone(zone)
    store.count(since=since)
    (bound,) = _bound(calls)
    assert bound is since


def test_an_aware_bound_at_the_edge_of_datetime_is_not_clamped(
    store: PostgresEventLog, calls: list[tuple[str, Any]]
) -> None:
    # ``as_utc`` clamps a stamp it cannot name in UTC; the aware path does not
    # go through it, so this binds as it did before the naive fix.
    low = datetime.min.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    high = datetime.max.replace(tzinfo=timezone(timedelta(hours=-5)))
    store.get_events(since=low, until=high)
    since, until = _bound(calls)
    assert since is low
    assert until is high
