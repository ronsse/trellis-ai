"""Learning-scope axes on the outcome bridge (#560).

``ParameterScope`` has four axes and ``ParameterStore.resolve`` documents a
five-level backoff chain over them, but every ``OutcomeEvent`` the feedback
bridge wrote carried ``domain=None`` and ``intent_family=None``: the first was
never passed at all, and the second was read off a ``PackFeedback`` that no
agent-facing surface can put a value on. So every outcome for a component
collapsed into one global cell and levels 1-4 of that chain were dead.

The axes were never missing — the *pack* recorded both. These tests pin the
pack fallback, the precedence that decides it, and the property the issue is
actually about: two packs in two domains produce two cells.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.feedback import PackFeedback, record_feedback
from trellis.feedback.attribution import EMPTY_PACK_SCOPE, lookup_pack_scope
from trellis.learning.tuners.rule_tuner import aggregate_outcomes
from trellis.schemas.outcome import (
    KEYWORD_SEARCH_COMPONENT_ID,
    PACK_BUILDER_COMPONENT_ID,
    SEMANTIC_SEARCH_COMPONENT_ID,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore

PACK_ID = "pack-scope"


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
    *,
    pack_id: str = PACK_ID,
    payload_extra: dict[str, Any] | None = None,
    strategies: tuple[str, ...] = ("keyword", "semantic"),
) -> None:
    """A ``PACK_ASSEMBLED`` event carrying one item per named strategy."""
    items = [
        {
            "item_id": f"item-{strategy}",
            "item_type": "document",
            "strategy_source": strategy,
        }
        for strategy in strategies
    ]
    payload: dict[str, Any] = {
        "injected_item_ids": [row["item_id"] for row in items],
        "injected_items": items,
    }
    payload.update(payload_extra or {})
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _record(
    feedback: PackFeedback,
    *,
    tmp_path: Path,
    outcome_store: SQLiteOutcomeStore,
    event_log: SQLiteEventLog | None,
    pack_id: str | None = PACK_ID,
) -> None:
    record_feedback(
        feedback,
        log_dir=tmp_path / "log",
        event_log=event_log,
        outcome_store=outcome_store,
        pack_id=pack_id,
    )


def _agent_feedback(
    *, pack_id: str = PACK_ID, cited: tuple[str, ...] = ()
) -> PackFeedback:
    """Exactly what an agent-facing surface builds.

    Both surfaces (MCP ``record_feedback``, REST ``POST
    /packs/{id}/feedback``) construct feedback through this one classmethod,
    which takes neither axis — so this is the shape #560 is about, and
    building it any other way would test a caller that does not exist.
    """
    return PackFeedback.from_agent_signal(
        run_id="run-1",
        success=True,
        helpful_item_ids=cited,
        pack_id=pack_id,
    )


def _axes(store: SQLiteOutcomeStore) -> dict[str, tuple[str | None, str | None]]:
    return {ev.component_id: (ev.domain, ev.intent_family) for ev in store.query()}


class TestThePackSuppliesTheAxes:
    def test_agent_feedback_reaches_a_real_cell(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """The regression test for #560, through the real construction path.

        Nothing exercised ``from_agent_signal`` all the way to an
        ``OutcomeEvent`` before, which is how both axes stayed ``None`` on
        every row production ever wrote.
        """
        _emit_pack(
            event_log,
            payload_extra={"domain": "platform", "intent_family": "pipeline_planning"},
        )

        _record(
            _agent_feedback(),
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=event_log,
        )

        axes = _axes(outcome_store)
        assert axes[PACK_BUILDER_COMPONENT_ID] == ("platform", "pipeline_planning")

    def test_every_row_lands_in_the_same_cell(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """A strategy rate and the pack rate it came from must be comparable.

        The scope is resolved once and handed to both bridges; resolving it
        twice would let the two halves of one grade drift into cells a tuner
        aggregates apart.
        """
        _emit_pack(
            event_log,
            payload_extra={"domain": "fincore", "intent_family": "eda_investigation"},
        )

        _record(
            _agent_feedback(),
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=event_log,
        )

        axes = _axes(outcome_store)
        assert set(axes) == {
            PACK_BUILDER_COMPONENT_ID,
            KEYWORD_SEARCH_COMPONENT_ID,
            SEMANTIC_SEARCH_COMPONENT_ID,
        }
        assert set(axes.values()) == {("fincore", "eda_investigation")}

    def test_two_domains_make_two_cells(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """The property the whole issue is about, read through the tuner.

        ``domain`` is the only axis ``SearchStrategy._resolve_param`` narrows
        on, so a component that cannot produce more than one cell cannot be
        tuned per domain at all.
        """
        _emit_pack(
            event_log,
            pack_id="pack-a",
            payload_extra={"domain": "platform"},
            strategies=("keyword",),
        )
        _emit_pack(
            event_log,
            pack_id="pack-b",
            payload_extra={"domain": "fincore"},
            strategies=("keyword",),
        )
        for pack_id in ("pack-a", "pack-b"):
            _record(
                _agent_feedback(pack_id=pack_id),
                tmp_path=tmp_path,
                outcome_store=outcome_store,
                event_log=event_log,
                pack_id=pack_id,
            )

        keyword_cells = {
            agg.scope.domain
            for agg in aggregate_outcomes(list(outcome_store.query()))
            if agg.scope.component_id == KEYWORD_SEARCH_COMPONENT_ID
        }
        assert keyword_cells == {"platform", "fincore"}


class TestPrecedence:
    def test_feedback_intent_family_wins_over_the_pack(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Feedback first, pack as fallback — ``pack_observations``' ordering.

        No agent-facing surface can set it today, but a caller constructing
        ``PackFeedback`` directly can, and the two joins over these same two
        events must not disagree about which cell a pack belongs to.
        """
        _emit_pack(
            event_log,
            payload_extra={"domain": "platform", "intent_family": "general_context"},
        )
        feedback = PackFeedback(
            run_id="run-1",
            phase="retrieve",
            intent="fetch context",
            outcome="success",
            items_served=[],
            items_referenced=[],
            intent_family="source_analysis",
        )

        _record(
            feedback,
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=event_log,
        )

        assert _axes(outcome_store)[PACK_BUILDER_COMPONENT_ID] == (
            "platform",
            "source_analysis",
        )

    def test_domain_has_no_feedback_side_to_prefer(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """``PackFeedback`` has no ``domain`` field, so the pack is the only source.

        Pinned because it is the reason this axis needs no precedence rule —
        if a ``domain`` field is ever added, this test fails and the rule has
        to be chosen deliberately rather than inherited.
        """
        assert not hasattr(
            PackFeedback(
                run_id="r",
                phase="retrieve",
                intent="i",
                outcome="success",
                items_served=[],
                items_referenced=[],
            ),
            "domain",
        )

        _emit_pack(event_log, payload_extra={"domain": "retrieval"})
        _record(
            _agent_feedback(),
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=event_log,
        )

        assert _axes(outcome_store)[PACK_BUILDER_COMPONENT_ID][0] == "retrieval"


class TestRefusesToGuess:
    def test_unknown_pack_leaves_both_axes_unset(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """No pack, no axes — never a placeholder cell.

        A guessed family would key rows into a cell no ``PackBuilder`` emits,
        which is worse than the unscoped cell: the unscoped one is the
        documented fallback level of the backoff chain.
        """
        _record(
            _agent_feedback(pack_id="pack-that-never-existed"),
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=event_log,
            pack_id="pack-that-never-existed",
        )

        assert _axes(outcome_store)[PACK_BUILDER_COMPONENT_ID] == (None, None)

    def test_no_event_log_degrades_to_the_old_behaviour(
        self, tmp_path: Path, outcome_store: SQLiteOutcomeStore
    ) -> None:
        """Nothing to look the pack up in still emits the pack-level row."""
        _record(
            _agent_feedback(),
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=None,
        )

        assert _axes(outcome_store) == {PACK_BUILDER_COMPONENT_ID: (None, None)}

    def test_trace_level_feedback_has_no_pack_to_read(
        self,
        tmp_path: Path,
        outcome_store: SQLiteOutcomeStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """Feedback on a trace grades work no pack informed."""
        _record(
            PackFeedback.from_agent_signal(run_id="trace-1", success=True),
            tmp_path=tmp_path,
            outcome_store=outcome_store,
            event_log=event_log,
            pack_id=None,
        )

        assert _axes(outcome_store) == {PACK_BUILDER_COMPONENT_ID: (None, None)}

    @pytest.mark.parametrize("blank", ["", "   ", None, 42, ["platform"]])
    def test_a_blank_axis_is_unscoped_not_an_empty_cell(
        self, event_log: SQLiteEventLog, blank: Any
    ) -> None:
        """``""`` is a *distinct* cell from "unscoped", and nothing emits it.

        A list is rejected for the same reason and not flattened: ``domain``
        is single-valued per call by contract, and a multi-domain pack backs
        off to a wider cell rather than inventing a compound key.
        """
        _emit_pack(event_log, payload_extra={"domain": blank, "intent_family": blank})

        assert lookup_pack_scope(event_log, PACK_ID) == EMPTY_PACK_SCOPE

    def test_a_pack_predating_the_keys_reads_as_unscoped(
        self, event_log: SQLiteEventLog
    ) -> None:
        """Neither key present is the same answer as a blank one."""
        _emit_pack(event_log)

        assert lookup_pack_scope(event_log, PACK_ID) == EMPTY_PACK_SCOPE
