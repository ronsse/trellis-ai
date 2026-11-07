"""A pack from a builder with no document store withholds a promoted precedent (#1080).

The builder factory always wires the knowledge plane's document store, so only a
caller that builds a :class:`~trellis.retrieve.pack_builder.PackBuilder` by hand
can leave it out. Without it the builder has neither the precedent's own row nor
its source to ask, and an outage must not read as permission: a promoted
precedent is withheld as ``sensitivity_unreadable`` below ``restricted``, and
served to a ``restricted`` caller, which reads everything. An item outside the
precedent namespace is untouched either way.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.sensitivity import (
    SENSITIVITY_UNREADABLE_REJECTION_REASON,
)
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackItem, SectionRequest

PRECEDENT = "precedent://learning/asset_generation/doc-source"
OPEN = "doc-open"


def _item(item_id: str) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="the deploy checklist requires draining the write queue first",
        relevance_score=0.5,
        metadata={},
    )


def _builder() -> PackBuilder:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = "keyword"
    strategy.search.return_value = [_item(OPEN), _item(PRECEDENT)]
    return PackBuilder(strategies=[strategy])


class TestFlatPack:
    @pytest.mark.parametrize("clearance", ["internal", "confidential"])
    def test_the_precedent_is_withheld_as_unreadable(self, clearance) -> None:
        pack = _builder().build("deploy checklist", clearance=clearance)

        assert [i.item_id for i in pack.items] == [OPEN]
        rejected = {r.item_id: r.reason for r in pack.retrieval_report.rejected_items}
        assert rejected == {PRECEDENT: SENSITIVITY_UNREADABLE_REJECTION_REASON}
        assert pack.metadata["withholding"]["by_reason"] == {
            SENSITIVITY_UNREADABLE_REJECTION_REASON: 1
        }

    def test_a_restricted_caller_is_served_both(self) -> None:
        pack = _builder().build("deploy checklist", clearance="restricted")

        assert {i.item_id for i in pack.items} == {OPEN, PRECEDENT}
        assert pack.retrieval_report.rejected_items == []

    def test_a_pool_without_a_precedent_is_unchanged(self) -> None:
        strategy = MagicMock(spec=SearchStrategy)
        strategy.name = "keyword"
        strategy.search.return_value = [_item(OPEN), _item("doc-two")]

        pack = PackBuilder(strategies=[strategy]).build(
            "deploy checklist", clearance="internal"
        )

        assert [i.item_id for i in pack.items] == [OPEN, "doc-two"]
        assert pack.retrieval_report.rejected_items == []


class TestSectionedPack:
    def test_the_precedent_is_withheld_as_unreadable(self) -> None:
        pack = _builder().build_sectioned(
            "deploy checklist",
            clearance="internal",
            sections=[SectionRequest(name="All")],
        )

        assert [i.item_id for i in pack.all_items] == [OPEN]
        # A sectioned pack reports what it withheld once, above its sections.
        assert pack.metadata["withholding"]["by_reason"] == {
            SENSITIVITY_UNREADABLE_REJECTION_REASON: 1
        }
