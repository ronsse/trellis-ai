"""Tests for the serving-boundary metadata filter."""

from __future__ import annotations

from trellis.retrieve.servable import (
    NON_SERVABLE_METADATA_KEYS,
    strip_non_servable,
)
from trellis.schemas.classification import SHADOW_TAGS_KEY
from trellis.schemas.pack import PackItem


def _item(item_id: str, metadata: dict | None = None) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="text",
        relevance_score=0.5,
        metadata=metadata or {},
    )


class TestStripNonServable:
    def test_strips_shadow_tags(self) -> None:
        out = strip_non_servable(
            [_item("d1", {"title": "T", SHADOW_TAGS_KEY: {"domain": ["x"]}})]
        )
        assert out[0].metadata == {"title": "T"}

    def test_deny_list_is_narrow(self) -> None:
        """Servable-by-default: a new key must not need registering to be served.

        Guards against the deny-list quietly becoming an allow-list.
        """
        assert {SHADOW_TAGS_KEY} == NON_SERVABLE_METADATA_KEYS

    def test_passes_everything_else_through(self) -> None:
        meta = {
            "title": "T",
            "content_tags": {"domain": ["ops"]},
            "auto_importance": 0.5,
            "source_system": "dbt",
            "a_key_invented_tomorrow": 1,
        }
        assert strip_non_servable([_item("d1", meta)])[0].metadata == meta

    def test_clean_items_are_not_copied(self) -> None:
        """The no-strip path is the common one; it should cost nothing."""
        item = _item("d1", {"title": "T"})
        assert strip_non_servable([item])[0] is item

    def test_never_mutates_the_source_item(self) -> None:
        item = _item("d1", {"title": "T", SHADOW_TAGS_KEY: {"domain": ["x"]}})
        strip_non_servable([item])
        assert SHADOW_TAGS_KEY in item.metadata

    def test_empty_input(self) -> None:
        assert strip_non_servable([]) == []
        assert strip_non_servable([_item("d1")])[0].metadata == {}

    def test_preserves_order_and_other_fields(self) -> None:
        items = [
            _item("a", {SHADOW_TAGS_KEY: {}}),
            _item("b", {"title": "keep"}),
        ]
        out = strip_non_servable(items)
        assert [i.item_id for i in out] == ["a", "b"]
        assert out[0].excerpt == "text"
        assert out[0].relevance_score == 0.5


class TestEnforcedForEveryStrategy:
    def test_a_strategy_added_later_is_covered(self) -> None:
        """The reason this lives in PackBuilder rather than in each strategy.

        ``PackBuilder`` takes strategies by injection, so a rule applied inside
        the three built-ins would not hold for a fourth. This is that fourth.
        """
        from unittest.mock import MagicMock

        from trellis.retrieve.pack_builder import PackBuilder
        from trellis.retrieve.strategies import SearchStrategy

        leaky = MagicMock(spec=SearchStrategy)
        leaky.name = "third_party"
        leaky.search.return_value = [
            _item("d1", {"title": "T", SHADOW_TAGS_KEY: {"domain": ["secret"]}})
        ]

        pack = PackBuilder(strategies=[leaky]).build("anything")
        assert pack.items, "fixture must retrieve, else the test is vacuous"
        assert SHADOW_TAGS_KEY not in pack.items[0].metadata
        assert "secret" not in str(pack.model_dump(mode="json"))
