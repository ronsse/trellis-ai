"""Tests for the :mod:`trellis.retrieve.rerankers.base` module.

Covers ``RankedItem`` schema, the ``Reranker`` ABC contract, and a
MagicMock-based protocol implementation check that future rerankers
can be drop-in substituted via duck typing on ``rerank``/``name``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from trellis.retrieve.rerankers.base import RankedItem, Reranker
from trellis.schemas.pack import PackItem


def _pack_item(item_id: str = "x", score: float = 0.5) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="some excerpt",
        relevance_score=score,
        strategy_source="keyword",
    )


class TestRankedItem:
    def test_default_score_and_details(self) -> None:
        item = _pack_item()
        ranked = RankedItem(item=item)
        assert ranked.reranker_score == 0.0
        assert ranked.reranker_details == {}

    def test_explicit_score_and_details(self) -> None:
        item = _pack_item()
        ranked = RankedItem(
            item=item,
            reranker_score=0.42,
            reranker_details={"signal": "boost"},
        )
        assert ranked.reranker_score == 0.42
        assert ranked.reranker_details == {"signal": "boost"}

    def test_extra_fields_forbidden(self) -> None:
        # TrellisModel base sets extra="forbid"; verify here.
        with pytest.raises(ValidationError):
            RankedItem(item=_pack_item(), bogus_field=1)  # type: ignore[call-arg]

    def test_holds_pack_item_reference(self) -> None:
        item = _pack_item("abc", score=0.9)
        ranked = RankedItem(item=item, reranker_score=1.0)
        assert ranked.item.item_id == "abc"
        assert ranked.item.relevance_score == 0.9


class TestRerankerABC:
    def test_cannot_instantiate_directly(self) -> None:
        # ABC with abstractmethods cannot be instantiated.
        with pytest.raises(TypeError):
            Reranker()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class _PassThrough(Reranker):
            @property
            def name(self) -> str:
                return "passthrough"

            def rerank(
                self,
                query: str,
                candidates: list[PackItem],
            ) -> list[PackItem]:
                _ = query
                return list(candidates)

        rr = _PassThrough()
        assert rr.name == "passthrough"
        items = [_pack_item("a"), _pack_item("b")]
        out = rr.rerank("q", items)
        assert [i.item_id for i in out] == ["a", "b"]

    def test_subclass_missing_methods_cannot_instantiate(self) -> None:
        class _MissingRerank(Reranker):
            @property
            def name(self) -> str:
                return "broken"

        with pytest.raises(TypeError):
            _MissingRerank()  # type: ignore[abstract]

    def test_magicmock_spec_satisfies_protocol(self) -> None:
        """A MagicMock(spec=Reranker) should expose ``name`` and ``rerank``.

        This is the fixture pattern most tests use in this repo, so we
        guard the contract: if ``Reranker``'s public surface ever changes,
        consumers using ``MagicMock(spec=Reranker)`` will see it via this
        test as well as their own.
        """
        mock = MagicMock(spec=Reranker)
        # Both abstract members are addressable on the spec.
        mock.rerank.return_value = [_pack_item("z")]
        mock.name = "mock-reranker"
        out = mock.rerank("query", [_pack_item("z")])
        assert out[0].item_id == "z"
        assert mock.name == "mock-reranker"
