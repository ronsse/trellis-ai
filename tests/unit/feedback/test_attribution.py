"""Tests for :mod:`trellis.feedback.attribution`.

The module answers two membership questions — *which ids did this pack
serve?* and *which of those did it serve with a body?* — for a surface
that is about to reject or guide a caller. Every wrong answer here costs
something asymmetric: a false positive rejects a caller who had nothing
to cite, a false negative silently lets an uncited call through. So the
tests pin the fail-open direction explicitly, not just the happy path.

The bodied half is read out of a *writer's* payload key, which is the
shape this repo keeps getting wrong (#325/#326: a reader keyed on a
field its writer had moved, silently reporting a constant). A payload
fixture cannot catch that — it keeps passing after the move — so the
bodied tests are anchored by a round trip through a real
:class:`~trellis.retrieve.pack_builder.PackBuilder` emit, and the
fixture-driven cases exist only to exercise shapes a builder will not
produce on demand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.feedback.attribution import (
    lookup_pack_bodied_item_ids,
    lookup_pack_item_ids,
    payload_is_attributed,
    payload_pack_id,
)
from trellis.retrieve.disclosure import DisclosureConfig
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackBudget, PackItem
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Module-level so the raise site stays a bare ``raise <var>``.
_LOG_DOWN = RuntimeError("event log down")


@pytest.fixture
def event_log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _emit_pack(
    event_log: SQLiteEventLog, pack_id: str, payload: dict[str, Any]
) -> None:
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _assembled_payload(event_log: SQLiteEventLog) -> dict[str, Any]:
    events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
    assert events, "expected a PACK_ASSEMBLED event"
    return events[0].payload


class TestLookupPackItemIds:
    def test_returns_served_ids_in_order(self, event_log: SQLiteEventLog) -> None:
        _emit_pack(event_log, "pack_1", {"injected_item_ids": ["c", "a", "b"]})

        assert lookup_pack_item_ids(event_log, "pack_1") == ["c", "a", "b"]

    def test_deduplicates_and_drops_falsy_entries(
        self, event_log: SQLiteEventLog
    ) -> None:
        _emit_pack(event_log, "pack_1", {"injected_item_ids": ["a", "", "a", "b", 7]})

        assert lookup_pack_item_ids(event_log, "pack_1") == ["a", "b"]

    def test_whitespace_in_pack_id_is_tolerated(
        self, event_log: SQLiteEventLog
    ) -> None:
        _emit_pack(event_log, "pack_1", {"injected_item_ids": ["a"]})

        assert lookup_pack_item_ids(event_log, "  pack_1  ") == ["a"]

    def test_unknown_pack_is_empty(self, event_log: SQLiteEventLog) -> None:
        assert lookup_pack_item_ids(event_log, "nope") == []

    def test_blank_pack_id_short_circuits(self, event_log: SQLiteEventLog) -> None:
        assert lookup_pack_item_ids(event_log, "") == []
        assert lookup_pack_item_ids(event_log, "   ") == []

    def test_pack_without_injected_item_ids_is_empty(
        self, event_log: SQLiteEventLog
    ) -> None:
        """A sectioned pack emits no per-item rows — nothing to offer."""
        _emit_pack(event_log, "pack_sectioned", {"section_count": 2})

        assert lookup_pack_item_ids(event_log, "pack_sectioned") == []

    def test_non_list_payload_is_empty(self, event_log: SQLiteEventLog) -> None:
        _emit_pack(event_log, "pack_odd", {"injected_item_ids": "a,b"})

        assert lookup_pack_item_ids(event_log, "pack_odd") == []

    def test_store_outage_fails_open(self) -> None:
        """A store outage must not turn a recordable signal into a failure."""

        class Broken:
            def get_events(self, **_: Any) -> list[Any]:
                raise _LOG_DOWN

        assert lookup_pack_item_ids(Broken(), "pack_1") == []  # type: ignore[arg-type]


#: Long enough that an excerpt is a real body rather than a stub.
_BODY = "the northwind loader retries on a stale manifest " * 8


class _FixedStrategy(SearchStrategy):
    """Returns a fixed candidate list, so the pack shape is the variable."""

    def __init__(self, items: list[PackItem]) -> None:
        self._items = items

    @property
    def name(self) -> str:
        return "keyword"

    def search(
        self,
        intent: str,
        domain: str | None = None,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[PackItem]:
        return [item.model_copy() for item in self._items[:limit]]


def _candidates(count: int) -> list[PackItem]:
    return [
        PackItem(
            item_id=f"doc:{index:03d}",
            item_type="document",
            excerpt=_BODY,
            relevance_score=1.0 - index * 0.01,
            estimated_tokens=60,
        )
        for index in range(count)
    ]


class TestLookupPackBodiedItemIds:
    """The served list minus #359's pointers, read off the real writer."""

    def test_round_trip_through_a_real_pack_build(
        self, event_log: SQLiteEventLog
    ) -> None:
        """The anchor test: a builder emit, not a hand-written payload.

        A fixture keeps passing after the writer moves the key — which is
        exactly how #325/#326 shipped. Both sides of the assertion are
        derived from the emitted event, so the only thing pinned is that
        this reader agrees with whatever ``PackBuilder`` actually wrote.
        """
        builder = PackBuilder(
            strategies=[_FixedStrategy(_candidates(5))],
            event_log=event_log,
            disclosure=DisclosureConfig(body_items=2),
        )
        pack = builder.build(intent="loader retries", budget=PackBudget(max_items=10))

        payload = _assembled_payload(event_log)
        served = payload["injected_item_ids"]
        pointers = payload["disclosure"]["pointer_item_ids"]
        assert pointers, "fixture must actually demote something to be meaningful"

        bodied = lookup_pack_bodied_item_ids(event_log, pack.pack_id)

        assert bodied == [item_id for item_id in served if item_id not in pointers]
        assert len(bodied) == 2

    def test_index_mode_pack_has_no_bodied_half(
        self, event_log: SQLiteEventLog
    ) -> None:
        """The one shape where subtracting pointers is exactly backwards.

        ``pack_builder`` passes ``DISCLOSURE_OFF`` for an index build, so
        the telemetry reads ``mode="off"`` with an empty pointer list —
        while *every* item is a pointer. Subtraction alone would report
        the whole pack as bodied and ask the caller to judge five
        excerpts it never saw.
        """
        builder = PackBuilder(
            strategies=[_FixedStrategy(_candidates(5))],
            event_log=event_log,
            disclosure=DisclosureConfig(body_items=2),
        )
        pack = builder.build(
            intent="loader retries",
            budget=PackBudget(max_items=10),
            index_mode=True,
        )

        payload = _assembled_payload(event_log)
        assert payload["injected_item_ids"], "the pack did serve items"
        assert payload["disclosure"]["pointer_item_ids"] == []

        assert lookup_pack_bodied_item_ids(event_log, pack.pack_id) == []

    def test_pack_with_no_disclosure_record_is_all_bodied(
        self, event_log: SQLiteEventLog
    ) -> None:
        """Absence is not uncertainty — it predates #359, so all bodied."""
        _emit_pack(event_log, "pack_old", {"injected_item_ids": ["a", "b"]})

        assert lookup_pack_bodied_item_ids(event_log, "pack_old") == ["a", "b"]

    def test_empty_disclosure_record_is_all_bodied(
        self, event_log: SQLiteEventLog
    ) -> None:
        _emit_pack(
            event_log, "pack_1", {"injected_item_ids": ["a", "b"], "disclosure": {}}
        )

        assert lookup_pack_bodied_item_ids(event_log, "pack_1") == ["a", "b"]

    def test_pointer_ids_the_pack_never_served_are_ignored(
        self, event_log: SQLiteEventLog
    ) -> None:
        """Served order is authoritative; the pointer list only subtracts."""
        _emit_pack(
            event_log,
            "pack_1",
            {
                "injected_item_ids": ["a", "b", "c"],
                "disclosure": {"pointer_item_ids": ["c", "zz"]},
            },
        )

        assert lookup_pack_bodied_item_ids(event_log, "pack_1") == ["a", "b"]

    @pytest.mark.parametrize(
        "disclosure",
        [
            "off",
            ["c"],
            7,
            {"pointer_item_ids": "c"},
            {"pointer_item_ids": {"c": True}},
            {"mode": "on"},
        ],
    )
    def test_unrecognised_disclosure_shape_narrows_to_nothing(
        self, event_log: SQLiteEventLog, disclosure: Any
    ) -> None:
        """Uncertainty narrows the bodied set, never widens it.

        Refusing a caller for not judging an item they were never shown
        is the expensive mistake, so a shape this reader does not
        understand yields no bodied ids at all rather than the full
        served list.
        """
        _emit_pack(
            event_log,
            "pack_1",
            {"injected_item_ids": ["a", "b"], "disclosure": disclosure},
        )

        assert lookup_pack_bodied_item_ids(event_log, "pack_1") == []

    def test_sectioned_pack_is_empty(self, event_log: SQLiteEventLog) -> None:
        """No per-item rows at all — nothing to ask a verdict on."""
        _emit_pack(event_log, "pack_sectioned", {"section_count": 2})

        assert lookup_pack_bodied_item_ids(event_log, "pack_sectioned") == []

    def test_unknown_pack_and_blank_id_are_empty(
        self, event_log: SQLiteEventLog
    ) -> None:
        assert lookup_pack_bodied_item_ids(event_log, "nope") == []
        assert lookup_pack_bodied_item_ids(event_log, "   ") == []

    def test_store_outage_fails_open(self) -> None:
        class Broken:
            def get_events(self, **_: Any) -> list[Any]:
                raise _LOG_DOWN

        assert lookup_pack_bodied_item_ids(Broken(), "pack_1") == []  # type: ignore[arg-type]

    def test_bodied_is_a_subset_of_served(self, event_log: SQLiteEventLog) -> None:
        """The invariant the MCP gate rests on, stated directly.

        The bodied gate hands unjudged ids back to a caller as ids it can
        cite; an id outside the served set would be uncitable and the
        retry could never succeed.
        """
        builder = PackBuilder(
            strategies=[_FixedStrategy(_candidates(6))],
            event_log=event_log,
            disclosure=DisclosureConfig(body_items=3),
        )
        pack = builder.build(intent="loader retries", budget=PackBudget(max_items=10))

        served = lookup_pack_item_ids(event_log, pack.pack_id)
        bodied = lookup_pack_bodied_item_ids(event_log, pack.pack_id)

        assert set(bodied) <= set(served)
        assert bodied == [item_id for item_id in served if item_id in set(bodied)]


class TestPayloadPredicates:
    @pytest.mark.parametrize(
        "payload",
        [
            {"helpful_item_ids": ["a"]},
            {"unhelpful_item_ids": ["a"]},
            {"followed_advisory_ids": ["adv"]},
        ],
    )
    def test_attributed_shapes(self, payload: dict[str, Any]) -> None:
        assert payload_is_attributed(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"rating": 1.0},
            {"helpful_item_ids": []},
            {"helpful_item_ids": None},
            {"helpful_item_ids": "a"},
        ],
    )
    def test_unattributed_shapes(self, payload: dict[str, Any]) -> None:
        assert payload_is_attributed(payload) is False

    def test_pack_id_is_read_from_the_join_key(self) -> None:
        assert payload_pack_id({"pack_id": " pack_1 "}) == "pack_1"

    def test_pack_id_nested_in_metadata_does_not_count(self) -> None:
        """``join_pack_feedback`` reads the top-level key and only that.

        A ``pack_id`` buried in ``metadata`` is invisible to the join, so
        counting it as pack-targeted would report a joinable event that
        the loop never sees.
        """
        assert payload_pack_id({"metadata": {"pack_id": "pack_1"}}) == ""

    @pytest.mark.parametrize("payload", [{}, {"pack_id": ""}, {"pack_id": None}])
    def test_missing_pack_id(self, payload: dict[str, Any]) -> None:
        assert payload_pack_id(payload) == ""
