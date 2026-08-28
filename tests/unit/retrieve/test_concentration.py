"""Tests for repeat-source measurement (``trellis.retrieve.concentration``).

The properties worth pinning are the ones that make this a *measurement*
and keep it honest:

* it is read-only — no item is dropped, reordered or rewritten, because
  the production numbers refused the rollup this measures (see the module
  docstring);
* counting is per ``(pack, item)`` **serving**, never per distinct id —
  counting per distinct id is what understated a real cost as ``0/25``
  the last time this repo measured a serving policy;
* an extra serving already demoted to a pointer by #359 is not counted as
  a body a rollup could reclaim, or the same saving is claimed twice;
* the parent is resolved from ``metadata["parent_doc_id"]`` when present
  and from the chunk id scheme when it is not, because the semantic axis
  serves a metadata snapshot taken at embed time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.retrieve.concentration import (
    ParentConcentration,
    measure_parent_concentration,
    resolve_parent_id,
)
from trellis.retrieve.disclosure import DisclosureConfig
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackBudget, PackItem
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


_PARENT = "conversation:claude-ai:b183ecc5"

#: Long enough that the budget walk charges a real per-item cost.
_BODY = "the northwind loader retries on a stale manifest " * 8


def _chunk(index: int, *, parent: str = _PARENT, tokens: int = 60) -> PackItem:
    """A chunk serving that carries its parent link in metadata."""
    return PackItem(
        item_id=f"{parent}#chunk-{index}",
        item_type="document",
        excerpt=_BODY,
        relevance_score=1.0 - index * 0.001,
        estimated_tokens=tokens,
        metadata={"parent_doc_id": parent, "chunk_index": index},
    )


def _standalone(index: int, *, tokens: int = 60) -> PackItem:
    return PackItem(
        item_id=f"doc:{index:03d}",
        item_type="document",
        excerpt=_BODY,
        relevance_score=0.5,
        estimated_tokens=tokens,
        metadata={},
    )


class _FixedStrategy(SearchStrategy):
    """Returns a fixed candidate list, so the pack shape is the variable."""

    def __init__(self, items: list[PackItem], name: str = "keyword") -> None:
        self._items = items
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def search(
        self,
        intent: str,
        domain: str | None = None,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[PackItem]:
        return [item.model_copy() for item in self._items[:limit]]


# ---------------------------------------------------------------------------
# resolve_parent_id
# ---------------------------------------------------------------------------


def test_parent_resolved_from_metadata() -> None:
    assert resolve_parent_id(_chunk(3)) == _PARENT


def test_parent_falls_back_to_chunk_id_scheme_without_metadata() -> None:
    """The semantic axis serves an embed-time snapshot; the id is the backstop."""
    bare = PackItem(item_id=f"{_PARENT}#chunk-9", item_type="document")
    assert resolve_parent_id(bare) == _PARENT


def test_non_chunk_item_is_its_own_parent() -> None:
    """Otherwise a pack of unrelated items reports one giant group."""
    assert resolve_parent_id(_standalone(1)) == "doc:001"


def test_parent_row_and_its_chunk_resolve_together() -> None:
    """The parent row keeps full content, so it is a serving of itself."""
    parent_row = PackItem(item_id=_PARENT, item_type="document")
    assert resolve_parent_id(parent_row) == resolve_parent_id(_chunk(4))


# ---------------------------------------------------------------------------
# measure_parent_concentration
# ---------------------------------------------------------------------------


def test_empty_pack_measures_zero() -> None:
    assert measure_parent_concentration([]) == ParentConcentration()


def test_pack_without_repeats_reports_no_groups() -> None:
    result = measure_parent_concentration([_standalone(i) for i in range(4)])
    assert result.groups == 0
    assert result.extra_servings == 0
    assert result.extra_tokens == 0
    # Measured, and the pack was clean — distinguishable from "never ran".
    assert result.max_group_size == 1


def test_two_chunks_of_one_parent_form_one_group() -> None:
    result = measure_parent_concentration([_chunk(0), _chunk(1), _standalone(9)])
    assert result.groups == 1
    assert result.extra_servings == 1
    assert result.extra_tokens == 60
    assert result.max_group_size == 2


def test_extras_counted_per_serving_not_per_distinct_parent() -> None:
    """Five servings of one parent are four extras, not one."""
    result = measure_parent_concentration([_chunk(i) for i in range(5)])
    assert result.groups == 1
    assert result.extra_servings == 4
    assert result.extra_tokens == 240
    assert result.max_group_size == 5


def test_two_parents_each_repeating_are_two_groups() -> None:
    other = "conversation:claude-ai:2360a3e8"
    items = [
        _chunk(0),
        _chunk(1),
        _chunk(0, parent=other),
        _chunk(1, parent=other),
        _standalone(7),
    ]
    result = measure_parent_concentration(items)
    assert result.groups == 2
    assert result.extra_servings == 2


def test_parent_row_served_beside_its_chunk_is_one_group() -> None:
    parent_row = PackItem(
        item_id=_PARENT, item_type="document", estimated_tokens=90, metadata={}
    )
    result = measure_parent_concentration([parent_row, _chunk(8)])
    assert result.groups == 1
    assert result.extra_servings == 1
    assert result.extra_tokens == 60


def test_pointer_extras_excluded_from_body_totals() -> None:
    """An extra already demoted by #359 costs a pointer, not a body.

    Counting it as reclaimable body would claim the same saving twice.
    """
    items = [_chunk(0), _chunk(1), _chunk(2)]
    result = measure_parent_concentration(
        items, pointer_item_ids={items[2].item_id}
    )
    assert result.extra_servings == 2
    assert result.extra_tokens == 120
    assert result.extra_body_servings == 1
    assert result.extra_body_tokens == 60


def test_body_totals_equal_extras_when_nothing_was_demoted() -> None:
    result = measure_parent_concentration([_chunk(0), _chunk(1)])
    assert result.extra_body_servings == result.extra_servings
    assert result.extra_body_tokens == result.extra_tokens


def test_measurement_is_read_only() -> None:
    """It measures a refused rollup; it must never perform one."""
    items = [_chunk(0), _chunk(1), _standalone(3)]
    before = [item.model_copy(deep=True) for item in items]
    measure_parent_concentration(items)
    assert items == before


def test_missing_token_estimates_do_not_raise() -> None:
    """``estimated_tokens`` is optional on PackItem; absence charges zero."""
    a = PackItem(item_id=f"{_PARENT}#chunk-0", item_type="document")
    b = PackItem(item_id=f"{_PARENT}#chunk-1", item_type="document")
    result = measure_parent_concentration([a, b])
    assert result.extra_servings == 1
    assert result.extra_tokens == 0


def test_as_telemetry_emits_every_field_even_when_clean() -> None:
    payload = ParentConcentration().as_telemetry()
    assert payload == {
        "groups": 0,
        "extra_servings": 0,
        "extra_tokens": 0,
        "max_group_size": 0,
        "extra_body_servings": 0,
        "extra_body_tokens": 0,
    }


# ---------------------------------------------------------------------------
# PackBuilder wiring
# ---------------------------------------------------------------------------


def _assembled_payload(event_log: Any) -> dict[str, Any]:
    events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED)
    assert events, "expected a PACK_ASSEMBLED event"
    return events[0].payload


def test_pack_assembled_carries_parent_concentration(event_log: Any) -> None:
    builder = PackBuilder(
        strategies=[_FixedStrategy([_chunk(0), _chunk(1), _standalone(5)])],
        event_log=event_log,
    )
    builder.build(intent="loader retries", budget=PackBudget(max_items=10))

    payload = _assembled_payload(event_log)["parent_concentration"]
    assert payload["groups"] == 1
    assert payload["extra_servings"] == 1
    assert payload["max_group_size"] == 2


def test_concentration_present_on_a_clean_pack(event_log: Any) -> None:
    """Absence of the key must mean "not measured", never "nothing found"."""
    builder = PackBuilder(
        strategies=[_FixedStrategy([_standalone(i) for i in range(3)])],
        event_log=event_log,
    )
    builder.build(intent="loader retries", budget=PackBudget(max_items=10))

    payload = _assembled_payload(event_log)["parent_concentration"]
    assert payload["groups"] == 0
    assert payload["max_group_size"] == 1


def test_concentration_does_not_change_what_is_served(event_log: Any) -> None:
    """The measurement is inert: the same pack, item for item."""
    items = [_chunk(i) for i in range(4)] + [_standalone(9)]
    builder = PackBuilder(strategies=[_FixedStrategy(items)], event_log=event_log)
    pack = builder.build(intent="loader retries", budget=PackBudget(max_items=10))

    assert [item.item_id for item in pack.items] == [item.item_id for item in items]


def test_extras_demoted_by_disclosure_are_not_counted_as_bodies(
    event_log: Any,
) -> None:
    """Ties the pass ordering: concentration runs after graduated disclosure."""
    items = [_chunk(i) for i in range(4)]
    builder = PackBuilder(
        strategies=[_FixedStrategy(items)],
        event_log=event_log,
        disclosure=DisclosureConfig(body_items=2),
    )
    builder.build(intent="loader retries", budget=PackBudget(max_items=10))

    payload = _assembled_payload(event_log)["parent_concentration"]
    assert payload["extra_servings"] == 3
    # Ranks 3 and 4 are pointers; only rank 2 remains a reclaimable body.
    assert payload["extra_body_servings"] == 1
