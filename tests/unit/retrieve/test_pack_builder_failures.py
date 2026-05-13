"""Failure-injection tests for ``PackBuilder`` — C2 Phase 4.

The builder now surfaces strategy failures instead of silently producing
empty packs:

* A single configured strategy that raises → ``PackAssemblyError``.
* All strategies in a multi-strategy pipeline raise → ``PackAssemblyError``.
* One of several strategies raises → the build continues with survivors
  and the failure is recorded in the ``PACK_ASSEMBLED`` event payload
  under ``strategy_failures``.
* A configured reranker that raises → ``PackAssemblyError``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.pack_builder import (
    PackAssemblyError,
    PackBuilder,
    StrategyFailure,
)
from trellis.retrieve.rerankers.base import Reranker
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


def _make_strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    """Mock strategy returning the given items from ``.search(...)``."""
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _failing_strategy(name: str, exc: Exception) -> SearchStrategy:
    """Mock strategy whose ``.search(...)`` raises ``exc``."""
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.side_effect = exc
    return strategy


def _item(item_id: str, score: float, excerpt: str = "text") -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=excerpt,
        relevance_score=score,
    )


class TestRequiredStrategyFailureRaises:
    """Single configured strategy that fails must raise — never silent-empty."""

    def test_single_strategy_failure_raises_pack_assembly_error(self) -> None:
        bad = _failing_strategy("kw", RuntimeError("index missing"))
        builder = PackBuilder(strategies=[bad])
        with pytest.raises(PackAssemblyError) as excinfo:
            builder.build("q")
        assert "kw" in str(excinfo.value)
        assert "index missing" in str(excinfo.value)
        # ``strategy_failures`` carried on the exception.
        assert len(excinfo.value.strategy_failures) == 1
        failure = excinfo.value.strategy_failures[0]
        assert isinstance(failure, StrategyFailure)
        assert failure.strategy == "kw"
        assert failure.error_class == "RuntimeError"

    def test_single_strategy_failure_in_sectioned_build_raises(self) -> None:
        bad = _failing_strategy("kw", RuntimeError("vector store offline"))
        builder = PackBuilder(strategies=[bad])
        with pytest.raises(PackAssemblyError):
            builder.build_sectioned(
                "q", sections=[SectionRequest(name="default")],
            )


class TestAllStrategiesFailRaises:
    """When every strategy raises, the build cannot proceed — must raise."""

    def test_all_strategies_fail_raises(self) -> None:
        s1 = _failing_strategy("kw", RuntimeError("a"))
        s2 = _failing_strategy("sem", RuntimeError("b"))
        s3 = _failing_strategy("graph", RuntimeError("c"))
        builder = PackBuilder(strategies=[s1, s2, s3])
        with pytest.raises(PackAssemblyError) as excinfo:
            builder.build("q")
        assert "All 3 configured strategies failed" in str(excinfo.value)
        assert len(excinfo.value.strategy_failures) == 3
        names = {f.strategy for f in excinfo.value.strategy_failures}
        assert names == {"kw", "sem", "graph"}


class TestPartialStrategyFailureRecorded:
    """One of several strategies fails → continue, record into event payload."""

    def test_one_of_three_fails_pack_returned_with_survivors(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(db_path=tmp_path / "events.db")
        good_a = _make_strategy("kw", [_item("d1", 0.9)])
        bad = _failing_strategy("sem", RuntimeError("embedder down"))
        good_b = _make_strategy("graph", [_item("e1", 0.7)])
        builder = PackBuilder(
            strategies=[good_a, bad, good_b], event_log=event_log
        )
        pack = builder.build("q")

        # Survivor strategies returned items.
        item_ids = {item.item_id for item in pack.items}
        assert item_ids == {"d1", "e1"}
        # ``strategies_used`` excludes the failed one.
        assert set(pack.retrieval_report.strategies_used) == {"kw", "graph"}

    def test_pack_assembled_event_contains_strategy_failures(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(db_path=tmp_path / "events.db")
        good = _make_strategy("kw", [_item("d1", 0.9)])
        bad = _failing_strategy("sem", ValueError("bad query vector"))
        builder = PackBuilder(strategies=[good, bad], event_log=event_log)
        builder.build("q")

        events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
        assert len(events) == 1
        payload = events[0].payload or {}
        failures = payload.get("strategy_failures")
        assert isinstance(failures, list)
        assert len(failures) == 1
        assert failures[0]["strategy"] == "sem"
        assert failures[0]["error_class"] == "ValueError"
        assert "bad query vector" in failures[0]["message"]

    def test_pack_assembled_event_has_empty_failures_when_all_succeed(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(db_path=tmp_path / "events.db")
        a = _make_strategy("kw", [_item("d1", 0.9)])
        b = _make_strategy("sem", [_item("v1", 0.8)])
        builder = PackBuilder(strategies=[a, b], event_log=event_log)
        builder.build("q")

        events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
        assert len(events) == 1
        payload = events[0].payload or {}
        # Empty list (not missing) — schema consistency for downstream consumers.
        assert payload.get("strategy_failures") == []

    def test_sectioned_event_carries_strategy_failures(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(db_path=tmp_path / "events.db")
        good = _make_strategy("kw", [_item("d1", 0.9)])
        bad = _failing_strategy("sem", RuntimeError("oops"))
        builder = PackBuilder(strategies=[good, bad], event_log=event_log)
        builder.build_sectioned("q", sections=[SectionRequest(name="all")])

        events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
        assert len(events) == 1
        payload = events[0].payload or {}
        assert len(payload.get("strategy_failures", [])) == 1
        assert payload["strategy_failures"][0]["strategy"] == "sem"


class TestRerankerFailureRaises:
    """Configured reranker that raises must surface — never silent fallback."""

    def test_reranker_failure_raises_pack_assembly_error(self) -> None:
        good = _make_strategy("kw", [_item("d1", 0.9)])
        bad_reranker = MagicMock(spec=Reranker)
        bad_reranker.name = "cross_encoder"
        bad_reranker.rerank.side_effect = RuntimeError("model load failed")
        builder = PackBuilder(strategies=[good], reranker=bad_reranker)
        with pytest.raises(PackAssemblyError) as excinfo:
            builder.build("q")
        assert "cross_encoder" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, RuntimeError)


class TestStrategyFailureSerialization:
    """``StrategyFailure.to_event_payload`` produces JSON-serializable dicts."""

    def test_to_event_payload_shape(self) -> None:
        f = StrategyFailure(
            strategy="kw", error_class="RuntimeError", message="boom"
        )
        assert f.to_event_payload() == {
            "strategy": "kw",
            "error_class": "RuntimeError",
            "message": "boom",
        }
