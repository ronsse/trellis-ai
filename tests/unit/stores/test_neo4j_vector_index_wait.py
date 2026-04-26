"""Tests for ``wait_for_vector_index_online`` (Phase 1.4).

Pure-unit tests covering the polling state machine: ONLINE returns,
FAILED fast-fails, POPULATING / not-yet-visible eventually times out
with the last observed state attached.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("neo4j")

from trellis.stores.neo4j.base import (
    VectorIndexNotOnlineError,
    wait_for_vector_index_online,
)


def _driver_yielding_states(*states: dict[str, object] | None) -> MagicMock:
    """Build a mock driver whose session.run().single() returns each state in turn.

    A ``None`` entry simulates "index not visible yet" (record is None)
    — what AuraDB returns in the first ~100ms after CREATE before the
    index entry shows up in ``SHOW VECTOR INDEXES``.
    """
    driver = MagicMock(name="driver")
    session_results = []
    for state in states:
        record = None if state is None else MagicMock(name="record")
        if record is not None:
            # Mimic the dict-style access on a Neo4j Record (single()).
            record.__getitem__.side_effect = state.__getitem__
            record.get.side_effect = state.get
        session_results.append(record)

    session_iter = iter(session_results)

    def session_factory(*args: object, **kwargs: object) -> MagicMock:
        session = MagicMock(name="session")
        session.__enter__ = lambda self_: session
        session.__exit__ = lambda self_, *exc: None
        result = MagicMock(name="result")
        result.single.return_value = next(session_iter)
        session.run.return_value = result
        return session

    driver.session.side_effect = session_factory
    return driver


class TestWaitForVectorIndexOnline:
    def test_returns_immediately_when_state_is_online(self) -> None:
        driver = _driver_yielding_states(
            {"state": "ONLINE", "populationPercent": 100.0}
        )
        wait_for_vector_index_online(
            driver, database="neo4j", index_name="vec", poll_interval=0.01
        )
        # No exception → ONLINE was observed on first poll.

    def test_polls_through_populating_to_online(self) -> None:
        driver = _driver_yielding_states(
            {"state": "POPULATING", "populationPercent": 30.0},
            {"state": "POPULATING", "populationPercent": 80.0},
            {"state": "ONLINE", "populationPercent": 100.0},
        )
        wait_for_vector_index_online(
            driver,
            database="neo4j",
            index_name="vec",
            poll_interval=0.01,
            timeout=5.0,
        )

    def test_polls_through_not_visible_to_online(self) -> None:
        # Simulates AuraDB's first-100ms window where SHOW VECTOR INDEXES
        # returns no rows for the freshly-created index.
        driver = _driver_yielding_states(
            None,
            None,
            {"state": "ONLINE", "populationPercent": 100.0},
        )
        wait_for_vector_index_online(
            driver,
            database="neo4j",
            index_name="vec",
            poll_interval=0.01,
            timeout=5.0,
        )

    def test_failed_state_raises_immediately(self) -> None:
        # FAILED won't recover on its own — fast-fail rather than burning
        # the timeout.
        driver = _driver_yielding_states({"state": "FAILED", "populationPercent": 10.0})
        with pytest.raises(VectorIndexNotOnlineError) as excinfo:
            wait_for_vector_index_online(
                driver,
                database="neo4j",
                index_name="vec",
                poll_interval=0.01,
                timeout=5.0,
            )
        assert excinfo.value.state == "FAILED"
        assert excinfo.value.index_name == "vec"
        assert excinfo.value.population_percent == 10.0

    def test_timeout_raises_with_last_state_attached(self) -> None:
        # Always POPULATING — timeout should fire with the last state
        # surfaced for diagnostics.
        states = [{"state": "POPULATING", "populationPercent": 30.0} for _ in range(20)]
        driver = _driver_yielding_states(*states)
        with pytest.raises(VectorIndexNotOnlineError) as excinfo:
            wait_for_vector_index_online(
                driver,
                database="neo4j",
                index_name="vec",
                poll_interval=0.01,
                timeout=0.1,
            )
        assert excinfo.value.state == "POPULATING"
        assert excinfo.value.timeout == 0.1
        assert excinfo.value.population_percent == 30.0

    def test_timeout_with_no_state_observed(self) -> None:
        # SHOW VECTOR INDEXES never surfaces the index → timeout fires
        # with state=None.
        driver = _driver_yielding_states(*([None] * 20))
        with pytest.raises(VectorIndexNotOnlineError) as excinfo:
            wait_for_vector_index_online(
                driver,
                database="neo4j",
                index_name="missing",
                poll_interval=0.01,
                timeout=0.1,
            )
        assert excinfo.value.state is None
        assert excinfo.value.population_percent is None
        assert excinfo.value.index_name == "missing"

    def test_error_message_mentions_index_and_timeout(self) -> None:
        err = VectorIndexNotOnlineError("vec", "POPULATING", 50.0, 30.0)
        msg = str(err)
        assert "vec" in msg
        assert "30" in msg
        assert "POPULATING" in msg
        assert "50" in msg
