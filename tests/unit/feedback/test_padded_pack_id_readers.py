"""A feedback row recorded with a padded ``pack_id`` still joins in every reader.

Rows written before the two feedback families agreed on one stored form keep a
padded id. Four readers compared the payload's id raw to
``PACK_ASSEMBLED.entity_id`` and dropped them; ``pack_value`` (and the learning
join) already stripped. Each reader is run over the same padded row and over
its clean twin, and must count both.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    analyze_advisory_effectiveness,
    analyze_effectiveness,
)
from trellis.retrieve.evaluate import analyze_dimension_predictiveness
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

PACK = "pack-synthetic-1"
PADDED = f"  {PACK}\t"


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _emit_pack(log: SQLiteEventLog) -> None:
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=PACK,
        entity_type="pack",
        payload={
            "injected_item_ids": ["item-a", "item-b"],
            "injected_items": [
                {"item_id": "item-a", "estimated_tokens": 100, "rank": 0},
                {"item_id": "item-b", "estimated_tokens": 100, "rank": 1},
            ],
            "advisory_ids": ["adv-1"],
        },
    )


def _emit_padded_feedback(log: SQLiteEventLog) -> None:
    """A row as the pre-fix writer left it: the padded id in both places."""
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=PADDED,
        entity_type="pack",
        payload={
            "feedback_id": "fb-1",
            "pack_id": PADDED,
            "rating": 0.9,
            "success": True,
            "helpful_item_ids": ["item-a"],
            "unhelpful_item_ids": ["item-b"],
        },
    )


def _effectiveness(log: SQLiteEventLog, tmp_path: Path) -> int:
    return analyze_effectiveness(log, days=30).total_feedback


def _advisory_effectiveness(log: SQLiteEventLog, tmp_path: Path) -> int:
    store = AdvisoryStore(tmp_path / "advisories.json")
    return analyze_advisory_effectiveness(log, store, days=30).total_feedback


def _evaluate(log: SQLiteEventLog, tmp_path: Path) -> int:
    log.emit(
        EventType.PACK_QUALITY_SCORED,
        source="test",
        entity_id=PACK,
        entity_type="pack",
        payload={
            "dimensions": {"completeness": 0.8},
            "weighted_score": 0.8,
            "findings_count": 0,
        },
    )
    return analyze_dimension_predictiveness(log, days=30).total_matched_feedback


def _advisory_generator(log: SQLiteEventLog, tmp_path: Path) -> int:
    packs = log.get_events(event_type=EventType.PACK_ASSEMBLED)
    feedback = log.get_events(event_type=EventType.FEEDBACK_RECORDED)
    return len(AdvisoryGenerator._join_packs_feedback(packs, feedback))


def _pack_value(log: SQLiteEventLog, tmp_path: Path) -> int:
    return summarize_pack_value(log, days=30).attributed_packs


#: The four readers the report named, and the two that read the same join.
READERS: list[Any] = [
    pytest.param(_effectiveness, id="analyze_effectiveness"),
    pytest.param(_advisory_effectiveness, id="analyze_advisory_effectiveness"),
    pytest.param(_evaluate, id="analyze_dimension_predictiveness"),
    pytest.param(_advisory_generator, id="advisory_generator"),
    pytest.param(_pack_value, id="pack_value"),
]


@pytest.mark.parametrize("reader", READERS)
def test_a_padded_row_joins_in_every_reader(
    reader: Any, event_log: SQLiteEventLog, tmp_path: Path
) -> None:
    _emit_pack(event_log)
    _emit_padded_feedback(event_log)

    assert reader(event_log, tmp_path) == 1


@pytest.mark.parametrize("reader", READERS)
def test_a_clean_row_still_joins_the_same_way(
    reader: Any, event_log: SQLiteEventLog, tmp_path: Path
) -> None:
    """The control: the padded case and the clean case read identically."""
    _emit_pack(event_log)
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=PACK,
        entity_type="pack",
        payload={
            "feedback_id": "fb-1",
            "pack_id": PACK,
            "rating": 0.9,
            "success": True,
            "helpful_item_ids": ["item-a"],
            "unhelpful_item_ids": ["item-b"],
        },
    )

    assert reader(event_log, tmp_path) == 1


def test_a_blank_payload_id_falls_back_to_the_entity_id(
    event_log: SQLiteEventLog, tmp_path: Path
) -> None:
    """A whitespace-only payload id is absent, so the entity id is the key."""
    _emit_pack(event_log)
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=PACK,
        entity_type="pack",
        payload={"pack_id": "   ", "rating": 0.9, "success": True},
    )

    assert _effectiveness(event_log, tmp_path) == 1


def test_event_pack_id_helper_shapes() -> None:
    from trellis.feedback.attribution import event_pack_id

    def event(entity_id: str | None, payload: dict[str, Any]) -> Event:
        return Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=entity_id,
            payload=payload,
        )

    assert event_pack_id(event(None, {"pack_id": PADDED})) == PACK
    assert event_pack_id(event(" t-1 ", {})) == "t-1"
    assert event_pack_id(event("t-1", {"pack_id": ""})) == "t-1"
    assert event_pack_id(event(None, {})) == ""
    assert event_pack_id(event("   ", {"pack_id": 7})) == ""
