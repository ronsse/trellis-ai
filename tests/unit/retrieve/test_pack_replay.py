"""Tests for counterfactual pack-policy replay (``trellis.retrieve.pack_replay``).

Three families, in the order they matter:

1. **Identity.** An empty policy must reproduce the served window exactly.
   Without that, every delta the module reports is measured against a
   baseline it invented, and the whole method is unfalsifiable.
2. **The refill.** The finding this module exists to make visible is that
   a narrower excerpt does not produce a cheaper pack, because the greedy
   walk spends the saving on more tail. That has to be reachable in a
   test, not just in production data.
3. **Honest costs.** A policy can always raise the fraction by serving
   less; the counts that say what serving less lost must not be
   understate-able — in particular, per *serving*, so a memory bodied in
   one pack cannot mask its being withheld in another.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from trellis.retrieve.pack_replay import ReplayPolicy, replay_pack_value
from trellis.retrieve.pack_value import MIN_ATTRIBUTED_PACKS
from trellis.stores.base.event_log import Event, EventLog, EventType


class _FakeEventLog(EventLog):
    """In-memory event log — same shape as the one in test_pack_value."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def append(self, event: Event) -> None:
        self.events.append(event)

    def get_events(
        self,
        *,
        event_type: EventType | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        order: str = "asc",
        payload_filters: dict[str, str] | None = None,
    ) -> list[Event]:
        result = self.events
        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        result = sorted(result, key=lambda e: e.occurred_at, reverse=order == "desc")
        return result[:limit]

    def emit(self, *args: Any, **kwargs: Any) -> Event:  # pragma: no cover
        raise NotImplementedError

    def count(self, **kwargs: Any) -> int:  # pragma: no cover
        return len(self.events)

    def close(self) -> None:  # pragma: no cover
        return None


def _pack_event(
    pack_id: str,
    items: list[tuple[str, int]],
    *,
    max_tokens: int = 10_000,
    rejected: list[tuple[str, int]] | None = None,
) -> Event:
    """A flat PACK_ASSEMBLED carrying both halves of the walk.

    ``items`` are ``(item_id, tokens)`` admitted; ``rejected`` are the
    candidates the budget turned away — the replay needs them to be able
    to re-admit anything at all.
    """
    trace = [
        {"item_id": i, "item_tokens": t, "running_total": 0, "included": True}
        for i, t in items
    ] + [
        {"item_id": i, "item_tokens": t, "running_total": 0, "included": False}
        for i, t in (rejected or [])
    ]
    return Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "pack_id": pack_id,
            "budget_max_tokens": max_tokens,
            "injected_items": [
                {
                    "item_id": i,
                    "item_type": "document",
                    "rank": rank,
                    "estimated_tokens": t,
                    "strategy_source": "semantic",
                    "title": f"Memory {i}",
                }
                for rank, (i, t) in enumerate(items, start=1)
            ],
            "budget_trace": trace,
        },
    )


def _feedback_event(pack_id: str, helpful: list[str]) -> Event:
    return Event(
        event_type=EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "pack_id": pack_id,
            "rating": 0.8,
            "success": True,
            "helpful_item_ids": helpful,
            "unhelpful_item_ids": [],
        },
    )


def _window(packs: int = MIN_ATTRIBUTED_PACKS, *, tail: int = 10) -> _FakeEventLog:
    """A window whose packs each have a fat, uncited tail."""
    log = _FakeEventLog()
    for n in range(packs):
        pack_id = f"pack-{n}"
        items = [(f"p{n}-i{k}", 120) for k in range(2 + tail)]
        log.append(_pack_event(pack_id, items))
        log.append(_feedback_event(pack_id, [f"p{n}-i0"]))
    return log


# --------------------------------------------------------------------------
# 1. Identity
# --------------------------------------------------------------------------


def test_empty_policy_reproduces_the_served_window() -> None:
    report = replay_pack_value(_window(), policy=ReplayPolicy())

    assert report.token_delta == 0.0
    assert report.fraction_delta == 0.0
    assert report.counterfactual.injected_tokens == report.baseline.injected_tokens
    assert report.counterfactual.items == report.baseline.items
    assert report.helpful_bodies_withheld == 0
    assert report.helpful_items_dropped == 0
    assert report.admitted_ungraded_items == 0


def test_baseline_fraction_is_the_served_fraction() -> None:
    report = replay_pack_value(_window(tail=2), policy=ReplayPolicy(body_items=2))

    # 4 items of 120 tokens each, one cited helpful, in every pack.
    assert report.baseline.useful_token_fraction == pytest.approx(0.25, abs=1e-4)


# --------------------------------------------------------------------------
# 2. The refill — the finding the module exists to expose
# --------------------------------------------------------------------------


def test_a_narrower_excerpt_is_spent_on_more_tail_not_saved() -> None:
    """The width lever's payoff is eaten by the greedy re-admitting."""
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack-{n}"
        admitted = [(f"p{n}-i{k}", 120) for k in range(8)]
        turned_away = [(f"p{n}-r{k}", 120) for k in range(20)]
        log.append(_pack_event(pack_id, admitted, max_tokens=960, rejected=turned_away))
        log.append(_feedback_event(pack_id, [f"p{n}-i0"]))

    narrower = replay_pack_value(
        log, policy=ReplayPolicy(excerpt_max_chars=240, refill=True)
    )

    assert narrower.counterfactual.items > narrower.baseline.items
    assert narrower.admitted_ungraded_items > 0
    # The pack did not get cheaper, and the fraction fell because the
    # saving bought items nobody had graded.
    assert narrower.token_delta is not None
    assert narrower.token_delta > -0.10
    assert narrower.fraction_delta is not None
    assert narrower.fraction_delta < 0


def test_no_refill_isolates_the_pricing_effect() -> None:
    """Same cap, admission held fixed: tokens fall, the ratio barely moves."""
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack-{n}"
        admitted = [(f"p{n}-i{k}", 120) for k in range(8)]
        log.append(
            _pack_event(
                pack_id,
                admitted,
                max_tokens=960,
                rejected=[(f"p{n}-r{k}", 120) for k in range(20)],
            )
        )
        log.append(_feedback_event(pack_id, [f"p{n}-i0"]))

    held = replay_pack_value(
        log, policy=ReplayPolicy(excerpt_max_chars=240, refill=False)
    )

    assert held.counterfactual.items == held.baseline.items
    assert held.admitted_ungraded_items == 0
    assert held.token_delta is not None
    assert held.token_delta < -0.4
    # A uniform width scaling shrinks numerator and denominator together.
    assert held.fraction_delta == pytest.approx(0.0, abs=0.05)


# --------------------------------------------------------------------------
# 3. Honest costs
# --------------------------------------------------------------------------


def test_graduation_saves_tokens_and_reports_the_bodies_it_withheld() -> None:
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack-{n}"
        items = [(f"p{n}-i{k}", 120) for k in range(12)]
        log.append(_pack_event(pack_id, items))
        # Cited helpful at rank 1 (kept) and rank 10 (past the cut).
        log.append(_feedback_event(pack_id, [f"p{n}-i0", f"p{n}-i9"]))

    report = replay_pack_value(log, policy=ReplayPolicy(body_items=4))

    assert report.token_delta is not None
    assert report.token_delta < -0.3
    assert report.helpful_bodies_withheld == MIN_ATTRIBUTED_PACKS
    assert report.helpful_items_dropped == 0
    assert report.counterfactual.pointer_items_served > 0


def test_an_item_ceiling_reports_drops_not_withholdings() -> None:
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack-{n}"
        log.append(_pack_event(pack_id, [(f"p{n}-i{k}", 120) for k in range(12)]))
        log.append(_feedback_event(pack_id, [f"p{n}-i0", f"p{n}-i9"]))

    report = replay_pack_value(log, policy=ReplayPolicy(max_items=4))

    assert report.helpful_items_dropped == MIN_ATTRIBUTED_PACKS
    assert report.helpful_bodies_withheld == 0


def test_withholding_is_counted_per_serving_not_per_distinct_id() -> None:
    """A body served in one pack must not mask its withholding in another.

    The same memory can sit at rank 1 in one pack and rank 11 in the next.
    Counting distinct ids let the first mask the second and reported a
    cost of zero for a policy that had really withheld a body.
    """
    log = _FakeEventLog()
    shared = "doc:shared"
    for n in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack-{n}"
        # Rank 1 in even packs, rank 12 in odd ones.
        ids = (
            [shared] + [f"p{n}-i{k}" for k in range(11)]
            if n % 2 == 0
            else [f"p{n}-i{k}" for k in range(11)] + [shared]
        )
        log.append(_pack_event(pack_id, [(i, 120) for i in ids]))
        log.append(_feedback_event(pack_id, [shared]))

    report = replay_pack_value(log, policy=ReplayPolicy(body_items=4))

    odd_packs = MIN_ATTRIBUTED_PACKS // 2
    assert report.helpful_bodies_withheld == odd_packs
    assert report.helpful_items_total == MIN_ATTRIBUTED_PACKS


def test_a_thin_sample_is_refused_not_rounded() -> None:
    report = replay_pack_value(
        _window(packs=MIN_ATTRIBUTED_PACKS - 1), policy=ReplayPolicy(body_items=2)
    )

    assert report.suppressed is True
    assert report.baseline.useful_token_fraction is None
    assert report.counterfactual.useful_token_fraction is None
    assert report.fraction_delta is None
    assert any("suppressed" in note.lower() for note in report.notes)


def test_a_pack_without_a_budget_trace_is_excluded_and_counted() -> None:
    """The walk cannot be re-run from an event that did not record it."""
    log = _window()
    orphan = _pack_event("pack-orphan", [("o-1", 100)])
    orphan.payload["budget_trace"] = []
    log.append(orphan)
    log.append(_feedback_event("pack-orphan", ["o-1"]))

    report = replay_pack_value(log, policy=ReplayPolicy(body_items=2))

    assert report.packs_without_budget_trace == 1
    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS
    assert any("budget_trace" in note for note in report.notes)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"excerpt_max_chars": 0},
        {"body_items": 0},
        {"max_items": -1},
    ],
)
def test_degenerate_policies_are_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        ReplayPolicy(**kwargs)
