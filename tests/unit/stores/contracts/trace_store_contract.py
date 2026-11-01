"""TraceStore contract test suite — runs against every backend.

Mirrors the shape of :mod:`tests.unit.stores.contracts.graph_store_contract`
and :mod:`tests.unit.stores.contracts.vector_store_contract`. Defines the
shared semantics every ``TraceStore`` backend must honour. Backend-specific
test files (``test_sqlite_trace_contract.py`` etc.) subclass
:class:`TraceStoreContractTests` and provide a ``store`` fixture.

The harness deliberately:

* Does **not** test backend-specific schema / index / migration behaviour
  — those tests live in the per-backend ``test_<backend>_*`` files. The
  contract suite is *additive*.
* Uses only the public ``TraceStore`` ABC surface (``append``, ``get``,
  ``query``, ``count``, ``close``). If the contract needs something the
  ABC does not expose, the ABC needs the missing method, not the harness.
* Sleeps briefly between operations whose ordering is checked, since
  Postgres ``TIMESTAMPTZ`` and SQLite ISO strings have similar but
  not sub-millisecond resolution.

Subclass shape::

    class TestSQLiteTraceContract(TraceStoreContractTests):
        @pytest.fixture
        def store(self, tmp_path):
            store = SQLiteTraceStore(tmp_path / "traces.db")
            yield store
            store.close()
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from trellis.core.base import utc_now
from trellis.errors import StoreError
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep

if TYPE_CHECKING:
    from trellis.stores.base.trace import TraceStore


def _sleep_for_ordering() -> None:
    """Sleep long enough that two appends get distinct ``created_at`` values."""
    time.sleep(0.005)


def _make_trace(**kwargs: Any) -> Trace:
    """Build a valid ``Trace`` with sensible defaults; override via kwargs."""
    defaults: dict[str, Any] = {
        "source": TraceSource.AGENT,
        "intent": "test task",
        "steps": [],
        "context": TraceContext(agent_id="agent-1", domain="platform"),
    }
    defaults.update(kwargs)
    return Trace(**defaults)


class TraceStoreContractTests:
    """Contract tests every ``TraceStore`` backend must pass.

    Subclasses must provide a pytest fixture named ``store`` that yields a
    fresh, empty :class:`~trellis.stores.base.trace.TraceStore` instance and
    tears it down afterwards.
    """

    # ------------------------------------------------------------------
    # append / get — round-trip
    # ------------------------------------------------------------------

    def test_append_returns_trace_id(self, store: TraceStore) -> None:
        trace = _make_trace()
        tid = store.append(trace)
        assert tid == trace.trace_id

    def test_append_then_get_round_trips_all_fields(self, store: TraceStore) -> None:
        trace = _make_trace(
            intent="deploy auth service",
            context=TraceContext(agent_id="agent-7", domain="platform"),
            outcome=Outcome(status=OutcomeStatus.SUCCESS, metrics={"time_s": 42}),
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="kubectl apply",
                    args={"f": "deploy.yaml"},
                )
            ],
            metadata={"trigger": "manual"},
        )
        store.append(trace)
        got = store.get(trace.trace_id)
        assert got is not None
        assert got.trace_id == trace.trace_id
        assert got.source == TraceSource.AGENT
        assert got.intent == "deploy auth service"
        assert got.context is not None
        assert got.context.agent_id == "agent-7"
        assert got.context.domain == "platform"
        assert got.outcome is not None
        assert got.outcome.status == OutcomeStatus.SUCCESS
        assert got.outcome.metrics == {"time_s": 42}
        assert len(got.steps) == 1
        assert got.steps[0].name == "kubectl apply"
        assert got.steps[0].args == {"f": "deploy.yaml"}
        assert got.metadata == {"trigger": "manual"}

    def test_get_returns_none_for_missing(self, store: TraceStore) -> None:
        assert store.get("does-not-exist") is None

    # ------------------------------------------------------------------
    # idempotency — duplicate trace_id rejected
    # ------------------------------------------------------------------

    def test_append_duplicate_trace_id_raises_store_error(
        self, store: TraceStore
    ) -> None:
        # Per ``TraceStore.append`` ABC: "Raises if trace_id already exists."
        # The error type is ``StoreError`` — traces are immutable so a
        # duplicate is a hard reject, not a no-op.
        trace = _make_trace()
        store.append(trace)
        with pytest.raises(StoreError):
            store.append(trace)

    def test_append_duplicate_does_not_overwrite(self, store: TraceStore) -> None:
        original = _make_trace(intent="original intent")
        store.append(original)
        # Build a *different* Trace object that re-uses the same trace_id.
        # The duplicate must be rejected and the original preserved.
        duplicate = _make_trace(intent="impostor intent")
        duplicate_with_collision = duplicate.model_copy(
            update={"trace_id": original.trace_id}
        )
        with pytest.raises(StoreError):
            store.append(duplicate_with_collision)
        got = store.get(original.trace_id)
        assert got is not None
        assert got.intent == "original intent"

    # ------------------------------------------------------------------
    # query — empty store
    # ------------------------------------------------------------------

    def test_query_empty_store_returns_empty_list(self, store: TraceStore) -> None:
        results = store.query()
        assert results == []
        assert isinstance(results, list)

    def test_count_empty_store_returns_zero(self, store: TraceStore) -> None:
        assert store.count() == 0

    def test_query_filters_on_empty_store_return_empty_list(
        self, store: TraceStore
    ) -> None:
        # Empty-store behaviour must hold under filtering too — never None,
        # never raise.
        assert store.query(source="agent") == []
        assert store.query(domain="platform") == []
        assert store.query(agent_id="ghost") == []

    # ------------------------------------------------------------------
    # query — filters
    # ------------------------------------------------------------------

    def test_query_by_source(self, store: TraceStore) -> None:
        store.append(_make_trace(source=TraceSource.AGENT))
        store.append(_make_trace(source=TraceSource.HUMAN))
        store.append(_make_trace(source=TraceSource.AGENT))
        results = store.query(source="agent")
        assert len(results) == 2
        assert all(r.source == TraceSource.AGENT for r in results)

    def test_query_by_domain(self, store: TraceStore) -> None:
        store.append(_make_trace(context=TraceContext(domain="platform")))
        store.append(_make_trace(context=TraceContext(domain="data")))
        results = store.query(domain="platform")
        assert len(results) == 1
        assert results[0].context is not None
        assert results[0].context.domain == "platform"

    def test_query_by_agent_id(self, store: TraceStore) -> None:
        store.append(_make_trace(context=TraceContext(agent_id="a1")))
        store.append(_make_trace(context=TraceContext(agent_id="a2")))
        results = store.query(agent_id="a1")
        assert len(results) == 1
        assert results[0].context is not None
        assert results[0].context.agent_id == "a1"

    def test_query_returns_empty_for_unknown_filter_value(
        self, store: TraceStore
    ) -> None:
        store.append(_make_trace(source=TraceSource.AGENT))
        assert store.query(source="ghost-source") == []
        assert store.query(domain="ghost-domain") == []
        assert store.query(agent_id="ghost-agent") == []

    # ------------------------------------------------------------------
    # query — time range
    # ------------------------------------------------------------------

    def test_query_by_since_includes_now(self, store: TraceStore) -> None:
        now = utc_now()
        store.append(_make_trace())
        results = store.query(since=now - timedelta(hours=1))
        assert len(results) == 1

    def test_query_by_since_excludes_past(self, store: TraceStore) -> None:
        store.append(_make_trace())
        # All traces created before "now + 1h" — using `since` in the future
        # must filter them out.
        results = store.query(since=utc_now() + timedelta(hours=1))
        assert results == []

    def test_query_by_until_includes_past(self, store: TraceStore) -> None:
        store.append(_make_trace())
        results = store.query(until=utc_now() + timedelta(hours=1))
        assert len(results) == 1

    def test_query_by_until_excludes_future(self, store: TraceStore) -> None:
        store.append(_make_trace())
        # All traces created at "now" — using `until` in the past must
        # filter them out.
        results = store.query(until=utc_now() - timedelta(hours=1))
        assert results == []

    def test_query_by_time_range(self, store: TraceStore) -> None:
        now = utc_now()
        store.append(_make_trace())
        results = store.query(
            since=now - timedelta(hours=1), until=now + timedelta(hours=1)
        )
        assert len(results) == 1

    # ------------------------------------------------------------------
    # query — time range compares by instant, whatever offset was written
    # ------------------------------------------------------------------

    def test_since_finds_a_trace_written_with_a_negative_offset(
        self, store: TraceStore
    ) -> None:
        # One hour ago, written as eleven hours behind UTC: the wall-clock
        # digits read twelve hours ago, which a text comparison against a
        # two-hours-ago bound would call too old.
        instant = utc_now().replace(microsecond=0) - timedelta(hours=1)
        behind = timezone(timedelta(hours=-11))
        trace = _make_trace(created_at=instant.astimezone(behind))
        store.append(trace)
        found = store.query(since=utc_now() - timedelta(hours=2))
        assert [t.trace_id for t in found] == [trace.trace_id]

    def test_until_finds_a_trace_written_with_a_positive_offset(
        self, store: TraceStore
    ) -> None:
        # One hour ago, written as fourteen hours ahead of UTC: the digits
        # read thirteen hours from now, which a text comparison against an
        # ``until`` of now would call too new.
        instant = utc_now().replace(microsecond=0) - timedelta(hours=1)
        ahead = timezone(timedelta(hours=14))
        trace = _make_trace(created_at=instant.astimezone(ahead))
        store.append(trace)
        found = store.query(until=utc_now())
        assert [t.trace_id for t in found] == [trace.trace_id]

    def test_a_bound_given_with_an_offset_means_its_instant(
        self, store: TraceStore
    ) -> None:
        instant = utc_now().replace(microsecond=0) - timedelta(hours=1)
        trace = _make_trace(created_at=instant)
        store.append(trace)
        ahead = timezone(timedelta(hours=14))
        behind = timezone(timedelta(hours=-11))
        # Both bounds name an instant 30 minutes either side of the trace.
        found = store.query(
            since=(instant - timedelta(minutes=30)).astimezone(behind),
            until=(instant + timedelta(minutes=30)).astimezone(ahead),
        )
        assert [t.trace_id for t in found] == [trace.trace_id]
        # And one that is 30 minutes past it, in the same dress, excludes it.
        assert (
            store.query(since=(instant + timedelta(minutes=30)).astimezone(behind))
            == []
        )

    def test_results_are_ordered_by_instant_across_offsets(
        self, store: TraceStore
    ) -> None:
        now = utc_now().replace(microsecond=0)
        # ``earlier`` happened first but its digits are the larger: written
        # fourteen hours ahead of UTC, they read twelve hours from now.
        earlier = _make_trace(
            intent="earlier",
            created_at=(now - timedelta(hours=2)).astimezone(
                timezone(timedelta(hours=14))
            ),
        )
        later = _make_trace(intent="later", created_at=now - timedelta(hours=1))
        store.append(earlier)
        store.append(later)
        assert [t.intent for t in store.query()] == ["later", "earlier"]

    def test_bounds_are_inclusive_to_the_microsecond(self, store: TraceStore) -> None:
        instant = datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)
        written = instant.astimezone(timezone(timedelta(hours=5, minutes=30)))
        trace = _make_trace(created_at=written)
        store.append(trace)
        tick = timedelta(microseconds=1)
        assert len(store.query(since=instant, until=instant)) == 1
        assert store.query(since=instant + tick) == []
        assert store.query(until=instant - tick) == []

    def test_a_naive_created_at_is_read_as_utc(self, store: TraceStore) -> None:
        # ``Trace`` accepts a naive ``created_at`` and Postgres reads one as
        # UTC in its default session, so the store does too: the trace is
        # found by the instant its digits name in UTC, naive bound or aware.
        now = utc_now().replace(microsecond=0)
        naive = (now - timedelta(hours=1)).replace(tzinfo=None)
        trace = _make_trace(created_at=naive)
        store.append(trace)
        around = {
            "since": now - timedelta(hours=2),
            "until": now - timedelta(minutes=30),
        }
        assert [t.trace_id for t in store.query(**around)] == [trace.trace_id]
        naive_around = {k: v.replace(tzinfo=None) for k, v in around.items()}
        assert [t.trace_id for t in store.query(**naive_around)] == [trace.trace_id]
        # Outside the window, an hour either side, it is not found.
        assert store.query(since=now - timedelta(minutes=30)) == []
        assert store.query(until=now - timedelta(hours=2)) == []
        # Both bounds are inclusive at the very instant, which a text compare
        # misses: "...T05:06:07" sorts before "...T05:06:07+00:00".
        at = naive.replace(tzinfo=UTC)
        assert [t.trace_id for t in store.query(since=at, until=at)] == [trace.trace_id]

    def test_naive_and_aware_traces_query_together_in_order(
        self, store: TraceStore
    ) -> None:
        now = utc_now().replace(microsecond=0)
        old_naive = _make_trace(
            intent="old naive",
            created_at=(now - timedelta(hours=3)).replace(tzinfo=None),
        )
        aware = _make_trace(intent="aware", created_at=now - timedelta(hours=2))
        new_naive = _make_trace(
            intent="new naive",
            created_at=(now - timedelta(hours=1)).replace(tzinfo=None),
        )
        for trace in (aware, old_naive, new_naive):
            store.append(trace)
        found = store.query(since=now - timedelta(hours=4), until=now)
        assert [t.intent for t in found] == ["new naive", "aware", "old naive"]

    # ------------------------------------------------------------------
    # query — limit + ordering
    # ------------------------------------------------------------------

    def test_query_respects_limit(self, store: TraceStore) -> None:
        for _ in range(10):
            store.append(_make_trace())
        results = store.query(limit=3)
        assert len(results) == 3

    def test_query_default_limit_does_not_drop_small_result(
        self, store: TraceStore
    ) -> None:
        for _ in range(5):
            store.append(_make_trace())
        results = store.query()
        assert len(results) == 5

    def test_query_returns_results_in_reverse_chronological_order(
        self, store: TraceStore
    ) -> None:
        # Per ``SQLiteTraceStore.query`` — default ordering is
        # ``ORDER BY created_at DESC`` so the most recent appears first.
        # The contract is "newest first" as observed across implementations.
        first = _make_trace(intent="first")
        store.append(first)
        _sleep_for_ordering()
        second = _make_trace(intent="second")
        store.append(second)
        _sleep_for_ordering()
        third = _make_trace(intent="third")
        store.append(third)

        results = store.query()
        assert len(results) == 3
        intents = [r.intent for r in results]
        assert intents == ["third", "second", "first"]

    # ------------------------------------------------------------------
    # count — total + filtered
    # ------------------------------------------------------------------

    def test_count_total(self, store: TraceStore) -> None:
        store.append(_make_trace())
        store.append(_make_trace(source=TraceSource.HUMAN))
        assert store.count() == 2

    def test_count_filtered_by_source(self, store: TraceStore) -> None:
        store.append(_make_trace(source=TraceSource.AGENT))
        store.append(_make_trace(source=TraceSource.HUMAN))
        store.append(_make_trace(source=TraceSource.AGENT))
        assert store.count(source="agent") == 2
        assert store.count(source="human") == 1

    def test_count_filtered_by_domain(self, store: TraceStore) -> None:
        store.append(_make_trace(context=TraceContext(domain="platform")))
        store.append(_make_trace(context=TraceContext(domain="data")))
        store.append(_make_trace(context=TraceContext(domain="platform")))
        assert store.count(domain="platform") == 2
        assert store.count(domain="data") == 1

    def test_count_unknown_filter_value_returns_zero(self, store: TraceStore) -> None:
        store.append(_make_trace())
        assert store.count(source="ghost") == 0
        assert store.count(domain="ghost") == 0

    def test_count_by_domain_groups_and_omits_none(self, store: TraceStore) -> None:
        store.append(_make_trace(context=TraceContext(domain="platform")))
        store.append(_make_trace(context=TraceContext(domain="data")))
        store.append(_make_trace(context=TraceContext(domain="platform")))
        store.append(_make_trace(context=TraceContext(agent_id="agent-1")))
        counts = store.count_by_domain()
        assert counts == {"platform": 2, "data": 1}

    def test_count_by_domain_empty_store_returns_empty_dict(
        self, store: TraceStore
    ) -> None:
        assert store.count_by_domain() == {}

    # ------------------------------------------------------------------
    # round-trip — nested structures preserved
    # ------------------------------------------------------------------

    def test_outcome_round_trip(self, store: TraceStore) -> None:
        trace = _make_trace(
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={"time_s": 42},
                summary="all good",
            ),
        )
        store.append(trace)
        got = store.get(trace.trace_id)
        assert got is not None
        assert got.outcome is not None
        assert got.outcome.status == OutcomeStatus.SUCCESS
        assert got.outcome.metrics == {"time_s": 42}
        assert got.outcome.summary == "all good"

    def test_steps_round_trip(self, store: TraceStore) -> None:
        trace = _make_trace(
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="kubectl apply",
                    args={"f": "deploy.yaml"},
                ),
                TraceStep(
                    step_type="observation",
                    name="check",
                    result={"ok": True},
                ),
            ],
        )
        store.append(trace)
        got = store.get(trace.trace_id)
        assert got is not None
        assert len(got.steps) == 2
        assert got.steps[0].name == "kubectl apply"
        assert got.steps[0].args == {"f": "deploy.yaml"}
        assert got.steps[1].result == {"ok": True}

    def test_metadata_round_trip(self, store: TraceStore) -> None:
        trace = _make_trace(metadata={"foo": "bar", "n": 7, "nested": {"k": "v"}})
        store.append(trace)
        got = store.get(trace.trace_id)
        assert got is not None
        assert got.metadata == {"foo": "bar", "n": 7, "nested": {"k": "v"}}
