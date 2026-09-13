"""Per-strategy fan-out of a pack grade into ``OutcomeEvent`` rows (#557 D2).

The bridge stamped ``retrieve.pack_builder.PackBuilder`` while every shipped
:data:`~trellis.learning.tuners.rule_tuner.DEFAULT_RULES` entry targets a
*strategy*, so the two halves of the learning loop addressed different
components and could never meet. These tests pin the join back together —
and pin the three places it must refuse to guess, because a fabricated
denominator is what makes a tuner change a parameter for no reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.feedback import PackFeedback, record_feedback
from trellis.schemas.outcome import (
    GRAPH_SEARCH_COMPONENT_ID,
    KEYWORD_SEARCH_COMPONENT_ID,
    PACK_BUILDER_COMPONENT_ID,
    SEMANTIC_SEARCH_COMPONENT_ID,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore

PACK_ID = "pack-fanout"


@pytest.fixture
def outcome_store(tmp_path: Path):
    store = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    yield store
    store.close()


@pytest.fixture
def event_log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


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


def _item(item_id: str, strategy: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"item_id": item_id, "item_type": "document"}
    if strategy is not None:
        row["strategy_source"] = strategy
    return row


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


def _by_component(store: SQLiteOutcomeStore) -> dict[str, Any]:
    return {ev.component_id: ev for ev in store.query()}


class TestFanOut:
    def test_one_row_per_contributing_strategy(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(
            event_log,
            [
                _item("k1", "keyword"),
                _item("k2", "keyword"),
                _item("s1", "semantic"),
                _item("g1", "graph"),
            ],
        )
        result = record_feedback(
            # Cited one keyword item and the graph item; the semantic item
            # was served and went uncited.
            _feedback(items_referenced=["k1", "g1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        assert result.strategy_outcomes_emitted == 3
        rows = _by_component(outcome_store)
        # The pack-level row is still emitted — the fan-out adds, never
        # replaces. Losing it would break every PackBuilder-scoped consumer.
        assert PACK_BUILDER_COMPONENT_ID in rows
        assert set(rows) == {
            PACK_BUILDER_COMPONENT_ID,
            KEYWORD_SEARCH_COMPONENT_ID,
            SEMANTIC_SEARCH_COMPONENT_ID,
            GRAPH_SEARCH_COMPONENT_ID,
        }

    def test_denominator_comes_from_the_pack_numerator_from_the_agent(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """The measurement the agent cannot make on its own.

        ``from_agent_signal`` leaves ``items_served`` empty by design, so
        the pack-level row reports ``items_served=None``. Each strategy row
        reports the count the *pack* recorded for that strategy, and counts
        only the citations that fall inside it.
        """
        _emit_pack(
            event_log,
            [
                _item("k1", "keyword"),
                _item("k2", "keyword"),
                _item("k3", "keyword"),
                _item("s1", "semantic"),
            ],
        )
        record_feedback(
            _feedback(items_referenced=["k1", "s1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        rows = _by_component(outcome_store)
        assert rows[PACK_BUILDER_COMPONENT_ID].outcome.items_served is None

        keyword = rows[KEYWORD_SEARCH_COMPONENT_ID].outcome
        assert keyword.items_served == 3
        assert keyword.items_referenced == 1

        semantic = rows[SEMANTIC_SEARCH_COMPONENT_ID].outcome
        assert semantic.items_served == 1
        assert semantic.items_referenced == 1

    def test_citation_is_not_credited_to_a_strategy_that_did_not_serve_it(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(event_log, [_item("k1", "keyword"), _item("s1", "semantic")])
        record_feedback(
            _feedback(items_referenced=["s1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        rows = _by_component(outcome_store)
        assert rows[KEYWORD_SEARCH_COMPONENT_ID].outcome.items_referenced == 0
        assert rows[SEMANTIC_SEARCH_COMPONENT_ID].outcome.items_referenced == 1

    def test_rows_carry_the_pack_context_and_are_marked_as_derived(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(event_log, [_item("k1", "keyword")])
        record_feedback(
            _feedback(items_referenced=["k1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        row = _by_component(outcome_store)[KEYWORD_SEARCH_COMPONENT_ID]
        assert row.pack_id == PACK_ID
        assert row.intent_family == "plan"
        assert row.phase == "retrieve"
        assert row.run_id == "run-1"
        assert row.agent_id == "agent-1"
        assert row.metadata["strategy_source"] == "keyword"
        # A consumer must be able to tell a derived row from one a strategy
        # measured for itself, should such a producer ever exist.
        assert row.metadata["fanned_out_from"] == PACK_BUILDER_COMPONENT_ID

    def test_success_is_the_packs_bit_repeated(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Pinned as documentation, not as a claim about the strategies.

        ``ComponentOutcome.success`` is a required bool and the only value
        in hand is the grade the agent gave the whole pack, so every
        contributor is credited with it. Measured on the reference
        deployment this makes per-strategy success rates near-identical
        (0.189 / 0.191 / 0.192); ``reference_rate`` is the separating
        signal on these rows, and a rule keyed on ``success_rate`` here is
        reading a property of the packs a strategy appeared in.
        """
        _emit_pack(event_log, [_item("k1", "keyword"), _item("s1", "semantic")])
        record_feedback(
            _feedback(outcome="failure", items_referenced=["k1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        rows = _by_component(outcome_store)
        assert rows[KEYWORD_SEARCH_COMPONENT_ID].outcome.success is False
        # Cited nothing, same success bit — by construction, not by measurement.
        assert rows[SEMANTIC_SEARCH_COMPONENT_ID].outcome.success is False


class TestRefusesToGuess:
    def test_unmappable_strategy_source_is_dropped_not_reassigned(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """An unattributable serving must not inflate another denominator."""
        _emit_pack(
            event_log,
            [
                _item("k1", "keyword"),
                _item("x1", "some_future_strategy"),
                _item("x2", "some_future_strategy"),
            ],
        )
        result = record_feedback(
            _feedback(items_referenced=["k1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        assert result.strategy_outcomes_emitted == 1
        rows = _by_component(outcome_store)
        assert set(rows) == {PACK_BUILDER_COMPONENT_ID, KEYWORD_SEARCH_COMPONENT_ID}
        # Three items were served; keyword served one of them.
        assert rows[KEYWORD_SEARCH_COMPONENT_ID].outcome.items_served == 1

    def test_item_with_no_strategy_source_is_dropped(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(event_log, [_item("k1", "keyword"), _item("u1", None)])
        record_feedback(
            _feedback(items_referenced=["k1", "u1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        keyword = _by_component(outcome_store)[KEYWORD_SEARCH_COMPONENT_ID].outcome
        assert keyword.items_served == 1
        assert keyword.items_referenced == 1

    def test_no_event_log_means_no_fan_out_but_still_a_pack_row(
        self, tmp_path: Path, outcome_store: SQLiteOutcomeStore
    ) -> None:
        result = record_feedback(
            _feedback(items_referenced=["k1"]),
            log_dir=tmp_path,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        assert result.strategy_outcomes_emitted == 0
        assert result.outcome_emitted is True
        assert set(_by_component(outcome_store)) == {PACK_BUILDER_COMPONENT_ID}

    def test_unknown_pack_yields_no_fan_out(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _emit_pack(event_log, [_item("k1", "keyword")], pack_id="some-other-pack")
        result = record_feedback(
            _feedback(items_referenced=["k1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        assert result.strategy_outcomes_emitted == 0
        assert set(_by_component(outcome_store)) == {PACK_BUILDER_COMPONENT_ID}

    def test_sectioned_pack_emits_no_injected_items_and_no_fan_out(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """A sectioned build writes no ``injected_items`` (CLAUDE.md).

        The right answer is *no rows*, never a row claiming a measured zero
        — the #557 failure this whole change exists to remove.
        """
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=PACK_ID,
            entity_type="pack",
            payload={"sections": [{"name": "objective"}]},
        )
        result = record_feedback(
            _feedback(items_referenced=["k1"]),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            pack_id=PACK_ID,
        )

        assert result.strategy_outcomes_emitted == 0
        assert set(_by_component(outcome_store)) == {PACK_BUILDER_COMPONENT_ID}

    def test_trace_level_feedback_has_no_pack_and_no_fan_out(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        result = record_feedback(
            _feedback(),
            log_dir=tmp_path,
            event_log=event_log,
            outcome_store=outcome_store,
            entity_id="trace-9",
            entity_type="trace",
        )

        assert result.strategy_outcomes_emitted == 0
        assert set(_by_component(outcome_store)) == {PACK_BUILDER_COMPONENT_ID}
