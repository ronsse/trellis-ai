"""Tests for :mod:`trellis.feedback.attribution`.

The module answers one question — *which ids did this pack serve?* — for
a surface that is about to reject or guide a caller. Every wrong answer
here costs something asymmetric: a false positive rejects a caller who
had nothing to cite, a false negative silently lets an uncited call
through. So the tests pin the fail-open direction explicitly, not just
the happy path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.feedback.attribution import (
    lookup_pack_item_ids,
    payload_is_attributed,
    payload_pack_id,
)
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
