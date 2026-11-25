"""The default-off "Nearby" section over the relevance cutoff's cut items."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from trellis.core.hashing import estimate_tokens
from trellis.retrieve.excerpts import ContentFloorConfig
from trellis.retrieve.feedback_prior import ItemEvidence, scan_ledger
from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.nearby import (
    MAX_GROUPS,
    MAX_IDS_PER_GROUP,
    MAX_SECTION_TOKENS,
    build_nearby,
    format_nearby_section,
    group_cut_items,
    group_label,
    nearby_listed_ids,
    resolve_nearby_mode,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.pack_value import _fetch_join
from trellis.retrieve.relevance_cutoff import (
    RelevanceCutoffConfig,
    apply_relevance_cutoff,
)
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import Pack, PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    import pytest

CUTOFF_ON = RelevanceCutoffConfig(mode="on", semantic_min=0.5)
WEAK_IDS = {"weak-alpha-01", "weak-beta-02"}


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _item(item_id: str, score: float, **metadata: Any) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="some text",
        relevance_score=score,
        metadata=metadata,
    )


def _strategies() -> list[SearchStrategy]:
    return [
        _strategy(
            "semantic",
            [
                _item("strong-01", 0.9),
                _item(
                    "weak-alpha-01",
                    0.4,
                    title="Alpha note",
                    content_tags={"domain": ["billing"]},
                ),
                _item("weak-beta-02", 0.35, title="Beta note", source_system="wiki"),
                _item("both-03", 0.3),
            ],
        ),
        _strategy("keyword", [_item("both-03", 7.5), _item("kw-04", 6.0)]),
    ]


def _builder(
    *,
    nearby: str | None,
    cutoff: RelevanceCutoffConfig = CUTOFF_ON,
    event_log: SQLiteEventLog | None = None,
    holdout_rate: float = 0.0,
) -> PackBuilder:
    return PackBuilder(
        strategies=_strategies(),
        content_floor=ContentFloorConfig(mode="off"),
        relevance_cutoff_config=cutoff,
        nearby_mode=nearby,  # type: ignore[arg-type]
        event_log=event_log,
        holdout_rate=holdout_rate,
    )


def _payload(log: SQLiteEventLog) -> dict[str, Any]:
    (event,) = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
    return event.payload


def _markdown(pack: Pack, **kwargs: Any) -> str:
    items = [
        {"item_id": i.item_id, "item_type": i.item_type, "excerpt": i.excerpt}
        for i in pack.items
    ]
    return format_pack_as_markdown(items, "q", pack_id=pack.pack_id, **kwargs)


def _row(item_id: str, **fields: Any) -> dict[str, Any]:
    return {"item_id": item_id, "estimated_tokens": 10, **fields}


class TestGrouping:
    def test_key_precedence_domain_then_source_then_type(self) -> None:
        assert group_label(_row("a", domain=["x"], source_system="s")) == "domain: x"
        assert group_label(_row("a", domain=[], source_system="s")) == "source: s"
        assert group_label(_row("a", item_type="trace")) == "type: trace"
        assert group_label(_row("a")) == "type: item"

    def test_largest_group_first_then_label(self) -> None:
        rows = [
            _row("1", domain=["b"]),
            _row("2", domain=["a"]),
            _row("3", source_system="z"),
            _row("4", source_system="z"),
        ]
        assert [g["label"] for g in group_cut_items(rows)] == [
            "source: z",
            "domain: a",
            "domain: b",
        ]
        assert group_cut_items(rows) == group_cut_items(list(rows))

    def test_ids_per_group_capped(self) -> None:
        rows = [_row(f"id{n}", domain=["d"]) for n in range(MAX_IDS_PER_GROUP + 3)]
        (group,) = group_cut_items(rows)
        assert len(group["item_ids"]) == MAX_IDS_PER_GROUP
        assert group["ids_left_out"] == 3
        assert group["count"] == MAX_IDS_PER_GROUP + 3

    def test_group_cap_and_left_out_counts(self) -> None:
        rows = [_row(f"i{n}", domain=[f"d{n}"]) for n in range(MAX_GROUPS + 2)]
        block = build_nearby(rows, mode="on", pack_id="P")
        assert block is not None
        assert len(block["groups"]) <= MAX_GROUPS
        assert block["groups_left_out"] == len(rows) - len(block["groups"])
        assert block["items_left_out"] == len(rows) - len(block["listed_item_ids"])
        assert "more groups left out" in format_nearby_section(block, "P")

    def test_token_cap_holds(self) -> None:
        rows = [
            _row(
                f"long-identifier-{n:04d}-xxxxxxxx",
                domain=[f"d{n % 5}"],
                title="T" * 80,
            )
            for n in range(40)
        ]
        block = build_nearby(rows, mode="on", pack_id="01PACKIDPACKIDPACKIDPACKID")
        assert block is not None
        text = format_nearby_section(block, "01PACKIDPACKIDPACKIDPACKID")
        assert estimate_tokens(text) <= MAX_SECTION_TOKENS
        assert block["section_tokens"] == estimate_tokens(text)
        assert block["groups_left_out"] > 0

    def test_off_or_empty_is_none(self) -> None:
        assert build_nearby([_row("a")], mode="off", pack_id="P") is None
        assert build_nearby([], mode="on", pack_id="P") is None
        assert build_nearby(None, mode="on", pack_id="P") is None
        assert format_nearby_section(None, "P") == ""

    def test_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_NEARBY_SECTION", raising=False)
        assert resolve_nearby_mode() == "off"
        assert resolve_nearby_mode({"nearby_section": "on"}) == "on"
        monkeypatch.setenv("TRELLIS_NEARBY_SECTION", "bogus")
        assert resolve_nearby_mode({"nearby_section": "on"}) == "off"


class TestFlatPack:
    def test_section_renders_and_event_lists_ids(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(nearby="on", event_log=log).build("q", clearance="restricted")
        block = pack.metadata["nearby"]
        assert set(block["listed_item_ids"]) == WEAK_IDS
        assert {g["label"] for g in block["groups"]} == {
            "domain: billing",
            "source: wiki",
        }
        text = _markdown(pack, nearby=block)
        assert "## Nearby" in text
        assert "`weak-alpha-01`" in text
        assert (
            f'get_items(item_ids=[<a group\'s ids>], pack_id="{pack.pack_id}")' in text
        )
        assert text.index("## Nearby") < text.index("Cite feedback")
        assert _payload(log)["nearby"] == block
        assert nearby_listed_ids(_payload(log)) == WEAK_IDS

    def test_index_formatter_renders_it_too(self) -> None:
        pack = _builder(nearby="on").build("q", clearance="restricted")
        text = format_pack_as_index_markdown(
            [], "q", pack_id=pack.pack_id, nearby=pack.metadata["nearby"]
        )
        assert "`weak-beta-02`" in text

    def test_off_equals_absent(self, tmp_path: Path) -> None:
        """Off: no key on the pack or the event, and the markdown is unchanged."""
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(nearby="off", event_log=log).build("q", clearance="restricted")
        assert set(pack.metadata) == {"withholding", "relevance_cutoff"}
        assert "nearby" not in _payload(log)
        assert _markdown(pack, nearby=pack.metadata.get("nearby")) == _markdown(pack)
        assert "Nearby" not in _markdown(pack)

    def test_cutoff_off_or_shadow_renders_nothing(self, tmp_path: Path) -> None:
        for n, cutoff in enumerate(
            [RelevanceCutoffConfig(), RelevanceCutoffConfig("shadow", 0.5)]
        ):
            log = SQLiteEventLog(tmp_path / f"events{n}.db")
            pack = _builder(nearby="on", cutoff=cutoff, event_log=log).build(
                "q", clearance="restricted"
            )
            assert "nearby" not in pack.metadata
            assert "nearby" not in _payload(log)

    def test_held_out_pack_gets_no_section_and_no_ids(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(nearby="on", event_log=log, holdout_rate=1.0).build(
            "q", clearance="restricted"
        )
        assert "nearby" not in pack.metadata
        payload = _payload(log)
        assert "nearby" not in payload
        dumped = json.dumps(payload)
        assert not any(i in dumped for i in WEAK_IDS)

    def test_public_only_pack_gets_no_section(self, tmp_path: Path) -> None:
        from tests.unit.retrieve.test_public_clearance import _PoolSemantic
        from tests.unit.retrieve.test_sensitivity import label

        docs = SQLiteDocumentStore(tmp_path / "documents.db")
        for n in range(5):
            docs.put(f"p{n}", "body", {"title": f"t{n}", **label("public")})
        pool = [
            PackItem(
                item_id=f"p{n}",
                item_type="document",
                excerpt="body",
                relevance_score=s,
                score_breakdown={"native_semantic": s},
            )
            for n, s in enumerate([0.9, 0.2, 0.1, 0.05, 0.01])
        ]
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = PackBuilder(
            strategies=[_PoolSemantic(pool)],
            document_store=docs,
            content_floor=ContentFloorConfig(mode="off"),
            relevance_cutoff_config=CUTOFF_ON,
            nearby_mode="on",
            event_log=log,
        ).build("intent", clearance="public")
        assert "nearby" not in pack.metadata
        assert all("nearby" not in e.payload for e in log.get_events(limit=50))


class TestSectionedPack:
    def test_sectioned_pack_carries_and_renders_it(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(nearby="on", event_log=log).build_sectioned(
            intent="q", sections=[SectionRequest(name="all")], clearance="restricted"
        )
        block = pack.metadata["nearby"]
        assert set(block["listed_item_ids"]) == WEAK_IDS
        assert _payload(log)["nearby"] == block
        text = format_sectioned_pack_as_markdown(
            [], "q", pack_id=pack.pack_id, nearby=block
        )
        assert "## Nearby" in text
        assert "`weak-alpha-01`" in text

    def test_sectioned_held_out_has_none(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(nearby="on", event_log=log, holdout_rate=1.0).build_sectioned(
            intent="q", sections=[SectionRequest(name="all")], clearance="restricted"
        )
        assert "nearby" not in pack.metadata
        assert "nearby" not in _payload(log)


class TestFetchJoin:
    def test_fetch_of_a_listed_item_is_fetch_evidence(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        try:
            pack = _builder(nearby="on", event_log=log).build(
                "q", clearance="restricted"
            )
            log.emit(
                EventType.PACK_ITEMS_FETCHED,
                source="mcp",
                entity_id=pack.pack_id,
                payload={
                    "pack_id": pack.pack_id,
                    "served_item_ids": ["weak-alpha-01", "never-listed"],
                },
            )
            ledger, _ = scan_ledger(
                log, since=datetime.now(tz=UTC) - timedelta(days=1), limit=100
            )
        finally:
            log.close()
        assert ledger.table() == {
            "weak-alpha-01": ItemEvidence(helpful=0, unhelpful=0, fetched=1)
        }

    def test_value_report_classifies_a_nearby_fetch(self) -> None:
        servings = {("P", "served"): (10, "unjudged")}
        listed = {("P", "listed")}
        kwargs = {"pack_ids": {"P": {}}, "servings": servings, "listed": listed}
        assert _fetch_join(("P", "served"), has_pack=True, **kwargs) == "joined"
        assert _fetch_join(("P", "listed"), has_pack=True, **kwargs) == "nearby"
        assert _fetch_join(("P", "other"), has_pack=True, **kwargs) == (
            "item_not_served"
        )
        assert _fetch_join(("Q", "listed"), has_pack=True, **kwargs) == (
            "pack_not_in_window"
        )


class TestCutoffLeftovers:
    def test_other_axis_with_non_finite_score_still_counts(self) -> None:
        weak = PackItem(
            item_id="x",
            item_type="document",
            excerpt="t",
            relevance_score=0.1,
            score_breakdown={"native_semantic": 0.1},
            metadata={"source_strategy": "semantic"},
        )
        # The keyword copy's score was NaN, so it recorded no native key.
        kw_copy = weak.model_copy(
            update={
                "score_breakdown": {},
                "relevance_score": math.nan,
                "metadata": {"source_strategy": "keyword"},
            }
        )
        others = [
            PackItem(
                item_id=f"s{n}",
                item_type="document",
                excerpt="t",
                relevance_score=0.1,
                score_breakdown={"native_semantic": 0.1},
            )
            for n in range(4)
        ]
        result = apply_relevance_cutoff(
            [weak, kw_copy, *others],
            config=RelevanceCutoffConfig(mode="on", semantic_min=0.5),
            price=lambda _i: 1,
        )
        assert "x" in {i.item_id for i in result.kept}
        assert "x" not in {i.item_id for i in result.cut}

    def test_cut_rows_carry_grouping_fields(self) -> None:
        pack = _builder(nearby="off").build("q", clearance="restricted")
        rows = {r["item_id"]: r for r in pack.metadata["relevance_cutoff"]["cut_items"]}
        assert rows["weak-alpha-01"]["domain"] == ["billing"]
        assert rows["weak-beta-02"]["source_system"] == "wiki"
        assert rows["weak-beta-02"]["item_type"] == "document"
