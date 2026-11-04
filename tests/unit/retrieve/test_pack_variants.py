"""Every ``PACK_ASSEMBLED`` row names the arm it was assembled under.

``variants`` is telemetry only. These tests hold both halves of that sentence:
the key is on every row, flat and sectioned, and building a pack with the
stamping in place serves exactly what building it without the stamping serves.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from tests.unit.retrieve import test_pack_holdout_seam as seam
from trellis.retrieve import pack_builder as pack_builder_module
from trellis.retrieve.formatters import format_pack_as_markdown
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.sqlite.event_log import SQLiteEventLog

event_log = seam.event_log
advisory_store = seam.advisory_store


def _recomputed_draw(pack_id: str) -> float:
    """The assignment draw, re-derived here rather than imported."""
    digest = hashlib.sha256(b"trellis/pack-holdout/v1:" + pack_id.encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / float(1 << 53)


def _rendered(pack: Any) -> str:
    return format_pack_as_markdown(
        [item.model_dump() for item in pack.items],
        pack.intent,
        pack_id="fixed-for-comparison",
        advisories=pack.advisories,
    )


def _without_variants(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "variants"}


class TestFlatRows:
    def test_a_zero_rate_stamps_an_empty_mapping(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        pack = seam._builder(event_log, advisory_store, 0.0).build(
            "rotate keys", budget=seam._BUDGET, clearance="restricted"
        )
        assert seam._payload(event_log, pack.pack_id)["variants"] == {}

    def test_a_rate_of_one_stamps_held_out(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        pack = seam._builder(event_log, advisory_store, 1.0).build(
            "rotate keys", budget=seam._BUDGET, clearance="restricted"
        )
        row = seam._payload(event_log, pack.pack_id)
        assert row["variants"] == {"pack_holdout": "held_out"}
        assert row["holdout"] is True

    def test_the_arm_agrees_with_the_draw_on_every_pack(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        rate = 0.5
        builder = seam._builder(event_log, advisory_store, rate)
        arms: set[str] = set()
        for _ in range(60):
            pack = builder.build(
                "rotate keys", budget=seam._BUDGET, clearance="restricted"
            )
            row = seam._payload(event_log, pack.pack_id)
            expected = "held_out" if _recomputed_draw(pack.pack_id) < rate else "served"
            assert row["variants"] == {"pack_holdout": expected}
            assert row["holdout"] is (expected == "held_out")
            arms.add(expected)
        assert arms == {"held_out", "served"}


class TestSectionedRows:
    @pytest.mark.parametrize(
        ("rate", "expected"),
        [(0.0, {}), (1.0, {"pack_holdout": "held_out"})],
    )
    def test_the_sectioned_row_carries_the_key(
        self,
        event_log: SQLiteEventLog,
        advisory_store: AdvisoryStore,
        rate: float,
        expected: dict[str, str],
    ) -> None:
        pack = seam._builder(event_log, advisory_store, rate).build_sectioned(
            "rotate keys", sections=seam._SECTIONS, clearance="restricted"
        )
        row = seam._payload(event_log, pack.pack_id)
        assert row["variants"] == expected

    def test_the_arm_agrees_with_the_draw_on_every_sectioned_pack(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        rate = 0.5
        builder = seam._builder(event_log, advisory_store, rate)
        arms: set[str] = set()
        for _ in range(60):
            pack = builder.build_sectioned(
                "rotate keys", sections=seam._SECTIONS, clearance="restricted"
            )
            row = seam._payload(event_log, pack.pack_id)
            expected = "held_out" if _recomputed_draw(pack.pack_id) < rate else "served"
            assert row["variants"] == {"pack_holdout": expected}
            arms.add(expected)
        assert arms == {"held_out", "served"}


class TestServingIsByteIdentical:
    """The pack built with the stamping equals the pack built without it.

    "Without" is the same builder with ``active_variants`` replaced by a stub
    returning ``{}``, so the only difference between the two arms of each test
    is the stamping code path. Pack ids and timestamps differ between any two
    builds and are excluded the way the seam suite excludes them.
    """

    @pytest.mark.parametrize("rate", [0.0, 1.0])
    def test_flat_pack_items_order_and_rendering(
        self,
        event_log: SQLiteEventLog,
        advisory_store: AdvisoryStore,
        monkeypatch: pytest.MonkeyPatch,
        rate: float,
    ) -> None:
        def build() -> Any:
            # Rates 0 and 1 draw the same arm on every pack, so the two builds
            # compared here are the same pack in everything but their ids.
            return seam._builder(event_log, advisory_store, rate).build(
                "rotate keys",
                budget=seam._BUDGET,
                clearance="restricted",
            )

        with_stamp = build()
        with monkeypatch.context() as patch:
            patch.setattr(pack_builder_module, "active_variants", lambda **_kwargs: {})
            without_stamp = build()

        assert [i.item_id for i in with_stamp.items] == [
            i.item_id for i in without_stamp.items
        ]
        assert seam._comparable(with_stamp) == seam._comparable(without_stamp)
        assert _rendered(with_stamp) == _rendered(without_stamp)

    @pytest.mark.parametrize("rate", [0.0, 1.0])
    def test_sectioned_pack_is_identical(
        self,
        event_log: SQLiteEventLog,
        advisory_store: AdvisoryStore,
        monkeypatch: pytest.MonkeyPatch,
        rate: float,
    ) -> None:
        def build() -> Any:
            return seam._builder(event_log, advisory_store, rate).build_sectioned(
                "rotate keys",
                sections=seam._SECTIONS,
                clearance="restricted",
            )

        with_stamp = build()
        with monkeypatch.context() as patch:
            patch.setattr(pack_builder_module, "active_variants", lambda **_kwargs: {})
            without_stamp = build()

        assert [[i.item_id for i in s.items] for s in with_stamp.sections] == [
            [i.item_id for i in s.items] for s in without_stamp.sections
        ]
        assert seam._comparable(with_stamp) == seam._comparable(without_stamp)

    @pytest.mark.parametrize("rate", [0.0, 1.0])
    def test_the_row_differs_only_by_the_variants_key(
        self,
        event_log: SQLiteEventLog,
        advisory_store: AdvisoryStore,
        monkeypatch: pytest.MonkeyPatch,
        rate: float,
    ) -> None:
        with_stamp = seam._builder(event_log, advisory_store, rate).build(
            "rotate keys", budget=seam._BUDGET, clearance="restricted"
        )
        with monkeypatch.context() as patch:
            patch.setattr(pack_builder_module, "active_variants", lambda **_kwargs: {})
            without_stamp = seam._builder(event_log, advisory_store, rate).build(
                "rotate keys", budget=seam._BUDGET, clearance="restricted"
            )
        stamped = seam._payload(event_log, with_stamp.pack_id)
        plain = seam._payload(event_log, without_stamp.pack_id)
        assert "variants" in stamped
        assert plain["variants"] == {}
        assert seam._mask_duration(_without_variants(stamped)) == seam._mask_duration(
            _without_variants(plain)
        )

    def test_the_draw_for_a_recorded_pack_is_still_a_function_of_id_and_rate(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore
    ) -> None:
        builder = seam._builder(event_log, advisory_store, 0.3)
        for _ in range(40):
            pack = builder.build(
                "rotate keys", budget=seam._BUDGET, clearance="restricted"
            )
            row = seam._payload(event_log, pack.pack_id)
            assert row["holdout"] is (_recomputed_draw(pack.pack_id) < 0.3)
            assert bool(pack.items) is not row["holdout"]
