"""The pack holdout at its one seam: ``PackBuilder.build`` / ``build_sectioned``.

Every pack-serving surface builds through
:func:`~trellis.retrieve.builder_factory.build_pack_builder`, the only
constructor of :class:`PackBuilder`, so the draw happens in the builder:
after the pack exists, because its ``pack_id`` is the draw's input, and
before anything leaves. A withheld pack reaches the caller empty, items
and advisories both (the whole pack is the treatment), and reads like a
pack that found nothing. Its ``PACK_ASSEMBLED`` row keeps the would-be
pack under separate keys, so no reader of ``injected_items`` counts a
withheld item as served and session dedup does not spend it.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import summarize_withheld
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import (
    Pack,
    PackBudget,
    PackItem,
    SectionedPack,
    SectionRequest,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog

RATE_ENV = "TRELLIS_PACK_HOLDOUT_RATE"

#: Four items with distinct bodies, spread scores and two affinities: not a
#: uniform fixture, so comparing a withheld row against a served control
#: can fail on order, on membership or on any per-item field.
_ITEM_SPECS = [
    ("doc-alpha", 0.91, "reference", "Rotate the signing key before the audit window."),
    ("doc-bravo", 0.74, "domain_knowledge", "The ingest worker retries with backoff."),
    ("doc-charlie", 0.52, "reference", "Schema migrations run one table at a time."),
    ("doc-delta", 0.33, "domain_knowledge", "Cache entries expire after idle minutes."),
]

#: ``max_items=3`` cuts ``doc-delta``, so the served control also withholds
#: something (#404) — which a withheld pack's own summary must not repeat.
_BUDGET = PackBudget(max_items=3, max_tokens=4000)

_SECTIONS = [
    SectionRequest(
        name="reference",
        retrieval_affinities=["reference"],
        max_items=2,
        max_tokens=900,
    ),
    SectionRequest(
        name="domain",
        retrieval_affinities=["domain_knowledge"],
        max_items=1,
        max_tokens=2600,
    ),
]

#: Fields that differ between any two builds whatever the arm.
_PER_BUILD = {"pack_id", "assembled_at", "created_at", "updated_at"}

#: The flat ``PACK_ASSEMBLED`` keys at the base this change was cut from
#: (58be23d4), read off ``PackBuilder._emit_telemetry`` by hand and checked
#: against a base-checkout probe. At a rate of 0 the holdout change added
#: only the two holdout keys; see ``_KEYS_ADDED_SINCE_BASE`` for what a
#: later change (``duration_ms``) added on top of that.
_BASE_FLAT_KEYS = frozenset(
    {
        "intent",
        "domain",
        "agent_id",
        "session_id",
        "run_id",
        "intent_family",
        "project",
        "items_count",
        "injected_item_ids",
        "injected_item_hashes",
        "injected_items",
        "strategies_used",
        "candidates_found",
        "budget_max_items",
        "budget_max_tokens",
        "rejected_items",
        "content_floor",
        "disclosure",
        "parent_concentration",
        "withholding",
        "budget_trace",
        "advisory_ids",
        "advisories_matched",
        "advisories_explored",
        "reranker",
        "semantic_dedup_enabled",
        "semantic_dedup_rejected",
        "strategy_failures",
        "meta_filtered_count",
        "index_mode",
        "token_counter",
        "token_budget_safety_margin",
        "token_budget_effective",
        "token_total_estimated",
    }
)

#: Same, for ``_emit_sectioned_telemetry``.
_BASE_SECTIONED_KEYS = frozenset(
    {
        "intent",
        "domain",
        "agent_id",
        "session_id",
        "run_id",
        "intent_family",
        "project",
        "section_count",
        "total_items",
        "injected_item_hashes",
        "sections",
        "advisory_ids",
        "advisories_matched",
        "advisories_explored",
        "reranker",
        "semantic_dedup_enabled",
        "semantic_dedup_rejected",
        "content_floor",
        "withholding",
        "strategy_failures",
        "meta_filtered_count",
        "token_counter",
        "token_budget_safety_margin",
        "token_budget_effective",
        "token_total_estimated",
    }
)

_HOLDOUT_KEYS = {"holdout", "holdout_rate"}

#: Keys added to both payloads since the ``_BASE_*_KEYS`` snapshot was taken,
#: by any change, not just this one: the holdout keys (this file's own
#: subject), plus ``duration_ms`` (real build latency, previously hard-coded
#: to ``0`` and emitted nowhere) and ``advisories_filtered_legacy``
#: (decision-ledger D-4, option B — how many matching advisories were
#: withheld for predating the #394 generator repair). ``_BASE_*_KEYS`` stays
#: an honest historical snapshot of 58be23d4 rather than being backfilled,
#: so a *later* addition extends this set, not the base one.
_KEYS_ADDED_SINCE_BASE = _HOLDOUT_KEYS | {"duration_ms", "advisories_filtered_legacy"}


def _items() -> list[PackItem]:
    return [
        PackItem(
            item_id=item_id,
            item_type="document",
            excerpt=body,
            relevance_score=score,
            metadata={"content_tags": {"retrieval_affinity": [affinity]}},
        )
        for item_id, score, affinity, body in _ITEM_SPECS
    ]


def _strategy(items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = "keyword"
    strategy.search.return_value = items
    return strategy


def _advisory(
    advisory_id: str, confidence: float, category: AdvisoryCategory
) -> Advisory:
    return Advisory(
        advisory_id=advisory_id,
        category=category,
        confidence=confidence,
        message=f"Synthetic advisory {advisory_id}",
        evidence=AdvisoryEvidence(
            sample_size=12,
            success_rate_with=0.8,
            success_rate_without=0.45,
            effect_size=0.35,
            evidence_confidence=1.0,
        ),
        scope="global",
    )


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def advisory_store(tmp_path: Path) -> AdvisoryStore:
    store = AdvisoryStore(tmp_path / "adv.json")
    store.put(_advisory("adv-entity", 0.82, AdvisoryCategory.ENTITY))
    store.put(_advisory("adv-approach", 0.61, AdvisoryCategory.APPROACH))
    store.put(_advisory("adv-scope", 0.37, AdvisoryCategory.SCOPE))
    return store


def _builder(
    event_log: SQLiteEventLog | None,
    advisory_store: AdvisoryStore | None,
    rate: float,
    items: list[PackItem] | None = None,
) -> PackBuilder:
    return PackBuilder(
        strategies=[_strategy(_items() if items is None else items)],
        event_log=event_log,
        advisory_store=advisory_store,
        holdout_rate=rate,
    )


def _payload(event_log: SQLiteEventLog, pack_id: str) -> dict[str, Any]:
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=5
    )
    assert len(events) == 1
    return dict(events[0].payload)


def _mask_duration(node: Any) -> Any:
    """``node`` with every ``duration_ms`` masked.

    It is real wall-clock elapsed time for that call's build, so two
    separately-executed builds (held vs. greenfield, or rate 0 vs. a rate
    whose draw still serves) are not expected to report the same value.
    """
    if isinstance(node, dict):
        return {
            key: "<elapsed>" if key == "duration_ms" else _mask_duration(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_mask_duration(value) for value in node]
    return node


def _comparable(pack: Pack | SectionedPack) -> dict[str, Any]:
    return _mask_duration(pack.model_dump(exclude=_PER_BUILD))


def _empty_withholding() -> dict[str, Any]:
    return summarize_withheld([], []).as_telemetry()


class TestConstruction:
    @pytest.mark.parametrize("rate", [-0.1, 1.1, math.nan, math.inf])
    def test_a_rate_outside_the_unit_interval_is_refused(self, rate: float) -> None:
        with pytest.raises(ValueError, match="holdout_rate"):
            PackBuilder(holdout_rate=rate)

    @pytest.mark.parametrize("rate", [0.0, 0.3, 1.0])
    def test_the_rate_is_readable(self, rate: float) -> None:
        assert PackBuilder(holdout_rate=rate).holdout_rate == rate

    def test_the_default_is_zero(self) -> None:
        assert PackBuilder().holdout_rate == 0.0


class TestFlatWithheld:
    def test_the_caller_gets_an_empty_pack_with_its_own_id(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        control = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET
        )
        held = _builder(event_log, advisory_store, 1.0).build(
            "rotate keys", budget=_BUDGET
        )

        # The control served a varied pack, advisories and a withholding note.
        assert [i.item_id for i in control.items] == [
            "doc-alpha",
            "doc-bravo",
            "doc-charlie",
        ]
        assert len(control.advisories) == 3
        assert control.metadata["withholding"] != _empty_withholding()

        assert held.items == []
        assert held.advisories == []
        assert held.pack_id
        assert held.pack_id != control.pack_id
        assert held.intent == control.intent
        assert held.budget == control.budget
        assert held.intent_family == control.intent_family
        assert held.metadata == {"withholding": _empty_withholding()}

    def test_it_is_indistinguishable_from_a_pack_that_found_nothing(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        held = _builder(event_log, advisory_store, 1.0).build(
            "rotate keys", budget=_BUDGET, session_id="sess-1", run_id="run-1"
        )
        greenfield = _builder(event_log, None, 0.0, items=[]).build(
            "rotate keys", budget=_BUDGET, session_id="sess-1", run_id="run-1"
        )
        assert greenfield.items == []
        assert _comparable(held) == _comparable(greenfield)

    def test_the_row_keeps_the_would_be_pack_under_its_own_keys(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        control = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET
        )
        held = _builder(event_log, advisory_store, 1.0).build(
            "rotate keys", budget=_BUDGET
        )
        served_row = _payload(event_log, control.pack_id)
        held_row = _payload(event_log, held.pack_id)

        assert held_row["holdout"] is True
        assert held_row["holdout_rate"] == 1.0
        assert served_row["holdout"] is False
        assert served_row["holdout_rate"] == 0.0

        # The would-be pack, under keys no served-set reader consults.
        assert len(served_row["injected_items"]) == 3
        assert held_row["holdout_items"] == served_row["injected_items"]
        assert served_row["advisory_ids"] == ["adv-entity", "adv-approach", "adv-scope"]
        assert held_row["holdout_advisory_ids"] == served_row["advisory_ids"]

        # What the agent received: nothing.
        assert held_row["injected_items"] == []
        assert held_row["injected_item_ids"] == []
        assert held_row["injected_item_hashes"] == {}
        assert held_row["items_count"] == 0
        assert held_row["advisory_ids"] == []
        assert held_row["advisories_matched"] == 0
        assert held_row["advisories_explored"] == []
        assert held_row["withholding"] == _empty_withholding()
        assert served_row["withholding"] != held_row["withholding"]
        assert held_row["rejected_items"] == []
        assert held_row["budget_trace"] == []
        assert held_row["candidates_found"] == 0
        assert held_row["token_total_estimated"] == 0

        # Process facts stay true: the build ran its axes against a budget.
        for key in ("strategies_used", "budget_max_items", "budget_max_tokens"):
            assert held_row[key] == served_row[key]

    def test_index_mode_withholds_the_same_way(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        held = _builder(event_log, advisory_store, 1.0).build(
            "rotate keys", budget=_BUDGET, index_mode=True
        )
        row = _payload(event_log, held.pack_id)
        assert held.items == []
        assert row["holdout"] is True
        assert row["index_mode"] is True
        assert [r["item_id"] for r in row["holdout_items"]] == [
            "doc-alpha",
            "doc-bravo",
            "doc-charlie",
        ]
        assert row["injected_items"] == []
        greenfield = _builder(event_log, None, 0.0, items=[]).build(
            "rotate keys", budget=_BUDGET, index_mode=True
        )
        natural = _payload(event_log, greenfield.pack_id)
        assert row["disclosure"] == natural["disclosure"]

    def test_the_arm_is_the_draw_on_the_packs_own_id(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        from trellis.core.pack_holdout import is_held_out

        builder = _builder(event_log, advisory_store, 0.5)
        arms = []
        for _ in range(40):
            pack = builder.build("rotate keys", budget=_BUDGET)
            row = _payload(event_log, pack.pack_id)
            expected = is_held_out(pack.pack_id, 0.5)
            assert row["holdout"] is expected
            assert row["holdout_rate"] == 0.5
            assert (pack.items == []) is expected
            assert ("holdout_items" in row) is expected
            arms.append(expected)
        # Both arms occur: all 40 in one arm has probability 2 * 2**-40.
        assert 0 < sum(arms) < len(arms)


class TestFlatAtRateZero:
    def test_only_keys_since_base_are_added(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        pack = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET
        )
        row = _payload(event_log, pack.pack_id)
        assert set(row) - _BASE_FLAT_KEYS == _KEYS_ADDED_SINCE_BASE
        assert set(row) >= _BASE_FLAT_KEYS
        assert row["holdout"] is False
        assert row["holdout_rate"] == 0.0
        assert [i["item_id"] for i in row["injected_items"]] == [
            "doc-alpha",
            "doc-bravo",
            "doc-charlie",
        ]

    def test_a_served_pack_is_the_same_at_any_rate(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        zero = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET
        )
        tiny = _builder(event_log, advisory_store, 1e-12).build(
            "rotate keys", budget=_BUDGET
        )
        zero_row = _payload(event_log, zero.pack_id)
        tiny_row = _payload(event_log, tiny.pack_id)
        # Served (a draw below 1e-12 has probability 1e-12).
        assert tiny_row["holdout"] is False
        assert tiny_row["holdout_rate"] == 1e-12
        assert _mask_duration(
            {k: v for k, v in tiny_row.items() if k != "holdout_rate"}
        ) == _mask_duration({k: v for k, v in zero_row.items() if k != "holdout_rate"})
        assert _comparable(tiny) == _comparable(zero)


class TestSectionedWithheld:
    def test_the_caller_gets_empty_sections_and_no_advisories(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        control = _builder(event_log, advisory_store, 0.0).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )
        held = _builder(event_log, advisory_store, 1.0).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )

        assert [[i.item_id for i in s.items] for s in control.sections] == [
            ["doc-alpha", "doc-charlie"],
            ["doc-bravo"],
        ]
        assert len(control.advisories) == 3

        assert [s.name for s in held.sections] == ["reference", "domain"]
        assert [s.items for s in held.sections] == [[], []]
        assert [s.budget for s in held.sections] == [s.budget for s in control.sections]
        assert held.advisories == []
        assert held.total_items == 0
        assert held.metadata == {"withholding": _empty_withholding()}

    def test_it_is_indistinguishable_from_a_pack_that_found_nothing(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        held = _builder(event_log, advisory_store, 1.0).build_sectioned(
            "rotate keys", sections=_SECTIONS, session_id="sess-2"
        )
        greenfield = _builder(event_log, None, 0.0, items=[]).build_sectioned(
            "rotate keys", sections=_SECTIONS, session_id="sess-2"
        )
        assert greenfield.total_items == 0
        assert _comparable(held) == _comparable(greenfield)

    def test_the_row_keeps_the_would_be_sections_under_their_own_key(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        control = _builder(event_log, advisory_store, 0.0).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )
        held = _builder(event_log, advisory_store, 1.0).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )
        served_row = _payload(event_log, control.pack_id)
        held_row = _payload(event_log, held.pack_id)

        assert held_row["holdout"] is True
        assert held_row["holdout_rate"] == 1.0
        assert served_row["holdout"] is False

        assert held_row["holdout_sections"] == served_row["sections"]
        assert held_row["holdout_advisory_ids"] == served_row["advisory_ids"]
        assert len(served_row["advisory_ids"]) == 3

        assert [s["item_ids"] for s in held_row["sections"]] == [[], []]
        assert [s["items_count"] for s in held_row["sections"]] == [0, 0]
        assert [s["name"] for s in held_row["sections"]] == ["reference", "domain"]
        assert held_row["injected_item_hashes"] == {}
        assert held_row["total_items"] == 0
        assert held_row["advisory_ids"] == []
        assert held_row["advisories_matched"] == 0
        assert held_row["withholding"] == _empty_withholding()
        assert held_row["token_total_estimated"] == 0

    def test_only_keys_since_base_are_added_at_rate_zero(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        pack = _builder(event_log, advisory_store, 0.0).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )
        row = _payload(event_log, pack.pack_id)
        assert set(row) - _BASE_SECTIONED_KEYS == _KEYS_ADDED_SINCE_BASE
        assert set(row) >= _BASE_SECTIONED_KEYS
        assert row["holdout"] is False
        assert row["holdout_rate"] == 0.0

    def test_the_arm_is_the_draw_on_the_packs_own_id(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        from trellis.core.pack_holdout import is_held_out

        builder = _builder(event_log, advisory_store, 0.5)
        arms = []
        for _ in range(40):
            pack = builder.build_sectioned("rotate keys", sections=_SECTIONS)
            row = _payload(event_log, pack.pack_id)
            expected = is_held_out(pack.pack_id, 0.5)
            assert row["holdout"] is expected
            assert (pack.total_items == 0) is expected
            arms.append(expected)
        assert 0 < sum(arms) < len(arms)


class TestSessionDedup:
    """A withheld item was never served, so the session may still get it."""

    def test_a_withheld_flat_pack_does_not_spend_its_items(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        held = _builder(event_log, advisory_store, 1.0).build(
            "rotate keys", budget=_BUDGET, session_id="sess-h"
        )
        after = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET, session_id="sess-h"
        )
        assert held.items == []
        assert [i.item_id for i in after.items] == [
            "doc-alpha",
            "doc-bravo",
            "doc-charlie",
        ]

    def test_dedup_is_live_in_this_fixture(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        first = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET, session_id="sess-s"
        )
        again = _builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=_BUDGET, session_id="sess-s"
        )
        assert len(first.items) == 3
        assert [i.item_id for i in again.items] == ["doc-delta"]

    def test_a_withheld_sectioned_pack_does_not_spend_its_items(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        held = _builder(event_log, advisory_store, 1.0).build_sectioned(
            "rotate keys", sections=_SECTIONS, session_id="sess-hs"
        )
        after = _builder(event_log, advisory_store, 0.0).build_sectioned(
            "rotate keys", sections=_SECTIONS, session_id="sess-hs"
        )
        assert held.total_items == 0
        assert [[i.item_id for i in s.items] for s in after.sections] == [
            ["doc-alpha", "doc-charlie"],
            ["doc-bravo"],
        ]

    def test_sectioned_dedup_is_live_in_this_fixture(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        _builder(event_log, advisory_store, 0.0).build_sectioned(
            "rotate keys", sections=_SECTIONS, session_id="sess-ss"
        )
        again = _builder(event_log, advisory_store, 0.0).build_sectioned(
            "rotate keys", sections=_SECTIONS, session_id="sess-ss"
        )
        assert [[i.item_id for i in s.items] for s in again.sections] == [
            [],
            ["doc-delta"],
        ]


class TestNaturallyEmptyPackBlindsAdvisoriesToo:
    """R1 (#844): advisory selection does not depend on item count, so a
    pack that genuinely found nothing can still carry advisories (#404's
    rule) — except while the holdout is live, where that would be the one
    tell separating a naturally empty pack from a withheld one (both have
    no items; only the naturally empty one kept its advisories). The
    builder rule closes that: a served, item-less pack carries no
    advisories whenever ``holdout_rate > 0``, matching a withheld pack's
    ``_withhold_flat`` / ``_withhold_sectioned`` zeroing.
    """

    def test_flat_build_drops_advisories_when_naturally_empty_and_rate_is_live(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        natural_empty = _builder(event_log, advisory_store, 1e-12, items=[]).build(
            "rotate keys", budget=_BUDGET
        )
        row = _payload(event_log, natural_empty.pack_id)

        assert natural_empty.items == []
        assert row["holdout"] is False  # a 1e-12 draw is never held out
        assert natural_empty.advisories == []
        assert row["advisory_ids"] == []
        # The telemetry count is untouched: this blinds what the caller is
        # served, not what the store matched.
        assert row["advisories_matched"] == 3

    def test_flat_build_keeps_advisories_when_naturally_empty_at_rate_zero(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        """Inert at the default rate: #404's rule is unchanged when the
        holdout is off."""
        natural_empty = _builder(event_log, advisory_store, 0.0, items=[]).build(
            "rotate keys", budget=_BUDGET
        )
        assert natural_empty.items == []
        assert len(natural_empty.advisories) == 3

    def test_flat_audit_trail_keeps_the_true_would_be_advisories(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        """A pack that is both naturally item-less and drawn into the
        holdout must still record the true matched advisories under
        ``holdout_advisory_ids`` — the blind applies to the served copy
        only, never to the apart-kept would-be pack."""
        held = _builder(event_log, advisory_store, 1.0, items=[]).build(
            "rotate keys", budget=_BUDGET
        )
        row = _payload(event_log, held.pack_id)

        assert row["holdout"] is True
        assert held.advisories == []
        assert row["holdout_advisory_ids"] == [
            "adv-entity",
            "adv-approach",
            "adv-scope",
        ]

    def test_sectioned_build_drops_advisories_when_naturally_empty_and_rate_is_live(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        natural_empty = _builder(
            event_log, advisory_store, 1e-12, items=[]
        ).build_sectioned("rotate keys", sections=_SECTIONS)
        row = _payload(event_log, natural_empty.pack_id)

        assert natural_empty.total_items == 0
        assert row["holdout"] is False
        assert natural_empty.advisories == []
        assert row["advisory_ids"] == []
        assert row["advisories_matched"] == 3

    def test_sectioned_build_keeps_advisories_when_naturally_empty_at_rate_zero(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        natural_empty = _builder(
            event_log, advisory_store, 0.0, items=[]
        ).build_sectioned("rotate keys", sections=_SECTIONS)
        assert natural_empty.total_items == 0
        assert len(natural_empty.advisories) == 3

    def test_sectioned_audit_trail_keeps_the_true_would_be_advisories(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        held = _builder(event_log, advisory_store, 1.0, items=[]).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )
        row = _payload(event_log, held.pack_id)

        assert row["holdout"] is True
        assert held.advisories == []
        assert row["holdout_advisory_ids"] == [
            "adv-entity",
            "adv-approach",
            "adv-scope",
        ]


class TestFactoryWiring:
    """``build_pack_builder`` reads the knob per construction, as every surface does."""

    @staticmethod
    def _registry(tmp_path: Path) -> StoreRegistry:
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(f"data_dir: {data_dir}\n")
        return StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0.25", 0.25), ("1", 1.0), (None, 0.0), ("-1", 0.0), ("lots", 0.0)],
    )
    def test_the_factory_passes_the_configured_rate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        raw: str | None,
        expected: float,
    ) -> None:
        if raw is None:
            monkeypatch.delenv(RATE_ENV, raising=False)
        else:
            monkeypatch.setenv(RATE_ENV, raw)
        registry = self._registry(tmp_path)
        try:
            assert build_pack_builder(registry, surface="test").holdout_rate == expected
        finally:
            registry.close()

    def test_a_factory_build_at_rate_one_withholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RATE_ENV, "1")
        registry = self._registry(tmp_path)
        try:
            registry.knowledge.document_store.put("d1", "a canary rollout runbook body")
            registry.knowledge.document_store.put("d2", "canary rollout pause criteria")
            pack = build_pack_builder(registry, surface="test").build("canary rollout")
            events = registry.operational.event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED, limit=10
            )
        finally:
            registry.close()
        assert pack.items == []
        assert [e.entity_id for e in events] == [pack.pack_id]
        row = events[0].payload
        assert row["holdout"] is True
        assert sorted(r["item_id"] for r in row["holdout_items"]) == ["d1", "d2"]
