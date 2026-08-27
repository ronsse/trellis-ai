"""Tests for served-context value density (``trellis.retrieve.pack_value``).

The measurement these guard is one this repo has repeatedly gotten wrong
in the same way: a ratio wired so it can only ever read one value. So the
first thing asserted is that ``useful_token_fraction`` *moves* — 0.0, a
middle value, and 1.0 are all reachable from real payload shapes — and
the rest guard the honesty rules (refusal below the minimum, sectioned
packs excluded with a count, uncited feedback kept out of the denominator).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from trellis.retrieve.pack_value import (
    MIN_ATTRIBUTED_PACKS,
    SUPPRESSED_THIN_SAMPLE,
    summarize_pack_value,
)
from trellis.stores.base.event_log import Event, EventLog, EventType


class _FakeEventLog(EventLog):
    """In-memory event log — same shape as the one in test_token_tracker."""

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
    ) -> list[Event]:
        result = self.events
        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        if entity_id is not None:
            result = [e for e in result if e.entity_id == entity_id]
        if since is not None:
            result = [e for e in result if e.occurred_at >= since]
        return result[:limit]

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        return len(self.get_events(event_type=event_type, since=since))

    def close(self) -> None:
        pass


def _emit_pack(
    log: _FakeEventLog,
    pack_id: str,
    items: list[tuple[str, int, str, str]],
    *,
    intent_family: str = "general_context",
) -> None:
    """Emit a flat PACK_ASSEMBLED. Items are ``(id, tokens, strategy, type)``."""
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "intent_family": intent_family,
            "injected_item_ids": [i[0] for i in items],
            "injected_items": [
                {
                    "item_id": item_id,
                    "estimated_tokens": tokens,
                    "strategy_source": strategy,
                    "item_type": item_type,
                    "rank": rank,
                }
                for rank, (item_id, tokens, strategy, item_type) in enumerate(items)
            ],
        },
    )


def _emit_sectioned_pack(log: _FakeEventLog, pack_id: str) -> None:
    """A ``build_sectioned`` pack: sections, and no ``injected_items[]``."""
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={"intent": "x", "section_count": 2, "sections": [], "total_items": 4},
    )


def _emit_feedback(
    log: _FakeEventLog,
    pack_id: str | None,
    *,
    helpful: list[str] | None = None,
    unhelpful: list[str] | None = None,
    intent_family: str = "general_context",
) -> None:
    payload: dict[str, Any] = {
        "helpful_item_ids": helpful or [],
        "unhelpful_item_ids": unhelpful or [],
        "intent_family": intent_family,
        "rating": 0.5,
        "success": True,
    }
    if pack_id is not None:
        payload["pack_id"] = pack_id
    log.emit(
        EventType.FEEDBACK_RECORDED, source="mcp", entity_id=pack_id, payload=payload
    )


def _populate(log: _FakeEventLog, count: int, *, helpful_per_pack: int = 1) -> None:
    """``count`` attributed packs, each 2 items of 100 tokens."""
    for index in range(count):
        pack_id = f"pack_{index}"
        _emit_pack(
            log,
            pack_id,
            [
                (f"{pack_id}_a", 100, "semantic", "vector"),
                (f"{pack_id}_b", 100, "keyword", "document"),
            ],
        )
        helpful = [f"{pack_id}_a", f"{pack_id}_b"][:helpful_per_pack]
        unhelpful = [i for i in (f"{pack_id}_a", f"{pack_id}_b") if i not in helpful]
        _emit_feedback(log, pack_id, helpful=helpful, unhelpful=unhelpful)


# ---------------------------------------------------------------------------
# The number must be able to move
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("helpful_per_pack", "expected"),
    [(0, 0.0), (1, 0.5), (2, 1.0)],
)
def test_useful_token_fraction_moves(helpful_per_pack: int, expected: float) -> None:
    """0.0, 0.5 and 1.0 are all reachable — the metric is not a constant."""
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS, helpful_per_pack=helpful_per_pack)
    report = summarize_pack_value(log, days=30)

    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS
    assert report.useful_token_fraction == expected


def test_fraction_tracks_token_weight_not_item_count() -> None:
    """A cited item's *size* moves the fraction, not just its existence."""
    log = _FakeEventLog()
    for index in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack_{index}"
        _emit_pack(
            log,
            pack_id,
            [
                (f"{pack_id}_big", 900, "semantic", "vector"),
                (f"{pack_id}_small", 100, "semantic", "vector"),
            ],
        )
        _emit_feedback(
            log,
            pack_id,
            helpful=[f"{pack_id}_small"],
            unhelpful=[f"{pack_id}_big"],
        )
    report = summarize_pack_value(log, days=30)
    # One of two items cited, but only a tenth of the tokens.
    assert report.useful_token_fraction == 0.1


# ---------------------------------------------------------------------------
# Refusal below the minimum
# ---------------------------------------------------------------------------


def test_refuses_ratio_below_minimum_attributed_packs() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS - 1)
    report = summarize_pack_value(log, days=30)

    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS - 1
    assert report.suppressed is True
    assert report.suppressed_reason == SUPPRESSED_THIN_SAMPLE
    # Refused, not zeroed — a reader must not mistake the two.
    assert report.useful_token_fraction is None
    assert report.unhelpful_token_fraction is None
    assert report.dollars_per_cited_item is None
    # The minimum is stated, and the raw evidence survives.
    assert report.min_attributed_packs == MIN_ATTRIBUTED_PACKS
    assert report.injected_tokens > 0
    assert any("below the" in note for note in report.notes)


def test_states_ratio_at_exactly_the_minimum() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    report = summarize_pack_value(log, days=30)

    assert report.suppressed is False
    assert report.useful_token_fraction is not None
    assert report.dollars_per_cited_item is not None


def test_axis_cells_are_suppressed_independently() -> None:
    """A stated headline does not license a thin per-axis cell."""
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    _emit_pack(
        log,
        "rare",
        [("rare_a", 100, "graph", "entity")],
        intent_family="asset_generation",
    )
    _emit_feedback(log, "rare", helpful=["rare_a"], intent_family="asset_generation")
    report = summarize_pack_value(log, days=30)

    assert report.suppressed is False
    families = {cell.key: cell for cell in report.by_intent_family}
    assert families["general_context"].suppressed is False
    rare = families["asset_generation"]
    assert rare.attributed_packs == 1
    assert rare.suppressed is True
    assert rare.suppressed_reason == SUPPRESSED_THIN_SAMPLE
    assert rare.useful_token_fraction is None
    # Counts still reported so the operator sees the evidence.
    assert rare.injected_tokens == 100
    assert rare.helpful_tokens == 100


# ---------------------------------------------------------------------------
# Structural exclusions, stated rather than silent
# ---------------------------------------------------------------------------


def test_sectioned_packs_excluded_and_counted() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    _emit_sectioned_pack(log, "sectioned_1")
    _emit_sectioned_pack(log, "sectioned_2")
    _emit_feedback(log, "sectioned_1", helpful=["whatever"])
    report = summarize_pack_value(log, days=30)

    assert report.sectioned_packs_excluded == 2
    assert report.flat_packs == MIN_ATTRIBUTED_PACKS
    # Citing a sectioned pack cannot contribute per-item rows.
    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS
    assert report.pack_targeted_unjoined == 1
    assert any("sectioned" in note for note in report.notes)


def test_uncited_pack_targeted_feedback_excluded_from_denominator() -> None:
    """Zero citations is absent signal, not evidence of a useless pack."""
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS, helpful_per_pack=2)
    _emit_pack(log, "ungraded", [("ungraded_a", 5000, "semantic", "vector")])
    _emit_feedback(log, "ungraded")  # names the pack, cites nothing

    report = summarize_pack_value(log, days=30)
    assert report.pack_targeted_feedback == MIN_ATTRIBUTED_PACKS + 1
    assert report.pack_targeted_uncited == 1
    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS
    # The 5000 uncited tokens never entered the denominator.
    assert report.injected_tokens == MIN_ATTRIBUTED_PACKS * 200
    assert report.useful_token_fraction == 1.0


def test_untargeted_feedback_ignored() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    for _ in range(20):
        _emit_feedback(log, None, helpful=["something"])
    report = summarize_pack_value(log, days=30)

    assert report.feedback_events == MIN_ATTRIBUTED_PACKS + 20
    assert report.pack_targeted_feedback == MIN_ATTRIBUTED_PACKS
    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS


# ---------------------------------------------------------------------------
# Buckets and hygiene
# ---------------------------------------------------------------------------


def test_unjudged_tokens_are_their_own_bucket() -> None:
    """Uncited is not unhelpful — the headline is a lower bound."""
    log = _FakeEventLog()
    for index in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack_{index}"
        _emit_pack(
            log,
            pack_id,
            [
                (f"{pack_id}_a", 100, "semantic", "vector"),
                (f"{pack_id}_b", 100, "semantic", "vector"),
                (f"{pack_id}_c", 100, "semantic", "vector"),
            ],
        )
        _emit_feedback(
            log, pack_id, helpful=[f"{pack_id}_a"], unhelpful=[f"{pack_id}_b"]
        )
    report = summarize_pack_value(log, days=30)

    assert report.helpful_tokens == 500
    assert report.unhelpful_tokens == 500
    assert report.unjudged_tokens == 500
    assert report.useful_token_fraction == pytest.approx(1 / 3, abs=1e-4)
    assert report.unjudged_token_fraction == pytest.approx(1 / 3, abs=1e-4)
    assert any("lower bound" in note for note in report.notes)


def test_cited_ids_not_served_are_counted() -> None:
    """A malformed citation deflates the numerator; surface it."""
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    _emit_feedback(log, "pack_0", helpful=["entity:pack_0_a"])  # wrong prefix
    report = summarize_pack_value(log, days=30)

    assert report.cited_ids_not_served == 1
    assert any("not served" in note for note in report.notes)


def test_helpful_wins_over_contradictory_unhelpful() -> None:
    log = _FakeEventLog()
    for index in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"pack_{index}"
        _emit_pack(log, pack_id, [(f"{pack_id}_a", 100, "semantic", "vector")])
        _emit_feedback(
            log, pack_id, helpful=[f"{pack_id}_a"], unhelpful=[f"{pack_id}_a"]
        )
    report = summarize_pack_value(log, days=30)

    assert report.helpful_tokens == 500
    assert report.unhelpful_tokens == 0
    assert report.unjudged_tokens == 0


def test_multiple_graders_on_one_pack_union_not_double_count() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS - 1)
    _emit_pack(
        log,
        "shared",
        [("shared_a", 100, "semantic", "vector"), ("shared_b", 100, "graph", "entity")],
    )
    _emit_feedback(log, "shared", helpful=["shared_a"])
    _emit_feedback(log, "shared", helpful=["shared_b"])
    report = summarize_pack_value(log, days=30)

    assert report.attributed_packs == MIN_ATTRIBUTED_PACKS
    # 200 tokens counted once, both items credited.
    assert report.injected_tokens == (MIN_ATTRIBUTED_PACKS - 1) * 200 + 200


# ---------------------------------------------------------------------------
# The call-level join on the new TOKEN_TRACKED.pack_id
# ---------------------------------------------------------------------------


def test_response_token_join_reports_zero_coverage_without_pack_id() -> None:
    """Legacy TOKEN_TRACKED rows carry no pack_id; say so, don't guess."""
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    for _ in range(4):
        log.emit(
            EventType.TOKEN_TRACKED,
            source="mcp:get_context",
            payload={
                "layer": "mcp",
                "operation": "get_context",
                "response_tokens": 900,
            },
        )
    report = summarize_pack_value(log, days=30)

    assert report.response_events == 4
    assert report.response_events_with_pack_id == 0
    assert report.response_pack_id_coverage == 0.0
    assert report.response_tokens_attributed == 0
    assert report.response_dollars_per_cited_item is None
    assert any(
        "no token_tracked event carries a pack_id" in n.lower() for n in report.notes
    )


def test_response_token_join_prices_attributed_packs() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    for index in range(MIN_ATTRIBUTED_PACKS):
        log.emit(
            EventType.TOKEN_TRACKED,
            source="mcp:get_context",
            payload={
                "layer": "mcp",
                "operation": "get_context",
                "response_tokens": 300,
                "pack_id": f"pack_{index}",
            },
        )
    # A pack-free operation must not dilute the join.
    log.emit(
        EventType.TOKEN_TRACKED,
        source="mcp:get_graph",
        payload={"layer": "mcp", "operation": "get_graph", "response_tokens": 50},
    )
    report = summarize_pack_value(log, days=30, price_per_mtok=3.0)

    assert report.response_events == MIN_ATTRIBUTED_PACKS + 1
    assert report.response_events_with_pack_id == MIN_ATTRIBUTED_PACKS
    assert report.attributed_packs_with_response_tokens == MIN_ATTRIBUTED_PACKS
    assert report.response_tokens_attributed == 300 * MIN_ATTRIBUTED_PACKS
    assert report.response_dollars_per_cited_item is not None


def test_repeated_response_events_for_one_pack_count_once() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    for _ in range(3):
        log.emit(
            EventType.TOKEN_TRACKED,
            source="mcp:get_context",
            payload={
                "layer": "mcp",
                "operation": "get_context",
                "response_tokens": 400,
                "pack_id": "pack_0",
            },
        )
    report = summarize_pack_value(log, days=30)

    assert report.attributed_packs_with_response_tokens == 1
    assert report.response_tokens_attributed == 400


# ---------------------------------------------------------------------------
# Empty / degenerate
# ---------------------------------------------------------------------------


def test_empty_event_log_refuses_rather_than_reporting_zero() -> None:
    report = summarize_pack_value(_FakeEventLog(), days=30)

    assert report.attributed_packs == 0
    assert report.suppressed is True
    assert report.useful_token_fraction is None
    assert report.dollars_per_cited_item is None
    assert report.injected_tokens == 0


def test_dollars_per_cited_item_scales_with_price() -> None:
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    cheap = summarize_pack_value(log, days=30, price_per_mtok=3.0)
    dear = summarize_pack_value(log, days=30, price_per_mtok=30.0)

    assert cheap.dollars_per_cited_item is not None
    assert dear.dollars_per_cited_item is not None
    assert dear.dollars_per_cited_item == pytest.approx(
        cheap.dollars_per_cited_item * 10, rel=1e-6
    )


def test_never_describes_itself_as_benefit() -> None:
    """Naming discipline is load-bearing: precision served != benefit."""
    log = _FakeEventLog()
    _populate(log, MIN_ATTRIBUTED_PACKS)
    report = summarize_pack_value(log, days=30)
    blob = " ".join(report.notes).lower()

    assert "not benefit" in blob
    for field in report.model_dump():
        assert "benefit" not in field


def test_cost_is_what_the_walk_charged_not_the_excerpt_read_cost() -> None:
    """An index-mode or graduated pack serves something other than its excerpt.

    ``injected_items[].estimated_tokens`` deliberately keeps carrying the
    excerpt *read* cost in index mode (#305), so pricing an index pack from
    it charges a body the prompt never carried. ``budget_trace[]`` records
    what was actually spent, and that is what the fraction is computed over.
    """
    log = _FakeEventLog()
    for index in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"idx-{index}"
        log.append(
            Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "index_mode": True,
                    "injected_items": [
                        {
                            "item_id": f"{pack_id}-a",
                            "item_type": "vector",
                            "strategy_source": "semantic",
                            # Read cost of the body the index line stands for.
                            "estimated_tokens": 120,
                        },
                        {
                            "item_id": f"{pack_id}-b",
                            "item_type": "vector",
                            "strategy_source": "semantic",
                            "estimated_tokens": 120,
                        },
                    ],
                    "budget_trace": [
                        # What the index lines actually cost.
                        {
                            "item_id": f"{pack_id}-a",
                            "item_tokens": 30,
                            "running_total": 30,
                            "included": True,
                        },
                        {
                            "item_id": f"{pack_id}-b",
                            "item_tokens": 30,
                            "running_total": 60,
                            "included": True,
                        },
                    ],
                },
            )
        )
        log.append(
            Event(
                event_type=EventType.FEEDBACK_RECORDED,
                source="mcp",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "helpful_item_ids": [f"{pack_id}-a"],
                    "unhelpful_item_ids": [f"{pack_id}-b"],
                    "rating": 0.5,
                    "success": True,
                },
            )
        )

    report = summarize_pack_value(log)

    # 2 lines x 30 tokens x n packs — not 2 x 120.
    assert report.injected_tokens == 60 * MIN_ATTRIBUTED_PACKS
    assert report.helpful_tokens == 30 * MIN_ATTRIBUTED_PACKS
    assert report.useful_token_fraction == pytest.approx(0.5, abs=1e-4)


def test_a_pack_without_a_budget_trace_falls_back_to_estimated_tokens() -> None:
    """Every event written before budget_trace existed must still price."""
    log = _FakeEventLog()
    for index in range(MIN_ATTRIBUTED_PACKS):
        pack_id = f"old-{index}"
        log.append(
            Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "injected_items": [
                        {
                            "item_id": f"{pack_id}-a",
                            "item_type": "vector",
                            "strategy_source": "semantic",
                            "estimated_tokens": 100,
                        }
                    ],
                },
            )
        )
        log.append(
            Event(
                event_type=EventType.FEEDBACK_RECORDED,
                source="mcp",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "helpful_item_ids": [f"{pack_id}-a"],
                    "rating": 1.0,
                    "success": True,
                },
            )
        )

    report = summarize_pack_value(log)

    assert report.injected_tokens == 100 * MIN_ATTRIBUTED_PACKS
    assert report.useful_token_fraction == pytest.approx(1.0, abs=1e-4)
