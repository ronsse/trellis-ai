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
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.class_prior import OFF, ClassPriorConfig
from trellis.retrieve.disclosure import DISCLOSURE_OFF, DisclosureConfig
from trellis.retrieve.excerpts import ContentFloorConfig
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.pack_replay import (
    ReplayPolicy,
    replay_class_prior,
    replay_graph_selection,
    replay_pack_value,
)
from trellis.retrieve.pack_value import MIN_ATTRIBUTED_PACKS
from trellis.retrieve.strategies import (
    GRAPH_SELECTION_CALLER_SEEDED,
    GRAPH_SELECTION_RECENCY_WINDOW,
    GRAPH_SELECTION_SEEDED,
    SearchStrategy,
)
from trellis.schemas.pack import PackBudget, PackItem
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


class _FakeEventLog(EventLog):
    """In-memory event log — same shape as the one in test_pack_value."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def _append(self, event: Event) -> None:
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


# --------------------------------------------------------------------------
# 4. The class-prior replay arm (trellis.retrieve.class_prior)
# --------------------------------------------------------------------------


def _class_prior_pack_event(
    pack_id: str,
    rows: list[dict[str, Any]],
    *,
    rejected: list[dict[str, Any]] | None = None,
) -> Event:
    """A flat PACK_ASSEMBLED whose ``injected_items`` carry the extra
    ``document_form`` field :func:`replay_class_prior` reads. Each row:
    ``item_id``, ``form``, ``axis``, ``tokens``, ``score``, ``rank`` — the
    unused ``score``/``rank`` keys are dead data callers may still pass,
    kept only so existing fixtures need no trimming.

    ``rejected`` mirrors the pack's own ``rejected_items[]`` (each row:
    ``item_id``, ``reason``, ``axis``) for scenarios where a losing
    duplicate on another axis is on record.
    """
    return Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "pack_id": pack_id,
            "injected_items": [
                {
                    "item_id": row["item_id"],
                    "item_type": "document",
                    "rank": row["rank"],
                    "estimated_tokens": row["tokens"],
                    "strategy_source": row["axis"],
                    "document_form": row["form"],
                }
                for row in rows
            ],
            "rejected_items": [
                {
                    "item_id": row["item_id"],
                    "item_type": "document",
                    "relevance_score": 0.0,
                    "reason": row["reason"],
                    "strategy_source": row["axis"],
                }
                for row in (rejected or [])
            ],
            "budget_trace": [],
        },
    )


def test_off_mode_reports_all_zero_counts_and_the_off_note() -> None:
    log = _FakeEventLog()
    log.append(
        _class_prior_pack_event(
            "pack-0",
            [
                {
                    "item_id": "c1",
                    "form": "conversation",
                    "axis": "keyword",
                    "tokens": 50,
                    "score": 0.9,
                    "rank": 1,
                }
            ],
        )
    )
    log.append(_feedback_event("pack-0", ["c1"]))

    report = replay_class_prior(log, config=OFF)

    assert report.matched_servings == 0
    assert report.helpful_lost == 0
    assert report.unhelpful_removed == 0
    assert report.tokens_freed == 0
    assert any('mode is "off"' in note for note in report.notes)


def test_withhold_counts_helpful_lost_unhelpful_removed_and_tokens_freed() -> None:
    config = ClassPriorConfig(mode="withhold")
    log = _FakeEventLog()
    # pack-0: the matching (conversation, keyword) item was cited helpful.
    log.append(
        _class_prior_pack_event(
            "pack-0",
            [
                {
                    "item_id": "c1",
                    "form": "conversation",
                    "axis": "keyword",
                    "tokens": 50,
                    "score": 0.9,
                    "rank": 1,
                },
                {
                    "item_id": "m1",
                    "form": "capture",
                    "axis": "keyword",
                    "tokens": 30,
                    "score": 0.7,
                    "rank": 2,
                },
            ],
        )
    )
    log.append(_feedback_event("pack-0", ["c1"]))
    # pack-1: the matching item was cited UNhelpful — the removal the rule
    # is for. The capture-form item is never eligible, even cited helpful.
    log.append(
        _class_prior_pack_event(
            "pack-1",
            [
                {
                    "item_id": "c2",
                    "form": "conversation",
                    "axis": "keyword",
                    "tokens": 80,
                    "score": 0.6,
                    "rank": 1,
                },
                {
                    "item_id": "m2",
                    "form": "capture",
                    "axis": "keyword",
                    "tokens": 40,
                    "score": 0.5,
                    "rank": 2,
                },
            ],
        )
    )
    feedback = _feedback_event("pack-1", ["m2"])
    feedback.payload["unhelpful_item_ids"] = ["c2"]
    log.append(feedback)

    report = replay_class_prior(log, config=config)

    assert report.attributed_packs == 2
    # Only the two conversation/keyword rows match; the two capture rows
    # never do, whatever they were cited.
    assert report.matched_servings == 2
    assert report.helpful_lost == 1
    assert report.unhelpful_removed == 1
    assert report.tokens_freed == 50 + 80
    assert any("it cannot grade one" in note for note in report.notes)


def test_axis_scoping_excludes_a_matching_form_on_another_axis() -> None:
    """Default axes is {keyword}; a semantic-axis conversation item never counts."""
    config = ClassPriorConfig(mode="withhold")  # axes default: {"keyword"}
    log = _FakeEventLog()
    log.append(
        _class_prior_pack_event(
            "pack-0",
            [
                {
                    "item_id": "c1",
                    "form": "conversation",
                    "axis": "semantic",
                    "tokens": 50,
                    "score": 0.9,
                    "rank": 1,
                }
            ],
        )
    )
    log.append(_feedback_event("pack-0", ["c1"]))

    report = replay_class_prior(log, config=config)

    assert report.matched_servings == 0
    assert report.helpful_lost == 0
    assert report.tokens_freed == 0
    # document_form IS present on the one row in this window — it just
    # doesn't match on this axis — so the note says nothing was eligible,
    # never that there is no history to grade.
    assert any("nothing was eligible" in note for note in report.notes)
    assert not any("no history yet" in note for note in report.notes)


def test_no_document_form_in_the_window_says_no_history_yet_not_nothing_eligible() -> (
    None
):
    """A pack assembled before this gate existed carries no document_form
    on any row. Zero matched servings there must read as "there is no
    history yet for this rule to grade" — a stronger and different claim
    than "nothing was eligible", which implies the field was there to look
    at and simply never matched."""
    config = ClassPriorConfig(mode="withhold")
    log = _FakeEventLog()
    log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id="pack-0",
            entity_type="pack",
            payload={
                "pack_id": "pack-0",
                "injected_items": [
                    {
                        "item_id": "c1",
                        "item_type": "document",
                        "rank": 1,
                        "estimated_tokens": 50,
                        "strategy_source": "keyword",
                        # No "document_form" key at all — the pre-feature shape.
                    }
                ],
                "budget_trace": [],
            },
        )
    )
    log.append(_feedback_event("pack-0", ["c1"]))

    report = replay_class_prior(log, config=config)

    assert report.matched_servings == 0
    assert any("no history yet" in note for note in report.notes)
    assert not any("nothing was eligible" in note for note in report.notes)


def test_a_serving_still_served_by_another_axis_is_not_counted_as_removed() -> None:
    """The gate is axis-scoped: withholding the configured-axis copy of a
    matched item does not remove it from the pack when the pack's own
    dedup record shows another, unconfigured axis's copy losing to it. A
    matched serving in that shape must not count as removed, must not
    free its tokens, and must not count as helpful lost or unhelpful
    removed — whatever the feedback said — and must be counted separately.
    """
    config = ClassPriorConfig(mode="withhold")  # axes default: {"keyword"}
    log = _FakeEventLog()
    log.append(
        _class_prior_pack_event(
            "pack-0",
            [
                {
                    "item_id": "c1",
                    "form": "conversation",
                    "axis": "keyword",
                    "tokens": 50,
                    "score": 0.9,
                    "rank": 1,
                }
            ],
            rejected=[{"item_id": "c1", "reason": "dedup", "axis": "semantic"}],
        )
    )
    feedback = _feedback_event("pack-0", [])
    feedback.payload["unhelpful_item_ids"] = ["c1"]
    log.append(feedback)

    report = replay_class_prior(log, config=config)

    assert report.matched_servings == 1
    assert report.still_served_by_other_axis == 1
    assert report.helpful_lost == 0
    assert report.unhelpful_removed == 0
    assert report.tokens_freed == 0
    assert any("still_served_by_other_axis" in note for note in report.notes)


def test_a_dedup_loss_on_a_configured_axis_does_not_suppress_removal() -> None:
    """The exclusion is specific to a losing copy *outside* config.axes.
    A dedup row whose strategy_source is itself inside config.axes (e.g.
    two keyword-axis copies of the same id) says nothing about another
    axis rescuing the item, so the serving still counts as removed.
    """
    config = ClassPriorConfig(mode="withhold")  # axes default: {"keyword"}
    log = _FakeEventLog()
    log.append(
        _class_prior_pack_event(
            "pack-0",
            [
                {
                    "item_id": "c1",
                    "form": "conversation",
                    "axis": "keyword",
                    "tokens": 50,
                    "score": 0.9,
                    "rank": 1,
                }
            ],
            rejected=[{"item_id": "c1", "reason": "dedup", "axis": "keyword"}],
        )
    )
    log.append(_feedback_event("pack-0", ["c1"]))

    report = replay_class_prior(log, config=config)

    assert report.matched_servings == 1
    assert report.still_served_by_other_axis == 0
    assert report.helpful_lost == 1
    assert report.tokens_freed == 50


# --------------------------------------------------------------------------
# 4. The builder-to-replay contract, built end to end
# --------------------------------------------------------------------------
#
# Every scenario above hand-builds the PACK_ASSEMBLED payload, so none of
# them pins the one fact replay's ``_served_by_other_axis`` actually
# depends on: that a dedup row in a real pack's own ``rejected_items``
# carries the *loser's* ``strategy_source``, not the winner's
# (``pack_builder.py``'s ``_deduplicate_tracked``). These two build the OFF
# pack through the real ``PackBuilder``, write its event to a real event
# log, and replay over it — closing that gap.


def _axis_strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    """A fake axis whose ``search`` hands back a fresh copy of ``items`` —
    a real strategy never returns the same :class:`PackItem` instance
    twice, and dedup's "loser" bookkeeping must not depend on identity."""
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.side_effect = lambda *a, **k: [
        item.model_copy(deep=True) for item in items
    ]
    return strategy


def _conversation_item(
    item_id: str, score: float, axis: str, *, noise: bool = False
) -> PackItem:
    metadata: dict[str, Any] = {
        "source_strategy": axis,
        "document_form": "conversation",
    }
    if noise:
        metadata["content_tags"] = {"signal_quality": "noise"}
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=f"{item_id} distinct body words here " * 6,
        relevance_score=score,
        metadata=metadata,
    )


def _cite_unhelpful(log: SQLiteEventLog, pack_id: str, item_id: str) -> None:
    log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "rating": 0.2,
                "success": False,
                "helpful_item_ids": [],
                "unhelpful_item_ids": [item_id],
            },
        )
    )


def test_builder_dedup_row_feeds_replay_still_served_by_other_axis(
    tmp_path: Path,
) -> None:
    """A conversation parent is found by keyword (~0.9) and by semantic
    (~0.4); with ``axes={keyword}`` the keyword copy wins dedup and the
    pack's own ``rejected_items`` records the semantic copy as the dedup
    loser. Replaying withhold over that real event must report the item as
    still served by the unconfigured axis, not as removed — it must fail
    if ``pack_builder.py``'s dedup-loser row stamped the *winning* copy's
    axis (``strategy_source=existing.strategy_source``) instead of the
    loser's own.
    """
    strategies = [
        _axis_strategy("keyword", [_conversation_item("conv", 0.9, "keyword")]),
        _axis_strategy("semantic", [_conversation_item("conv", 0.4, "semantic")]),
    ]
    log = SQLiteEventLog(tmp_path / "events.db")
    builder = PackBuilder(
        strategies=strategies,
        event_log=log,
        content_floor=ContentFloorConfig(mode="off"),
        class_prior_config=ClassPriorConfig(mode="off", axes=frozenset({"keyword"})),
    )
    off_pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=100_000))
    assert [item.item_id for item in off_pack.items] == ["conv"]
    rejected = off_pack.retrieval_report.rejected_items
    assert [(r.item_id, r.reason, r.strategy_source) for r in rejected] == [
        ("conv", "dedup", "semantic")
    ]

    events = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
    assert len(events) == 1
    pack_id = events[0].payload.get("pack_id") or events[0].entity_id
    _cite_unhelpful(log, pack_id, "conv")

    report = replay_class_prior(
        log, config=ClassPriorConfig(mode="withhold", axes=frozenset({"keyword"}))
    )

    assert report.still_served_by_other_axis == 1
    assert report.unhelpful_removed == 0
    assert report.tokens_freed == 0


def test_builder_noise_rejected_other_axis_copy_does_not_block_removal(
    tmp_path: Path,
) -> None:
    """Converse shape: the semantic copy is rejected for a non-dedup
    reason (noise), so dedup never runs between the two copies and the
    keyword copy serves alone. Replaying withhold must then count the
    citation as a removal — it must fail if the ``reason == "dedup"``
    condition in ``_served_by_other_axis`` (``pack_replay.py``) were
    deleted, since the surviving noise-rejected row would then be
    (wrongly) read as a dedup loss on an axis outside ``config.axes``.
    """
    strategies = [
        _axis_strategy("keyword", [_conversation_item("conv", 0.9, "keyword")]),
        _axis_strategy(
            "semantic", [_conversation_item("conv", 0.4, "semantic", noise=True)]
        ),
    ]
    log = SQLiteEventLog(tmp_path / "events.db")
    builder = PackBuilder(
        strategies=strategies,
        event_log=log,
        content_floor=ContentFloorConfig(mode="off"),
        class_prior_config=ClassPriorConfig(mode="off", axes=frozenset({"keyword"})),
    )
    off_pack = builder.build("q", budget=PackBudget(max_items=50, max_tokens=100_000))
    assert [item.item_id for item in off_pack.items] == ["conv"]
    rejected = off_pack.retrieval_report.rejected_items
    assert [(r.item_id, r.reason, r.strategy_source) for r in rejected] == [
        ("conv", "noise", "semantic")
    ]

    events = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
    assert len(events) == 1
    pack_id = events[0].payload.get("pack_id") or events[0].entity_id
    _cite_unhelpful(log, pack_id, "conv")

    report = replay_class_prior(
        log, config=ClassPriorConfig(mode="withhold", axes=frozenset({"keyword"}))
    )

    assert report.unhelpful_removed == 1
    assert report.still_served_by_other_axis == 0


# --------------------------------------------------------------------------
# 5. replay_graph_selection (#961)
# --------------------------------------------------------------------------
#
# The class-prior arm counts what a gate would withhold but says it cannot
# say what the freed budget would serve. This arm can, because each pack's
# ``budget_trace`` lists the candidates the token walk turned away: it
# re-runs the walk without the withheld servings, trusting the re-run only
# once it has reproduced the walk the pack recorded.

_RW = GRAPH_SELECTION_RECENCY_WINDOW
_SD = GRAPH_SELECTION_SEEDED
_CS = GRAPH_SELECTION_CALLER_SEEDED


def _gs_row(
    item_id: str, tokens: int = 100, axis: str = "graph", stamp: str | None = None
) -> dict[str, Any]:
    return {"item_id": item_id, "tokens": tokens, "axis": axis, "stamp": stamp}


def _gs_pack_event(
    pack_id: str,
    served: list[dict[str, Any]],
    *,
    turned_away: list[dict[str, Any]] | None = None,
    dedup_losers: list[dict[str, Any]] | None = None,
    cut_by_items: list[dict[str, Any]] | None = None,
    max_items: int | None = None,
    price_cut_rows: bool = True,
    max_tokens: float = 10_000,
    with_trace: bool = True,
    holdout: bool = False,
) -> Event:
    """A flat PACK_ASSEMBLED carrying ``graph_selection`` and both walk halves.

    ``served`` rows are ``{item_id, tokens, axis, stamp}``; ``turned_away``
    rows are the same shape, recorded as ``included: false`` in
    ``budget_trace`` and as ``token_budget`` in ``rejected_items`` (which,
    like the real event, carries no stamp). ``dedup_losers`` are
    ``{item_id, axis}`` copies that lost dedup to a served row.
    ``cut_by_items`` rows never reach ``budget_trace``: they are recorded as
    ``max_items`` rejections, priced with ``estimated_tokens`` unless
    ``price_cut_rows`` is off.
    """
    away = turned_away or []
    rejected = [
        {
            "item_id": row["item_id"],
            "item_type": "document",
            "relevance_score": 0.0,
            "reason": "token_budget",
            "strategy_source": row["axis"],
        }
        for row in away
    ] + [
        {
            "item_id": row["item_id"],
            "item_type": "document",
            "relevance_score": 0.0,
            "reason": "dedup",
            "strategy_source": row["axis"],
        }
        for row in (dedup_losers or [])
    ]
    for row in cut_by_items or []:
        cut_row: dict[str, Any] = {
            "item_id": row["item_id"],
            "item_type": "document",
            "relevance_score": 0.0,
            "reason": "max_items",
            "strategy_source": row["axis"],
        }
        if price_cut_rows:
            cut_row["estimated_tokens"] = row["tokens"]
        rejected.append(cut_row)
    payload: dict[str, Any] = {
        "pack_id": pack_id,
        "budget_max_tokens": max_tokens,
        "injected_items": [
            {
                "item_id": row["item_id"],
                "item_type": "document",
                "rank": rank,
                "estimated_tokens": row["tokens"],
                "strategy_source": row["axis"],
                "graph_selection": row["stamp"],
            }
            for rank, row in enumerate(served, start=1)
        ],
        "rejected_items": rejected,
        "budget_trace": (
            [
                {
                    "item_id": row["item_id"],
                    "item_tokens": row["tokens"],
                    "running_total": 0,
                    "included": included,
                }
                for rows, included in ((served, True), (away, False))
                for row in rows
            ]
            if with_trace
            else []
        ),
    }
    if max_items is not None:
        payload["budget_max_items"] = max_items
    if holdout:
        payload["holdout"] = True
    return Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_tokens", 10.5),
        ("item_tokens", float("nan")),
        ("item_tokens", float("inf")),
        ("item_tokens", None),
        ("walk_tokens", 10.5),
        ("walk_tokens", float("inf")),
    ],
    ids=[
        "fractional",
        "nan",
        "inf",
        "missing",
        "walk_fractional",
        "walk_inf",
    ],
)
def test_an_unreadable_trace_charge_refuses_the_pack_in_both_replays(
    field: str, value: Any
) -> None:
    """``int(inf)`` raises, so one corrupt step used to abort the whole replay."""
    log = _cut_log()
    step = log.events[0].payload["budget_trace"][1]
    if value is None:
        step.pop(field, None)
    else:
        step[field] = value

    selection = replay_graph_selection(log, graph_selection=_RW)
    value_report = replay_pack_value(log, policy=ReplayPolicy())

    assert selection.matched_servings == 1
    assert selection.refilled_packs == 0
    assert selection.unmodelled_packs == 1
    assert any("unreadable token charge" in n for n in selection.notes)
    assert value_report.packs_without_budget_trace == 1
    assert value_report.attributed_packs == 0


def test_a_corrupt_effective_budget_excludes_the_pack_from_the_policy_replay() -> None:
    """``replay_pack_value`` walked 0 for it, emptying the counterfactual pack."""
    log = _arms_log(nominal=10_000, effective=float("nan"))

    refilling = replay_pack_value(log, policy=ReplayPolicy())

    assert refilling.packs_without_budget_trace == 1
    assert refilling.attributed_packs == 0
    assert refilling.baseline.items == refilling.counterfactual.items == 0
    assert any("no usable budget_trace or token budget" in n for n in refilling.notes)

    # Without refill the budget is never read, so the pack still prices.
    pricing = replay_pack_value(log, policy=ReplayPolicy(refill=False))

    assert pricing.packs_without_budget_trace == 0
    assert pricing.baseline.items == pricing.counterfactual.items == 3


def _gs_feedback(
    pack_id: str, helpful: list[str] | None = None, unhelpful: list[str] | None = None
) -> Event:
    return Event(
        event_type=EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "pack_id": pack_id,
            "rating": 0.5,
            "success": bool(helpful),
            "helpful_item_ids": helpful or [],
            "unhelpful_item_ids": unhelpful or [],
        },
    )


def _selection_log(*, holdout_pack: bool = False) -> _FakeEventLog:
    """Five attributed packs, every selection value present, one non-graph item.

    Each pack serves ``sem`` (semantic, unstamped, cited helpful so the pack
    is attributed) beside up to three stamped graph servings whose ids end in
    ``rw`` (every pack), ``sd`` (packs 0-2) and ``cs`` (packs 0-1). Pack 0
    cites its graph servings helpful, pack 1 unhelpful, the rest not at all,
    so the three values give three different answers.
    """
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack-{n}"
        served = [_gs_row(f"p{n}-rw", stamp=_RW)]
        if n < 3:
            served.append(_gs_row(f"p{n}-sd", stamp=_SD))
        if n < 2:
            served.append(_gs_row(f"p{n}-cs", stamp=_CS))
        served.append(_gs_row(f"p{n}-sem", axis="semantic"))
        log.append(_gs_pack_event(pack_id, served))
        graph_ids = [r["item_id"] for r in served if r["axis"] == "graph"]
        log.append(
            _gs_feedback(
                pack_id,
                helpful=[f"p{n}-sem", *(graph_ids if n == 0 else [])],
                unhelpful=graph_ids if n == 1 else [],
            )
        )
    if holdout_pack:
        # Blind by design: neither the pack nor the feedback naming it may
        # reach any count.
        log.append(
            _gs_pack_event(
                "pack-held",
                [_gs_row("held-rw", stamp=_RW), _gs_row("held-sem", axis="semantic")],
                holdout=True,
            )
        )
        log.append(_gs_feedback("pack-held", helpful=["held-rw", "held-sem"]))
    return log


@pytest.mark.parametrize(
    ("value", "matched", "helpful", "unhelpful", "unjudged", "tokens"),
    [
        (_RW, 5, 1, 1, 3, 500),
        (_SD, 3, 1, 1, 1, 300),
        (_CS, 2, 1, 1, 0, 200),
    ],
)
def test_graph_selection_counts_each_value_per_serving(
    value: str,
    matched: int,
    helpful: int,
    unhelpful: int,
    unjudged: int,
    tokens: int,
) -> None:
    log = _selection_log()
    events_before = len(log.events)

    report = replay_graph_selection(log, graph_selection=value)

    assert report.graph_selection == value
    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS
    assert report.matched_servings == matched
    assert report.helpful_lost == helpful
    assert report.unhelpful_removed == unhelpful
    assert report.unjudged_removed == unjudged
    assert report.tokens_freed == tokens
    assert report.still_served_by_other_axis == 0
    # The buckets reconcile to what matched, so nothing is dropped silently.
    assert (
        report.helpful_lost
        + report.unhelpful_removed
        + report.unjudged_removed
        + report.still_served_by_other_axis
        == report.matched_servings
    )
    # Read only: the replay appended nothing.
    assert len(log.events) == events_before


def test_a_holdout_pack_and_its_feedback_reach_no_count() -> None:
    clean = replay_graph_selection(_selection_log(), graph_selection=_RW)
    held = replay_graph_selection(
        _selection_log(holdout_pack=True), graph_selection=_RW
    )

    assert held.model_dump() == clean.model_dump()
    assert held.attributed_packs == MIN_ATTRIBUTED_PACKS


def test_the_value_is_matched_exactly_not_by_prefix_or_case() -> None:
    log = _selection_log()

    # "seeded" is not a substring match for "caller_seeded", and vice versa.
    assert replay_graph_selection(log, graph_selection=_SD).matched_servings == 3
    assert replay_graph_selection(log, graph_selection="SEEDED").matched_servings == 0
    assert replay_graph_selection(log, graph_selection=f" {_CS} ").matched_servings == 2


def test_a_non_graph_item_is_never_matched() -> None:
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        log.append(_gs_pack_event(f"pack-{n}", [_gs_row(f"p{n}-sem", axis="semantic")]))
        log.append(_gs_feedback(f"pack-{n}", helpful=[f"p{n}-sem"]))

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 0
    assert report.tokens_freed == 0
    assert report.refilled_packs == 0
    assert any("graph_selection stamp at all" in note for note in report.notes)


def test_an_absent_value_says_nothing_was_eligible_not_nothing_found() -> None:
    log = _FakeEventLog()
    for n in range(MIN_ATTRIBUTED_PACKS):
        log.append(_gs_pack_event(f"pack-{n}", [_gs_row(f"p{n}-g", stamp=_SD)]))
        log.append(_gs_feedback(f"pack-{n}", helpful=[f"p{n}-g"]))

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 0
    assert any("nothing was eligible" in note for note in report.notes)


def test_a_thin_window_is_labelled_as_raw_tallies() -> None:
    thin = replay_graph_selection(_selection_log(), graph_selection=_RW, days=30)
    assert not any("minimum" in note for note in thin.notes)

    log = _FakeEventLog()
    log.append(_gs_pack_event("pack-0", [_gs_row("g0", stamp=_RW)]))
    log.append(_gs_feedback("pack-0", helpful=["g0"]))
    report = replay_graph_selection(log, graph_selection=_RW)
    assert report.attributed_packs == 1
    assert any("raw tallies" in note for note in report.notes)


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_value_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        replay_graph_selection(_FakeEventLog(), graph_selection=value)


def test_a_matched_serving_with_a_dedup_copy_on_another_axis_still_survives() -> None:
    log = _FakeEventLog()
    log.append(
        _gs_pack_event(
            "pack-0",
            [_gs_row("dup", stamp=_RW), _gs_row("sem", axis="semantic")],
            dedup_losers=[{"item_id": "dup", "axis": "semantic"}],
        )
    )
    log.append(_gs_feedback("pack-0", helpful=["sem"], unhelpful=["dup"]))

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 1
    assert report.still_served_by_other_axis == 1
    assert report.unhelpful_removed == 0
    assert report.tokens_freed == 0
    assert any("non-graph axis" in note for note in report.notes)


# -- the freed budget --------------------------------------------------------


def _refill_log(
    served: list[dict[str, Any]],
    turned_away: list[dict[str, Any]],
    *,
    max_tokens: int,
    helpful: list[str],
) -> _FakeEventLog:
    log = _FakeEventLog()
    log.append(
        _gs_pack_event("pack-0", served, turned_away=turned_away, max_tokens=max_tokens)
    )
    log.append(_gs_feedback("pack-0", helpful=helpful))
    return log


def test_the_freed_budget_admits_what_the_walk_had_turned_away() -> None:
    log = _refill_log(
        [_gs_row("rw", stamp=_RW), _gs_row("sem", axis="semantic")],
        [
            _gs_row("fat", 150, axis="semantic"),
            _gs_row("thin", 50, axis="keyword"),
        ],
        max_tokens=300,
        helpful=["sem"],
    )

    report = replay_graph_selection(log, graph_selection=_RW)

    # 100 + 100 + 150 overflowed 300 originally; without ``rw`` the walk
    # takes sem 100, fat 150 and thin 50, exactly 300.
    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0
    assert report.replacement_servings == 2
    assert report.replacement_tokens == 200
    assert report.replacement_by_strategy == {"semantic": 1, "keyword": 1}


def test_the_refill_stops_at_the_first_overflow_like_the_builder() -> None:
    log = _refill_log(
        [_gs_row("rw", stamp=_RW), _gs_row("sem", axis="semantic")],
        [
            _gs_row("huge", 250, axis="semantic"),
            _gs_row("small", 10, axis="keyword"),
        ],
        max_tokens=300,
        helpful=["sem"],
    )

    report = replay_graph_selection(log, graph_selection=_RW)

    # sem 100 + huge 250 overflows again; ``small`` would fit but sits behind
    # the overflow, which the builder's ``budget_exceeded`` latch never admits.
    assert report.refilled_packs == 1
    assert report.replacement_servings == 0
    assert report.replacement_tokens == 0


def test_an_unserved_graph_candidate_of_the_withheld_value_is_not_a_replacement() -> (
    None
):
    log = _refill_log(
        [_gs_row("rw", stamp=_RW), _gs_row("sem", axis="semantic")],
        [
            _gs_row("rw-extra", 100, axis="graph"),
            _gs_row("tail", 100, axis="semantic"),
        ],
        max_tokens=250,
        helpful=["sem"],
    )

    report = replay_graph_selection(log, graph_selection=_RW)

    # The pack's served graph rows all say recency_window, so the turned-away
    # graph candidate was chosen the same way and withhold drops it too.
    assert report.unclassified_graph_candidates == 0
    assert report.replacement_by_strategy == {"semantic": 1}


def test_an_unserved_graph_candidate_is_left_out_when_the_pack_mixes_values() -> None:
    log = _refill_log(
        [
            _gs_row("rw", stamp=_RW),
            _gs_row("sd", stamp=_SD),
        ],
        [
            _gs_row("unknown", 100, axis="graph"),
            _gs_row("tail", 50, axis="semantic"),
        ],
        max_tokens=250,
        helpful=["sd"],
    )

    report = replay_graph_selection(log, graph_selection=_RW)

    # Only served rows record a stamp and the two disagree, so the graph
    # candidate's selection is unknowable: counted, not guessed at.
    assert report.unclassified_graph_candidates == 1
    assert report.replacement_by_strategy == {"semantic": 1}
    assert any("left out of the re-run" in note for note in report.notes)


def test_a_pack_with_no_budget_trace_keeps_its_counts_but_not_a_refill() -> None:
    log = _FakeEventLog()
    log.append(
        _gs_pack_event(
            "pack-0",
            [_gs_row("rw", stamp=_RW), _gs_row("sem", axis="semantic")],
            with_trace=False,
        )
    )
    log.append(_gs_feedback("pack-0", helpful=["sem"], unhelpful=["rw"]))

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.unhelpful_removed == 1
    assert report.tokens_freed == 100
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert any("could not have their two-stage budget" in n for n in report.notes)


def test_a_walk_that_does_not_reproduce_the_record_is_not_trusted() -> None:
    # The pack served 200 tokens under a recorded 150-token budget, as a
    # safety margin or graduated disclosure would leave it; a re-run that
    # cannot reproduce that walk has no standing to say what comes next.
    log = _refill_log(
        [_gs_row("rw", stamp=_RW), _gs_row("sem", axis="semantic")],
        [_gs_row("tail", 20, axis="semantic")],
        max_tokens=150,
        helpful=["sem"],
    )

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 1
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert report.replacement_servings == 0


# -- the same thing through the real builder ----------------------------------


def _graph_item(item_id: str, score: float, stamp: str) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt=f"{item_id} distinct body words here " * 6,
        relevance_score=score,
        metadata={"source_strategy": "graph", "graph_selection": stamp},
    )


def _semantic_item(item_id: str, score: float) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=f"{item_id} distinct body words here " * 6,
        relevance_score=score,
        metadata={"source_strategy": "semantic"},
    )


def test_a_real_builder_pack_reproduces_and_refills(tmp_path: Path) -> None:
    """Replay over an event the real ``PackBuilder`` wrote, not a fixture.

    A fixture can agree with the replay about a field the builder does not
    write. Here the two graph items carry a stamp, the budget turns two
    semantic candidates away, and the replay must read the stamp from the
    event, reproduce the recorded walk from ``budget_trace`` and
    ``budget_max_tokens``, and admit the turned-away candidates once the
    recency-window servings are gone.
    """
    strategies = [
        _axis_strategy(
            "graph",
            [
                _graph_item("g1", 0.95, _RW),
                _graph_item("g2", 0.94, _RW),
            ],
        ),
        _axis_strategy(
            "semantic",
            [
                _semantic_item("s1", 0.9),
                _semantic_item("s2", 0.8),
                _semantic_item("s3", 0.7),
            ],
        ),
    ]
    probe = PackBuilder(
        strategies=strategies, content_floor=ContentFloorConfig(mode="off")
    ).build("q", budget=PackBudget(max_items=50, max_tokens=100_000))
    per_item = probe.items[0].estimated_tokens or 0
    assert per_item > 0

    log = SQLiteEventLog(tmp_path / "events.db")
    builder = PackBuilder(
        strategies=strategies,
        event_log=log,
        content_floor=ContentFloorConfig(mode="off"),
    )
    # Room for three items: g1, g2 and the best semantic one are served.
    pack = builder.build(
        "q", budget=PackBudget(max_items=50, max_tokens=per_item * 3 + 5)
    )
    served = [item.item_id for item in pack.items]
    assert served == ["g1", "g2", "s1"]

    event = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)[0]
    pack_id = event.payload.get("pack_id") or event.entity_id
    log.append(_gs_feedback(pack_id, helpful=["g1", "s1"], unhelpful=["g2"]))

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 2
    assert report.helpful_lost == 1
    assert report.unhelpful_removed == 1
    assert report.tokens_freed == 2 * per_item
    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0
    assert report.replacement_by_strategy == {"semantic": 2}
    assert report.replacement_tokens == 2 * per_item


# -- the max_items cut and the effective budget ----------------------------------


def _cut_log(
    *,
    max_tokens: float = 10_000,
    cut_tokens: int = 100,
    price_cut_rows: bool = True,
    max_items: int | None = 3,
) -> _FakeEventLog:
    """A pack that served three rows and lost two more at the ``max_items`` cut."""
    log = _FakeEventLog()
    log.append(
        _gs_pack_event(
            "pack-0",
            [
                _gs_row("rw", stamp=_RW),
                _gs_row("sem-1", axis="semantic"),
                _gs_row("sem-2", axis="semantic"),
            ],
            cut_by_items=[
                _gs_row("cut-1", cut_tokens, axis="semantic"),
                _gs_row("cut-2", axis="keyword"),
            ],
            max_items=max_items,
            price_cut_rows=price_cut_rows,
            max_tokens=max_tokens,
        )
    )
    log.append(_gs_feedback("pack-0", helpful=["sem-1"]))
    return log


def test_a_freed_item_slot_admits_what_the_max_items_cut_turned_away() -> None:
    report = replay_graph_selection(_cut_log(), graph_selection=_RW)

    # Three slots, and ``rw`` held one of them: without it the cut that had
    # turned ``cut-1`` and ``cut-2`` away lets exactly one back in. The token
    # budget is nowhere near binding, so this is the item stage alone.
    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0
    assert report.replacement_servings == 1
    assert report.replacement_tokens == 100
    assert report.replacement_by_strategy == {"semantic": 1}


def test_a_freed_slot_still_has_to_fit_the_token_budget() -> None:
    # ``cut-1`` takes the freed slot but 100 + 100 + 150 overflows 300; the
    # walk stops there, and ``cut-2`` behind it is past the item cut anyway.
    log = _cut_log(max_tokens=300, cut_tokens=150)

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.refilled_packs == 1
    assert report.replacement_servings == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"max_items": None}, "budget_max_items"),
        ({"price_cut_rows": False}, "token cost the event does not record"),
    ],
)
def test_a_cut_that_cannot_be_re_run_is_unmodelled_not_zero(
    kwargs: dict[str, Any], reason: str
) -> None:
    report = replay_graph_selection(_cut_log(**kwargs), graph_selection=_RW)

    # Reporting 0 replacements here would be a claim the data cannot back.
    assert report.matched_servings == 1
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert report.replacement_servings == 0
    assert any(reason in note for note in report.notes)
    # The walk itself was fine; it is the item cut that cannot be re-run, so
    # the caveat must not say the token walk could not be.
    assert not any("token walk could not" in note for note in report.notes)
    assert any(
        "two-stage budget (item cut, then token walk)" in n for n in report.notes
    )


def _with_trace(trace: list[Any]) -> _FakeEventLog:
    event = _gs_pack_event(
        "pack-0", [_gs_row("rw", stamp=_RW), _gs_row("sem", axis="semantic")]
    )
    event.payload["budget_trace"] = trace
    log = _FakeEventLog()
    log.append(event)
    log.append(_gs_feedback("pack-0", helpful=["sem"]))
    return log


@pytest.mark.parametrize(
    "trace",
    [
        # Nothing usable in it: an empty walk "reproduces" an empty record.
        ["not a step"],
        # The withheld serving is not in it: the re-run never saw what it removes.
        [
            {
                "item_id": "sem",
                "item_tokens": 100,
                "running_total": 100,
                "included": True,
            }
        ],
    ],
    ids=["empty_candidates", "withheld_id_missing"],
)
def test_a_reproduction_that_saw_nothing_is_not_a_reproduction(
    trace: list[Any],
) -> None:
    report = replay_graph_selection(_with_trace(trace), graph_selection=_RW)

    assert report.matched_servings == 1
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert any("budget_trace" in note for note in report.notes)


def _build_logged(
    log: SQLiteEventLog,
    strategies: list[SearchStrategy],
    budget: PackBudget,
    **builder_kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Build one pack through the real ``PackBuilder``; return its id and payload."""
    PackBuilder(
        strategies=strategies,
        event_log=log,
        content_floor=ContentFloorConfig(mode="off"),
        **builder_kwargs,
    ).build("q", budget=budget)
    event = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)[0]
    return event.payload.get("pack_id") or event.entity_id, event.payload


def _served(payload: dict[str, Any]) -> list[str]:
    return [row["item_id"] for row in payload.get("injected_items") or []]


def _max_items_bound_log(
    tmp_path: Path,
) -> tuple[SQLiteEventLog, str, dict[str, Any], set[str], dict[str, int]]:
    """A real pack where ``max_items`` is the binding cut, and the builder's answer.

    Returns the log holding the pack and its feedback, the pack id and
    payload, the items the builder serves once the recency-window rows are
    gone but did not serve before (the counterfactual), and each of those
    items' cost as the counterfactual build charged it.
    """
    graph = _axis_strategy(
        "graph", [_graph_item("g1", 0.95, _RW), _graph_item("g2", 0.94, _RW)]
    )
    semantic = _axis_strategy(
        "semantic",
        [
            _semantic_item("s1", 0.9),
            _semantic_item("s2", 0.8),
            _semantic_item("s3", 0.7),
        ],
    )
    budget = PackBudget(max_items=3, max_tokens=100_000)
    log = SQLiteEventLog(tmp_path / "events.db")
    pack_id, payload = _build_logged(log, [graph, semantic], budget)
    assert _served(payload) == ["g1", "g2", "s1"]
    assert [r["reason"] for r in payload["rejected_items"]] == ["max_items"] * 2
    log.append(_gs_feedback(pack_id, helpful=["g1"], unhelpful=["g2"]))
    _, cf_payload = _build_logged(
        SQLiteEventLog(tmp_path / "cf.db"), [semantic], budget
    )
    expected = set(_served(cf_payload)) - set(_served(payload))
    assert expected == {"s2", "s3"}
    cost = {
        row["item_id"]: row["estimated_tokens"] for row in cf_payload["injected_items"]
    }
    return log, pack_id, payload, expected, cost


def test_a_real_max_items_bound_pack_replays_as_modelled_not_unpriced(
    tmp_path: Path,
) -> None:
    """The refill against the real builder when ``max_items`` is the binding cut.

    Until the builder priced its ``max_items`` rejections (#970) every such
    pack was refused as ``max_items_unpriced``, so the refill figure covered
    only token-bound packs. The counterfactual is the builder's own answer:
    the same candidates without the recency-window rows, and the replay, run
    over the event exactly as the builder wrote it, has to agree with it.
    """
    log, _, _, expected, cost = _max_items_bound_log(tmp_path)

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 2
    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0
    assert report.replacement_servings == len(expected)
    assert report.replacement_tokens == sum(cost[i] for i in expected)
    assert report.replacement_by_strategy == {"semantic": len(expected)}
    assert not any("could not have their two-stage budget" in n for n in report.notes)


def test_an_event_written_before_the_builder_priced_the_cut_stays_unpriced(
    tmp_path: Path,
) -> None:
    """The same pack with the field absent, as every older event reads."""
    _, pack_id, payload, _, _ = _max_items_bound_log(tmp_path)
    old_rows = [
        {k: v for k, v in row.items() if k != "estimated_tokens"}
        for row in payload["rejected_items"]
    ]
    assert all("estimated_tokens" in row for row in payload["rejected_items"])
    old_log = SQLiteEventLog(tmp_path / "old.db")
    old_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload={**payload, "rejected_items": old_rows},
        )
    )
    old_log.append(_gs_feedback(pack_id, helpful=["g1"], unhelpful=["g2"]))

    report = replay_graph_selection(old_log, graph_selection=_RW)

    assert report.matched_servings == 2
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert report.replacement_servings == 0
    assert any("max_items cut whose token cost" in note for note in report.notes)


def _sized_item(
    item_id: str, score: float, axis: str, repeats: int, stamp: str | None = None
) -> PackItem:
    meta: dict[str, Any] = {"source_strategy": axis}
    if stamp:
        meta["graph_selection"] = stamp
    return PackItem(
        item_id=item_id,
        item_type="entity" if axis == "graph" else "document",
        excerpt=f"{item_id} distinct body words here " * repeats,
        relevance_score=score,
        metadata=meta,
    )


def _disclosed_max_items_log(
    tmp_path: Path,
    *,
    disclosure: DisclosureConfig | None,
    n_served: int,
    admits: bool,
) -> tuple[SQLiteEventLog, str, dict[str, Any], set[str], dict[str, int]]:
    """A real ``max_items``-bound pack whose tail graduated disclosure demotes.

    ``g1`` (recency window) and ``n_served`` semantic rows are served; two long
    semantic rows are cut at ``max_items``. The token budget sits one token
    short of what the builder's own counterfactual (no ``g1``) walks over the
    rows ``s1..sN`` plus the first cut row, or exactly at it when ``admits``.
    Disclosure re-prices the demoted ``s`` rows in ``budget_trace`` to
    pointers, so a replay that sums those against the cut row's walk-time
    price frees more budget than the pack ever paid.

    Returns the log, pack id and payload, the items the builder's own
    counterfactual serves that the pack did not, and each row's walk charge.
    """
    graph = _axis_strategy("graph", [_sized_item("g1", 0.95, "graph", 6, _RW)])
    ids = [f"s{i}" for i in range(1, n_served + 1)]
    semantic = _axis_strategy(
        "semantic",
        [
            *(_sized_item(i, 0.9 - n * 0.01, "semantic", 6) for n, i in enumerate(ids)),
            _sized_item("c1", 0.5, "semantic", 20),
            _sized_item("c2", 0.4, "semantic", 20),
        ],
    )
    # Disclosure off prices every row at the walk's own charge.
    probe = _build_logged(
        SQLiteEventLog(tmp_path / "probe.db"),
        [graph, semantic],
        PackBudget(max_items=100, max_tokens=1_000_000),
        disclosure=DISCLOSURE_OFF,
    )[1]
    walk = {r["item_id"]: r["estimated_tokens"] for r in probe["injected_items"]}
    cf_walk = sum(walk[i] for i in ids) + walk["c1"]
    budget = PackBudget(
        max_items=n_served + 1, max_tokens=cf_walk if admits else cf_walk - 1
    )

    log = SQLiteEventLog(tmp_path / "events.db")
    pack_id, payload = _build_logged(
        log, [graph, semantic], budget, disclosure=disclosure
    )
    # The preconditions that make this the failing shape.
    assert _served(payload) == ["g1", *ids]
    assert [r["item_id"] for r in payload["rejected_items"]] == ["c1", "c2"]
    assert payload["token_budget_effective"] == budget.max_tokens
    assert payload["disclosure"]["pointer_item_ids"]
    charged = {
        s["item_id"]: (s["item_tokens"], s.get("walk_tokens"))
        for s in payload["budget_trace"]
    }
    assert sum(charged[i][0] for i in ids) < sum(walk[i] for i in ids)
    assert all(charged[i][1] in (None, walk[i]) for i in ids)
    log.append(_gs_feedback(pack_id, helpful=["s1"], unhelpful=["g1"]))
    _, cf_payload = _build_logged(
        SQLiteEventLog(tmp_path / "cf.db"), [semantic], budget, disclosure=disclosure
    )
    expected = set(_served(cf_payload)) - set(_served(payload))
    assert expected == ({"c1"} if admits else set())
    return log, pack_id, payload, expected, walk


_DISCLOSED_SHAPES = pytest.mark.parametrize(
    ("disclosure", "n_served"),
    [
        # One body, two pointers: the smallest pack disclosure re-prices.
        (DisclosureConfig(body_items=1), 2),
        # The shipped default (``DEFAULT_BODY_ITEMS`` is 12) with a tail past it.
        (None, 13),
    ],
    ids=["body_items_1", "defaults"],
)


@_DISCLOSED_SHAPES
@pytest.mark.parametrize("admits", [False, True], ids=["cut_row_stays_out", "admitted"])
def test_a_disclosed_max_items_bound_pack_refills_as_the_builder_does(
    tmp_path: Path,
    disclosure: DisclosureConfig | None,
    n_served: int,
    admits: bool,
) -> None:
    """Graduated disclosure re-prices ``budget_trace`` after the walk (PR #976).

    The trace then holds pointer prices for the demoted rows while a
    ``max_items``-cut row is priced at the walk's full charge. A refill that
    summed the mix freed more budget than the withheld row ever did and
    admitted a row the builder, given the same candidates without it, turns
    away. The replay has to land where the builder's own counterfactual
    build does, in both directions.
    """
    log, _, payload, expected, walk = _disclosed_max_items_log(
        tmp_path, disclosure=disclosure, n_served=n_served, admits=admits
    )
    assert any("walk_tokens" in step for step in payload["budget_trace"])

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.matched_servings == 1
    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0
    assert report.replacement_servings == len(expected)
    assert report.replacement_tokens == sum(walk[i] for i in expected)


def test_a_disclosed_pack_written_before_walk_tokens_is_unmodelled(
    tmp_path: Path,
) -> None:
    """The same pack as an older build wrote it: trace re-priced, walk charge gone.

    The walk cannot be rebuilt from pointer prices, so the pack is refused
    for the reason that names it rather than walked and over-admitted.
    """
    _, pack_id, payload, _, _ = _disclosed_max_items_log(
        tmp_path, disclosure=DisclosureConfig(body_items=1), n_served=2, admits=False
    )
    old_trace = [
        {k: v for k, v in step.items() if k != "walk_tokens"}
        for step in payload["budget_trace"]
    ]
    old_log = SQLiteEventLog(tmp_path / "old.db")
    old_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload={**payload, "budget_trace": old_trace},
        )
    )
    old_log.append(_gs_feedback(pack_id, helpful=["s1"], unhelpful=["g1"]))

    report = replay_graph_selection(old_log, graph_selection=_RW)

    assert report.matched_servings == 1
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert report.replacement_servings == 0
    assert any("graduated disclosure" in note for note in report.notes)


def test_a_malformed_disclosure_block_is_not_trusted() -> None:
    """``pointer_item_ids`` that is not a list cannot be told from a re-priced trace."""
    log = _cut_log()
    log.events[0].payload["disclosure"] = {"mode": "graduated", "pointer_item_ids": 3}

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert any("graduated disclosure" in note for note in report.notes)


def test_the_policy_replay_prices_a_disclosed_pack_at_the_walk_charge(
    tmp_path: Path,
) -> None:
    """``_price`` read a pointer's price as the body it stands for.

    A policy that serves every admitted row as a body (``body_items=None``)
    must cost what the same builder charges with disclosure off.
    """
    strategies = [
        _axis_strategy(
            "semantic",
            [_sized_item(f"s{i}", 0.9 - i * 0.01, "semantic", 6) for i in range(14)],
        )
    ]
    budget = PackBudget(max_items=50, max_tokens=1_000_000)
    log = SQLiteEventLog(tmp_path / "events.db")
    pack_id, payload = _build_logged(log, strategies, budget)
    assert payload["disclosure"]["pointer_item_ids"]
    log.append(_gs_feedback(pack_id, helpful=["s0"]))
    _, off = _build_logged(
        SQLiteEventLog(tmp_path / "off.db"),
        strategies,
        budget,
        disclosure=DISCLOSURE_OFF,
    )

    report = replay_pack_value(log, policy=ReplayPolicy())

    assert report.counterfactual.items == len(_served(off))
    assert report.counterfactual.injected_tokens == sum(
        r["estimated_tokens"] for r in off["injected_items"]
    )
    # The baseline is what was paid: pointers at their pointer price.
    assert report.baseline.injected_tokens == sum(
        r["estimated_tokens"] for r in payload["injected_items"]
    )
    assert report.baseline.injected_tokens < report.counterfactual.injected_tokens


def _disclosed_pack(
    tmp_path: Path, *, helpful: list[str] | None = None
) -> tuple[SQLiteEventLog, dict[str, Any], list[str]]:
    """A real flat pack whose last two rows graduated disclosure demoted (#978).

    Fourteen rows against the default of twelve bodies. Returns the log (the
    pack and one feedback event naming ``helpful``, default the first body
    and the last pointer), the payload and the ids served as pointers.
    """
    strategies = [
        _axis_strategy(
            "semantic",
            [_sized_item(f"s{i}", 0.9 - i * 0.01, "semantic", 6) for i in range(14)],
        )
    ]
    log = SQLiteEventLog(tmp_path / "events.db")
    pack_id, payload = _build_logged(
        log, strategies, PackBudget(max_items=50, max_tokens=1_000_000)
    )
    pointers = payload["disclosure"]["pointer_item_ids"]
    assert pointers == ["s12", "s13"]
    log.append(_gs_feedback(pack_id, helpful=helpful or ["s0", pointers[-1]]))
    return log, payload, pointers


def test_the_baseline_counts_a_disclosed_pack_s_pointers_as_pointers(
    tmp_path: Path,
) -> None:
    """The as-served arm marked every row a body (#978).

    A pack that served two rows as graduated-disclosure pointers reported
    ``pointer_items_served=0`` and all fourteen under ``body_items_served``,
    while the counterfactual arm of the same replay counted pointers as
    pointers. ``disclosure.pointer_item_ids`` is the event's record of which.
    """
    log, payload, pointers = _disclosed_pack(tmp_path)

    report = replay_pack_value(log, policy=ReplayPolicy())

    assert report.baseline.items == len(payload["injected_items"]) == 14
    assert report.baseline.pointer_items_served == len(pointers) == 2
    assert report.baseline.body_items_served == 12
    # The arms agree on what a pointer is, so an empty policy keeps the split.
    assert report.counterfactual.pointer_items_served == 0
    assert report.counterfactual.body_items_served == 14


def test_a_helpful_item_served_only_as_a_pointer_adds_no_baseline_helpful_tokens(
    tmp_path: Path,
) -> None:
    """A pointer delivers an id, not substance (the arm's own rule, #978).

    ``s0`` carries a body and ``s13`` a pointer, and both are cited helpful.
    Only the body's tokens are helpful tokens; the pointer's tokens stay in
    the injected total and fall to unjudged-or-unhelpful, never helpful.
    """
    log, payload, _ = _disclosed_pack(tmp_path)
    paid = {r["item_id"]: r["estimated_tokens"] for r in payload["injected_items"]}

    report = replay_pack_value(log, policy=ReplayPolicy())

    assert report.baseline.helpful_tokens == paid["s0"]
    assert report.baseline.helpful_tokens < paid["s0"] + paid["s13"]
    assert report.baseline.injected_tokens == sum(paid.values())
    assert report.baseline.unjudged_tokens == sum(paid.values()) - paid["s0"]


def test_a_pointer_only_helpful_pack_has_zero_baseline_helpful_tokens(
    tmp_path: Path,
) -> None:
    log, _, pointers = _disclosed_pack(tmp_path, helpful=["s12", "s13"])

    report = replay_pack_value(log, policy=ReplayPolicy())

    assert report.baseline.helpful_tokens == 0
    assert report.baseline.unjudged_tokens == report.baseline.injected_tokens
    assert report.baseline.pointer_items_served == len(pointers)


@pytest.mark.parametrize(
    "disclosure",
    [None, {}, {"mode": "graduated", "pointer_item_ids": 3}],
    ids=["absent", "empty", "malformed"],
)
def test_a_pack_with_no_readable_pointer_list_keeps_an_all_body_baseline(
    disclosure: object,
) -> None:
    """No record of a demotion is read as none, as the baseline always read it."""
    log = _FakeEventLog()
    pack = _pack_event("pack-0", [(f"i{k}", 100) for k in range(3)])
    if disclosure is not None:
        pack.payload["disclosure"] = disclosure
    log.append(pack)
    log.append(_feedback_event("pack-0", ["i2"]))

    report = replay_pack_value(log, policy=ReplayPolicy())

    assert report.baseline.pointer_items_served == 0
    assert report.baseline.body_items_served == 3
    assert report.baseline.helpful_tokens == 100


def test_a_real_pack_under_a_safety_margin_walks_the_effective_budget(
    tmp_path: Path,
) -> None:
    """``token_budget_safety_margin`` shrinks the walk's budget below ``max_tokens``.

    The builder records that as ``token_budget_effective``. A refill that
    walked ``budget_max_tokens`` instead would admit a candidate the builder,
    given the same candidates without the graph row, turns away.
    """

    def item(item_id: str, score: float, words: int, axis: str) -> PackItem:
        meta: dict[str, Any] = {"source_strategy": axis}
        if axis == "graph":
            meta["graph_selection"] = _RW
        return PackItem(
            item_id=item_id,
            item_type="document",
            excerpt=" ".join(f"{item_id}w{i}" for i in range(words)),
            relevance_score=score,
            metadata=meta,
        )

    graph = _axis_strategy("graph", [item("g1", 0.95, 50, "graph")])
    semantic = _axis_strategy("semantic", [item("s1", 0.9, 90, "semantic")])
    probe = PackBuilder(
        strategies=[graph, semantic], content_floor=ContentFloorConfig(mode="off")
    ).build("q", budget=PackBudget(max_items=50, max_tokens=1_000_000))
    tokens = {i.item_id: i.estimated_tokens for i in probe.items}
    # s1 fits the nominal budget and not the 20%-reserved one, and g1 + s1
    # fits neither, so the real pack serves g1 alone.
    max_tokens = int(tokens["s1"] / 0.9) + 1
    budget = PackBudget(max_items=50, max_tokens=max_tokens)
    margin = {"token_budget_safety_margin": 0.2}
    log = SQLiteEventLog(tmp_path / "events.db")
    pack_id, payload = _build_logged(log, [graph, semantic], budget, **margin)
    assert payload["token_budget_effective"] < max_tokens
    assert tokens["s1"] <= max_tokens
    assert tokens["s1"] > payload["token_budget_effective"]
    assert _served(payload) == ["g1"]
    log.append(_gs_feedback(pack_id, helpful=["g1"]))
    _, cf_payload = _build_logged(
        SQLiteEventLog(tmp_path / "cf.db"), [semantic], budget, **margin
    )
    expected = set(_served(cf_payload)) - set(_served(payload))

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0
    assert report.replacement_servings == len(expected) == 0


# -- one budget for every arm, and what counts as a recorded one -----------------


def _arms_log(
    *, nominal: float, effective: float | None, recorded_effective: bool = True
) -> _FakeEventLog:
    """One pack, three rows served at 100 tokens each and two turned away.

    Against a 300-token walk the pack serves exactly the first three, so a
    replay that walks any bigger budget admits the turned-away rows too.
    """
    event = _gs_pack_event(
        "pack-0",
        [
            _gs_row("rw", stamp=_RW),
            _gs_row("sem-1", axis="semantic"),
            _gs_row("sem-2", axis="semantic"),
        ],
        turned_away=[
            _gs_row("away-1", axis="semantic"),
            _gs_row("away-2", axis="semantic"),
        ],
        max_tokens=nominal,
    )
    if recorded_effective:
        event.payload["token_budget_effective"] = effective
    log = _FakeEventLog()
    log.append(event)
    log.append(_gs_feedback("pack-0", helpful=["sem-1"]))
    return log


@pytest.mark.parametrize(
    ("nominal", "effective", "recorded_effective"),
    [
        # A safety margin: the walk ran against 300 of a nominal 10,000.
        (10_000, 300, True),
        # An event from before ``token_budget_effective``: the nominal budget
        # is the only one on record, and it is the walk's.
        (300, None, False),
    ],
    ids=["effective_budget", "nominal_fallback"],
)
def test_both_replay_arms_walk_the_same_budget(
    nominal: float, effective: float | None, recorded_effective: bool
) -> None:
    """``replay_pack_value`` used to read ``budget_max_tokens`` alone.

    Under a margin that is 10,000 against a walk of 300, so its refill
    admitted both turned-away rows while ``replay_graph_selection`` admitted
    one: two arms of one replay disagreeing about what the pack could spend.
    """
    log = _arms_log(
        nominal=nominal, effective=effective, recorded_effective=recorded_effective
    )

    value = replay_pack_value(log, policy=ReplayPolicy())
    selection = replay_graph_selection(log, graph_selection=_RW)

    # Served as 3, and a no-op policy re-admits exactly those 3.
    assert value.baseline.items == 3
    assert value.counterfactual.items == 3
    assert value.admitted_ungraded_items == 0
    # ``rw`` frees 100 of the 300, which readmits one row and not two.
    assert selection.refilled_packs == 1
    assert selection.replacement_servings == 1


@pytest.mark.parametrize("budget", [10_000.0, 10_000], ids=["integral_float", "int"])
def test_an_integral_float_budget_is_a_recorded_budget(budget: float) -> None:
    report = replay_graph_selection(_cut_log(max_tokens=budget), graph_selection=_RW)

    assert report.refilled_packs == 1
    assert report.unmodelled_packs == 0


@pytest.mark.parametrize(
    "budget",
    [10_000.5, float("nan"), float("inf")],
    ids=["fractional", "nan", "inf"],
)
def test_a_budget_that_is_not_a_whole_number_is_refused_not_truncated(
    budget: float,
) -> None:
    """``int()`` would have replayed ``10_000.5`` as 10,000 and raised on ``inf``."""
    report = replay_graph_selection(_cut_log(max_tokens=budget), graph_selection=_RW)

    assert report.matched_servings == 1
    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert any("recorded no token budget" in note for note in report.notes)


def test_a_corrupt_effective_budget_does_not_fall_back_to_the_nominal_one() -> None:
    """The fallback is for an *absent* effective budget, not an unreadable one.

    The nominal budget is the larger of the two whenever a margin applies, so
    falling back from a corrupt effective value would silently walk a budget
    the pack never had.
    """
    log = _arms_log(nominal=10_000, effective=300.5)

    report = replay_graph_selection(log, graph_selection=_RW)

    assert report.refilled_packs == 0
    assert report.unmodelled_packs == 1
    assert any("recorded no token budget" in note for note in report.notes)
