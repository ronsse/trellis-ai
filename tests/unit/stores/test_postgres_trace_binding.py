"""``PostgresTraceStore`` binds every timestamp as an aware UTC instant.

``traces.created_at`` is ``TIMESTAMPTZ``. psycopg binds a naive ``datetime`` as
``timestamp``, which Postgres converts to ``timestamptz`` in the *session's*
time zone, while the embed worker and the other stores read a naive stamp as
UTC. Under a UTC session the two agree; under any other they put the same
naive trace twelve hours apart. Binding ``as_utc(...)`` makes the answer
independent of the session. No database is needed to see what is bound, so this
runs everywhere psycopg is installed; the live contract suite covers the rest.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.postgres.trace import PostgresTraceStore

_NAIVE = datetime(2026, 3, 4, 5, 6, 7)  # noqa: DTZ001
_AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


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
    def _conn(self: PostgresTraceStore):
        yield _Connection(recorded)

    monkeypatch.setattr(PostgresTraceStore, "_conn", _conn)
    return recorded


@pytest.fixture
def store(calls: list[tuple[str, Any]]) -> PostgresTraceStore:
    # ``__init__`` opens a pool and creates the schema; neither is under test.
    return PostgresTraceStore.__new__(PostgresTraceStore)


def _trace(intent: str, created_at: datetime) -> Trace:
    return Trace(
        source=TraceSource.AGENT,
        intent=intent,
        context=TraceContext(agent_id="agent-1", domain="platform"),
        created_at=created_at,
    )


def _bound(calls: list[tuple[str, Any]]) -> list[datetime]:
    return [p for p in calls[-1][1] if isinstance(p, datetime)]


def _assert_utc(moment: datetime) -> None:
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_append_binds_a_naive_created_at_as_utc(
    store: PostgresTraceStore, calls: list[tuple[str, Any]]
) -> None:
    trace = _trace("naive", _NAIVE)
    store.append(trace)
    (bound,) = _bound(calls)
    _assert_utc(bound)
    assert bound == _AT


def test_append_binds_an_offset_created_at_as_the_same_instant(
    store: PostgresTraceStore, calls: list[tuple[str, Any]]
) -> None:
    behind = timezone(timedelta(hours=-11))
    trace = _trace("offset", _AT.astimezone(behind))
    store.append(trace)
    (bound,) = _bound(calls)
    _assert_utc(bound)
    assert bound == _AT


def test_query_binds_naive_bounds_as_utc(
    store: PostgresTraceStore, calls: list[tuple[str, Any]]
) -> None:
    store.query(since=_NAIVE, until=_NAIVE + timedelta(hours=1))
    since, until = _bound(calls)
    _assert_utc(since)
    _assert_utc(until)
    assert (since, until) == (_AT, _AT + timedelta(hours=1))


def test_query_binds_offset_bounds_as_the_same_instants(
    store: PostgresTraceStore, calls: list[tuple[str, Any]]
) -> None:
    ahead = timezone(timedelta(hours=14))
    store.query(since=_AT.astimezone(ahead), until=_AT.astimezone(ahead))
    since, until = _bound(calls)
    _assert_utc(since)
    _assert_utc(until)
    assert (since, until) == (_AT, _AT)
