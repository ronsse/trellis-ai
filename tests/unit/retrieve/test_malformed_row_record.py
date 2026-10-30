"""A skipped malformed row leaves a durable record (#1050, item 1).

#1048 made ``SemanticSearch`` skip a vector row whose metadata cannot be read,
so one bad row no longer fails the axis. That left the defect in a log line:
nothing in the pack or in ``trellis analyze health`` said a row had been
dropped, and a loser whose successor was the skipped row was served with an
empty ``rejected_items``.

The channel is the per-call return value (``SemanticSearch.search_result``),
because a strategy is shared across requests and can hold no per-request
state. The builder records each skipped row at the collect seam as a
``malformed_metadata`` rejection, apart from ``all_items`` so supersession
never sees it, and counts it in ``PACK_ASSEMBLED.malformed_row_skips``, the
key health reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.sensitivity import SENSITIVITY_WITHHELD_IDS_KEY
from trellis.retrieve.strategies import (
    MALFORMED_METADATA_REJECTION_REASON,
    GraphSearch,
    KeywordSearch,
    SearchStrategy,
    SemanticSearch,
)
from trellis.retrieve.telemetry import KNOWN_REJECTION_REASONS
from trellis.retrieve.withholding import withholding_from_payload
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    LIFECYCLE_KEY,
    DataClassification,
)
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from pathlib import Path

#: Clears the content floor's five-substance-word demotion.
_BODY = "the deploy checklist requires draining the write queue first"
_REASON = "malformed_metadata"
#: A fresh stamp, so a bool importance is malformed because it is a bool and
#: not because it is unstamped.
_NOW = datetime.now(UTC).isoformat()


def _row(item_id: str, score: float, **metadata: Any) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "score": score,
        "metadata": {"content": _BODY, "auto_importance": 0.2, **metadata},
    }


def _good() -> dict[str, Any]:
    return _row("good", 0.5)


def _bad(**metadata: Any) -> dict[str, Any]:
    # Scores higher than the sibling, so an accidental serve would sort first.
    return _row("bad", 0.99, **{"auto_importance": "high", **metadata})


def _semantic(rows: list[dict[str, Any]]) -> SemanticSearch:
    store = MagicMock()
    store.query.return_value = rows
    return SemanticSearch(store, MagicMock(return_value=[0.1, 0.2]))


def _keyword(items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = "keyword"
    strategy.search.return_value = items
    return strategy


def _doc(item_id: str, score: float, **metadata: Any) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=_BODY,
        relevance_score=score,
        metadata={"source_strategy": "keyword", **metadata},
    )


def _keyword_axis(docs: list[dict[str, Any]]) -> KeywordSearch:
    store = MagicMock()
    store.search.return_value = docs
    return KeywordSearch(store)


def _doc_row(doc_id: str, rank: float, **metadata: Any) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "content": _BODY,
        "metadata": {"auto_importance": 0.2, **metadata},
        "rank": rank,
    }


def _graph_axis(nodes: list[dict[str, Any]]) -> GraphSearch:
    store = MagicMock()
    store.query.return_value = nodes
    store.execute_node_query.return_value = nodes
    return GraphSearch(store)


def _node(node_id: str, **properties: Any) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": "service",
        "properties": {"name": node_id, "description": _BODY, **properties},
    }


def _label(level: str) -> dict[str, Any]:
    return {
        DATA_CLASSIFICATION_KEY: DataClassification(
            sensitivity=level,  # type: ignore[arg-type]
            classified_by=["test"],
        ).model_dump(mode="json")
    }


def _payload(log: SQLiteEventLog) -> dict[str, Any]:
    return log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[0].payload


def _served(pack: Any) -> list[str]:
    return [i.item_id for i in pack.items]


def _rows(pack: Any) -> list[tuple[str, str, str | None]]:
    return [
        (r.item_id, r.reason, r.strategy_source)
        for r in pack.retrieval_report.rejected_items
    ]


_SECTIONS = [SectionRequest(name="All", max_items=20, max_tokens=20000)]


class TestStrategyChannel:
    def test_search_never_returns_a_placeholder(self) -> None:
        strategy = _semantic([_good(), _bad()])
        assert [i.item_id for i in strategy.search("q")] == ["good"]

    def test_search_result_carries_the_placeholder_beside_the_items(self) -> None:
        result = _semantic([_good(), _bad()]).search_result("q")

        assert [i.item_id for i in result.items] == ["good"]
        assert [i.item_id for i in result.skipped] == ["bad"]
        placeholder = result.skipped[0]
        assert placeholder.item_type == "vector"
        assert placeholder.excerpt == ""
        assert placeholder.relevance_score == 0.0
        # The row's own metadata travels (the clearance rules read it), and
        # the strategy is stamped like any item the axis builds.
        assert placeholder.metadata["auto_importance"] == "high"
        assert placeholder.metadata["source_strategy"] == "semantic"

    def test_an_unreadable_excerpt_is_recorded_like_an_unreadable_importance(
        self,
    ) -> None:
        result = _semantic(
            [_good(), {"item_id": "bad", "score": 0.9, "metadata": {"content": 5}}]
        ).search_result("q")

        assert [i.item_id for i in result.items] == ["good"]
        assert [i.item_id for i in result.skipped] == ["bad"]
        assert result.skipped[0].item_type == "vector"
        assert result.skipped[0].excerpt == ""

    def test_a_clean_axis_skips_nothing(self) -> None:
        assert _semantic([_good()]).search_result("q").skipped == []

    def test_the_reason_is_documented(self) -> None:
        assert MALFORMED_METADATA_REJECTION_REASON == _REASON
        assert _REASON in KNOWN_REJECTION_REASONS


class TestFlatPack:
    def test_a_skipped_row_is_a_rejected_item_and_a_counted_skip(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[
                _keyword([_doc("kw", 0.4)]),
                _semantic([_good(), _bad()]),
            ],
            event_log=log,
        )
        pack = builder.build("deploy checklist", clearance="restricted")

        assert sorted(_served(pack)) == ["good", "kw"]
        assert _rows(pack) == [("bad", _REASON, "semantic")]
        assert pack.retrieval_report.rejected_items[0].item_type == "vector"
        payload = _payload(log)
        assert payload["malformed_row_skips"] == {"semantic": 1}
        assert [
            (r["item_id"], r["reason"], r["strategy_source"], r["item_type"])
            for r in payload["rejected_items"]
        ] == [("bad", _REASON, "semantic", "vector")]
        assert payload["withholding"]["by_reason"] == {_REASON: 1}
        assert pack.metadata["withholding"]["by_reason"] == {_REASON: 1}

        withholding = withholding_from_payload(pack.metadata["withholding"])
        rows = [
            {
                "item_id": i.item_id,
                "item_type": i.item_type,
                "excerpt": i.excerpt,
                "relevance_score": i.relevance_score,
            }
            for i in pack.items
        ]
        for rendered in (
            format_pack_as_markdown(
                rows, "deploy checklist", max_tokens=4000, withholding=withholding
            ),
            format_pack_as_index_markdown(
                rows,
                "deploy checklist",
                max_tokens=4000,
                pack_id="pk1",
                withholding=withholding,
            ),
        ):
            assert f"{_REASON} 1" in rendered

    def test_a_clean_build_carries_an_empty_skip_map(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(strategies=[_semantic([_good()])], event_log=log)
        builder.build("deploy checklist", clearance="restricted")

        assert _payload(log)["malformed_row_skips"] == {}

    def test_every_skip_is_counted(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        second = _row("bad-2", 0.98)
        second["metadata"]["content"] = 5
        builder = PackBuilder(
            strategies=[_semantic([_good(), _bad(), second])], event_log=log
        )
        pack = builder.build("deploy checklist", clearance="restricted")

        assert _served(pack) == ["good"]
        assert sorted(r[0] for r in _rows(pack)) == ["bad", "bad-2"]
        assert _payload(log)["malformed_row_skips"] == {"semantic": 2}


class TestSectionedPack:
    def test_the_skip_is_counted_and_withheld(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[_keyword([_doc("kw", 0.4)]), _semantic([_good(), _bad()])],
            event_log=log,
        )
        pack = builder.build_sectioned(
            "deploy checklist", clearance="restricted", sections=_SECTIONS
        )

        served = sorted(i.item_id for s in pack.sections for i in s.items)
        assert served == ["good", "kw"]
        payload = _payload(log)
        assert payload["malformed_row_skips"] == {"semantic": 1}
        assert payload["withholding"]["by_reason"] == {_REASON: 1}
        assert pack.metadata["withholding"]["by_reason"] == {_REASON: 1}
        withholding = withholding_from_payload(pack.metadata["withholding"])
        rendered = format_sectioned_pack_as_markdown(
            [s.model_dump() for s in pack.sections],
            "deploy checklist",
            max_tokens=4000,
            withholding=withholding,
        )
        assert f"{_REASON} 1" in rendered

    def test_a_clean_build_carries_an_empty_skip_map(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(strategies=[_semantic([_good()])], event_log=log)
        builder.build_sectioned(
            "deploy checklist", clearance="restricted", sections=_SECTIONS
        )

        assert _payload(log)["malformed_row_skips"] == {}


class TestKeywordAndGraphAxes:
    """All three axes skip and record a malformed row (#1050, items 2 and 3).

    A bad document on the keyword axis and a bad node on the graph axis used to
    fail their axis for every pack; now each costs only itself, and is
    recorded the way a bad vector row is.
    """

    @staticmethod
    def _two_axes() -> list[SearchStrategy]:
        return [
            _keyword_axis(
                [
                    _doc_row("kw-bad", -0.99, auto_importance="high"),
                    _doc_row("kw-good", -0.5),
                ]
            ),
            _graph_axis(
                [
                    _node("n-bad", auto_importance=True, importance_scored_at=_NOW),
                    _node("n-good"),
                ]
            ),
        ]

    def test_a_flat_pack_serves_both_axes_and_records_both_skips(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = PackBuilder(strategies=self._two_axes(), event_log=log).build(
            "deploy checklist", clearance="restricted"
        )

        assert sorted(_served(pack)) == ["kw-good", "n-good"]
        assert sorted(
            (r.item_id, r.reason, r.strategy_source, r.item_type)
            for r in pack.retrieval_report.rejected_items
        ) == [
            ("kw-bad", _REASON, "keyword", "document"),
            ("n-bad", _REASON, "graph", "entity"),
        ]
        payload = _payload(log)
        assert payload["malformed_row_skips"] == {"keyword": 1, "graph": 1}
        assert payload["withholding"]["by_reason"] == {_REASON: 2}

    def test_a_sectioned_pack_serves_both_axes_and_counts_both_skips(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = PackBuilder(strategies=self._two_axes(), event_log=log).build_sectioned(
            "deploy checklist", clearance="restricted", sections=_SECTIONS
        )

        served = sorted(i.item_id for s in pack.sections for i in s.items)
        assert served == ["kw-good", "n-good"]
        payload = _payload(log)
        assert payload["malformed_row_skips"] == {"keyword": 1, "graph": 1}
        assert pack.metadata["withholding"]["by_reason"] == {_REASON: 2}

    def test_all_three_axes_are_counted_in_one_pack(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = PackBuilder(
            strategies=[*self._two_axes(), _semantic([_good(), _bad()])],
            event_log=log,
        ).build("deploy checklist", clearance="restricted")

        assert sorted(_served(pack)) == ["good", "kw-good", "n-good"]
        assert _payload(log)["malformed_row_skips"] == {
            "keyword": 1,
            "graph": 1,
            "semantic": 1,
        }

    def test_the_axes_leave_the_same_record_whatever_the_bad_value(
        self, tmp_path: Path
    ) -> None:
        # A stamp it cannot read, not an importance it cannot read.
        stamp = {"content_tags": {"importance_scored_at": "garbage"}}
        log = SQLiteEventLog(tmp_path / "events.db")
        strategies: list[SearchStrategy] = [
            _keyword_axis([_doc_row("kw-bad", -0.9, auto_importance=0.97, **stamp)]),
            _graph_axis([_node("n-bad", auto_importance=0.97, **stamp)]),
        ]
        pack = PackBuilder(strategies=strategies, event_log=log).build(
            "deploy checklist", clearance="restricted"
        )

        assert _served(pack) == []
        assert _payload(log)["malformed_row_skips"] == {"keyword": 1, "graph": 1}

    def test_a_restricted_skipped_document_and_node_are_concealed(self) -> None:
        strategies: list[SearchStrategy] = [
            _keyword_axis(
                [
                    _doc_row(
                        "kw-bad", -0.9, auto_importance="high", **_label("restricted")
                    )
                ]
            ),
            _graph_axis(
                [_node("n-bad", auto_importance="high", **_label("restricted"))]
            ),
        ]
        pack = PackBuilder(strategies=strategies).build(
            "deploy checklist", clearance="internal"
        )

        assert sorted(pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY]) == [
            "kw-bad",
            "n-bad",
        ]


class TestSupersession:
    """The pairwise rule stands: a loser is withheld only while its successor
    is a candidate, and a skipped row is not one."""

    @staticmethod
    def _strategies() -> list[SearchStrategy]:
        loser = _doc(
            "old",
            0.9,
            **{LIFECYCLE_KEY: {"state": "superseded", "superseded_by": "new"}},
        )
        successor = _row("new", 0.8, auto_importance="high")
        return [_keyword([loser, _doc("u", 0.3)]), _semantic([_good(), successor])]

    def test_a_flat_loser_is_served_and_the_successor_is_explained(self) -> None:
        pack = PackBuilder(strategies=self._strategies()).build(
            "deploy checklist", clearance="restricted"
        )

        assert sorted(_served(pack)) == ["good", "old", "u"]
        assert _rows(pack) == [("new", _REASON, "semantic")]

    def test_a_sectioned_loser_is_served_and_the_successor_is_counted(self) -> None:
        pack = PackBuilder(strategies=self._strategies()).build_sectioned(
            "deploy checklist", clearance="restricted", sections=_SECTIONS
        )

        served = sorted(i.item_id for s in pack.sections for i in s.items)
        assert served == ["good", "old", "u"]
        assert pack.metadata["withholding"]["by_reason"] == {_REASON: 1}


class TestClearance:
    """A skipped row is concealed as any rejected candidate is (#1022)."""

    @staticmethod
    def _builder() -> PackBuilder:
        return PackBuilder(
            strategies=[_semantic([_good(), _bad(**_label("restricted"))])]
        )

    def test_a_flat_pack_conceals_a_restricted_skipped_id(self) -> None:
        pack = self._builder().build("deploy checklist", clearance="internal")

        assert _rows(pack) == [("bad", _REASON, "semantic")]
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == ["bad"]

    def test_a_sectioned_pack_conceals_it_too(self) -> None:
        pack = self._builder().build_sectioned(
            "deploy checklist", clearance="internal", sections=_SECTIONS
        )

        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == ["bad"]

    def test_an_unlabelled_skipped_id_stays_visible(self) -> None:
        pack = PackBuilder(strategies=[_semantic([_good(), _bad()])]).build(
            "deploy checklist", clearance="internal"
        )

        assert SENSITIVITY_WITHHELD_IDS_KEY not in pack.metadata

    def test_a_restricted_caller_has_nothing_to_conceal(self) -> None:
        pack = self._builder().build("deploy checklist", clearance="restricted")

        assert SENSITIVITY_WITHHELD_IDS_KEY not in pack.metadata


class TestHoldout:
    """A withheld pack still ran its strategies, so the count survives."""

    def test_a_held_out_flat_pack_still_counts_the_skip(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[_semantic([_good(), _bad()])], event_log=log, holdout_rate=1.0
        )
        pack = builder.build("deploy checklist", clearance="restricted")

        payload = _payload(log)
        assert pack.items == []
        assert payload["holdout"] is True
        assert payload["rejected_items"] == []
        assert payload["malformed_row_skips"] == {"semantic": 1}

    def test_a_held_out_sectioned_pack_still_counts_the_skip(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[_semantic([_good(), _bad()])], event_log=log, holdout_rate=1.0
        )
        builder.build_sectioned(
            "deploy checklist", clearance="restricted", sections=_SECTIONS
        )

        payload = _payload(log)
        assert payload["holdout"] is True
        assert payload["malformed_row_skips"] == {"semantic": 1}


@pytest.mark.parametrize("rows", [[], [_good()]])
def test_a_pack_without_a_skip_reports_none(rows: list[dict[str, Any]]) -> None:
    pack = PackBuilder(strategies=[_semantic(rows)]).build(
        "deploy checklist", clearance="restricted"
    )

    assert _rows(pack) == []
