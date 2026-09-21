"""Replaying feedback history into the outcome store (B3).

The outcome stack bridges *new* feedback into ``OutcomeEvent`` rows, so a
deployment that already has months of history gets an empty store on the
day it ships and the rule tuner's first useful pass is a trailing window
away. These tests pin the replay that fixes it — and pin the three
properties that make a replay safe rather than a second writer:

* the replayed row is **the same row** the live path would have written,
* a second pass writes nothing, and a *partial* pass completes,
* rows the live bridge never wrote are never invented.

The extraction that makes the first property structural — one
``bridge_feedback_to_outcomes`` called by both paths — is proved by
``test_strategy_outcome_fanout.py`` and ``test_outcome_scope_axes.py``
staying green against it; this file proves the replay on top.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from trellis.feedback import PackFeedback, record_feedback
from trellis.feedback.backfill import _CollectingOutcomeStore, backfill_outcomes
from trellis.feedback.recording import bridge_feedback_to_outcomes
from trellis.learning.tuners.rule_tuner import aggregate_outcomes
from trellis.schemas.outcome import (
    GRAPH_SEARCH_COMPONENT_ID,
    KEYWORD_SEARCH_COMPONENT_ID,
    PACK_BUILDER_COMPONENT_ID,
    SEMANTIC_SEARCH_COMPONENT_ID,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore

if TYPE_CHECKING:
    from trellis.schemas.outcome import OutcomeEvent

PACK_ID = "pack-backfill"

#: Fields that legitimately differ between two writes of the same row:
#: a fresh ULID, and the wall clock at insert.
_VOLATILE_FIELDS = {"event_id", "recorded_at"}


@pytest.fixture
def outcome_store(tmp_path: Path):
    store = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    yield store
    store.close()


@pytest.fixture
def replay_store(tmp_path: Path):
    """A second, independent store — the replay's destination."""
    store = SQLiteOutcomeStore(tmp_path / "replay.db")
    yield store
    store.close()


@pytest.fixture
def event_log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _item(item_id: str, strategy: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"item_id": item_id, "item_type": "document"}
    if strategy is not None:
        row["strategy_source"] = strategy
    return row


def _emit_pack(
    event_log: SQLiteEventLog,
    injected_items: list[dict[str, Any]],
    *,
    pack_id: str = PACK_ID,
) -> None:
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "injected_item_ids": [row["item_id"] for row in injected_items],
            "injected_items": injected_items,
        },
    )


def _feedback(**overrides: Any) -> PackFeedback:
    defaults: dict[str, Any] = {
        "run_id": "run-1",
        "phase": "retrieve",
        "intent": "fetch context",
        "outcome": "success",
        "items_served": [],
        "items_referenced": [],
        "intent_family": "plan",
        "agent_id": "agent-1",
    }
    defaults.update(overrides)
    return PackFeedback(**defaults)


def _emit_feedback_event(
    event_log: SQLiteEventLog,
    feedback: PackFeedback,
    *,
    pack_id: str | None = PACK_ID,
    occurred_at: datetime | None = None,
) -> None:
    """Write the event the *live* MCP/REST surface writes."""
    if occurred_at is None:
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            entity_type="pack" if pack_id else None,
            payload=feedback.to_event_payload(pack_id=pack_id),
        )
        return
    # ``emit`` stamps the row with "now"; a backdated event has to be
    # appended directly.
    event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            entity_type="pack" if pack_id else None,
            payload=feedback.to_event_payload(pack_id=pack_id),
            occurred_at=occurred_at,
        )
    )


def _comparable(event: OutcomeEvent) -> dict[str, Any]:
    return event.model_dump(exclude=_VOLATILE_FIELDS)


def _keyed(store: SQLiteOutcomeStore) -> dict[str, dict[str, Any]]:
    return {row.component_id: _comparable(row) for row in store.query(limit=10_000)}


class TestPayloadRoundTrip:
    """``from_event_payload`` is the inverse ``to_event_payload`` needs."""

    def test_round_trip_preserves_everything_the_bridge_reads(self) -> None:
        original = _feedback(
            items_served=["a", "b", "c"],
            items_referenced=["a"],
            unhelpful_item_ids=["c"],
            followed_advisory_ids=["adv-1"],
            relevance_scores={"a": 0.9, "c": 0.1},
            metadata={"notes": "pinned"},
            rating=0.75,
        )

        rebuilt = PackFeedback.from_event_payload(
            original.to_event_payload(pack_id=PACK_ID)
        )

        assert rebuilt is not None
        assert rebuilt == original

    def test_ungraded_rating_is_the_one_lossy_field(self) -> None:
        # ``to_event_payload`` always emits ``effective_rating`` so the key
        # is never missing, which makes "ungraded, succeeded" and "graded
        # 1.0" the same payload. Documented, and nothing in the outcome
        # bridge reads ``rating``.
        original = _feedback(rating=None, outcome="success")

        rebuilt = PackFeedback.from_event_payload(original.to_event_payload())

        assert rebuilt is not None
        assert original.rating is None
        assert rebuilt.rating == 1.0
        assert rebuilt.succeeded is original.succeeded

    def test_governed_payload_is_refused(self) -> None:
        # The ``Command(FEEDBACK_RECORD)`` → ``FeedbackRecordHandler``
        # family emits these four keys, never called the outcome bridge,
        # and carries no item attribution to fan out.
        assert (
            PackFeedback.from_event_payload(
                {
                    "target_id": "item-1",
                    "rating": 0.8,
                    "comment": "useful",
                    "success": True,
                    "pack_id": PACK_ID,
                }
            )
            is None
        )

    def test_missing_timestamp_falls_back_to_the_event_row(self) -> None:
        payload = _feedback().to_event_payload()
        del payload["timestamp_utc"]

        rebuilt = PackFeedback.from_event_payload(
            payload, default_timestamp_utc="2026-01-02T03:04:05+00:00"
        )

        assert rebuilt is not None
        assert rebuilt.timestamp_utc == "2026-01-02T03:04:05+00:00"

    def test_hostile_payload_types_do_not_raise(self) -> None:
        rebuilt = PackFeedback.from_event_payload(
            {
                "feedback_id": "fb-1",
                "run_id": 17,
                "items_served": ["a", 5, None],
                "helpful_item_ids": "not-a-list",
                "relevance_scores": {"a": "high", "b": 0.5},
                "rating": True,
                "metadata": {"ok": 1},
                "agent_id": "",
            }
        )

        assert rebuilt is not None
        assert rebuilt.run_id == ""
        assert rebuilt.items_served == ["a"]
        assert rebuilt.items_referenced == []
        assert rebuilt.relevance_scores == {"b": 0.5}
        # ``True`` is an ``int`` in Python; a bool is not a grade.
        assert rebuilt.rating is None
        assert rebuilt.agent_id is None


class TestReplayReproducesTheLiveRow:
    def test_replayed_rows_match_what_record_feedback_wrote(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        replay_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(
            event_log,
            [
                _item("k1", "keyword"),
                _item("s1", "semantic"),
                _item("g1", "graph"),
            ],
        )
        feedback = _feedback(
            items_served=["k1", "s1", "g1"],
            items_referenced=["k1", "g1"],
            relevance_scores={"k1": 0.9},
            metadata={"source": "live"},
        )
        # The live path: one call writes the JSONL row, the event, and the
        # outcome rows.
        record_feedback(
            feedback,
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
            source="mcp",
        )

        # The replay path: the same event, a store that has never seen it.
        report = backfill_outcomes(
            event_log=event_log,
            outcome_store=replay_store,
            apply=True,
        )

        assert report.rows_written == 4
        assert _keyed(replay_store) == _keyed(outcome_store)

    def test_occurred_at_comes_from_the_feedback_not_the_backfill(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # The whole point: a replayed row has to land in the *historical*
        # window the tuner reads, not in the window the backfill ran in.
        graded_at = datetime.now(UTC) - timedelta(days=9)
        _emit_pack(event_log, [_item("k1", "keyword")])
        _emit_feedback_event(
            event_log,
            _feedback(
                items_served=["k1"],
                items_referenced=["k1"],
                timestamp_utc=graded_at.isoformat(),
            ),
            occurred_at=graded_at,
        )

        backfill_outcomes(event_log=event_log, outcome_store=outcome_store, apply=True)

        rows = outcome_store.query(limit=10)
        assert rows
        assert all(
            abs((row.occurred_at - graded_at).total_seconds()) < 1 for row in rows
        )


class TestDryRunIsApplyWithheld:
    def test_dry_run_plans_exactly_what_apply_writes(
        self,
        outcome_store: SQLiteOutcomeStore,
        replay_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(event_log, [_item("k1", "keyword"), _item("s1", "semantic")])
        _emit_feedback_event(
            event_log, _feedback(items_served=["k1", "s1"], items_referenced=["k1"])
        )

        dry = backfill_outcomes(event_log=event_log, outcome_store=outcome_store)
        applied = backfill_outcomes(
            event_log=event_log, outcome_store=replay_store, apply=True
        )

        assert dry.applied is False
        assert dry.rows_written == 0
        assert outcome_store.count() == 0
        assert dry.rows_planned == applied.rows_planned
        assert dry.rows_pending == applied.rows_written == 3
        assert dry.rows_by_component == applied.rows_by_component
        assert dry.cells == applied.cells


class TestIdempotency:
    def test_second_pass_writes_nothing(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(event_log, [_item("k1", "keyword"), _item("g1", "graph")])
        _emit_feedback_event(
            event_log, _feedback(items_served=["k1", "g1"], items_referenced=["g1"])
        )

        first = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )
        second = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert first.rows_written == 3
        assert second.rows_written == 0
        assert second.rows_already_present == second.rows_planned == 3
        assert outcome_store.count() == 3

    def test_a_partial_fan_out_is_completed_not_duplicated(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # Key on ``(feedback_id, component_id)``, not ``feedback_id``: a
        # crash between the pack row and the fan-out must leave the next
        # run exactly the missing rows to write.
        _emit_pack(event_log, [_item("k1", "keyword"), _item("s1", "semantic")])
        feedback = _feedback(items_served=["k1", "s1"], items_referenced=["k1"])
        _emit_feedback_event(event_log, feedback)
        # Simulate the interrupted run: only the pack-level row landed.
        bridge_feedback_to_outcomes(
            feedback, outcome_store=outcome_store, pack_id=PACK_ID
        )
        assert outcome_store.count() == 1

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert report.rows_already_present == 1
        assert report.rows_written == 2
        assert set(report.rows_by_component) == {
            KEYWORD_SEARCH_COMPONENT_ID,
            SEMANTIC_SEARCH_COMPONENT_ID,
        }
        assert outcome_store.count() == 3


class TestWhatIsNotReplayed:
    def test_governed_events_are_counted_not_replayed(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="feedback.record",
            entity_id="item-1",
            payload={
                "target_id": "item-1",
                "rating": 0.9,
                "comment": "",
                "success": True,
                "pack_id": PACK_ID,
            },
        )

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert report.events_scanned == 1
        assert report.events_not_replayable == 1
        assert report.events_replayable == 0
        assert report.rows_planned == 0
        assert outcome_store.count() == 0

    def test_trace_level_feedback_gets_its_pack_row_and_no_fan_out(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # What the live bridge does with feedback naming no pack, so what
        # the replay must do. Roughly a quarter of real feedback.
        _emit_feedback_event(event_log, _feedback(), pack_id=None)

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert report.events_replayable == 1
        assert report.events_pack_targeted == 0
        assert report.rows_written == 1
        assert list(report.rows_by_component) == [PACK_BUILDER_COMPONENT_ID]

    def test_pack_targeted_events_are_counted_apart_from_the_rest(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # The operator reads this number to decide whether a thin backfill
        # is a thin *corpus* or a broken join: replayable says how much
        # history there is, pack-targeted says how much of it can fan out.
        # Both halves have to be present for the pair to mean anything.
        _emit_pack(event_log, [_item("item-1", "semantic")])
        _emit_feedback_event(event_log, _feedback(run_id="graded-a-pack"))
        _emit_feedback_event(
            event_log, _feedback(run_id="graded-a-trace"), pack_id=None
        )

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert report.events_replayable == 2
        assert report.events_pack_targeted == 1

    def test_events_outside_the_window_are_not_read(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=45)
        _emit_feedback_event(
            event_log, _feedback(run_id="old"), pack_id=None, occurred_at=old
        )
        _emit_feedback_event(event_log, _feedback(run_id="recent"), pack_id=None)

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, window_days=30
        )

        assert report.events_scanned == 1
        assert report.events_replayable == 1

    def test_hitting_the_event_limit_is_reported_not_absorbed(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        for index in range(3):
            _emit_feedback_event(
                event_log, _feedback(run_id=f"run-{index}"), pack_id=None
            )

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, event_limit=2
        )

        assert report.events_scanned == 2
        assert report.events_truncated is True


class TestFailureIsCountedNotSwallowed:
    def test_a_bridge_failure_is_reported(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _emit_feedback_event(event_log, _feedback(), pack_id=None)

        def _boom(*_args: object, **_kwargs: object) -> None:
            message = "outcome store unavailable"
            raise RuntimeError(message)

        monkeypatch.setattr("trellis.feedback.recording._emit_outcome", _boom)

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert report.events_replayable == 1
        assert report.events_failed == 1
        assert report.rows_planned == 0


class TestCellReporting:
    def test_cells_match_the_tuner_grouping_over_the_resulting_rows(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(
            event_log,
            [_item("k1", "keyword"), _item("g1", "graph"), _item("g2", "graph")],
        )
        _emit_feedback_event(
            event_log,
            _feedback(items_served=["k1", "g1", "g2"], items_referenced=["k1"]),
        )

        report = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        expected = aggregate_outcomes(outcome_store.query(limit=10_000))
        assert len(report.cells) == len(expected)
        by_component = {cell.component_id: cell for cell in report.cells}
        graph_cell = by_component[GRAPH_SEARCH_COMPONENT_ID]
        # Two graph items served, neither cited — the separating signal the
        # shipped rule reads.
        assert graph_cell.items_served == 2
        assert graph_cell.items_referenced == 0
        assert graph_cell.reference_rate == 0.0

    def test_cells_include_rows_already_in_the_store(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # A dry run has to report the population the tuner *would* see,
        # which is the store's existing rows plus the pending ones — not
        # the pending ones alone.
        _emit_pack(event_log, [_item("k1", "keyword")])
        _emit_feedback_event(
            event_log, _feedback(items_served=["k1"], items_referenced=["k1"])
        )
        backfill_outcomes(event_log=event_log, outcome_store=outcome_store, apply=True)

        report = backfill_outcomes(event_log=event_log, outcome_store=outcome_store)

        assert report.rows_pending == 0
        assert report.existing_rows_in_window == 2
        assert {cell.component_id for cell in report.cells} == {
            PACK_BUILDER_COMPONENT_ID,
            KEYWORD_SEARCH_COMPONENT_ID,
        }


class TestTheReplayClock:
    def test_a_payload_without_a_timestamp_uses_the_event_row(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # ``to_event_payload`` always writes ``timestamp_utc``, but the
        # replay reads history rather than its own output, so the
        # fallback has to be the event's own clock — not "now", which
        # would file every such row in the current window.
        landed = datetime.now(UTC) - timedelta(days=11)
        payload = _feedback().to_event_payload()
        del payload["timestamp_utc"]
        event_log.append(
            Event(
                event_type=EventType.FEEDBACK_RECORDED,
                source="mcp",
                payload=payload,
                occurred_at=landed,
            )
        )

        backfill_outcomes(event_log=event_log, outcome_store=outcome_store, apply=True)

        rows = outcome_store.query(limit=10)
        assert rows
        assert all(abs((row.occurred_at - landed).total_seconds()) < 1 for row in rows)

    def test_a_row_dated_outside_the_window_still_deduplicates(
        self,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # The event landed inside the window; the feedback it carries is
        # dated well before it, so the row it produces lands outside.
        # The skip scan spans the planned rows' own dates for exactly
        # this case — scanning the window alone would write it twice.
        graded_at = datetime.now(UTC) - timedelta(days=200)
        _emit_feedback_event(
            event_log,
            _feedback(timestamp_utc=graded_at.isoformat()),
            pack_id=None,
        )

        first = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )
        second = backfill_outcomes(
            event_log=event_log, outcome_store=outcome_store, apply=True
        )

        assert first.rows_written == 1
        assert second.rows_written == 0
        assert second.rows_already_present == 1
        assert outcome_store.count() == 1


class TestTheCollectingSink:
    """The dry run's sink answers reads the way a real store would.

    Nothing in the bridge reads it back today. These pin the property
    that lets a future one, rather than the current call graph.
    """

    def test_query_filters_and_orders_like_the_store_it_stands_in_for(
        self,
    ) -> None:
        sink = _CollectingOutcomeStore()
        older = datetime.now(UTC) - timedelta(days=2)
        newer = datetime.now(UTC) - timedelta(days=1)
        for run_id, when in (("a", newer), ("b", older)):
            bridge_feedback_to_outcomes(
                _feedback(run_id=run_id, timestamp_utc=when.isoformat()),
                outcome_store=sink,
            )
        component_id = sink.collected[0].component_id

        # Ascending by occurred_at, like every shipped backend.
        assert [row.run_id for row in sink.query(limit=10)] == ["b", "a"]
        assert [row.run_id for row in sink.query(run_id="b")] == ["b"]
        assert [row.run_id for row in sink.query(since=newer)] == ["a"]
        assert [row.run_id for row in sink.query(until=older)] == ["b"]
        assert len(sink.query(limit=1)) == 1
        assert sink.query(component_id="nope") == []

        assert sink.count() == 2
        assert sink.count(component_id=component_id) == 2
        assert sink.count(component_id="nope") == 0
        assert sink.count(since=newer) == 1
        assert sink.count(until=older) == 1

    def test_append_many_reports_what_it_took(self) -> None:
        sink = _CollectingOutcomeStore()
        bridge_feedback_to_outcomes(_feedback(), outcome_store=sink)
        rows = list(sink.collected)

        assert sink.append_many(rows) == len(rows)
        assert len(sink.collected) == 2 * len(rows)
