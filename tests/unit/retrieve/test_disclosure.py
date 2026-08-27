"""Tests for graduated disclosure (``trellis.retrieve.disclosure``).

The properties worth pinning are the ones that make this a *demotion*
rather than a trim, plus the one ordering constraint the whole saving
depends on:

* nothing is dropped, reordered, or made unaddressable;
* a demoted item's ``estimated_tokens`` records the pointer, not the
  withheld body — otherwise every downstream cost measurement prices text
  the prompt never carried;
* the pass runs *after* the token-budget walk, so the freed tokens are
  not handed straight back to the greedy as more tail.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.core.elision import format_char_count
from trellis.retrieve.disclosure import (
    DEFAULT_BODY_ITEMS,
    DISCLOSURE_OFF,
    POINTER_SELECTION_REASON,
    DisclosureConfig,
    apply_disclosure,
    pointer_excerpt,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackBudget, PackItem

#: Long enough that a pointer is unambiguously cheaper than the body.
_BODY = "deployment runbook step for the northwind loader " * 10


def _item(index: int, *, excerpt: str = _BODY, title: str | None = None) -> PackItem:
    return PackItem(
        item_id=f"doc:{index:03d}",
        item_type="document",
        excerpt=excerpt,
        relevance_score=1.0 - index * 0.001,
        metadata={"title": title} if title else {},
    )


class _FixedStrategy(SearchStrategy):
    """Returns a fixed candidate list, so budgets are the only variable."""

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


# --------------------------------------------------------------------------
# It is a demotion, not a trim
# --------------------------------------------------------------------------


def test_no_item_is_dropped_or_reordered() -> None:
    items = [_item(i) for i in range(30)]
    result = apply_disclosure(items, DisclosureConfig(body_items=5))

    assert len(result.items) == len(items)
    assert [i.item_id for i in result.items] == [i.item_id for i in items]


def test_head_keeps_its_body_and_tail_becomes_pointers() -> None:
    items = [_item(i) for i in range(10)]
    result = apply_disclosure(items, DisclosureConfig(body_items=4))

    assert [i.excerpt for i in result.items[:4]] == [i.excerpt for i in items[:4]]
    assert len(result.pointer_item_ids) == 6
    for item in result.items[4:]:
        assert item.selection_reason == POINTER_SELECTION_REASON
        assert len(item.excerpt) < len(_BODY)


def test_pointer_names_the_item_and_the_size_it_withheld() -> None:
    item = _item(1, title="Northwind loader runbook")
    pointer = pointer_excerpt(item)

    assert "Northwind loader runbook" in pointer
    assert "get_items fetches the source" in pointer
    # The size quoted is what this pack withheld, in the same rendering
    # truncate_excerpt marks its own cuts with.
    assert f"[+{format_char_count(len(item.excerpt))} chars" in pointer


def test_pointer_falls_back_to_the_excerpt_when_there_is_no_title() -> None:
    pointer = pointer_excerpt(_item(1))

    assert pointer.startswith("deployment runbook step")


def test_an_item_smaller_than_its_own_pointer_keeps_its_body() -> None:
    """A terse memory must never be replaced by something longer.

    The one-line gotcha is the shape this system most wants to serve; a
    pointer to it would cost more than the memory and deliver less.
    """
    terse = _item(9, excerpt="restart the api container before trellis admin init")
    items = [_item(i) for i in range(5)] + [terse]

    result = apply_disclosure(items, DisclosureConfig(body_items=2))

    assert terse.item_id not in result.pointer_item_ids
    assert result.items[-1].excerpt == terse.excerpt


def test_disclosure_off_is_a_no_op() -> None:
    items = [_item(i) for i in range(20)]
    result = apply_disclosure(items, DISCLOSURE_OFF)

    assert result.pointer_item_ids == []
    assert [i.excerpt for i in result.items] == [i.excerpt for i in items]
    assert result.tokens_before == result.tokens_after


def test_a_pack_shorter_than_the_cut_is_untouched() -> None:
    items = [_item(i) for i in range(3)]
    result = apply_disclosure(items, DisclosureConfig(body_items=DEFAULT_BODY_ITEMS))

    assert result.pointer_item_ids == []
    assert [i.excerpt for i in result.items] == [i.excerpt for i in items]


@pytest.mark.parametrize("bad", [0, -1])
def test_body_items_below_one_is_refused(bad: int) -> None:
    """Zero bodies is index mode, which has its own path and its own flag."""
    with pytest.raises(ValueError, match="body_items must be >= 1"):
        DisclosureConfig(body_items=bad)


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        DisclosureConfig(mode="trim")  # type: ignore[arg-type]


def test_telemetry_is_emitted_even_when_nothing_was_demoted() -> None:
    """ "Ran, found nothing" must be distinguishable from "never ran"."""
    result = apply_disclosure([_item(0)], DisclosureConfig(body_items=5))
    telemetry = result.as_telemetry()

    assert telemetry["mode"] == "graduated"
    assert telemetry["body_items"] == 5
    assert telemetry["pointer_count"] == 0
    assert telemetry["tokens_before"] == telemetry["tokens_after"]


# --------------------------------------------------------------------------
# Wired into PackBuilder
# --------------------------------------------------------------------------


def _build(disclosure: DisclosureConfig | None, *, max_tokens: int = 3000) -> Any:
    builder = PackBuilder(
        strategies=[_FixedStrategy([_item(i) for i in range(30)])],
        disclosure=disclosure,
    )
    return builder.build(
        "deploy", budget=PackBudget(max_items=50, max_tokens=max_tokens)
    )


def test_graduation_cuts_tokens_without_cutting_items() -> None:
    """The saving comes from the tail's width, never from the item set.

    Serving fewer items would be cheaper still and is deliberately not what
    this does — see the module docstring.
    """
    off = _build(DISCLOSURE_OFF)
    on = _build(DisclosureConfig(body_items=5))

    assert len(on.items) == len(off.items)
    assert {i.item_id for i in on.items} == {i.item_id for i in off.items}
    assert sum(i.estimated_tokens or 0 for i in on.items) < sum(
        i.estimated_tokens or 0 for i in off.items
    )


def test_estimated_tokens_records_the_pointer_not_the_withheld_body() -> None:
    """The cost every downstream analyzer reads must be the cost paid."""
    pack = _build(DisclosureConfig(body_items=3))
    demoted = [i for i in pack.items if i.selection_reason == POINTER_SELECTION_REASON]

    assert demoted, "expected the tail to be demoted"
    for item in demoted:
        assert item.estimated_tokens == len(item.excerpt) // 4 + 1


def test_budget_trace_is_repriced_to_the_text_actually_served() -> None:
    """Otherwise the token-total validator reads the saving as drift."""
    pack = _build(DisclosureConfig(body_items=3))
    charged = {
        step.item_id: step.item_tokens
        for step in pack.retrieval_report.budget_trace
        if step.included
    }

    for item in pack.items:
        assert charged[item.item_id] == item.estimated_tokens


def test_graduation_runs_after_the_walk_so_the_saving_is_not_refilled() -> None:
    """The ordering constraint the whole saving depends on.

    Priced before the walk, cheaper tail items would let the greedy admit
    more of them and the pack would cost the same. Priced after, the pack
    keeps its item set and comes in under the ceiling.
    """
    off = _build(DISCLOSURE_OFF, max_tokens=2000)
    on = _build(DisclosureConfig(body_items=4), max_tokens=2000)

    assert len(on.items) == len(off.items)
    assert sum(i.estimated_tokens or 0 for i in on.items) < 2000 * 0.8


def test_index_mode_is_exempt() -> None:
    """An index pack is already all pointers; graduating it cuts twice."""
    builder = PackBuilder(
        strategies=[_FixedStrategy([_item(i) for i in range(30)])],
        disclosure=DisclosureConfig(body_items=2),
    )
    pack = builder.build(
        "deploy", budget=PackBudget(max_items=50, max_tokens=3000), index_mode=True
    )

    assert not any(i.selection_reason == POINTER_SELECTION_REASON for i in pack.items)
