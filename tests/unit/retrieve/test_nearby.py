"""The default-off "Nearby" section over the relevance cutoff's cut items."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from trellis.core.hashing import estimate_tokens
from trellis.retrieve.excerpts import ContentFloorConfig, truncate_excerpt
from trellis.retrieve.feedback_prior import ItemEvidence, scan_ledger
from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.nearby import (
    LABEL_MAX_CHARS,
    MAX_GROUPS,
    MAX_IDS_PER_GROUP,
    MAX_SECTION_TOKENS,
    _clean,
    build_nearby,
    format_nearby_section,
    group_cut_items,
    group_label,
    nearby_event_block,
    nearby_listed_ids,
    resolve_nearby_mode,
)
from trellis.retrieve.pack_builder import PackBuilder, _ServedSet
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
from trellis_sdk._format import format_pack_as_markdown as sdk_format_pack
from trellis_sdk._format import (
    format_sectioned_pack_as_markdown as sdk_format_sectioned,
)

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
        assert _payload(log)["nearby"] == nearby_event_block(block)
        assert "markdown" not in _payload(log)["nearby"]
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
        assert _payload(log)["nearby"] == nearby_event_block(block)
        assert "markdown" not in _payload(log)["nearby"]
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


def _section(text: str) -> str:
    """The nearby section's lines inside a rendered pack."""
    start = text.index("## Nearby")
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


def _pack_payload(log: SQLiteEventLog, pack_id: str) -> dict[str, Any]:
    (payload,) = [
        e.payload
        for e in log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=50)
        if e.entity_id == pack_id
    ]
    return payload


class TestEmptyPackIsBlind:
    """A served pack emptied by session dedup reads like a held-out pack."""

    def _served_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setattr(
            "trellis.retrieve.pack_builder.is_held_out", lambda *_a, **_k: False
        )
        log = SQLiteEventLog(tmp_path / "served.db")
        builder = _builder(nearby="on", event_log=log, holdout_rate=0.5)
        first = builder.build("q", clearance="restricted", session_id="S")
        assert first.items
        assert "nearby" in first.metadata
        second = builder.build("q", clearance="restricted", session_id="S")
        assert not second.items
        return second, _pack_payload(log, second.pack_id)

    def test_flat_served_empty_lists_nothing_like_held_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the nearby block is indistinguishable, not the whole response.

        On the real surface a gate-emptied pack still shows its Withheld
        note, which a held-out pack does not. The ``_markdown`` helper passes
        no withholding note, so the equality below holds for the rendered
        pack minus that note, and the narrower checks pin the nearby part:
        no block, no Nearby text, no listed id.
        """
        served, served_payload = self._served_empty(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "trellis.retrieve.pack_builder.is_held_out", lambda *_a, **_k: True
        )
        log = SQLiteEventLog(tmp_path / "held.db")
        held = _builder(nearby="on", event_log=log, holdout_rate=0.5).build(
            "q", clearance="restricted"
        )
        held_payload = _payload(log)
        for pack, payload in ((served, served_payload), (held, held_payload)):
            assert "nearby" not in pack.metadata
            assert "nearby" not in payload
            assert "listed_item_ids" not in json.dumps(payload)
            text = _markdown(pack, nearby=pack.metadata.get("nearby"))
            assert "Nearby" not in text
            assert not any(i in text for i in WEAK_IDS)
        served_text = _markdown(served, nearby=served.metadata.get("nearby"))
        held_text = _markdown(held, nearby=held.metadata.get("nearby"))
        assert served_text.replace(served.pack_id, "P") == held_text.replace(
            held.pack_id, "P"
        )

    def test_sectioned_served_empty_has_no_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trellis.retrieve.pack_builder.is_held_out", lambda *_a, **_k: False
        )
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = _builder(nearby="on", event_log=log, holdout_rate=0.5)
        sections = [SectionRequest(name="all")]
        first = builder.build_sectioned(
            intent="q", sections=sections, clearance="restricted", session_id="S"
        )
        assert "nearby" in first.metadata
        second = builder.build_sectioned(
            intent="q", sections=sections, clearance="restricted", session_id="S"
        )
        assert not any(sec.items for sec in second.sections)
        assert "nearby" not in second.metadata
        assert "nearby" not in _pack_payload(log, second.pack_id)
        text = format_sectioned_pack_as_markdown(
            [], "q", pack_id=second.pack_id, nearby=second.metadata.get("nearby")
        )
        assert "Nearby" not in text

    def test_holdout_off_keeps_the_rule_inert(self, tmp_path: Path) -> None:
        """At ``holdout_rate`` 0 there is nothing to hide, as for advisories."""
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = _builder(nearby="on", event_log=log)
        builder.build("q", clearance="restricted", session_id="S")
        second = builder.build("q", clearance="restricted", session_id="S")
        assert not second.items
        assert "nearby" in second.metadata


class TestOnlyServable:
    def test_unservable_rows_are_counted_not_listed(self) -> None:
        rows = [_row("a", domain=["d"]), _row("b", domain=["d"]), _row("c")]
        block = build_nearby(
            rows,
            mode="on",
            pack_id="P",
            unservable={"a": "structural_filter", "c": "session_dedup"},
        )
        assert block is not None
        assert block["listed_item_ids"] == ["b"]
        assert block["item_count"] == 1
        assert block["not_listed"] == {"structural_filter": 1, "session_dedup": 1}
        assert "`a`" not in block["markdown"]

    def test_all_unservable_renders_nothing(self) -> None:
        block = build_nearby(
            [_row("a")], mode="on", pack_id="P", unservable={"a": "session_dedup"}
        )
        assert block is None

    def test_structural_and_meta_cut_items_are_not_listed(self) -> None:
        from trellis.schemas.well_known import ACTIVITY

        strategies = [
            _strategy(
                "semantic",
                [
                    _item("strong-01", 0.9),
                    _item("strong-02", 0.85),
                    _item("strong-03", 0.8),
                    _item("struct-04", 0.4, node_role="structural"),
                    _item(
                        "meta-05", 0.3, node_type=ACTIVITY, agent_id="trellis_meta_x"
                    ),
                    _item("plain-06", 0.2),
                ],
            )
        ]
        pack = PackBuilder(
            strategies=strategies,
            content_floor=ContentFloorConfig(mode="off"),
            relevance_cutoff_config=CUTOFF_ON,
            nearby_mode="on",
        ).build("q", clearance="restricted")
        cut_ids = {r["item_id"] for r in pack.metadata["relevance_cutoff"]["cut_items"]}
        assert cut_ids == {"struct-04", "meta-05", "plain-06"}
        block = pack.metadata["nearby"]
        assert block["listed_item_ids"] == ["plain-06"]
        assert block["not_listed"] == {
            "structural_filter": 1,
            "meta_activity_filter": 1,
        }
        reasons = {r.item_id: r.reason for r in pack.retrieval_report.rejected_items}
        assert reasons["struct-04"] == "below_relevance_cutoff"

    def test_session_served_cut_item_is_not_listed(self) -> None:
        builder = PackBuilder(strategies=[])
        items = [_item("seen", 0.1), _item("fresh", 0.1)]
        served = _ServedSet(ids=frozenset({"seen"}), hashes={})
        assert builder._nearby_unservable(
            items, include_structural=False, include_meta=False, served=served
        ) == {"seen": "session_dedup"}
        assert (
            builder._nearby_unservable(
                items, include_structural=False, include_meta=False, served=None
            )
            == {}
        )


class TestLabelsAndCap:
    def test_label_with_newline_and_heading_stays_on_one_line(self) -> None:
        row = _row("a", domain=["x\n## heading"], title="t`i\ntle")
        assert group_label(row) == "domain: x ## heading"
        block = build_nearby([row], mode="on", pack_id="P")
        assert block is not None
        lines = block["markdown"].splitlines()
        assert not any(line.startswith("## heading") for line in lines)
        assert sum(1 for line in lines if line.startswith("- ")) == 1
        assert '"ti tle"' in block["markdown"]

    def test_markdown_specials_are_escaped(self) -> None:
        row = _row("a", domain=["**bold** _emph_ <tag>"], title="see [x](http://e)")
        assert group_label(row) == r"domain: \*\*bold\*\* \_emph\_ \<tag\>"
        block = build_nearby([row], mode="on", pack_id="P")
        assert block is not None
        (line,) = [ln for ln in block["markdown"].splitlines() if ln.startswith("- ")]
        assert line.startswith(r"- **domain: \*\*bold\*\* \_emph\_ \<tag\>**: 1 item")
        assert r'"see \[x\](http://e)"' in line
        assert block["groups"][0]["titles"] == [r"see \[x\](http://e)"]

    def test_identifiers_keep_their_spelling(self) -> None:
        row = _row("a", domain=["TRELLIS_GRAPH_SEEDING"], title="pack_builder.py")
        assert group_label(row) == "domain: TRELLIS_GRAPH_SEEDING"
        block = build_nearby([row], mode="on", pack_id="P")
        assert block is not None
        assert block["groups"][0]["titles"] == ["pack_builder.py"]
        assert '"pack_builder.py"' in block["markdown"]

    def test_backslash_is_escaped_and_backtick_removed(self) -> None:
        row = _row("a", domain=["a\\b `c`"])
        assert group_label(row) == "domain: a\\\\b c"

    def test_truncation_never_splits_an_escape(self) -> None:
        def _odd_tail(text: str) -> bool:
            body = text.split("…", maxsplit=1)[0]
            return (len(body) - len(body.rstrip("\\"))) % 2 == 1

        raw_split = 0
        for n in range(40, 70):
            domain = "x" * n + "*" * 40
            label = group_label(_row("a", domain=[domain]))
            assert not _odd_tail(label), label
            assert len(label) <= LABEL_MAX_CHARS
            raw = truncate_excerpt(f"domain: {_clean(domain)}", LABEL_MAX_CHARS)
            raw_split += _odd_tail(raw)
        # The plain cut does split an escape somewhere in the sweep, so the
        # guard above is exercised, not vacuous.
        assert raw_split > 0

    def test_backtick_id_is_left_out(self) -> None:
        block = build_nearby(
            [_row("ok", domain=["d"]), _row("bad`id", domain=["d`x"])],
            mode="on",
            pack_id="P",
        )
        assert block is not None
        assert block["listed_item_ids"] == ["ok"]
        assert block["not_listed"] == {"unlistable_id": 1}
        assert "d`x" not in block["markdown"]

    def test_long_domain_stays_under_the_cap(self) -> None:
        block = build_nearby(
            [_row("a", domain=["d" * 400], title="T" * 400)], mode="on", pack_id="P"
        )
        assert block is not None
        assert len(block["groups"][0]["label"]) <= 60
        assert estimate_tokens(block["markdown"]) <= MAX_SECTION_TOKENS

    def test_oversized_first_group_is_trimmed_to_fit(self) -> None:
        rows = [
            _row(f"{n}" + "x" * 127, domain=["d" * 400], title="T" * 400)
            for n in range(MAX_IDS_PER_GROUP + 2)
        ]
        block = build_nearby(rows, mode="on", pack_id="01PACKIDPACKIDPACKIDPACKID")
        assert block is not None
        (group,) = block["groups"]
        assert 1 <= len(group["item_ids"]) < MAX_IDS_PER_GROUP
        assert len(group["item_ids"]) + group["ids_left_out"] == len(rows)
        assert estimate_tokens(block["markdown"]) <= MAX_SECTION_TOKENS
        assert block["section_tokens"] == estimate_tokens(block["markdown"])


class TestFormatterParity:
    def test_core_and_sdk_print_the_same_section(self) -> None:
        pack = _builder(nearby="on").build("q", clearance="restricted")
        items = [
            {"item_id": i.item_id, "item_type": i.item_type, "excerpt": i.excerpt}
            for i in pack.items
        ]
        wire = json.loads(json.dumps({"metadata": pack.metadata}))
        block = wire["metadata"]["nearby"]
        core = format_pack_as_markdown(items, "q", pack_id=pack.pack_id, nearby=block)
        sdk = sdk_format_pack(items, "q", pack_id=pack.pack_id, nearby=block)
        assert _section(core) == _section(sdk) == block["markdown"]
        core_sec = format_sectioned_pack_as_markdown(
            [], "q", pack_id=pack.pack_id, nearby=block
        )
        sdk_sec = sdk_format_sectioned([], "q", nearby=block)
        assert _section(core_sec) == _section(sdk_sec) == block["markdown"]

    def test_sdk_reads_the_block_off_pack_json(self) -> None:
        from trellis_sdk._format import nearby_of

        assert nearby_of({"metadata": {"nearby": {"markdown": "x"}}}) == {
            "markdown": "x"
        }
        assert nearby_of({}) is None
        assert sdk_format_pack([], "q", nearby=None) == sdk_format_pack([], "q")


def test_off_never_computes_the_unservable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> dict[str, str]:
        msg = "_nearby_unservable ran with nearby off"
        raise AssertionError(msg)

    monkeypatch.setattr(PackBuilder, "_nearby_unservable", _boom)
    pack = _builder(nearby="off").build("q", clearance="restricted")
    assert "nearby" not in pack.metadata
    pack = _builder(nearby="off").build_sectioned(
        intent="q", sections=[SectionRequest(name="all")], clearance="restricted"
    )
    assert "nearby" not in pack.metadata
