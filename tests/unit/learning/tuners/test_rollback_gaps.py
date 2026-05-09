"""Targeted gap-coverage tests for :mod:`trellis.learning.tuners.rollback`.

The main suite in ``test_rollback.py`` covers verdict spaces (unknown
version, no promotion event, no baseline, insufficient samples,
ok/degraded/event_only/demoted), the sweep helper, and policy
defaults. This file backfills the helper-level branches the audit
flagged: ``_aggregate_success_rate``'s zero-count short-circuit, the
saturation guard when a window exceeds the 10k row limit, and
``_load_promotion_event``'s rollback-skip preference.

Helpers are exercised directly because going through the public
``monitor_post_promotion`` API would require fabricating 10 001
outcome rows just to land on the saturation branch — an expensive
way to cover a few lines of helper code. ``MagicMock(spec=…)``
satisfies the same protocol the helpers actually call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.learning.tuners.rollback import (
    _AGGREGATE_SUCCESS_RATE_LIMIT,
    _aggregate_success_rate,
    _load_promotion_event,
)
from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.outcome import OutcomeStore
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    try:
        yield log
    finally:
        log.close()


# ---------------------------------------------------------------------------
# _aggregate_success_rate — zero count short-circuit + saturation guard
# ---------------------------------------------------------------------------


def test_aggregate_success_rate_zero_count_returns_no_signal():
    """``count == 0`` short-circuits before ``query()`` is called.

    Covers the ``return 0, None`` branch at ``rollback.py:530``. The
    rest of the suite always primes the outcome store with samples,
    so this fast path is unreached.
    """
    store = MagicMock(spec=OutcomeStore)
    store.count.return_value = 0

    samples, rate = _aggregate_success_rate(
        store,
        params_version="pv_empty",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert samples == 0
    assert rate is None
    # Crucially, the expensive query() call is skipped.
    store.query.assert_not_called()


def test_aggregate_success_rate_caps_query_at_limit_when_saturated():
    """Going over the 10k limit caps the ``query()`` call to the limit.

    Covers the saturation branch at ``rollback.py:531-537``. We don't
    need 10 001 outcomes to land there — the count probe is a separate
    call we can mock. The behavioural assertion is twofold:

    * ``query()`` receives ``limit=_AGGREGATE_SUCCESS_RATE_LIMIT``,
      not the unbounded count, so the function never tries to load
      every row.
    * The function still returns a rate computed from the truncated
      slice, matching the docstring's "rate, partial" contract.
    """
    store = MagicMock(spec=OutcomeStore)
    store.count.return_value = _AGGREGATE_SUCCESS_RATE_LIMIT + 1
    sample = OutcomeEvent(
        component_id="retrieve.strategies.KeywordSearch",
        params_version="pv_huge",
        outcome=ComponentOutcome(success=True, latency_ms=1.0),
    )
    store.query.return_value = [sample]

    samples, rate = _aggregate_success_rate(
        store,
        params_version="pv_huge",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 1, 8, tzinfo=UTC),
    )

    assert samples == 1
    assert rate == 1.0
    _, kwargs = store.query.call_args
    assert kwargs["limit"] == _AGGREGATE_SUCCESS_RATE_LIMIT
    assert kwargs["params_version"] == "pv_huge"


def test_aggregate_success_rate_query_returns_empty_after_count():
    """Defensive branch: ``count > 0`` but ``query()`` returns nothing.

    Covers the ``if not outcomes: return 0, None`` arm at
    ``rollback.py:544-545`` — a logical possibility (concurrent delete
    between the count probe and the query) that the integration tests
    can't trigger because they share the same DB connection.
    """
    store = MagicMock(spec=OutcomeStore)
    store.count.return_value = 5
    store.query.return_value = []

    samples, rate = _aggregate_success_rate(
        store,
        params_version="pv_race",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 1, 8, tzinfo=UTC),
    )
    assert samples == 0
    assert rate is None


# ---------------------------------------------------------------------------
# _load_promotion_event — rollback-skip preference
# ---------------------------------------------------------------------------


def test_load_promotion_event_skips_rollback_events(event_log):
    """When events for the same id include a rollback, prefer the original.

    Covers the ``if payload.get("reverted_from") is not None: continue``
    branch at ``rollback.py:488-489``. The existing sweep test exercises
    rollback-skipping at the sweep level; this pin nails the helper-level
    behaviour so refactors of the sort order don't silently break it.
    """
    # Event #1: rollback (recorded first, in the past).
    event_log.emit(
        EventType.PARAMS_UPDATED,
        source="tuner.rollback",
        entity_id="pv_target",
        entity_type="parameter_set",
        payload={
            "params_version": "pv_target",
            "baseline_version": "pv_unrelated",
            "reverted_from": "pv_other",
        },
    )
    # Event #2: original promotion (recorded second; newer in time but
    # the rollback at the same entity_id should be excluded by the filter).
    event_log.emit(
        EventType.PARAMS_UPDATED,
        source="tuner.promotion",
        entity_id="pv_target",
        entity_type="parameter_set",
        payload={
            "params_version": "pv_target",
            "baseline_version": "pv_baseline",
        },
    )

    event, baseline_version = _load_promotion_event(event_log, "pv_target")
    assert event is not None
    assert baseline_version == "pv_baseline"
    assert (event.payload or {}).get("reverted_from") is None


def test_load_promotion_event_returns_none_when_only_rollbacks_match(event_log):
    """If every matching event is a rollback, the helper returns ``(None, None)``.

    Forces the loop to exhaust without returning, hitting the trailing
    ``return None, None`` at ``rollback.py:491``.
    """
    event_log.emit(
        EventType.PARAMS_UPDATED,
        source="tuner.rollback",
        entity_id="pv_only_rollback",
        entity_type="parameter_set",
        payload={
            "params_version": "pv_only_rollback",
            "reverted_from": "pv_other",
        },
    )

    event, baseline_version = _load_promotion_event(event_log, "pv_only_rollback")
    assert event is None
    assert baseline_version is None


def test_load_promotion_event_returns_none_for_unknown_version(event_log):
    """No matching event at all → ``(None, None)``.

    Edge case: the helper must not crash when ``get_events`` returns an
    empty list. Anchors the empty-iteration fall-through.
    """
    event, baseline_version = _load_promotion_event(event_log, "pv_does_not_exist")
    assert event is None
    assert baseline_version is None


# ---------------------------------------------------------------------------
# Light contract test: helper signatures stay stable
# ---------------------------------------------------------------------------


def test_load_promotion_event_uses_get_events_with_correct_filters():
    """The helper hands the right kwargs to ``EventLog.get_events``.

    Spec-mocked EventLog confirms the call signature stays stable —
    important because the rollback-monitor flow relies on the
    ``entity_id``-keyed query to scope the lookup tightly.
    """
    log = MagicMock(spec=EventLog)
    log.get_events.return_value = []
    _load_promotion_event(log, "pv_x")
    log.get_events.assert_called_once()
    _, kwargs = log.get_events.call_args
    assert kwargs["event_type"] == EventType.PARAMS_UPDATED
    assert kwargs["entity_id"] == "pv_x"
    assert kwargs["limit"] == 50
